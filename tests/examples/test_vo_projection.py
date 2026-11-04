"""The Virtual Organisations adapter (design 0011, steps 3 and 5).

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
import logging
import os
import stat
import sys
from collections.abc import Callable
from pathlib import Path
from types import ModuleType, SimpleNamespace
from typing import Any, NamedTuple

import pytest
import yaml
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


# Unit: the writable set (design 0011, "Writes, in two phases"), from a pointer and a plain dict.
# A sluice-owned record shaped like an org record: none of its names come from the VO repo.

RECORD: dict[str, Any] = {
    "kind": "org",
    "version": 1,
    "org": "toy-org",
    "name": "Toy Org",
    "status": "creating",
    "template": {
        "name": "toy",
        "seed": {
            "title": {"type": "string"},
            "count": {"type": "integer"},
            "members": {"type": "list"},
            "extra": {"type": "mapping"},
            "a~b": {"type": "string"},
            "left": {"type": "integer", "default": 2},
        },
        "nodes": {"first": {"depends_on": [], "outputs": {"done_at": {"type": "date"}}}},
    },
    "seed": {
        "title": "Toy",
        "count": 3,
        "members": ["one", "two"],
        "extra": {"size": 4, "inner": {"colour": "red"}, "tags": ["x"], "ratio": 0.5},
        "a~b": "escaped",
    },
    "results": {"first": {"status": "done", "outputs": {"done_at": dt.date(2026, 11, 4)}}},
}

WRITABLE = [
    "/seed/title",
    "/seed/count",
    "/seed/members",  # A list is one leaf, replaced whole
    "/seed/extra/size",  # The leaves inside a mapping-typed value
    "/seed/extra/inner/colour",
    "/seed/extra/tags",
    "/seed/extra/ratio",
    "/seed/a~0b",  # RFC 6901 escaping
]
READ_ONLY = [
    "",
    "/kind",
    "/version",
    "/org",
    "/name",
    "/status",
    "/results",
    "/results/first/status",  # The engine's, for good
    "/results/first/outputs/done_at",
    "/template",
    "/template/name",
    "/template/seed/count/type",
    "/template/nodes/first/depends_on",  # A dependency
    "/seed",
    "/seed/left",  # Left to its default, so absent: adding it is a shape change
    "/seed/missing",
    "/seed/extra",  # A directory, not a leaf
    "/seed/extra/inner",
    "/seed/extra/missing",
    "/seed/members/0",  # A list's items aren't leaves of their own
    "/seed/count/0",
    "/seed/title/x",
    "/seedling/title",
]


@pytest.mark.parametrize("at", WRITABLE)
def test_seed_values_are_writable(without_vo: ModuleType, at: str) -> None:
    assert without_vo.writable(at, RECORD)


@pytest.mark.parametrize("at", READ_ONLY)
def test_everything_else_is_read_only(without_vo: ModuleType, at: str) -> None:
    assert not without_vo.writable(at, RECORD)


def test_seed_writes_are_allowed_whatever_status_and_results_say(without_vo: ModuleType) -> None:
    for status in ("creating", "done", "failed", "anything"):
        record = {**RECORD, "status": status}
        assert all(without_vo.writable(at, record) for at in WRITABLE)
    assert all(without_vo.writable(at, {**RECORD, "results": {}}) for at in WRITABLE)


def test_a_mapping_leaf_needs_a_mapping_declaration(without_vo: ModuleType) -> None:
    record = copy.deepcopy(RECORD)
    record["template"]["seed"]["extra"]["type"] = "string"  # validate_org would refuse it
    assert not without_vo.writable("/seed/extra/size", record)
    del record["template"]["seed"]["extra"]
    assert not without_vo.writable("/seed/extra/size", record)
    assert not without_vo.writable("/seed/title", {"seed": "not a mapping"})


class _Finding(NamedTuple):
    code: str
    path: str
    message: str


def _toy_validate_org(org: dict[str, Any]) -> list[_Finding]:
    """A stand-in for `validate_org`: declared integers only, enough to see a finding."""
    found = []
    for key, value in org["seed"].items():
        declared = org["template"]["seed"][key]["type"]
        if declared == "integer" and (type(value) is not int or value < 0):
            found.append(_Finding("seed-type", f"/seed/{key}", f"{key!r} isn't a count"))
    return found


TOY_VO = SimpleNamespace(
    load_org=lambda path: yaml.safe_load(Path(path).read_text(encoding="utf-8")),
    dump=lambda doc: yaml.safe_dump(doc, sort_keys=False),
    validate_org=_toy_validate_org,
)


@pytest.fixture
def toy(
    without_vo: ModuleType, tmp_path: Path, mount: Mount, monkeypatch: pytest.MonkeyPatch
) -> tuple[Dispatcher, Path]:
    """The adapter over the toy record, with a stand-in model package: runs in CI."""
    monkeypatch.setattr(without_vo, "vo", TOY_VO)
    path = tmp_path / "toy-org.yaml"
    path.write_text(yaml.safe_dump(RECORD, sort_keys=False), encoding="utf-8")
    app = Sluice()
    app.route("/orgs/toy-org/{rest*}", virtual=True, handler=without_vo.org_projection(path))
    return mount(app), path


async def session(d: Dispatcher, path: str, data: bytes) -> None:
    """`echo` into a leaf: open truncating, one write at offset 0, flush, release."""
    info = await d.open(await walk(d, path), os.O_WRONLY | os.O_TRUNC)
    try:
        assert await d.write(info.fh, 0, data) == len(data)
        await d.flush(info.fh)
    finally:
        await d.release(info.fh)


def test_writes_through_the_adapter(toy: tuple[Dispatcher, Path]) -> None:
    d, path = toy
    root = "/orgs/toy-org"
    run(session(d, f"{root}/seed/count", b"25\n"))
    run(session(d, f"{root}/seed/members", b"three\n4\n"))
    run(session(d, f"{root}/seed/extra/size", b"5\n"))  # Keeps its current type: an integer
    run(session(d, f"{root}/seed/extra/ratio", b"0.75\n"))
    run(session(d, f"{root}/seed/extra/tags", b"y\nz\n"))
    assert run(cat(d, f"{root}/seed/count")) == b"25\n"
    on_disk = yaml.safe_load(path.read_text(encoding="utf-8"))
    assert on_disk["seed"]["count"] == 25
    assert on_disk["seed"]["members"] == ["three", "4"]  # Items are strings
    assert on_disk["seed"]["extra"] == {
        "size": 5,
        "inner": {"colour": "red"},
        "tags": ["y", "z"],
        "ratio": 0.75,
    }
    assert {k: v for k, v in on_disk.items() if k != "seed"} == {
        k: v for k, v in RECORD.items() if k != "seed"
    }
    seed = run(ls(d, f"{root}/seed"))
    assert stat.S_IMODE(seed["count"].st_mode) == 0o644
    assert stat.S_IMODE(run(ls(d, root))["status"].st_mode) == 0o444


def test_the_adapter_refuses(
    toy: tuple[Dispatcher, Path], caplog: pytest.LogCaptureFixture
) -> None:
    d, path = toy
    root = "/orgs/toy-org"
    before = path.read_bytes()
    with caplog.at_level(logging.WARNING):
        # A cast failure at write, and a finding (`validate_org`'s) at write
        for written in (b"many-of-them\n", b"-1\n"):
            info = run(d.open(run(walk(d, f"{root}/seed/count")), os.O_WRONLY | os.O_TRUNC))
            assert errno_of(d.write(info.fh, 0, written)) == errno.EINVAL
            run(d.release(info.fh))
    assert "seed-type at /seed/count" in caplog.text
    assert "many-of-them" not in caplog.text
    for leaf in ("status", "results/first/status", "template/nodes/first/depends_on"):
        assert errno_of(d.open(run(walk(d, f"{root}/{leaf}")), os.O_WRONLY)) == errno.EACCES
    seed = run(walk(d, f"{root}/seed"))
    assert errno_of(d.create(seed, "left", 0o644, os.O_WRONLY)) == errno.EPERM
    assert path.read_bytes() == before


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
def test_only_seed_values_are_writable(d: Dispatcher, pilot: dict[str, Any]) -> None:
    from vo_projection import OrgModel

    model = OrgModel()
    key = next(iter(pilot["seed"]))
    for leaf in (f"{ROOT}/name", f"{ROOT}/template/name"):
        inode = run(walk(d, leaf))
        for flags in (os.O_WRONLY, os.O_RDWR, os.O_RDONLY | os.O_TRUNC):
            assert errno_of(d.open(inode, flags)) == errno.EACCES
        assert errno_of(d.setattr(inode, SetattrArgs(size=0))) == errno.EACCES
        assert errno_of(d.setattr(inode, SetattrArgs(mode=0o644))) == errno.EACCES
        assert not run(d.access(inode, os.W_OK))
    seed = run(ls(d, f"{ROOT}/seed"))
    for name, value in pilot["seed"].items():
        if not isinstance(value, dict):
            assert stat.S_IMODE(seed[name].st_mode) == 0o644
            assert model.writable(f"/seed/{name}", pilot)
    assert run(d.access(run(walk(d, f"{ROOT}/seed/{key}")), os.W_OK))
    for directory in (ROOT, f"{ROOT}/seed", f"{ROOT}/results"):
        inode = run(walk(d, directory))
        assert errno_of(d.create(inode, "new", 0o644, os.O_WRONLY)) == errno.EPERM
        assert errno_of(d.mkdir(inode, "new", 0o755)) == errno.EPERM
    root = run(walk(d, ROOT))
    assert errno_of(d.unlink(root, "name")) == errno.EPERM
    assert errno_of(d.rmdir(root, "seed")) == errno.EPERM
    assert errno_of(d.rename(root, "name", root, "title")) == errno.EXDEV
    for at in ("/name", "/template/name", "/results", f"/template/seed/{key}/type"):
        assert not model.writable(at, pilot)
    for at in ("/name", f"/seed/{key}", "/template/name", "/results"):
        assert not model.is_reference(at, pilot)


# Integration: writes (design 0011 step 5, its acceptance criteria)

WRITE = os.O_WRONLY | os.O_TRUNC  # What `echo x > leaf` opens with
SCALARS = {"integer", "boolean", "date", "datetime"}  # Declared types a bad text fails


def _keys_of(pilot: dict[str, Any], *types: str) -> list[str]:
    """The pilot's seed keys whose declared type is one of `types`, as the checkout has them."""
    declared = pilot["template"]["seed"]
    return [k for k in pilot["seed"] if declared[k]["type"] in types]


