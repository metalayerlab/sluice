"""The `sluice` command.

The wiki's CLI-Reference page is generated from this module (scripts/gen-wiki.py),
so command docstrings and option help are user documentation.
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Annotated, NoReturn

import typer

from sluice import __version__
from sluice.config import CONFIG_ENV, SETTINGS, Config, ConfigError, load_config
from sluice.context import MASK
from sluice.errors import AppLoadError, FsError, MountError, RouteError
from sluice.middleware import OPS
from sluice.routing import RouteTable

# Expected failures: one line and exit 2. Anything else is a bug: traceback, exit 1.
EXPECTED = (ConfigError, AppLoadError, RouteError, MountError)

app = typer.Typer(
    help=(
        "Sluice — a Python FUSE filesystem with an Express-style router that gates, rewrites,"
        " and middleware-intercepts every filesystem call between virtual paths and their"
        " backing stores."
    ),
    no_args_is_help=True,
    add_completion=False,
    pretty_exceptions_enable=False,
)

ConfigOption = Annotated[
    Path | None,
    typer.Option(
        "--config",
        "-c",
        envvar=CONFIG_ENV,
        help="Config file (default /etc/sluice/config.yaml; missing is fine).",
        show_default=False,
    ),
]


def _fail(e: Exception) -> NoReturn:
    typer.echo(f"error: {e}", err=True)
    raise typer.Exit(2) from e


def _load(path: Path | None) -> Config:
    try:
        cfg = load_config(path)
    except ConfigError as e:
        _fail(e)
    logging.basicConfig(
        level=cfg.log_level, format="%(asctime)s %(levelname)s %(name)s: %(message)s"
    )
    return cfg


def _table(cfg: Config) -> RouteTable:
    from sluice.mount import prepare

    try:
        return prepare(cfg)
    except EXPECTED as e:
        _fail(e)


@app.command()
def version() -> None:
    """Print the version."""
    typer.echo(__version__)


@app.command("config")
def show_config(config: ConfigOption = None) -> None:
    """Print the effective configuration and where each value came from. Secrets are masked."""
    cfg = _load(config)
    values: dict[str, object] = {
        "log_level": cfg.log_level,
        "app.module": cfg.app_module,
        "app.dir": cfg.app_dir,
        "mount.point": cfg.mount_point,
        "mount.fsname": cfg.mount_fsname,
        "mount.allow_other": cfg.mount_allow_other,
        "mount.cache_ms": cfg.mount_cache_ms,
        "mount.debug": cfg.mount_debug,
        "io.threads": cfg.io_threads,
        "env.expose": ",".join(cfg.env_expose),
        "secrets.dir": cfg.secrets_dir,
        "pipeline.slow_ms": cfg.pipeline_slow_ms,
    }
    for setting in SETTINGS:
        shown = "********" if setting.secret else values.get(setting.key)
        typer.echo(f"{setting.key} = {shown}  ({cfg.sources[setting.key]})")


@app.command()
def mount(config: ConfigOption = None) -> None:
    """Mount the app at `mount.point` and serve in the foreground.

    Imports `app.module`, loads the mount-time environment and secrets, validates the routes,
    mounts, and serves until SIGINT or SIGTERM, then unmounts. Unmount a crashed mount with
    `fusermount3 -u <mount.point>`.
    """
    from sluice.mount import serve

    cfg = _load(config)
    try:
        serve(cfg)
    except EXPECTED as e:
        _fail(e)


@app.command()
def routes(config: ConfigOption = None) -> None:
    """Validate the routes without mounting, and print the tree.

    Each route shows its backend state (own backend, inherited from an ancestor, virtual, or
    grouping) and the full middleware chain that runs beneath it, global middleware first.
    """
    table = _table(_load(config))
    for walked in table.walk():
        indent = "  " * walked.depth
        typer.echo(f"{indent}{walked.node.route.full_pattern}  [{walked.state}]")
        chain = ", ".join(repr(m) for m in walked.middlewares) or "(none)"
        typer.echo(f"{indent}  middleware: {chain}")


@app.command()
def resolve(
    path: Annotated[str, typer.Argument(help="A virtual path under the mount, e.g. /fuse/a/b.")],
    op: Annotated[
        str, typer.Option("--op", help="The operation whose middleware to list.")
    ] = "getattr",
    config: ConfigOption = None,
) -> None:
    """Dry-run the router for one virtual path.

    Prints the matched route, params (secret ones masked), the real path, and the middleware
    that would run for `--op`. Runs no middleware and no `param()` callbacks.
    """
    if op not in OPS:
        _fail(ValueError(f"--op: expected one of {', '.join(sorted(OPS))}, got {op!r}"))
    table = _table(_load(config))
    try:
        res = table.resolve(path)
    except FsError as e:
        typer.echo(f"refused: {e}")
        return
    if res is None:
        typer.echo("no route: ENOENT")
        return
    params = ", ".join(
        f"{k}={MASK if k in table.secret_params else v}" for k, v in res.params.items()
    )
    chain = [repr(m) for m in res.middlewares if m.applies(op)]
    callbacks = [p.name for p in res.param_specs]
    typer.echo(f"path:       {res.display_path}")
    typer.echo(f"route:      {res.route.full_pattern}")
    typer.echo(f"kind:       {res.kind}")
    typer.echo(f"params:     {params or '(none)'}")
    typer.echo(f"real path:  {res.real_path or '(none)'}")
    if res.owner is not None and res.owner is not res.route:
        typer.echo(f"backend of: {res.owner.full_pattern}")
    if res.children:
        typer.echo(f"children:   {', '.join(res.children)}")
    typer.echo(f"param():    {', '.join(callbacks) or '(none)'}")
    typer.echo(f"middleware: {', '.join(chain) or '(none)'}  (for {op})")


def main() -> None:
    app()
