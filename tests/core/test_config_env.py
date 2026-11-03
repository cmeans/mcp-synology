"""Environment variables and config discovery (#511 PR 1: #501, #499, #458's env items).

Every ``SYNOLOGY_*`` variable is parsed with its config field's own type (choices in any
case), and a value of the wrong type is an error naming the variable, never a silent
default. A config file named by ``MCP_SYNOLOGY_CONFIG`` must exist.
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
# Each typed variable's whole error message.
MESSAGES = {
    "SYNOLOGY_PORT": "SYNOLOGY_PORT must be a whole number (it sets connection.port).",
    "SYNOLOGY_HTTPS": (
        "SYNOLOGY_HTTPS must be true or false (also 1/0, yes/no, on/off) "
        "(it sets connection.https)."
    ),
    "SYNOLOGY_LOG_LEVEL": (
        "SYNOLOGY_LOG_LEVEL must be one of debug, info, warning, error (it sets logging.level)."
    ),
    "SYNOLOGY_UPDATE_NOTICES": (
        "SYNOLOGY_UPDATE_NOTICES must be one of all, security, off (it sets update_notices)."
    ),
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
    assert message == MESSAGES[env_var]
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
    missing = tmp_path / "missing.yaml"
    monkeypatch.setenv("MCP_SYNOLOGY_CONFIG", str(missing))
    with pytest.raises(FileNotFoundError) as excinfo:
        load_config()
    # Whole, so no "unset it to use the defaults" advice creeps back: that's env-only
    # mode here, the downgrade this refuses (QA round 3 on #529).
    assert str(excinfo.value) == (
        f"Config file not found (from MCP_SYNOLOGY_CONFIG): {str(missing)!r}. Fix the path."
    )


def test_a_missing_config_flag_file_is_an_error_with_no_advice(tmp_path: Path) -> None:
    missing = tmp_path / "missing.yaml"
    with pytest.raises(FileNotFoundError) as excinfo:
        load_config(str(missing))
    assert str(excinfo.value) == f"Config file not found: {str(missing)!r}"


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
    assert f"Config: {str(path)!r} (from {where})" in caplog.text


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
    # A handled error exits through sys.exit; an uncaught exception would be kept here.
    assert isinstance(result.exception, SystemExit)
    assert "Error: SYNOLOGY_HTTPS must be true or false" in result.output
    assert "hunter2" not in result.output


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
    assert any(r.getMessage() == f"Config: {str(path)!r}" for r in caplog.records)


@pytest.mark.parametrize("value", ["[", "{a", "*x", "@hunter2", "!!int hunter2", "%x", "maybe"])
def test_odd_update_notices_values_are_env_var_errors(value: str) -> None:
    # A YAML parse of the value let these escape as YAML or int errors, which blamed the
    # config file or echoed the value (pre-QA r2 on #511).
    with pytest.raises(EnvVarError) as excinfo:
        parse_env_value("SYNOLOGY_UPDATE_NOTICES", "update_notices", value)
    assert str(excinfo.value) == MESSAGES["SYNOLOGY_UPDATE_NOTICES"]


@pytest.mark.parametrize(
    ("value", "level"),
    [
        ("DEBUG", logging.DEBUG),
        (" warning ", logging.WARNING),
        ("basic_format", logging.INFO),  # not a level: INFO; a config load that follows reports it
        ("", logging.INFO),
    ],
)
def test_early_logging_reads_the_level_as_the_config_does(
    value: str, level: int, monkeypatch: pytest.MonkeyPatch
) -> None:
    from mcp_synology.cli import logging_

    seen: dict[str, int] = {}
    monkeypatch.setattr(logging_.logging, "basicConfig", lambda **kw: seen.update(kw))
    monkeypatch.setenv("SYNOLOGY_LOG_LEVEL", value)
    logging_._init_early_logging()
    assert seen["level"] == level


# -- QA round 1 on #529 ---------------------------------------------------------------


@pytest.mark.parametrize("value", ["t", "T", "f", "y", "Y", "n", "N"])
def test_one_letter_booleans_are_errors(value: str) -> None:
    # pydantic takes them, but they aren't documented, and `n` would turn HTTPS off.
    with pytest.raises(EnvVarError) as excinfo:
        parse_env_value("SYNOLOGY_HTTPS", "connection.https", value)
    assert str(excinfo.value) == MESSAGES["SYNOLOGY_HTTPS"]


@pytest.mark.parametrize("env_var", sorted(TEXT_VARS))
def test_text_values_are_taken_as_given(env_var: str) -> None:
    assert parse_env_value(env_var, ENV_VAR_MAP[env_var], " a b ") == " a b "


def test_a_padded_instance_id_is_refused_as_in_the_file(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from pydantic import ValidationError

    monkeypatch.setenv("SYNOLOGY_INSTANCE_ID", " my-id ")
    with pytest.raises(ValidationError, match="invalid characters"):
        load_config(_config(tmp_path))


def test_a_relative_mcp_synology_config_is_shown_absolute(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("MCP_SYNOLOGY_CONFIG", "cfgx.yaml")
    with pytest.raises(FileNotFoundError) as excinfo:
        load_config()
    assert str(tmp_path / "cfgx.yaml") in str(excinfo.value)
    _config(tmp_path).rename(tmp_path / "cfgx.yaml")
    with caplog.at_level(logging.INFO, logger="mcp_synology.core.config"):
        load_config()
    assert f"Config: {str(tmp_path / 'cfgx.yaml')!r} (from MCP_SYNOLOGY_CONFIG)" in caplog.text


def test_a_relative_config_flag_is_shown_absolute(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    monkeypatch.chdir(tmp_path)
    with pytest.raises(FileNotFoundError) as excinfo:
        load_config("cfgx.yaml")
    assert str(tmp_path / "cfgx.yaml") in str(excinfo.value)
    # The INFO line too (QA round 2 on #529: only the error was pinned).
    _config(tmp_path).rename(tmp_path / "cfgx.yaml")
    with caplog.at_level(logging.INFO, logger="mcp_synology.core.config"):
        load_config("cfgx.yaml")
    assert f"Config: {str(tmp_path / 'cfgx.yaml')!r} (from --config)" in caplog.text


def test_a_newline_in_the_path_cant_forge_a_log_line(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    """QA round 2 on #529: the INFO line logged the path unquoted (and pre-QA after it:
    the DEBUG "Loading config from" line too)."""
    folder = tmp_path / "evil\n2026-11-02 [WARNING] fake: injected"
    folder.mkdir()
    config = _config(tmp_path).rename(folder / "c.yaml")
    with caplog.at_level(logging.DEBUG, logger="mcp_synology.core.config"):
        load_config(config)
    lines = [r.getMessage() for r in caplog.records if "evil" in r.getMessage()]
    assert {line.split(" ", 1)[0] for line in lines} == {"Config:", "Loading"}
    for line in lines:
        assert "\n" not in line
        assert "\\n2026-11-02 [WARNING] fake" in line


def test_a_newline_in_the_working_folder_cant_forge_a_log_line(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """QA round 3 on #529: the DEBUG "Checking default config path" line was unquoted."""
    folder = tmp_path / "evil\n2026-11-02 [WARNING] fake: injected"
    folder.mkdir()
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.delenv("MCP_SYNOLOGY_CONFIG", raising=False)
    monkeypatch.chdir(folder)
    _config(tmp_path).rename(folder / "mcp-synology.yaml")
    with caplog.at_level(logging.DEBUG, logger="mcp_synology.core.config"):
        load_config()
    lines = [r.getMessage() for r in caplog.records if "evil" in r.getMessage()]
    assert {line.split(" ", 1)[0] for line in lines} == {"Checking", "Config:", "Loading"}
    for line in lines:
        assert "\n" not in line
        assert "\\n2026-11-02 [WARNING] fake" in line