def _refused_at_write(d: Dispatcher, leaf: str, data: bytes) -> None:
    info = run(d.open(run(walk(d, leaf)), WRITE))
    assert errno_of(d.write(info.fh, 0, data)) == errno.EINVAL  # What `echo` sees
    assert errno_of(d.flush(info.fh)) == errno.EINVAL  # And close(2)
    run(d.release(info.fh))


def _with_result(pilot: dict[str, Any]) -> tuple[dict[str, Any], str]:
    """The pilot as the engine leaves it after one node has run."""
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
    return record, node


@integration
def test_write_then_read_a_seed_value(
    vo: Any, d: Dispatcher, pilot: dict[str, Any], record_file: Path
) -> None:
    strings, lists = _keys_of(pilot, "string"), _keys_of(pilot, "list")
    assert strings  # The pilot has a string seed value to write
    run(session(d, f"{ROOT}/seed/{strings[0]}", b"A new value\n"))
    assert run(cat(d, f"{ROOT}/seed/{strings[0]}")) == b"A new value\n"
    expected = copy.deepcopy(pilot)
    expected["seed"][strings[0]] = "A new value"
    if lists:  # Replaced whole, one item per line, items strings
        run(session(d, f"{ROOT}/seed/{lists[0]}", b"first\n2\n"))
        assert run(cat(d, f"{ROOT}/seed/{lists[0]}")) == b"first\n2\n"
        expected["seed"][lists[0]] = ["first", "2"]
    # The written file loads through load_org, passes validate_org, and changed nothing else
    written = vo.load_org(record_file)
    assert written == expected
    assert vo.validate_org(written) == []
    assert list(written) == TOP  # Key order kept
    assert sorted(os.listdir(record_file.parent)) == [record_file.name]  # No temporary file


