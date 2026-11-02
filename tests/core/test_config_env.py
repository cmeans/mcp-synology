"""Environment variables and config discovery (#511 PR 1: #501, #499, #458's env items).

Every ``SYNOLOGY_*`` variable is parsed with its config field's own type, so it takes
exactly what the config file does, and an invalid value is an error naming the variable,
never a silent default. A config file named by ``MCP_SYNOLOGY_CONFIG`` must exist.
"""

from __future__ import annotations

import logging
import re
from pathlib import Path
from typing import Any

import pytest
import yaml

from mcp_synology.core.config import (
    ENV_VAR_MAP,
    EnvVarError,
    load_config,
    parse_env_value,
)

ROOT = Path(__file__).resolve().parents[2]


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch: pytest.MonkeyPatch) -> None:
    for var in (*ENV_VAR_MAP, "MCP_SYNOLOGY_CONFIG"):
        monkeypatch.delenv(var, raising=False)


def _config(tmp_path: Path, **connection: Any) -> Path:
    path = tmp_path / "config.yaml"
    raw = {
        "schema_version": 1,
        "connection": {"host": "nas.example", **connection},
        "modules": {"filestation": {"enabled": True}},
    }
    path.write_text(yaml.safe_dump(raw))
    return path


# -- one table, parsed by field type ---------------------------------------------------


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        ("true", True),
        ("TRUE", True),
        ("1", True),
        ("yes", True),
        ("on", True),
        ("On", True),
        ("false", False),
        ("0", False),
        ("no", False),
        ("off", False),
        (" off ", False),
    ],
)
def test_https_takes_the_standard_boolean_spellings(value: str, expected: bool) -> None:
    assert parse_env_value("SYNOLOGY_HTTPS", "connection.https", value) is expected


@pytest.mark.parametrize(
    ("env_var", "value", "expected"),
    [
        ("SYNOLOGY_PORT", "5001", 5001),
        ("SYNOLOGY_PORT", " 5001 ", 5001),
        ("SYNOLOGY_LOG_LEVEL", "DEBUG", "debug"),
        ("SYNOLOGY_LOG_LEVEL", "Warning", "warning"),
        ("SYNOLOGY_UPDATE_NOTICES", "SECURITY", "security"),
        ("SYNOLOGY_UPDATE_NOTICES", "off", "off"),
        ("SYNOLOGY_HOST", "nas.example", "nas.example"),
    ],
)
def test_values_are_parsed_by_field_type(env_var: str, value: str, expected: Any) -> None:
    assert parse_env_value(env_var, ENV_VAR_MAP[env_var], value) == expected


# Every variable whose field isn't free text, with a value that field refuses. The
# value looks like a password: an error must never echo it.
INVALID = {
    "SYNOLOGY_PORT": "hunter2",
    "SYNOLOGY_HTTPS": "hunter2",
    "SYNOLOGY_LOG_LEVEL": "hunter2",
    "SYNOLOGY_UPDATE_NOTICES": "hunter2",
}
TEXT_VARS = {
    "SYNOLOGY_HOST",
    "SYNOLOGY_USERNAME",
    "SYNOLOGY_PASSWORD",
    "SYNOLOGY_DEVICE_ID",
    "SYNOLOGY_INSTANCE_ID",
}


def test_every_variable_is_typed_or_free_text() -> None:
    assert set(INVALID) | TEXT_VARS == set(ENV_VAR_MAP)
    assert not set(INVALID) & TEXT_VARS


