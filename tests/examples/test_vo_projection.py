"""The Virtual Organisations adapter (design 0011, step 3).

The unit tests need no VO package. The integration tests build the pilot's org record at test
time from the Virtual Organisations checkout `VO_CHECKOUT` names, with its model package
importable (`uv run --with "$VO_CHECKOUT"`); nothing from that private repo is kept here. Without
both, `conftest.py` deselects them (design 0011, question 1):

    VO_CHECKOUT=/path/to/virtual-organisations RUN_INTEGRATION=1 \\
        uv run --with "$VO_CHECKOUT" pytest -m integration --no-cov tests/examples
"""

import copy
import datetime as dt
import errno
import importlib.util
import os
import stat
import sys
from collections.abc import Callable
from pathlib import Path
from types import ModuleType
from typing import Any

import pytest
from yaml_projection import DocumentError, Projection, render

from sluice import Sluice
from sluice.context import SetattrArgs
from sluice.dispatch import Dispatcher

from .conftest import EXAMPLES, cat, errno_of, ls, run, vo_checkout, walk

type Mount = Callable[[Sluice], Dispatcher]

EXAMPLE = Path("examples") / "smallest-charity"  # The VO example the pilot is built from
TOP = ["kind", "version", "org", "name", "status", "template", "seed", "results"]


def _fresh_adapter(name: str) -> ModuleType:
    """A new copy of vo_projection, imported under `name`, whatever is installed now."""
    spec = importlib.util.spec_from_file_location(name, EXAMPLES / "vo_projection.py")
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture
def without_vo(monkeypatch: pytest.MonkeyPatch) -> ModuleType:
    """The adapter as imported where the VO package isn't installed."""
    for name in ("virtual_organisations", "virtual_organisations.model"):
        monkeypatch.setitem(sys.modules, name, None)  # None makes the import fail
    return _fresh_adapter("vo_projection_without_vo")


# Unit: no VO package


def test_the_adapter_imports_without_the_package(without_vo: ModuleType) -> None:
    assert without_vo.vo is None
    assert callable(without_vo.org_projection)


def test_org_projection_says_to_install_the_package(without_vo: ModuleType, tmp_path: Path) -> None:
    with pytest.raises(ImportError, match="install the virtual-organisations project"):
        without_vo.org_projection(tmp_path / "org.yaml")
    assert issubclass(without_vo.ModelNotInstalled, ImportError)


# Integration: the pilot's org record, built from a VO checkout

integration = pytest.mark.integration


@pytest.fixture
def vo() -> Any:
    from virtual_organisations import model

    return model


@pytest.fixture
def pilot(vo: Any) -> dict[str, Any]:
    """Design 0011's pilot: the template as the copy, the seed's values, no results yet."""
    root = vo_checkout()
    assert root is not None  # conftest deselects these tests otherwise
    template = vo.load_template(root / EXAMPLE / "template.yaml")
    seed = vo.load_seed(root / EXAMPLE / "seed.yaml")
    record = {
        "kind": "org",
        "version": 1,
        "org": seed["org"],
        "name": seed["org"],
        "status": "creating",
        "template": template,
        "seed": seed["values"],
        "results": {},
    }
    assert vo.validate_org(record) == []
    return record


def write(vo: Any, path: Path, record: dict[str, Any]) -> Path:
    """What the engine does: write a new file beside the record and rename it over the old one."""
    new = path.with_suffix(".new")
    new.write_text(vo.dump(record), encoding="utf-8")
    os.replace(new, path)
    return path


@pytest.fixture
def record_file(vo: Any, pilot: dict[str, Any], tmp_path: Path) -> Path:
    return write(vo, tmp_path / "example-trust.yaml", pilot)


@pytest.fixture
def d(record_file: Path, mount: Mount) -> Dispatcher:
    from vo_projection import org_projection

    app = Sluice()
    app.route("/orgs/example-trust/{rest*}", virtual=True, handler=org_projection(record_file))
    return mount(app)


