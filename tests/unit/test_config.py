from collections.abc import Callable
from pathlib import Path

import pytest

from sluice.config import ConfigError, load_config


def test_defaults_when_no_file(tmp_path: Path) -> None:
    cfg = load_config(tmp_path / "missing.yaml", environ={})
    assert cfg.log_level == "INFO"
    assert cfg.sources["log_level"] == "default"


def test_file_overrides_default(write_config: Callable[..., Path]) -> None:
    cfg = load_config(write_config("log_level: debug\n"), environ={})
    assert cfg.log_level == "DEBUG"
    assert cfg.sources["log_level"] == "file"


def test_env_overrides_file(write_config: Callable[..., Path]) -> None:
    path = write_config("log_level: debug\n")
    cfg = load_config(path, environ={"SLUICE_LOG_LEVEL": "warning"})
    assert cfg.log_level == "WARNING"
    assert cfg.sources["log_level"] == "SLUICE_LOG_LEVEL"


def test_file_variable_overrides_env(tmp_path: Path) -> None:
    secret = tmp_path / "level"
    secret.write_text("error\n", encoding="utf-8")
    env = {"SLUICE_LOG_LEVEL": "warning", "SLUICE_LOG_LEVEL_FILE": str(secret)}
    cfg = load_config(tmp_path / "missing.yaml", environ=env)
    assert cfg.log_level == "ERROR"
    assert cfg.sources["log_level"] == "SLUICE_LOG_LEVEL_FILE"


def test_config_path_from_environment(write_config: Callable[..., Path]) -> None:
    path = write_config("log_level: error\n")
    cfg = load_config(environ={"SLUICE_CONFIG": str(path)})
    assert cfg.log_level == "ERROR"


def test_unreadable_file_variable(tmp_path: Path) -> None:
    env = {"SLUICE_LOG_LEVEL_FILE": str(tmp_path / "nope")}
    with pytest.raises(ConfigError, match="cannot read"):
        load_config(tmp_path / "missing.yaml", environ=env)


def test_invalid_log_level(tmp_path: Path) -> None:
    with pytest.raises(ConfigError, match="log_level"):
        load_config(tmp_path / "missing.yaml", environ={"SLUICE_LOG_LEVEL": "loud"})


def test_top_level_must_be_mapping(write_config: Callable[..., Path]) -> None:
    with pytest.raises(ConfigError, match="mapping"):
        load_config(write_config("- a\n- b\n"), environ={})


def test_empty_file_is_defaults(write_config: Callable[..., Path]) -> None:
    assert load_config(write_config(""), environ={}).log_level == "INFO"


def test_invalid_yaml(write_config: Callable[..., Path]) -> None:
    with pytest.raises(ConfigError, match="cannot read"):
        load_config(write_config("log_level: [unclosed\n"), environ={})


@pytest.mark.parametrize(
    ("raw", "expected"), [("yes", True), ("0", False), (True, True), ("off", False)]
)
def test_bool_coercion(raw: object, expected: bool) -> None:
    from sluice.config import Setting, _coerce

    assert _coerce(Setting("flag", False, "a flag"), raw) is expected


def test_bool_coercion_rejects_garbage() -> None:
    from sluice.config import Setting, _coerce

    with pytest.raises(ConfigError, match="expected bool"):
        _coerce(Setting("flag", False, "a flag"), "maybe")


def test_new_settings_defaults(tmp_path: Path) -> None:
    cfg = load_config(tmp_path / "missing.yaml", environ={})
    assert cfg.app_module == ""
    assert cfg.mount_fsname == "sluice"
    assert cfg.mount_allow_other is False
    assert cfg.mount_cache_ms == 0
    assert cfg.io_threads == 8
    assert cfg.env_expose == ()
    assert cfg.secrets_dir == Path("/run/secrets")
    assert cfg.pipeline_slow_ms == 0


def test_nested_keys_from_file_and_env(write_config: Callable[..., Path]) -> None:
    path = write_config("app:\n  module: routes:app\nmount:\n  point: /mnt/x\n  cache_ms: 50\n")
    cfg = load_config(path, environ={"SLUICE_MOUNT_ALLOW_OTHER": "yes", "SLUICE_IO_THREADS": "2"})
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


@pytest.mark.parametrize("name", ["SLUICE_CONFIG", "SLUICE_LOG_LEVEL_FILE"])
def test_env_expose_refuses_sluice_names(tmp_path: Path, name: str) -> None:
    with pytest.raises(ConfigError, match="sluice setting"):
        load_config(tmp_path / "m.yaml", environ={"SLUICE_ENV_EXPOSE": f"HOME,{name}"})


def test_env_expose_refuses_bad_names(tmp_path: Path) -> None:
    with pytest.raises(ConfigError, match="not an environment variable name"):
        load_config(tmp_path / "m.yaml", environ={"SLUICE_ENV_EXPOSE": "A-B"})


@pytest.mark.parametrize(
    ("var", "value", "match"),
    [
        ("SLUICE_MOUNT_CACHE_MS", "-1", "mount.cache_ms"),
        ("SLUICE_PIPELINE_SLOW_MS", "-5", "pipeline.slow_ms"),
        ("SLUICE_IO_THREADS", "0", "io.threads"),
        ("SLUICE_IO_THREADS", "many", "expected int"),
    ],
)
def test_numeric_limits(tmp_path: Path, var: str, value: str, match: str) -> None:
    with pytest.raises(ConfigError, match=match):
        load_config(tmp_path / "m.yaml", environ={var: value})


@pytest.mark.parametrize(
    ("yaml_text", "match"),
    [
        ("mount:\n  cache_ms: 0.5\n", r"mount\.cache_ms: expected int, got 0\.5"),
        ("pipeline:\n  slow_ms: true\n", r"pipeline\.slow_ms: expected int, got True"),
        ("io:\n  threads: 2.0\n", r"io\.threads: expected int, got 2\.0"),
        ("io:\n  threads: [2]\n", r"io\.threads: expected int, got \[2\]"),
    ],
)
def test_int_settings_reject_floats_and_bools_from_file(
    write_config: Callable[..., Path], yaml_text: str, match: str
) -> None:
    with pytest.raises(ConfigError, match=match):
        load_config(write_config(yaml_text), environ={})


def test_int_settings_accept_env_strings(tmp_path: Path) -> None:
    cfg = load_config(tmp_path / "m.yaml", environ={"SLUICE_MOUNT_CACHE_MS": " 250 "})
    assert cfg.mount_cache_ms == 250


@pytest.mark.parametrize(
    ("yaml_text", "match"),
    [
        ("mount:\n  point: [a]\n", r"mount\.point: expected str, got \['a'\]"),
        ("app:\n  module: {a: 1}\n", r"app\.module: expected str, got \{'a': 1\}"),
        ("secrets:\n  dir: [/run]\n", r"secrets\.dir: expected str, got \['/run'\]"),
    ],
)
def test_text_settings_reject_lists_and_mappings_from_file(
    write_config: Callable[..., Path], yaml_text: str, match: str
) -> None:
    with pytest.raises(ConfigError, match=match):
        load_config(write_config(yaml_text), environ={})


def test_text_settings_accept_yaml_scalars(write_config: Callable[..., Path]) -> None:
    cfg = load_config(write_config("mount:\n  fsname: 123\n"), environ={})
    assert cfg.mount_fsname == "123"
