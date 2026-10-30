"""Configuration.

Every setting can come from four places, and the first one that has it wins:

1. ``SLUICE_<NAME>_FILE``: a file that holds the value (Docker / Kubernetes secrets)
2. ``SLUICE_<NAME>``: an environment variable
3. the YAML config file (``SLUICE_CONFIG``, default ``/etc/sluice/config.yaml``)
4. the default in SETTINGS

``<NAME>`` is the dotted key in upper case, with the dots as underscores:
``listen.port`` is ``SLUICE_LISTEN_PORT``.

To add a setting, add it to SETTINGS and to Config, and read it in load_config.
The wiki's settings table is generated from SETTINGS (``scripts/gen-wiki.py``),
so write its help as you want users to read it. Values are never logged;
only where each one came from is.
"""

from __future__ import annotations

import os
import re
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

ENV_PREFIX = "SLUICE_"
CONFIG_ENV = f"{ENV_PREFIX}CONFIG"
DEFAULT_CONFIG_PATH = Path("/etc/sluice/config.yaml")
LOG_LEVELS = ("DEBUG", "INFO", "WARNING", "ERROR")

Value = str | int | bool
_ENV_NAME = re.compile(r"[A-Za-z_][A-Za-z0-9_]*")


class ConfigError(Exception):
    """The configuration can't be read, or a value in it is invalid."""


@dataclass(frozen=True)
class Setting:
    key: str
    default: Value
    help: str
    secret: bool = False

    @property
    def env(self) -> str:
        return ENV_PREFIX + self.key.upper().replace(".", "_")


SETTINGS: tuple[Setting, ...] = (
    Setting("log_level", "INFO", "Log level: `DEBUG`, `INFO`, `WARNING` or `ERROR`."),
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
    ),
    Setting(
        "mount.debug",
        False,
        "libfuse request tracing. Logs raw paths, including any token in them: lab use only.",
    ),
    Setting("io.threads", 8, "Worker threads for backend syscalls (at least 1)."),
    Setting(
        "env.expose",
        "",
        "Comma-separated environment variable names copied into `ctx.config.env`."
        " Empty: none. `SLUICE_*` names are refused.",
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
    ),
)


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


def _lookup(data: Mapping[str, Any], dotted: str) -> Any:
    node: Any = data
    for part in dotted.split("."):
        if not isinstance(node, Mapping) or part not in node:
            return None
        node = node[part]
    return node


def _coerce(setting: Setting, raw: Any) -> Value:
    """Give raw (a string from the environment, or a YAML scalar) the default's type."""
    try:
        if isinstance(setting.default, bool):
            if isinstance(raw, bool):
                return raw
            text = str(raw).strip().lower()
            if text in {"1", "true", "yes", "on"}:
                return True
            if text in {"0", "false", "no", "off"}:
                return False
            raise ValueError(raw)
        if isinstance(setting.default, int):
            return int(raw)
    except ValueError:
        kind = type(setting.default).__name__
        raise ConfigError(f"{setting.key}: expected {kind}, got {raw!r}") from None
    return str(raw)


def resolve(
    setting: Setting, data: Mapping[str, Any], environ: Mapping[str, str]
) -> tuple[Value, str]:
    """The setting's value and where it came from."""
    file_var = f"{setting.env}_FILE"
    if file_var in environ:
        path = Path(environ[file_var])
        try:
            return _coerce(setting, path.read_text(encoding="utf-8").strip()), file_var
        except OSError as e:
            raise ConfigError(f"cannot read {file_var} ({path}): {e.strerror}") from e
    if setting.env in environ:
        return _coerce(setting, environ[setting.env]), setting.env
    value = _lookup(data, setting.key)
    if value is not None:
        return _coerce(setting, value), "file"
    return setting.default, "default"


def read_file(path: Path) -> dict[str, Any]:
    try:
        loaded = yaml.safe_load(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return {}
    except (OSError, yaml.YAMLError) as e:
        raise ConfigError(f"cannot read {path}: {e}") from e
    if loaded is None:
        return {}
    if not isinstance(loaded, dict):
        raise ConfigError(f"{path}: the top level must be a mapping")
    return loaded


def load_config(path: Path | None = None, environ: Mapping[str, str] | None = None) -> Config:
    """Load the configuration. A missing config file is not an error: defaults apply."""
    env = os.environ if environ is None else environ
    if path is None:
        path = Path(env.get(CONFIG_ENV, str(DEFAULT_CONFIG_PATH)))
    data = read_file(path)

    values: dict[str, Value] = {}
    sources: dict[str, str] = {}
    for setting in SETTINGS:
        values[setting.key], sources[setting.key] = resolve(setting, data, env)

    log_level = str(values["log_level"]).upper()
    if log_level not in LOG_LEVELS:
        raise ConfigError(f"log_level: expected one of {', '.join(LOG_LEVELS)}, got {log_level!r}")

    for key in ("mount.cache_ms", "pipeline.slow_ms"):
        if int(values[key]) < 0:
            raise ConfigError(f"{key}: must be 0 or more, got {values[key]}")
    if int(values["io.threads"]) < 1:
        raise ConfigError(f"io.threads: must be at least 1, got {values['io.threads']}")

    expose = tuple(n.strip() for n in str(values["env.expose"]).split(",") if n.strip())
    for name in expose:
        if name.startswith(ENV_PREFIX):
            # sluice's own settings (and their _FILE paths) stay out of templates and middleware.
            raise ConfigError(f"env.expose: {name} is a sluice setting and can't be exposed")
        if not _ENV_NAME.fullmatch(name):
            raise ConfigError(f"env.expose: {name!r} is not an environment variable name")

    return Config(
        log_level=log_level,
        app_module=str(values["app.module"]),
        app_dir=str(values["app.dir"]),
        mount_point=str(values["mount.point"]),
        mount_fsname=str(values["mount.fsname"]),
        mount_allow_other=bool(values["mount.allow_other"]),
        mount_cache_ms=int(values["mount.cache_ms"]),
        mount_debug=bool(values["mount.debug"]),
        io_threads=int(values["io.threads"]),
        env_expose=expose,
        secrets_dir=Path(str(values["secrets.dir"])),
        pipeline_slow_ms=int(values["pipeline.slow_ms"]),
        sources=sources,
    )
