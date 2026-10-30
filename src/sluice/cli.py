"""The `sluice` command.

The wiki's CLI-Reference page is generated from this module (scripts/gen-wiki.py),
so command docstrings and option help are user documentation.
"""

from __future__ import annotations

from pathlib import Path
from typing import Annotated

import typer

from sluice import __version__
from sluice.config import CONFIG_ENV, SETTINGS, Config, ConfigError, load_config

app = typer.Typer(
    help="Sluice — a Python FUSE filesystem with an Express-style router that gates, rewrites, and middleware-intercepts every filesystem call between virtual paths and their backing stores.",
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


def _load(path: Path | None) -> Config:
    try:
        return load_config(path)
    except ConfigError as e:
        typer.echo(f"error: {e}", err=True)
        raise typer.Exit(2) from e


@app.command()
def version() -> None:
    """Print the version."""
    typer.echo(__version__)


@app.command("config")
def show_config(config: ConfigOption = None) -> None:
    """Print the effective configuration and where each value came from. Secrets are masked."""
    cfg = _load(config)
    values = {
        "log_level": cfg.log_level,
    }
    for setting in SETTINGS:
        shown = "********" if setting.secret else values.get(setting.key)
        typer.echo(f"{setting.key} = {shown}  ({cfg.sources[setting.key]})")


def main() -> None:
    app()
