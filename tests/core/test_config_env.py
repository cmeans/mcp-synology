"""Environment variables and config discovery (#511 PR 1: #501, #499, #458's env items).

Every ``SYNOLOGY_*`` variable is parsed with its config field's own type (choices in any
case), and a value of the wrong type is an error naming the variable, never a silent
default. A config file named by ``MCP_SYNOLOGY_CONFIG`` must exist.
"""

from __future__ import annotations

import logging
import os
import re
import subprocess
import sys
from pathlib import Path
from typing import Any

import pytest
import yaml

from mcp_synology.core.config import (
    ENV_VAR_MAP,
    EnvVarError,
    _as_compared,
    load_config,
    parse_env_value,
)
from tests.conftest import skip_unless_the_locale_can_encode

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
    "SYNOLOGY_PORT": (
        "SYNOLOGY_PORT must be a whole number from 1 to 65535 (it sets connection.port)."
    ),
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
    return set(
        re.findall(r"^\| `(SYNOLOGY_[A-Z_]+)` \|", doc.read_text(encoding="utf-8"), re.MULTILINE)
    )


@pytest.mark.parametrize("doc", ["README.md", "docs/specs/config-schema-spec.md"])
def test_the_documented_env_table_matches_the_code(doc: str) -> None:
    assert _documented(ROOT / doc) == set(ENV_VAR_MAP)


def test_the_readme_documents_mcp_synology_config() -> None:
    assert "MCP_SYNOLOGY_CONFIG" in (ROOT / "README.md").read_text(encoding="utf-8")


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
    monkeypatch.delenv("MCP_SYNOLOGY_CONFIG", raising=False)
    path = tmp_path / "home" / ".config" / "mcp-synology" / "config.yaml"
    path.parent.mkdir(parents=True)
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


# The text variables whose field has no rule of its own beyond being text.
FREE_TEXT_VARS = TEXT_VARS - {"SYNOLOGY_HOST", "SYNOLOGY_INSTANCE_ID"}


@pytest.mark.parametrize("env_var", sorted(FREE_TEXT_VARS))
def test_text_values_are_taken_as_given(env_var: str) -> None:
    assert parse_env_value(env_var, ENV_VAR_MAP[env_var], " a b ") == " a b "