@integration
def test_a_seed_type_error_is_einval_at_write(
    vo: Any, pilot: dict[str, Any], tmp_path: Path, mount: Mount, caplog: pytest.LogCaptureFixture
) -> None:
    from vo_projection import org_projection

    record = copy.deepcopy(pilot)
    typed = [k for k, v in record["template"]["seed"].items() if v["type"] in SCALARS]
    if typed:  # A declared integer, boolean, date or datetime; set from its default if absent
        key = typed[0]
        record["seed"].setdefault(key, record["template"]["seed"][key]["default"])
        bad = b"not-of-that-type\n"
    else:  # Only strings and lists: not UTF-8 is the one cast a string refuses
        key, bad = _keys_of(record, "string")[0], b"\xff\xfe\n"
    assert vo.validate_org(record) == []
    path = write(vo, tmp_path / "typed.yaml", record)
    app = Sluice()
    app.route("/orgs/example-trust/{rest*}", virtual=True, handler=org_projection(path))
    d = mount(app)
    before = path.read_bytes()
    with caplog.at_level(logging.WARNING):
        _refused_at_write(d, f"{ROOT}/seed/{key}", bad)
    assert path.read_bytes() == before  # Byte-identical
    assert f"cast at /seed/{key}" in caplog.text  # Code and pointer, never the value
    assert "not-of-that-type" not in caplog.text


