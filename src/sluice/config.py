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
)


@dataclass(frozen=True)
class Config:
    log_level: str
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

    return Config(
        log_level=log_level,
        sources=sources,
    )