@pytest.mark.parametrize("env_var", sorted(INVALID))
def test_an_invalid_value_names_the_variable_and_hides_the_value(
    env_var: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv(env_var, INVALID[env_var])
    with pytest.raises(EnvVarError) as excinfo:
        load_config(_config(tmp_path))
    message = str(excinfo.value)
    assert message.startswith(f"{env_var} must be ")
    assert "hunter2" not in message


# -- #501: HTTPS is never turned off by a value it doesn't take -------------------------


@pytest.mark.parametrize(("value", "https"), [("on", True), ("off", False), ("1", True)])
def test_https_env_overrides_the_config_by_value(
    value: str, https: bool, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("SYNOLOGY_HTTPS", value)
    config = load_config(_config(tmp_path, https=not https))
    assert config.connection is not None
    assert config.connection.https is https


@pytest.mark.parametrize("value", ["sure", "enabled", "tru"])
def test_an_unknown_https_value_is_an_error_not_off(
    value: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # It used to be read as false, turning off `https: true` in the config (#501).
    monkeypatch.setenv("SYNOLOGY_HTTPS", value)
    with pytest.raises(EnvVarError, match=r"^SYNOLOGY_HTTPS must be true or false"):
        load_config(_config(tmp_path, https=True))


@pytest.mark.parametrize("value", ["", "   "])
def test_an_empty_variable_counts_as_unset(
    value: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("SYNOLOGY_HTTPS", value)
    monkeypatch.setenv("SYNOLOGY_PORT", value)
    config = load_config(_config(tmp_path, https=True, port=5001))
    assert config.connection is not None
    assert (config.connection.https, config.connection.port) == (True, 5001)


def test_an_upper_case_log_level_loads(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("SYNOLOGY_LOG_LEVEL", "DEBUG")
    assert load_config(_config(tmp_path)).logging.level == "debug"


# -- #499: a named config file must exist ----------------------------------------------


def test_a_missing_mcp_synology_config_file_is_an_error_not_env_only(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # With SYNOLOGY_HOST set, it used to fall back to env-only mode: read-only File
    # Station over plain HTTP, instead of the config the variable names.
    monkeypatch.setenv("SYNOLOGY_HOST", "nas.example")
    monkeypatch.setenv("MCP_SYNOLOGY_CONFIG", str(tmp_path / "missing.yaml"))
    with pytest.raises(FileNotFoundError, match="MCP_SYNOLOGY_CONFIG"):
        load_config()


def test_env_only_mode_still_works_without_a_named_file(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("SYNOLOGY_HOST", "nas.example")
    with caplog.at_level(logging.INFO, logger="mcp_synology.core.config"):
        config = load_config()
    assert config.connection is not None and config.connection.host == "nas.example"
    assert "Config: environment variables only" in caplog.text
    assert "nas.example" not in caplog.text  # a host can hold credentials (#476)


@pytest.mark.parametrize("how", ["flag", "env"])
def test_the_config_source_is_logged_at_info(
    how: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    path = _config(tmp_path)
    with caplog.at_level(logging.INFO, logger="mcp_synology.core.config"):
        if how == "flag":
            load_config(path)
        else:
            monkeypatch.setenv("MCP_SYNOLOGY_CONFIG", str(path))
            load_config()
    where = "--config" if how == "flag" else "MCP_SYNOLOGY_CONFIG"
    assert f"Config: {path} (from {where})" in caplog.text


# -- the documented table matches the code's ---------------------------------------------


def _documented(doc: Path) -> set[str]:
    """The env vars in a doc's override table: rows starting with `SYNOLOGY_…`."""
    return set(re.findall(r"^\| `(SYNOLOGY_[A-Z_]+)` \|", doc.read_text(), re.MULTILINE))


@pytest.mark.parametrize("doc", ["README.md", "docs/specs/config-schema-spec.md"])
def test_the_documented_env_table_matches_the_code(doc: str) -> None:
    assert _documented(ROOT / doc) == set(ENV_VAR_MAP)


def test_the_readme_documents_mcp_synology_config() -> None:
    assert "MCP_SYNOLOGY_CONFIG" in (ROOT / "README.md").read_text()


@pytest.mark.parametrize("command", ["check", "serve"])
def test_the_cli_shows_the_error_without_a_traceback(
    command: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from click.testing import CliRunner

    from mcp_synology.cli import main

    result = CliRunner().invoke(
        main,
        [command, "--config", str(_config(tmp_path, https=True))],
        env={"SYNOLOGY_HTTPS": "hunter2"},
    )
    assert result.exit_code == 1
    assert "Error: SYNOLOGY_HTTPS must be true or false" in result.output
    assert "hunter2" not in result.output
    assert "Traceback" not in result.output


# -- pre-QA r1 ----------------------------------------------------------------------


def test_setup_refuses_a_missing_mcp_synology_config_file(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # It used to start creating a new config elsewhere, ignoring the variable (#499).
    from click.testing import CliRunner

    from mcp_synology.cli import main

    monkeypatch.setenv("HOME", str(tmp_path))
    result = CliRunner().invoke(
        main,
        ["setup"],
        env={"SYNOLOGY_HOST": "nas.invalid", "MCP_SYNOLOGY_CONFIG": str(tmp_path / "nope.yaml")},
        input="",
    )
    assert result.exit_code == 1
    assert "Config file not found (from MCP_SYNOLOGY_CONFIG)" in result.output
    assert "Let's create one" not in result.output


def test_a_blank_synology_host_is_not_env_only_mode(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("SYNOLOGY_HOST", "   ")
    with pytest.raises(FileNotFoundError, match="No config file found"):
        load_config()


def test_a_blank_mcp_synology_config_counts_as_unset(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("MCP_SYNOLOGY_CONFIG", "  ")
    monkeypatch.setenv("SYNOLOGY_HOST", "nas.example")
    config = load_config()
    assert config.connection is not None and config.connection.host == "nas.example"


def test_a_blank_password_variable_doesnt_hide_the_files_plaintext_password(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    path = tmp_path / "config.yaml"
    path.write_text(
        yaml.safe_dump(
            {
                "schema_version": 1,
                "connection": {"host": "nas.example"},
                "auth": {"password": "fakepw"},
                "modules": {"filestation": {"enabled": True}},
            }
        )
    )
    monkeypatch.setenv("SYNOLOGY_PASSWORD", "   ")
    with caplog.at_level(logging.WARNING, logger="mcp_synology.core.config"):
        load_config(path)
    assert "Plaintext password found in config file" in caplog.text
    assert "SYNOLOGY_PASSWORD environment variable" not in caplog.text


@pytest.mark.parametrize("value", ["no", "false", "off", "on", "yes", "true"])
def test_update_notices_takes_the_yaml_booleans_the_file_does(
    value: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from_file = yaml.safe_load(f"update_notices: {value}")["update_notices"]
    path = tmp_path / "config.yaml"
    path.write_text(
        yaml.safe_dump(
            {
                "schema_version": 1,
                "connection": {"host": "nas.example"},
                "update_notices": from_file,
                "modules": {"filestation": {"enabled": True}},
            }
        )
    )
    file_value = load_config(path).update_notices
    monkeypatch.setenv("SYNOLOGY_UPDATE_NOTICES", value)
    assert load_config(_config(tmp_path)).update_notices == file_value


def test_a_default_location_is_logged_at_info(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    monkeypatch.chdir(tmp_path)
    path = tmp_path / "mcp-synology.yaml"
    _config(tmp_path).rename(path)
    with caplog.at_level(logging.INFO, logger="mcp_synology.core.config"):
        load_config()
    assert any(r.getMessage() == f"Config: {path}" for r in caplog.records)


def test_a_blank_mcp_synology_config_still_finds_the_default_file(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    monkeypatch.chdir(tmp_path)
    _config(tmp_path).rename(tmp_path / "mcp-synology.yaml")
    monkeypatch.setenv("MCP_SYNOLOGY_CONFIG", "  ")
    config = load_config()
    assert config.connection is not None and config.connection.host == "nas.example"
    assert config.modules  # the file's, not env-only mode's synthesized config