@integration
def test_validate_org_checks_the_whole_changed_record(
    vo: Any,
    d: Dispatcher,
    pilot: dict[str, Any],
    record_file: Path,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """No cast-valid write to the pilot fails `validate_org` today, so one finding is added to
    the real one's: what a later rule (or the engine's record) would report."""
    key = _keys_of(pilot, "string")[0]
    seen: list[dict[str, Any]] = []
    real = vo.validate_org

    def validate_org(org: dict[str, Any]) -> list[Any]:
        seen.append(org)
        found = list(real(org))
        if org["seed"][key] == "Refused by a rule":
            found.append(vo.Finding("seed-type", f"/seed/{key}", "a rule refuses it."))
        return found

    monkeypatch.setattr(vo, "validate_org", validate_org)  # The module the adapter calls
    before = record_file.read_bytes()
    with caplog.at_level(logging.WARNING):
        _refused_at_write(d, f"{ROOT}/seed/{key}", b"Refused by a rule\n")
    assert record_file.read_bytes() == before
    assert f"seed-type at /seed/{key}: a rule refuses it." in caplog.text
    assert "Refused by a rule" not in caplog.text  # Never the value
    expected = copy.deepcopy(pilot)
    expected["seed"][key] = "Refused by a rule"
    assert seen[-1] == expected  # The whole candidate record, not the leaf


@integration
def test_a_write_to_a_dependency_is_eacces_at_open(
    d: Dispatcher, pilot: dict[str, Any], record_file: Path
) -> None:
    before = record_file.read_bytes()
    for node in pilot["template"]["nodes"]:
        inode = run(walk(d, f"{ROOT}/template/nodes/{node}/depends_on"))
        for flags in (os.O_WRONLY, os.O_RDWR, WRITE):
            assert errno_of(d.open(inode, flags)) == errno.EACCES
        assert errno_of(d.setattr(inode, SetattrArgs(size=0))) == errno.EACCES
    assert record_file.read_bytes() == before


@integration
def test_status_results_and_the_template_are_eacces(
    vo: Any, pilot: dict[str, Any], record_file: Path, d: Dispatcher
) -> None:
    record, node = _with_result(pilot)
    assert vo.validate_org(record) == []
    write(vo, record_file, record)
    run(ls(d, ROOT))  # opendir reloads
    before = record_file.read_bytes()
    key = next(iter(pilot["seed"]))
    leaves = [
        "kind",
        "version",
        "org",
        "name",
        "status",
        "updated",
        *(f"results/{node}/{name}" for name in record["results"][node]),
        "template/name",
        f"template/seed/{key}/type",
        f"template/nodes/{node}/description",
    ]
    for leaf in leaves:
        inode = run(walk(d, f"{ROOT}/{leaf}"))
        assert errno_of(d.open(inode, os.O_WRONLY)) == errno.EACCES, leaf
        assert errno_of(d.open(inode, WRITE)) == errno.EACCES, leaf
    assert record_file.read_bytes() == before
    # A seed write is still allowed with a result in place (design 0011: until the engine's
    # record rules otherwise)
    run(session(d, f"{ROOT}/seed/{_keys_of(pilot, 'string')[0]}", b"After a run\n"))
    assert vo.load_org(record_file)["results"] == record["results"]


@integration
def test_an_absent_seed_key_cannot_be_created(
    vo: Any, pilot: dict[str, Any], tmp_path: Path, mount: Mount
) -> None:
    from vo_projection import org_projection

    record = copy.deepcopy(pilot)
    defaulted = [k for k, v in record["template"]["seed"].items() if "default" in v]
    assert defaulted  # The pilot's template has a seed key with a default
    record["seed"].pop(defaulted[0], None)  # Left to its default
    path = write(vo, tmp_path / "defaulted.yaml", record)
    app = Sluice()
    app.route("/orgs/example-trust/{rest*}", virtual=True, handler=org_projection(path))
    d = mount(app)
    before = path.read_bytes()
    seed = run(walk(d, f"{ROOT}/seed"))
    assert errno_of(d.lookup(seed, defaulted[0])) == errno.ENOENT
    assert errno_of(d.create(seed, defaulted[0], 0o644, WRITE)) == errno.EPERM
    assert path.read_bytes() == before


@integration
def test_a_replace_by_the_engine_between_load_and_commit_is_ebusy(
    vo: Any, pilot: dict[str, Any], record_file: Path, d: Dispatcher
) -> None:
    key = _keys_of(pilot, "string")[0]
    info = run(d.open(run(walk(d, f"{ROOT}/seed/{key}")), WRITE))
    run(d.write(info.fh, 0, b"Mine\n"))
    theirs, _ = _with_result(pilot)
    write(vo, record_file, theirs)  # The engine's atomic replace, mid-session
    engine = record_file.read_bytes()
    assert errno_of(d.flush(info.fh)) == errno.EBUSY
    assert record_file.read_bytes() == engine  # Left as the engine left it
    run(d.flush(info.fh))  # A retry commits on top of the engine's record
    run(d.release(info.fh))
    written = vo.load_org(record_file)
    assert written["seed"][key] == "Mine"
    assert written["results"] == theirs["results"]
    assert vo.validate_org(written) == []


@integration
def test_an_atomic_replace_by_the_engine_is_reloaded(
    vo: Any, pilot: dict[str, Any], record_file: Path, d: Dispatcher
) -> None:
    record, node = _with_result(pilot)
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