ROOT = "/orgs/example-trust"


@integration
def test_ls_shows_the_record(d: Dispatcher, pilot: dict[str, Any]) -> None:
    top = run(ls(d, ROOT))
    assert list(top) == TOP
    for key in ("template", "seed", "results"):
        assert stat.S_ISDIR(top[key].st_mode)
    for key in ("kind", "version", "org", "name", "status"):
        assert stat.S_ISREG(top[key].st_mode)
        assert stat.S_IMODE(top[key].st_mode) == 0o444
    assert list(run(ls(d, f"{ROOT}/template"))) == list(pilot["template"])
    assert list(run(ls(d, f"{ROOT}/template/nodes"))) == list(pilot["template"]["nodes"])
    assert list(run(ls(d, f"{ROOT}/seed"))) == list(pilot["seed"])
    assert run(ls(d, f"{ROOT}/results")) == {}


@integration
def test_cat_reads_one_property(d: Dispatcher, pilot: dict[str, Any]) -> None:
    assert run(cat(d, f"{ROOT}/kind")) == b"org\n"
    assert run(cat(d, f"{ROOT}/version")) == b"1\n"
    assert run(cat(d, f"{ROOT}/status")) == b"creating\n"
    assert run(cat(d, f"{ROOT}/template/name")) == f"{pilot['template']['name']}\n".encode()
    for key, declaration in pilot["template"]["seed"].items():
        got = run(cat(d, f"{ROOT}/template/seed/{key}/type"))
        assert got == f"{declaration['type']}\n".encode()
    for node, spec in pilot["template"]["nodes"].items():
        got = run(cat(d, f"{ROOT}/template/nodes/{node}/depends_on"))
        assert got == "".join(f"{n}\n" for n in spec["depends_on"]).encode()  # One per line


def _expected(value: Any, declared: str) -> bytes:
    """A seed value's file by design 0011's table, written out here rather than reused."""
    match declared:
        case "list":
            return "".join(f"{item}\n" for item in value).encode()
        case "boolean":
            return b"true\n" if value else b"false\n"
        case _:
            return f"{value.isoformat() if isinstance(value, dt.date) else value}\n".encode()


@integration
def test_seed_values_render_by_their_declared_type(d: Dispatcher, pilot: dict[str, Any]) -> None:
    from vo_projection import OrgModel

    model = OrgModel()
    declarations = pilot["template"]["seed"]
    assert pilot["seed"]  # The pilot has seed values to check
    for key, value in pilot["seed"].items():
        declared = declarations[key]["type"]
        assert model.type_of(f"/seed/{key}", pilot) == declared
        if declared != "mapping":
            assert run(cat(d, f"{ROOT}/seed/{key}")) == _expected(value, declared)
    # Elsewhere the current value's type decides
    assert model.type_of("/template/name", pilot) is None
    assert model.type_of("/name", pilot) is None
    assert model.type_of("/seed/not-declared", pilot) is None


@integration
def test_a_version_2_record_is_refused(vo: Any, pilot: dict[str, Any], tmp_path: Path) -> None:
    from vo_projection import org_projection

    record = copy.deepcopy(pilot)
    record["version"] = 2
    with pytest.raises(DocumentError) as e:
        org_projection(write(vo, tmp_path / "v2.yaml", record))
    assert e.value.findings[0][:2] == ("version", "/version")
    record["version"] = True  # Equal to 1 in Python, but not version 1
    with pytest.raises(DocumentError):
        org_projection(write(vo, tmp_path / "v2.yaml", record))


@integration
def test_a_record_with_findings_is_refused(vo: Any, pilot: dict[str, Any], tmp_path: Path) -> None:
    from vo_projection import org_projection

    record = copy.deepcopy(pilot)
    record["status"] = "not-a-status"
    assert vo.validate_org(record)
    with pytest.raises(DocumentError) as e:
        org_projection(write(vo, tmp_path / "bad.yaml", record))
    assert ("schema", "/status") in {f[:2] for f in e.value.findings}


