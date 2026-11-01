import logging
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest
from access_log import access_log

from sluice import Sluice
from sluice.dispatch import Dispatcher

from .conftest import cat, errno_of, run, walk

type Mount = Callable[[Sluice], Dispatcher]


@pytest.fixture
def app(tmp_path: Path) -> Sluice:
    async def decode(ctx: Any, raw: Any) -> Any:
        return raw

    (tmp_path / "alice").mkdir()
    (tmp_path / "alice" / "f").write_text("f", encoding="utf-8")
    app = Sluice()
    app.use(access_log())
    app.param("token", decode, secret=True)
    app.route("/u/{user}/{token}", backend=f"{tmp_path}/{{user}}")
    return app


def test_logs_each_call_with_secrets_masked(
    mount: Mount, app: Sluice, caplog: pytest.LogCaptureFixture
) -> None:
    with caplog.at_level(logging.INFO, logger="sluice.access"):
        run(cat(mount(app), "/u/alice/T0k/f"))
    assert "read /u/alice/********/f uid=" in caplog.text
    assert "-> ok" in caplog.text
    assert "T0k" not in caplog.text


def test_logs_failures_by_errno_name(
    mount: Mount, app: Sluice, caplog: pytest.LogCaptureFixture
) -> None:
    with caplog.at_level(logging.INFO, logger="sluice.access"):
        errno_of(walk(mount(app), "/u/alice/T0k/missing"))
    assert "lookup /u/alice/********/missing" in caplog.text
    assert "-> ENOENT" in caplog.text


def test_logger_and_level_are_injectable(
    mount: Mount, tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    app = Sluice()
    app.use(access_log(logging.getLogger("audit"), logging.DEBUG))
    app.route("/b", backend=str(tmp_path))
    with caplog.at_level(logging.DEBUG, logger="audit"):
        run(walk(mount(app), "/b"))
    assert [r.levelno for r in caplog.records if r.name == "audit"] == [logging.DEBUG]
    assert repr(access_log()) == "access_log()"