@pytest.mark.parametrize(
    ("env_var", "message"),
    [
        (
            "SYNOLOGY_HOST",
            "SYNOLOGY_HOST (it sets connection.host) must be a bare host name or IP address, "
            "and it has a space or an invisible character. It isn't shown, since it may hold "
            "a password.",
        ),
        (
            "SYNOLOGY_INSTANCE_ID",
            "SYNOLOGY_INSTANCE_ID may hold only lowercase letters, digits and hyphens; "
            "capitals are lowered (it sets instance_id).",
        ),
    ],
)
def test_padded_text_with_a_rule_is_refused_naming_the_variable(
    env_var: str, message: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Taken as given, as in the file, so padding breaks the field's rule; since #511 PR 2
    the error names the variable, as for the typed ones."""
    monkeypatch.setenv(env_var, " my-id ")
    with pytest.raises(EnvVarError) as excinfo:
        load_config(_config(tmp_path))
    assert str(excinfo.value) == message


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


@pytest.mark.parametrize("via", ["flag", "env"])
def test_a_relative_path_from_a_deleted_folder_is_still_named(
    via: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Pre-QA on #529: absolute() raises in a deleted working folder, and the error
    was a bare "[Errno 2] No such file or directory" naming neither path nor source."""
    gone = tmp_path / "gone"
    gone.mkdir()
    monkeypatch.chdir(gone)
    gone.rmdir()
    if via == "env":
        monkeypatch.setenv("MCP_SYNOLOGY_CONFIG", "rel.yaml")
        expected = "Config file not found (from MCP_SYNOLOGY_CONFIG): 'rel.yaml'. Fix the path."
    else:
        monkeypatch.delenv("MCP_SYNOLOGY_CONFIG", raising=False)
        expected = "Config file not found: 'rel.yaml'"
    with pytest.raises(FileNotFoundError) as excinfo:
        load_config("rel.yaml" if via == "flag" else None)
    assert str(excinfo.value) == expected


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
    home_config = tmp_path / "home" / ".config" / "mcp-synology" / "config.yaml"
    home_config.parent.mkdir(parents=True)
    _config(tmp_path).rename(home_config)
    monkeypatch.setenv("SYNOLOGY_HOST", "nas.example")
    monkeypatch.setenv("MCP_SYNOLOGY_CONFIG", value)
    with pytest.raises(FileNotFoundError) as excinfo:
        load_config()
    assert str(excinfo.value) == BLANK_VAR


@pytest.mark.parametrize("value", ["", "   "])
def test_an_empty_config_flag_is_an_error(
    value: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # It used to fall through to the default location, and to env-only mode.
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    home_config = tmp_path / "home" / ".config" / "mcp-synology" / "config.yaml"
    home_config.parent.mkdir(parents=True)
    _config(tmp_path).rename(home_config)
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


# -- #533: an env override of the file's connection settings is never silent ------------

CONFIG_LOGGER = "mcp_synology.core.config"


class _Lines(logging.Handler):
    """A handler at NOTSET, as basicConfig's: the logger's own level does the filtering."""

    def __init__(self) -> None:
        super().__init__()
        self.records: list[logging.LogRecord] = []

    def emit(self, record: logging.LogRecord) -> None:
        self.records.append(record)

    def messages(self, level: int = logging.WARNING) -> list[str]:
        return [r.getMessage() for r in self.records if r.levelno == level]


@pytest.fixture
def at_error() -> Any:
    """The config logger at ERROR, as `SYNOLOGY_LOG_LEVEL=error` leaves it."""
    log = logging.getLogger(CONFIG_LOGGER)
    handler = _Lines()
    old_level, old_propagate = log.level, log.propagate
    log.addHandler(handler)
    log.setLevel(logging.ERROR)
    log.propagate = False
    yield handler
    log.removeHandler(handler)
    log.setLevel(old_level)
    log.propagate = old_propagate


def _file(tmp_path: Path, *, instance_id: str | None = None, **connection: Any) -> Path:
    path = tmp_path / "config.yaml"
    raw: dict[str, Any] = {
        "schema_version": 1,
        "connection": {"host": "nas.example", **connection},
        "modules": {"filestation": {"enabled": True}},
    }
    if instance_id is not None:
        raw["instance_id"] = instance_id
    path.write_text(yaml.safe_dump(raw))
    return path


def _override(var: str, path: str) -> str:
    return f"{var} overrides {path} from the config file. Unset {var} if that isn't intended."


@pytest.mark.parametrize(
    ("var", "value", "dotted", "file_settings"),
    [
        ("SYNOLOGY_HOST", "other.example", "connection.host", {}),
        ("SYNOLOGY_PORT", "5002", "connection.port", {"port": 5001}),
        ("SYNOLOGY_HTTPS", "yes", "connection.https", {"https": False}),
        ("SYNOLOGY_INSTANCE_ID", "other-nas", "instance_id", {"instance_id": "home-nas"}),
    ],
)
def test_an_override_of_the_files_setting_warns_at_any_level(
    var: str,
    value: str,
    dotted: str,
    file_settings: dict[str, Any],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    at_error: _Lines,
) -> None:
    settings = dict(file_settings)
    instance_id = settings.pop("instance_id", None)
    path = _file(tmp_path, instance_id=instance_id, **settings)
    monkeypatch.setenv(var, value)
    load_config(str(path))
    warnings = at_error.messages()
    assert _override(var, dotted) in warnings
    assert all(value not in w for w in warnings)


_OVERRIDES_DERIVED_ID = (
    "SYNOLOGY_INSTANCE_ID overrides the instance_id the config file's host gives. "
    "Unset SYNOLOGY_INSTANCE_ID if that isn't intended."
)


def test_an_override_of_the_derived_instance_id_says_where_it_comes_from(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, at_error: _Lines
) -> None:
    """No instance_id in the file: it's derived from the host ("nas"), the variable picks
    another keyring entry, and the warning doesn't send anyone looking for the key."""
    monkeypatch.setenv("SYNOLOGY_INSTANCE_ID", "other-nas")
    load_config(str(_file(tmp_path)))
    warnings = at_error.messages()
    assert _OVERRIDES_DERIVED_ID in warnings
    assert all("other-nas" not in w for w in warnings)


@pytest.mark.parametrize(
    ("var", "value", "file_settings"),
    [
        ("SYNOLOGY_HOST", "nas.example", {}),  # the file's own value
        ("SYNOLOGY_HTTPS", "on", {"https": True}),  # the same, in another spelling
        ("SYNOLOGY_PORT", "5002", {}),  # the file doesn't set it
        ("SYNOLOGY_INSTANCE_ID", "nas", {}),  # the ID the file's host gives
        ("SYNOLOGY_LOG_LEVEL", "error", {}),  # not a connection setting
        # The same value in another form (pre-QA on #533): the instance ID is lowered,
        # DNS ignores a host's case, and pydantic reads a text port as the number.
        ("SYNOLOGY_INSTANCE_ID", "home-nas", {"instance_id": "Home-NAS"}),
        ("SYNOLOGY_HOST", "nas.example", {"host": "NAS.example"}),
        ("SYNOLOGY_PORT", "5001", {"port": "5001"}),
        # The spellings the config treats as the same (QA round 1 on #535): the file's
        # value goes through the field's own type, and a host is compared as httpx sends it.
        ("SYNOLOGY_HTTPS", "true", {"https": "on"}),
        ("SYNOLOGY_HTTPS", "true", {"https": "yes"}),
        ("SYNOLOGY_HTTPS", "true", {"https": 1}),
        ("SYNOLOGY_HTTPS", "off", {"https": "no"}),
        ("SYNOLOGY_PORT", "5001", {"port": " 5001 "}),
        ("SYNOLOGY_HOST", "xn--bcher-kva.example", {"host": "b\u00fccher.example"}),
        (
            "SYNOLOGY_HOST",
            "b\u00fccher.example",
            {"host": "xn--bcher-kva.example", "instance_id": "home-nas"},
        ),
        (
            "SYNOLOGY_HOST",
            "B\u00dcCHER.example",
            {"host": "XN--BCHER-KVA.example", "instance_id": "home-nas"},
        ),
        ("SYNOLOGY_HOST", "nas.example", {"host": "nas\uff0eexample"}),  # IDNA's dot
        ("SYNOLOGY_HOST", "[fe80::1]", {"host": "[FE80::1]", "instance_id": "home-nas"}),
        # An IPv4-mapped address has dots (QA round 2 on #535).
        (
            "SYNOLOGY_HOST",
            "[::ffff:1.2.3.4]",
            {"host": "[::FFFF:1.2.3.4]", "instance_id": "home-nas"},
        ),
    ],
)
def test_no_override_warning_when_nothing_the_file_sets_changes(
    var: str,
    value: str,
    file_settings: dict[str, Any],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    at_error: _Lines,
) -> None:
    skip_unless_the_locale_can_encode(value)
    monkeypatch.setenv(var, value)
    load_config(str(_file(tmp_path, **file_settings)))
    assert not [w for w in at_error.messages() if "overrides" in w]


@pytest.mark.parametrize(
    ("var", "value", "dotted", "file_settings"),
    [
        ("SYNOLOGY_HTTPS", "on", "connection.https", {"https": "off"}),
        # A file value its field refuses is compared as written: it can't equal anything.
        ("SYNOLOGY_HTTPS", "on", "connection.https", {"https": "maybe"}),
        ("SYNOLOGY_PORT", "5001", "connection.port", {"port": "nope"}),
        # Reading these as a URL would leave just `nas.example`, hiding the change.
        ("SYNOLOGY_HOST", "nas.example", "connection.host", {"host": "admin@nas.example"}),
        ("SYNOLOGY_HOST", "nas.example", "connection.host", {"host": "nas.example:5001"}),
        ("SYNOLOGY_HOST", "nas.example", "connection.host", {"host": "nas.example/x"}),
        # httpx would bracket a bare `::1`, and a path isn't part of the address (pre-QA r2
        # and r3 on #535).
        ("SYNOLOGY_HOST", "[::1]", "connection.host", {"host": "::1"}),
        ("SYNOLOGY_HOST", "[::1]", "connection.host", {"host": "[::1]/dsm"}),
        # A zone names an interface, so its case stays.
        ("SYNOLOGY_HOST", "[fe80::1%ETH0]", "connection.host", {"host": "[fe80::1%eth0]"}),
        # Look-alikes are other hosts: Cyrillic a, a name IDNA refuses, and another name.
        ("SYNOLOGY_HOST", "n\u0430s.example", "connection.host", {"host": "nas.example"}),
        ("SYNOLOGY_HOST", "nas.example", "connection.host", {"host": "\uff4eas.example"}),
        ("SYNOLOGY_HOST", "b\u00fccher.example", "connection.host", {"host": "bucher.example"}),
    ],
)
def test_a_different_value_still_warns(
    var: str,
    value: str,
    dotted: str,
    file_settings: dict[str, Any],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    at_error: _Lines,
) -> None:
    skip_unless_the_locale_can_encode(value)
    monkeypatch.setenv(var, value)
    # A set instance ID: an odd host derives none the config would accept.
    load_config(str(_file(tmp_path, instance_id="home-nas", **file_settings)))
    assert _override(var, dotted) in at_error.messages()


@pytest.mark.parametrize(
    ("value", "file_host"),
    [
        ("[::1]", "[0:0:0:0:0:0:0:1]"),
        ("[::ffff:102:304]", "[::ffff:1.2.3.4]"),
    ],
)
def test_the_same_destination_written_another_way_warns(
    value: str,
    file_host: str,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    at_error: _Lines,
) -> None:
    """A bracketed address ignores only case, so these warn though they reach the same
    place. A false alarm is the safe side."""
    monkeypatch.setenv("SYNOLOGY_HOST", value)
    load_config(str(_file(tmp_path, instance_id="home-nas", host=file_host)))
    assert _override("SYNOLOGY_HOST", "connection.host") in at_error.messages()


@pytest.mark.parametrize(
    "value",
    [
        "admin@nas.example",
        "ADMIN@nas.example",
        "nas.example/DSM",
        "nas%2Fx",
        "nas.example:5001",
        "::2",  # not `::1`: the advice's own example, `[fd00::1]`, contains that
        "[::1]/DSM",
        "[ADMIN:PW]@nas",
        "Ｎas.example",
        "xn--bcher-kvb.example",
    ],
)
def test_a_variable_that_isnt_bare_stops_before_any_comparison(
    value: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, at_error: _Lines
) -> None:
    """Over any file value, so an override can't reach another user, path or port. The
    error names the variable, never the value (#532)."""
    skip_unless_the_locale_can_encode(value)
    monkeypatch.setenv("SYNOLOGY_HOST", value)
    with pytest.raises(EnvVarError) as exc:
        load_config(str(_file(tmp_path, instance_id="home-nas", host="nas.example")))
    assert str(exc.value).startswith("SYNOLOGY_HOST (it sets connection.host) must be")
    assert value not in str(exc.value)
    assert not [w for w in at_error.messages() if "overrides" in w]


@pytest.mark.parametrize(("value", "warns"), [("other", True), ("nas", False)])
def test_a_binary_host_derives_the_instance_id_the_config_uses(
    value: str,
    warns: bool,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    at_error: _Lines,
) -> None:
    """`host: !!binary bmFz` loads as `nas`, so its derived ID counts as set (QA round 2
    on #535: the variable used to change the keyring entry with no warning)."""
    config = tmp_path / "binary.yaml"
    config.write_text(
        "schema_version: 1\n"
        "connection:\n  host: !!binary bmFz\n"
        "modules:\n  filestation:\n    enabled: true\n",
        encoding="utf-8",
    )
    monkeypatch.setenv("SYNOLOGY_INSTANCE_ID", value)
    assert load_config(str(config)).instance_id == value
    assert (_OVERRIDES_DERIVED_ID in at_error.messages()) is warns


def test_only_the_instance_id_ignores_case_among_text_settings() -> None:
    assert _as_compared("instance_id", "Home-NAS") == "home-nas"
    assert _as_compared("auth.username", "Admin") == "Admin"


def test_the_real_logging_setup_shows_the_warnings_at_error(tmp_path: Path) -> None:
    """#533's repro through the CLI's own logging setup, not a test handler."""
    env = {k: v for k, v in os.environ.items() if not k.startswith(("SYNOLOGY_", "MCP_SYNOLOGY_"))}
    env.update(
        HOME=str(tmp_path),
        SYNOLOGY_LOG_LEVEL="error",
        SYNOLOGY_HTTPS="off",
        PYTHONIOENCODING="utf-8",
    )
    script = (
        "import sys\n"
        "from mcp_synology.cli.logging_ import _init_early_logging\n"
        "from mcp_synology.core.config import load_config\n"
        "_init_early_logging()\n"
        "load_config(sys.argv[1])\n"
    )
    done = subprocess.run(
        [sys.executable, "-c", script, str(_file(tmp_path, https=True))],
        env=env,
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert done.returncode == 0, done.stderr
    assert _override("SYNOLOGY_HTTPS", "connection.https") in done.stderr
    assert "HTTPS is disabled. Credentials will be sent in cleartext." in done.stderr


def test_env_only_mode_has_no_override_warning(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, at_error: _Lines
) -> None:
    # Every value comes from the environment, so there's nothing to override.
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("SYNOLOGY_HOST", "nas.example")
    monkeypatch.setenv("SYNOLOGY_INSTANCE_ID", "other-nas")
    monkeypatch.setenv("SYNOLOGY_HTTPS", "on")
    load_config()
    assert not [w for w in at_error.messages() if "overrides" in w]


def test_https_turned_off_by_a_variable_is_never_silent(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, at_error: _Lines
) -> None:
    """#533's repro: SYNOLOGY_HTTPS=off with SYNOLOGY_LOG_LEVEL=error printed nothing."""
    monkeypatch.setenv("SYNOLOGY_HTTPS", "off")
    load_config(str(_file(tmp_path, https=True)))
    warnings = at_error.messages()
    assert _override("SYNOLOGY_HTTPS", "connection.https") in warnings
    assert any(
        w.startswith("HTTPS is disabled. Credentials will be sent in cleartext.") for w in warnings
    )


@pytest.mark.parametrize(
    ("settings", "start"),
    [
        ({"https": False}, "HTTPS is disabled."),
        ({"https": True, "verify_ssl": False}, "SSL certificate verification is disabled."),
    ],
)
def test_transport_warnings_show_at_any_level(
    settings: dict[str, Any], start: str, tmp_path: Path, at_error: _Lines
) -> None:
    load_config(str(_file(tmp_path, **settings)))
    assert [w for w in at_error.messages() if w.startswith(start)]


@pytest.mark.parametrize("from_env", [False, True])
def test_password_warnings_show_at_any_level(
    from_env: bool, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, at_error: _Lines
) -> None:
    """Pre-QA on #533: the plaintext-password warnings were still hidden at ERROR."""
    path = tmp_path / "config.yaml"
    raw: dict[str, Any] = {
        "schema_version": 1,
        "connection": {"host": "nas.example", "https": True},
        "auth": {"username": "u"},
        "modules": {"filestation": {"enabled": True}},
    }
    if from_env:
        monkeypatch.setenv("SYNOLOGY_PASSWORD", "Hunter2x")
        start = "Password provided via the SYNOLOGY_PASSWORD environment variable."
    else:
        raw["auth"]["password"] = "Hunter2x"
        start = "Plaintext password found in config file."
    path.write_text(yaml.safe_dump(raw))
    load_config(str(path))
    assert [w for w in at_error.messages() if w.startswith(start)]


def test_an_override_warns_once_at_the_default_level(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    monkeypatch.setenv("SYNOLOGY_HOST", "other.example")
    with caplog.at_level(logging.INFO, logger=CONFIG_LOGGER):
        load_config(str(_file(tmp_path)))
    records = [r for r in caplog.records if "overrides" in r.getMessage()]
    assert len(records) == 1
    assert records[0].levelno == logging.WARNING