@integration
def test_every_write_is_refused(d: Dispatcher, pilot: dict[str, Any]) -> None:
    from vo_projection import OrgModel

    key = next(iter(pilot["seed"]))
    leaves = [f"{ROOT}/name", f"{ROOT}/seed/{key}", f"{ROOT}/template/name"]
    for leaf in leaves:
        inode = run(walk(d, leaf))
        for flags in (os.O_WRONLY, os.O_RDWR, os.O_RDONLY | os.O_TRUNC):
            assert errno_of(d.open(inode, flags)) == errno.EACCES
        assert errno_of(d.setattr(inode, SetattrArgs(size=0))) == errno.EACCES
        assert errno_of(d.setattr(inode, SetattrArgs(mode=0o644))) == errno.EACCES
        assert not run(d.access(inode, os.W_OK))
    for directory in (ROOT, f"{ROOT}/seed", f"{ROOT}/results"):
        inode = run(walk(d, directory))
        assert errno_of(d.create(inode, "new", 0o644, os.O_WRONLY)) == errno.EPERM
        assert errno_of(d.mkdir(inode, "new", 0o755)) == errno.EPERM
    root = run(walk(d, ROOT))
    assert errno_of(d.unlink(root, "name")) == errno.EPERM
    assert errno_of(d.rmdir(root, "seed")) == errno.EPERM
    assert errno_of(d.rename(root, "name", root, "title")) == errno.EXDEV
    model = OrgModel()
    for pointer in ("/name", f"/seed/{key}", "/template/name", "/results"):
        assert not model.writable(pointer, pilot)
        assert not model.is_reference(pointer, pilot)


@integration
def test_an_atomic_replace_by_the_engine_is_reloaded(
    vo: Any, pilot: dict[str, Any], record_file: Path, d: Dispatcher
) -> None:
    node = next(iter(pilot["template"]["nodes"]))
    started = dt.datetime(2026, 11, 4, 9, 30, tzinfo=dt.UTC)
    record = copy.deepcopy(pilot)
    record["updated"] = started + dt.timedelta(minutes=1)
    record["results"][node] = {  # Every key the engine adds to a result
        "status": "done",
        "attempt": 1,
        "started_at": started,
        "finished_at": started + dt.timedelta(seconds=5),
        "ran": True,
        "waiting_on": "approval",
        "request": "request-1",
        "reason_code": "example-reason",
        "reason": "An example reason.",
    }
    assert vo.validate_org(record) == []
    write(vo, record_file, record)
    assert list(run(ls(d, ROOT))) == [*TOP, "updated"]  # opendir reloads
    assert list(run(ls(d, f"{ROOT}/results"))) == [node]
    result = f"{ROOT}/results/{node}"
    assert list(run(ls(d, result))) == list(record["results"][node])
    assert run(cat(d, f"{result}/status")) == b"done\n"
    assert run(cat(d, f"{result}/attempt")) == b"1\n"
    assert run(cat(d, f"{result}/ran")) == b"true\n"
    assert run(cat(d, f"{result}/started_at")) == b"2026-11-04T09:30:00+00:00\n"
    assert run(cat(d, f"{ROOT}/updated")) == render(record["updated"])
    assert errno_of(d.open(run(walk(d, f"{result}/status")), os.O_WRONLY)) == errno.EACCES


@integration
def test_the_mount_example(record_file: Path, mount: Mount) -> None:
    """Design 0011's mount snippet, with the access layer, as Writing-an-App shows it."""
    from auth import auth
    from vo_projection import org_projection

    handler = org_projection(record_file)
    assert isinstance(handler, Projection)  # The generic projector, filled from the VO model
    app = Sluice()
    app.route(
        "/orgs/example-trust/{rest*}",
        virtual=True,
        handler=handler,
        middlewares=[auth()],
    )
    d = mount(app)
    assert run(cat(d, f"{ROOT}/kind")) == b"org\n"
