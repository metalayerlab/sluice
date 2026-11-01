import errno
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest
from auth import auth

from sluice import RouteError, Sluice
from sluice.dispatch import Dispatcher

from .conftest import cat, errno_of, run, walk

type Mount = Callable[[Sluice], Dispatcher]


@pytest.fixture
def backing(tmp_path: Path) -> Path:
    (tmp_path / "alice" / "a").mkdir(parents=True)
    (tmp_path / "alice" / "locked").mkdir()
    (tmp_path / "alice" / "a" / "f").write_text("f", encoding="utf-8")
    return tmp_path


def test_the_default_allows_everything(mount: Mount, backing: Path) -> None:
    app = Sluice()
    app.route("/u/{user}/{token}", backend=f"{backing}/{{user}}", middlewares=[auth()])
    assert run(cat(mount(app), "/u/alice/any/a/f")) == b"f"


def test_verify_sees_the_params_real_path_and_op(mount: Mount, backing: Path) -> None:
    calls: list[tuple[Any, ...]] = []

    async def verify(user: Any, token: Any, real_path: Path | None, op: str) -> bool:
        calls.append((user, token, real_path, op))
        return (user, token) == ("alice", "T0k")

    app = Sluice()
    app.route("/u/{user}/{token}", backend=f"{backing}/{{user}}", middlewares=[auth(verify)])
    d = mount(app)
    assert run(cat(d, "/u/alice/T0k/a/f")) == b"f"
    assert ("alice", "T0k", backing / "alice" / "a" / "f", "read") in calls
    assert errno_of(walk(d, "/u/alice/wrong")) == errno.EACCES


def test_custom_param_names(mount: Mount, backing: Path) -> None:
    async def verify(user: Any, token: Any, real_path: Path | None, op: str) -> bool:
        return (user, token) == ("alice", "k")

    app = Sluice()
    gate = auth(verify, user_param="who", token_param="key")  # noqa: S106 - a param name
    app.route("/u/{who}/{key}", backend=f"{backing}/{{who}}", middlewares=[gate])
    d = mount(app)
    assert run(walk(d, "/u/alice/k/a")) > 0
    assert errno_of(walk(d, "/u/alice/x")) == errno.EACCES


def test_rename_is_checked_against_the_target(mount: Mount, backing: Path) -> None:
    async def verify(user: Any, token: Any, real_path: Path | None, op: str) -> bool:
        return op != "rename" or real_path is None or "locked" not in real_path.parts

    app = Sluice()
    app.route("/u/{user}", backend=f"{backing}/{{user}}", middlewares=[auth(verify)])
    d = mount(app)

    async def move() -> None:
        src, dst = await walk(d, "/u/alice/a"), await walk(d, "/u/alice/locked")
        await d.rename(src, "f", dst, "f")

    assert errno_of(move()) == errno.EACCES
    assert (backing / "alice" / "a" / "f").exists()


def test_a_sync_verify_is_refused() -> None:
    with pytest.raises(RouteError, match="async"):
        auth(lambda *a: True)  # type: ignore[arg-type]  # the error under test


def test_repr_names_the_policy() -> None:
    async def verify(user: Any, token: Any, real_path: Path | None, op: str) -> bool:
        return True

    assert repr(auth()) == "auth(allow_all)"
    assert repr(auth(verify)) == "auth(test_repr_names_the_policy.<locals>.verify)"
