"""Configuration.

Every setting can come from four places, and the first one that has it wins:

1. ``SLUICE_<NAME>_FILE``: a file that holds the value (Docker / Kubernetes secrets)
2. ``SLUICE_<NAME>``: an environment variable
3. the YAML config file (``SLUICE_CONFIG``, default ``/etc/sluice/config.yaml``)
4. the default in SETTINGS

``<NAME>`` is the dotted key in upper case, with the dots as underscores:
``listen.port`` is ``SLUICE_LISTEN_PORT``.

The engine (lookup order, strict coercion, ``check=``) is interpose's (design 0003); this module
holds sluice's settings and its own rules. Its contract with the template-owned files (``cli.py``,
``scripts/gen-wiki.py``): it exports SETTINGS, CONFIG_ENV, Config (with ``sources``), ConfigError
and ``load_config(path=None, environ=None)``.

To add a setting, add it to SETTINGS and to Config, and read it in load_config. A rule on one
value goes in the setting's ``check``; a rule across settings goes in load_config. The wiki's
settings table is generated from SETTINGS (``scripts/gen-wiki.py``), so write its help as you
want users to read it. Values are never logged; only where each one came from is.
"""

from __future__ import annotations

import re
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import cast

from interpose.settings import (
    ConfigError,
    Setting,
    Settings,
    Value,
    non_negative,
    one_of,
    positive,
)

__all__ = [
    "CONFIG_ENV",
    "DEFAULT_CONFIG_PATH",
    "ENV_PREFIX",
    "LOG_LEVELS",
    "SETTINGS",
    "Config",
    "ConfigError",
    "load_config",
]

ENV_PREFIX = "SLUICE_"
DEFAULT_CONFIG_PATH = Path("/etc/sluice/config.yaml")
LOG_LEVELS = ("DEBUG", "INFO", "WARNING", "ERROR")

_ENV_NAME = re.compile(r"[A-Za-z_][A-Za-z0-9_]*")


def _exposable(value: Value) -> None:
    """`env.expose`: environment variable names, and none of sluice's own."""
    for name in cast(tuple[str, ...], value):
        if name.startswith(ENV_PREFIX):
            # sluice's own settings (and their _FILE paths) stay out of templates and middleware.
            raise ValueError(f"{name} is a sluice setting and can't be exposed")
        if not _ENV_NAME.fullmatch(name):
            raise ValueError(f"{name!r} is not an environment variable name")


SETTINGS = Settings(
    ENV_PREFIX,
    [
        Setting(
            "log_level",
            "INFO",
            "Log level: `DEBUG`, `INFO`, `WARNING` or `ERROR`.",
            check=one_of(*LOG_LEVELS),
        ),
        Setting(
            "app.module",
            "",
            "The app to serve, `module:attribute` (e.g. `myroutes:app`)."
            " Required by `mount`, `routes` and `resolve`.",
        ),
        Setting(
            "app.dir",
            "",
            "Directory put first on `sys.path` before importing `app.module`"
            " (e.g. a mounted ConfigMap). Empty: `sys.path` is unchanged.",
        ),
        Setting(
            "mount.point",
            "",
            "Where to mount: an existing, empty directory. Required by `mount`.",
        ),
        Setting("mount.fsname", "sluice", "Filesystem name shown by `mount` and `df`."),
        Setting(
            "mount.allow_other",
            False,
            "Let users other than the mounting one in. Needs `user_allow_other` in"
            " `/etc/fuse.conf`. With it on, middleware is the only gate.",
        ),
        Setting(
            "mount.cache_ms",
            0,
            "Kernel entry and attribute cache lifetime, in milliseconds."
            " Above 0, cached calls skip middleware.",
            check=non_negative,
        ),
        Setting(
            "mount.debug",
            False,
            "libfuse request tracing. Logs raw paths, including any token in them: lab use only.",
        ),
        Setting(
            "io.threads",
            8,
            "Worker threads for backend syscalls (at least 1).",
            check=positive,
        ),
        Setting(
            "env.expose",
            "",
            "Environment variable names copied into `ctx.config.env`."
            " Empty: none. `SLUICE_*` names are refused.",
            is_list=True,
            check=_exposable,
        ),
        Setting(
            "secrets.dir",
            "/run/secrets",
            "Each regular file in it becomes `ctx.config.secret[<filename>]` (trailing newline"
            " stripped). A missing directory means no secrets.",
        ),
        Setting(
            "pipeline.slow_ms",
            0,
            "Log a warning when a middleware or handler holds the event loop longer than this,"
            " in milliseconds. 0: off.",
            check=non_negative,
        ),
    ],
    default_path=DEFAULT_CONFIG_PATH,
)
CONFIG_ENV = SETTINGS.config_env


@dataclass(frozen=True)
class Config:
    log_level: str
    app_module: str = ""
    app_dir: str = ""
    mount_point: str = ""
    mount_fsname: str = "sluice"
    mount_allow_other: bool = False
    mount_cache_ms: int = 0
    mount_debug: bool = False
    io_threads: int = 8
    env_expose: tuple[str, ...] = ()
    secrets_dir: Path = Path("/run/secrets")
    pipeline_slow_ms: int = 0
    # Setting key -> where its value came from ("default", "file", an env var name).
    sources: Mapping[str, str] = field(default_factory=dict)


def load_config(path: Path | None = None, environ: Mapping[str, str] | None = None) -> Config:
    """Load the configuration. A missing config file is not an error: defaults apply."""
    r = SETTINGS.resolve(path, environ)
    v = r.values
    # No rule spans settings yet; one would go here, after resolve().
    return Config(
        log_level=cast(str, v["log_level"]).upper(),
        app_module=cast(str, v["app.module"]),
        app_dir=cast(str, v["app.dir"]),
        mount_point=cast(str, v["mount.point"]),
        mount_fsname=cast(str, v["mount.fsname"]),
        mount_allow_other=cast(bool, v["mount.allow_other"]),
        mount_cache_ms=cast(int, v["mount.cache_ms"]),
        mount_debug=cast(bool, v["mount.debug"]),
        io_threads=cast(int, v["io.threads"]),
        env_expose=cast(tuple[str, ...], v["env.expose"]),
        secrets_dir=Path(cast(str, v["secrets.dir"])),
        pipeline_slow_ms=cast(int, v["pipeline.slow_ms"]),
        sources=dict(r.sources),
    )