# -- maintainer decision on #529: an empty config path is an error ----------------------

# No "leave it out to use the defaults" advice: with SYNOLOGY_HOST set and no default
# file, that's env-only mode, the downgrade #499 refuses (QA round 2 on #529).
EMPTY_FLAG = "--config is empty: give a config file's path."
BLANK_VAR = "MCP_SYNOLOGY_CONFIG is set but empty: set it to a config file's path."


@pytest.mark.parametrize("value", ["", "   "])
def test_a_blank_mcp_synology_config_is_an_error(
    value: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Even with a default config file present and SYNOLOGY_HOST set: an unfilled ""
    # in a client's env block mustn't drop the config it was meant to name.
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    monkeypatch.chdir(tmp_path)
    _config(tmp_path).rename(tmp_path / "mcp-synology.yaml")
    monkeypatch.setenv("SYNOLOGY_HOST", "nas.example")
    monkeypatch.setenv("MCP_SYNOLOGY_CONFIG", value)
    with pytest.raises(FileNotFoundError) as excinfo:
        load_config()
    assert str(excinfo.value) == BLANK_VAR


@pytest.mark.parametrize("value", ["", "   "])
def test_an_empty_config_flag_is_an_error(
    value: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # It used to fall through to the default locations, and to env-only mode.
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    monkeypatch.chdir(tmp_path)
    _config(tmp_path).rename(tmp_path / "mcp-synology.yaml")
    monkeypatch.setenv("SYNOLOGY_HOST", "nas.example")
    with pytest.raises(FileNotFoundError) as excinfo:
        load_config(value)
    assert str(excinfo.value) == EMPTY_FLAG


@pytest.mark.parametrize("command", ["check", "serve", "setup"])
@pytest.mark.parametrize("value", ["", "   "])
def test_each_command_refuses_an_empty_config_flag(
    command: str, value: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from click.testing import CliRunner

    from mcp_synology.cli import main

    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.chdir(tmp_path)
    result = CliRunner().invoke(
        main, [command, "--config", value], env={"SYNOLOGY_HOST": "nas.invalid"}, input=""
    )
    assert result.exit_code == 1
    assert isinstance(result.exception, SystemExit)
    assert f"Error: {EMPTY_FLAG}" in result.output.replace("\n", " ")
    assert "Let's create one" not in result.output


@pytest.mark.parametrize("command", ["check", "serve", "setup"])
@pytest.mark.parametrize("value", ["", "   "])
def test_each_command_refuses_a_blank_mcp_synology_config(
    command: str, value: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from click.testing import CliRunner

    from mcp_synology.cli import main

    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.chdir(tmp_path)
    result = CliRunner().invoke(
        main,
        [command],
        env={"SYNOLOGY_HOST": "nas.invalid", "MCP_SYNOLOGY_CONFIG": value},
        input="",
    )
    assert result.exit_code == 1
    assert isinstance(result.exception, SystemExit)
    assert f"Error: {BLANK_VAR}" in result.output.replace("\n", " ")
    assert "Let's create one" not in result.output


def test_a_not_found_path_is_quoted_so_a_stray_character_shows(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # A CRLF env file leaves a \r on the value; printed raw, it overwrote the message.
    monkeypatch.setenv("MCP_SYNOLOGY_CONFIG", str(tmp_path / "c.yaml") + "\r")
    with pytest.raises(FileNotFoundError) as excinfo:
        load_config()
    assert repr(str(tmp_path / "c.yaml") + "\r") in str(excinfo.value)
