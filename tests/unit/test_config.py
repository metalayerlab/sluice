"""sluice's settings and its own rules.

The engine (lookup order, coercion, `_FILE`, YAML errors) is interpose's and tested there; these
tests pin what is sluice's: the keys and variables, defaults, checks and the `env.expose` rules.
"""

from collections.abc import Callable
from pathlib import Path

import pytest

from sluice.config import CONFIG_ENV, DEFAULT_CONFIG_PATH, SETTINGS, ConfigError, load_config


def test_keys_and_variables_are_unchanged() -> None:
    # Renaming one breaks every deployment that sets it: this list changes only on purpose.
    assert [(s.key, s.env) for s in SETTINGS] == [
        ("log_level", "SLUICE_LOG_LEVEL"),
        ("app.module", "SLUICE_APP_MODULE"),
        ("app.dir", "SLUICE_APP_DIR"),
        ("mount.point", "SLUICE_MOUNT_POINT"),
        ("mount.fsname", "SLUICE_MOUNT_FSNAME"),
        ("mount.allow_other", "SLUICE_MOUNT_ALLOW_OTHER"),
        ("mount.cache_ms", "SLUICE_MOUNT_CACHE_MS"),
        ("mount.debug", "SLUICE_MOUNT_DEBUG"),
        ("io.threads", "SLUICE_IO_THREADS"),
        ("env.expose", "SLUICE_ENV_EXPOSE"),
        ("secrets.dir", "SLUICE_SECRETS_DIR"),
        ("pipeline.slow_ms", "SLUICE_PIPELINE_SLOW_MS"),
    ]
    assert CONFIG_ENV == "SLUICE_CONFIG"
    assert Path("/etc/sluice/config.yaml") == DEFAULT_CONFIG_PATH
    assert not any(s.secret for s in SETTINGS)


def test_defaults_when_no_file(tmp_path: Path) -> None:
    cfg = load_config(tmp_path / "missing.yaml", environ={})
    assert cfg.log_level == "INFO"
    assert cfg.app_module == ""
    assert cfg.app_dir == ""
    assert cfg.mount_point == ""
    assert cfg.mount_fsname == "sluice"
    assert cfg.mount_allow_other is False
    assert cfg.mount_cache_ms == 0
    assert cfg.mount_debug is False
    assert cfg.io_threads == 8
    assert cfg.env_expose == ()
    assert cfg.secrets_dir == Path("/run/secrets")
    assert cfg.pipeline_slow_ms == 0
    assert set(cfg.sources.values()) == {"default"}


def test_config_path_from_environment(write_config: Callable[..., Path]) -> None:
    path = write_config("log_level: error\n")
    cfg = load_config(environ={"SLUICE_CONFIG": str(path)})
    assert cfg.log_level == "ERROR"
    assert cfg.sources["log_level"] == "file"


def test_log_level_is_upper_cased(write_config: Callable[..., Path]) -> None:
    cfg = load_config(write_config("log_level: debug\n"), environ={"SLUICE_LOG_LEVEL": "warning"})
    assert cfg.log_level == "WARNING"
    assert cfg.sources["log_level"] == "SLUICE_LOG_LEVEL"


def test_invalid_log_level(tmp_path: Path) -> None:
    match = r"log_level: expected one of DEBUG, INFO, WARNING, ERROR, got 'loud'"
    with pytest.raises(ConfigError, match=match):
        load_config(tmp_path / "missing.yaml", environ={"SLUICE_LOG_LEVEL": "loud"})


def test_nested_keys_from_file_and_env(write_config: Callable[..., Path]) -> None:
    path = write_config("app:\n  module: routes:app\nmount:\n  point: /mnt/x\n  cache_ms: 50\n")
    env = {"SLUICE_MOUNT_ALLOW_OTHER": "yes", "SLUICE_IO_THREADS": "2"}
    cfg = load_config(path, environ=env)
    assert cfg.app_module == "routes:app"
    assert cfg.mount_point == "/mnt/x"
    assert cfg.mount_cache_ms == 50
    assert cfg.mount_allow_other is True
    assert cfg.io_threads == 2
    assert cfg.sources["mount.cache_ms"] == "file"
    assert cfg.sources["io.threads"] == "SLUICE_IO_THREADS"


def test_env_expose_is_split_and_trimmed(tmp_path: Path) -> None:
    cfg = load_config(tmp_path / "m.yaml", environ={"SLUICE_ENV_EXPOSE": " HOME, REGION ,,"})
    assert cfg.env_expose == ("HOME", "REGION")


def test_env_expose_takes_a_yaml_list(write_config: Callable[..., Path]) -> None:
    cfg = load_config(write_config("env:\n  expose: [HOME, REGION]\n"), environ={})
    assert cfg.env_expose == ("HOME", "REGION")


@pytest.mark.parametrize("name", ["SLUICE_CONFIG", "SLUICE_LOG_LEVEL_FILE"])
def test_env_expose_refuses_sluice_names(tmp_path: Path, name: str) -> None:
    with pytest.raises(ConfigError, match=rf"env\.expose: {name} is a sluice setting"):
        load_config(tmp_path / "m.yaml", environ={"SLUICE_ENV_EXPOSE": f"HOME,{name}"})


def test_env_expose_refuses_sluice_names_from_a_yaml_list(
    write_config: Callable[..., Path],
) -> None:
    with pytest.raises(ConfigError, match="sluice setting"):
        load_config(write_config("env:\n  expose: [SLUICE_CONFIG]\n"), environ={})


def test_env_expose_refuses_bad_names(tmp_path: Path) -> None:
    match = r"env\.expose: 'A-B' is not an environment variable name"
    with pytest.raises(ConfigError, match=match):
        load_config(tmp_path / "m.yaml", environ={"SLUICE_ENV_EXPOSE": "A-B"})


@pytest.mark.parametrize(
    ("var", "value", "match"),
    [
        ("SLUICE_MOUNT_CACHE_MS", "-1", r"mount\.cache_ms: must be 0 or more, got -1"),
        ("SLUICE_PIPELINE_SLOW_MS", "-5", r"pipeline\.slow_ms: must be 0 or more, got -5"),
        ("SLUICE_IO_THREADS", "0", r"io\.threads: must be greater than 0, got 0"),
        ("SLUICE_IO_THREADS", "many", r"io\.threads: expected int"),
    ],
)
def test_numeric_limits(tmp_path: Path, var: str, value: str, match: str) -> None:
    with pytest.raises(ConfigError, match=match):
        load_config(tmp_path / "m.yaml", environ={var: value})


def test_numeric_limits_accept_the_boundary(tmp_path: Path) -> None:
    env = {"SLUICE_MOUNT_CACHE_MS": "0", "SLUICE_PIPELINE_SLOW_MS": "0", "SLUICE_IO_THREADS": "1"}
    cfg = load_config(tmp_path / "m.yaml", environ=env)
    assert (cfg.mount_cache_ms, cfg.pipeline_slow_ms, cfg.io_threads) == (0, 0, 1)
