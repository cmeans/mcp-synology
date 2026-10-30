"""Tests for core/config.py — config loading, validation, env var merging."""

from __future__ import annotations

import logging
import os
from typing import TYPE_CHECKING, Any
from unittest.mock import patch

import pytest
import yaml
from pydantic import ValidationError

from mcp_synology.core.config import (
    AppConfig,
    ConnectionConfig,
    LoggingConfig,
    ModuleConfig,
    _derive_instance_id,
    _merge_env_overrides,
    discover_config_path,
    load_config,
)

if TYPE_CHECKING:
    from pathlib import Path


def _minimal_raw() -> dict[str, Any]:
    return {
        "schema_version": 1,
        "connection": {"host": "192.168.1.100"},
        "modules": {"filestation": {"enabled": True}},
    }


class TestAppConfig:
    def test_minimal_config(self) -> None:
        config = AppConfig(**_minimal_raw())
        assert config.connection is not None
        assert config.connection.host == "192.168.1.100"
        assert config.connection.port == 5000  # default for http
        assert config.instance_id == "192-168-1-100"

    def test_full_config(self) -> None:
        raw: dict[str, Any] = {
            "schema_version": 1,
            "instance_id": "nas-primary",
            "connection": {
                "host": "nas.local",
                "port": 5001,
                "https": True,
                "verify_ssl": False,
                "timeout": 60,
            },
            "auth": {"username": "admin"},
            "modules": {
                "filestation": {
                    "enabled": True,
                    "permission": "write",
                    "settings": {"async_timeout": 180},
                },
            },
            "logging": {"level": "debug", "file": "/tmp/test.log"},
        }
        config = AppConfig(**raw)
        assert config.instance_id == "nas-primary"
        assert config.connection is not None
        assert config.connection.https is True
        assert config.modules["filestation"].permission == "write"

    def test_https_default_port(self) -> None:
        raw = _minimal_raw()
        raw["connection"]["https"] = True
        config = AppConfig(**raw)
        assert config.connection is not None
        assert config.connection.port == 5001

    def test_instance_id_from_ip(self) -> None:
        config = AppConfig(**_minimal_raw())
        assert config.instance_id == "192-168-1-100"

    def test_instance_id_from_hostname(self) -> None:
        raw = _minimal_raw()
        raw["connection"]["host"] = "nas.local"
        config = AppConfig(**raw)
        assert config.instance_id == "nas"

    def test_wrong_schema_version(self) -> None:
        raw = _minimal_raw()
        raw["schema_version"] = 99
        with pytest.raises(ValueError, match="schema_version"):
            AppConfig(**raw)

    def test_missing_modules(self) -> None:
        raw = _minimal_raw()
        raw["modules"] = {}
        with pytest.raises(ValueError, match="module"):
            AppConfig(**raw)

    def test_missing_connection_host(self) -> None:
        raw: dict[str, Any] = {
            "schema_version": 1,
            "modules": {"filestation": {"enabled": True}},
        }
        with pytest.raises(ValueError, match="host"):
            AppConfig(**raw)

    def test_unknown_top_level_key_rejected(self) -> None:
        raw = _minimal_raw()
        raw["typo_key"] = "oops"
        with pytest.raises(ValueError):
            AppConfig(**raw)

    def test_unknown_module_key_rejected(self) -> None:
        """A typo directly under a module (only enabled/permission/settings exist) is an
        error: `permision: write` used to be dropped, leaving the module read-only."""
        from pydantic import ValidationError

        from mcp_synology.core.config import format_validation_error

        raw = _minimal_raw()
        raw["modules"] = {"filestation": {"enabled": True, "permision": "write"}}
        with pytest.raises(ValidationError) as exc:
            AppConfig(**raw)
        assert "modules.filestation.permision" in format_validation_error(exc.value)

    @pytest.mark.parametrize(
        ("section", "key", "value"),
        [
            ("connection", "htps", True),
            ("connection", "verfy_ssl", False),
            ("auth", "pasword", "secret"),
            ("logging", "levle", "debug"),
        ],
    )
    def test_unknown_key_in_a_section_rejected(self, section: str, key: str, value: Any) -> None:
        """#386: a typo inside connection, auth or logging is an error naming the key. It
        was dropped: `htps: true` left `https` false, so the login went over plain HTTP."""
        from pydantic import ValidationError

        from mcp_synology.core.config import format_validation_error

        raw = _minimal_raw()
        raw.setdefault(section, {})[key] = value
        with pytest.raises(ValidationError) as exc:
            AppConfig(**raw)
        message = format_validation_error(exc.value)
        # An unknown key's value could be a misplaced credential: never echoed.
        assert f"  - {section}.{key}: Extra inputs are not permitted\n" in message + "\n"
        assert "(got " not in message

    @pytest.mark.parametrize(
        ("raw_update", "secret"),
        [
            ({"auth": {"password": 12345678}}, "12345678"),
            ({"auth": {"passwd": "hunter2"}}, "hunter2"),
            ({"password": "hunter2"}, "hunter2"),
            ({"connection": {"host": "nas", "api_token": "tok-1"}}, "tok-1"),
            ({"connection": {"host": "nas", "pasword": "hunter2"}}, "hunter2"),
            ({"pasword": "hunter2"}, "hunter2"),
            # Root and missing-field errors carry the whole mapping as their input.
            ({"instance_id": "my_nas", "auth": {"password": "hunter2"}}, "hunter2"),
            ({"schema_version": 9, "auth": {"password": "hunter2"}}, "hunter2"),
            ({"connection": {"passwd": "hunter2"}}, "hunter2"),
            ({"modules": {}, "auth": {"device_id": "dev-tok"}}, "dev-tok"),
            # A section given a plain value: a "should be a dictionary" error.
            ({"connection": "https://admin:hunter2@nas"}, "hunter2"),
            ({"logging": "hunter2"}, "hunter2"),
            ({"modules": {"filestation": "hunter2"}}, "hunter2"),
            ({"modules": {"filestation": {"settings": "hunter2"}}}, "hunter2"),
            # An unknown key with a value under auth: the key may be the password.
            ({"auth": {"hunter2": "y"}}, "hunter2"),
        ],
    )
    def test_validation_error_never_echoes_a_credential(
        self, raw_update: dict[str, Any], secret: str
    ) -> None:
        from pydantic import ValidationError

        from mcp_synology.core.config import format_validation_error

        raw = {**_minimal_raw(), **raw_update}
        with pytest.raises(ValidationError) as exc:
            AppConfig(**raw)
        assert secret not in format_validation_error(exc.value)

    def test_env_password_never_echoed(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Any
    ) -> None:
        """SYNOLOGY_PASSWORD is merged into the config before validation; a root error
        (here, no module enabled) used to print the whole mapping with it."""
        from pydantic import ValidationError

        from mcp_synology.core.config import format_validation_error, load_config

        path = tmp_path / "c.yaml"
        path.write_text(
            "schema_version: 1\nconnection: {host: nas}\nmodules: {filestation: {enabled: false}}\n"
        )
        monkeypatch.setenv("SYNOLOGY_PASSWORD", "env-secret")
        monkeypatch.setenv("SYNOLOGY_DEVICE_ID", "dev-tok")
        with pytest.raises(ValidationError) as exc:
            load_config(path)
        message = format_validation_error(exc.value)
        assert "env-secret" not in message and "dev-tok" not in message
        assert "<root>" in message

    @pytest.mark.parametrize(
        "password",
        ["*Hunter2x", "!Hunter2x", "@Hunter2x", "`Hunter2x", '"Hunter2x', "Hunt: er2x"],
    )
    def test_yaml_error_never_quotes_the_line(self, tmp_path: Any, password: str) -> None:
        """PyYAML quotes the offending line, and its problem text can quote the value."""
        import yaml

        from mcp_synology.core.config import format_yaml_error, load_config

        path = tmp_path / "c.yaml"
        path.write_text(
            "schema_version: 1\nconnection: {host: nas}\n"
            f"auth:\n  username: me\n  password: {password}\n"
            "modules: {filestation: {enabled: true}}\n"
        )
        with pytest.raises(yaml.YAMLError) as exc:
            load_config(path)
        assert "Hunt" in str(exc.value)  # PyYAML's own text would show it
        message = format_yaml_error(exc.value)
        assert "Hunt" not in message
        assert message.startswith("The config file isn't valid YAML at line ")

    def test_yaml_error_names_the_line_and_key(self, tmp_path: Any) -> None:
        import yaml

        from mcp_synology.core.config import format_yaml_error, load_config

        path = tmp_path / "c.yaml"
        path.write_text("schema_version: 1\nconnection:\n  host: nas\n  port: *x\n")
        with pytest.raises(yaml.YAMLError) as exc:
            load_config(path)
        assert format_yaml_error(exc.value).startswith(
            "The config file isn't valid YAML at line 4, column 9, on the `port` line. "
        )

    @pytest.mark.parametrize(
        "auth",
        [
            "auth:\n  username: me\n  password:\n    Hunter2x: @x\n",
            "auth:\n  username: me\n  password: abc\n   Hunter2x: ghi\n",
        ],
    )
    def test_a_password_line_is_never_taken_for_a_key(self, tmp_path: Any, auth: str) -> None:
        """Only the config's own keys are named: a password continued on its own line
        (`Hunter2x: …`) is text, not a key."""
        import yaml

        from mcp_synology.core.config import format_yaml_error, load_config

        path = tmp_path / "c.yaml"
        path.write_text("schema_version: 1\nconnection: {host: nas}\n" + auth)
        with pytest.raises(yaml.YAMLError) as exc:
            load_config(path)
        assert "Hunter" not in format_yaml_error(exc.value)

    @pytest.mark.parametrize(
        "value", ["!!int Hunter2x", "!!float Hunter2x", "!!timestamp Hunter2x", "!!bool Hunter2x"]
    )
    def test_a_tag_that_does_not_fit_is_never_quoted(self, tmp_path: Any, value: str) -> None:
        """PyYAML raises ValueError or AttributeError there, with the value in it."""
        import yaml

        from mcp_synology.core.config import ConfigValueError, format_yaml_error, load_config

        path = tmp_path / "c.yaml"
        path.write_text(
            "schema_version: 1\nconnection: {host: nas}\n"
            f"auth:\n  username: me\n  password: {value}\n"
        )
        with pytest.raises(ConfigValueError) as exc:
            load_config(path)
        assert isinstance(exc.value, yaml.YAMLError)
        assert exc.value.__cause__ is None and exc.value.__suppress_context__
        message = format_yaml_error(exc.value)
        assert "hunter" not in message.casefold()
        assert message.startswith("The config file has a value YAML can't build")

    @pytest.mark.parametrize("value", ['"Hunter2x', "[Hunter2x", "{Hunter2x"])
    def test_an_unclosed_value_names_where_it_starts(self, tmp_path: Any, value: str) -> None:
        import yaml

        from mcp_synology.core.config import format_yaml_error, load_config

        path = tmp_path / "c.yaml"
        path.write_text(
            "schema_version: 1\nconnection: {host: nas}\n"
            f"auth:\n  username: me\n  password: {value}\n"
        )
        with pytest.raises(yaml.YAMLError) as exc:
            load_config(path)
        message = format_yaml_error(exc.value)
        assert "(in the value that starts at line 5), on the `password` line. " in message
        assert "Hunter" not in message

    @pytest.mark.parametrize(
        "auth",
        [
            "{username: me, password: a,Hunter2x}",
            "{username: me, password: x,y,Hunter2x}",
            "{username: me, password: Hunter,2x}",
        ],
    )
    def test_a_password_split_by_a_comma_is_not_shown(self, auth: str) -> None:
        """YAML splits `password: a,Hunter2x` in a flow mapping at the comma; the second
        half arrives as an unknown key under auth, and isn't named."""
        import yaml
        from pydantic import ValidationError

        from mcp_synology.core.config import format_validation_error

        raw = yaml.safe_load(
            "schema_version: 1\nconnection: {host: nas}\n"
            f"auth: {auth}\nmodules: {{filestation: {{enabled: true}}}}\n"
        )
        with pytest.raises(ValidationError) as exc:
            AppConfig(**raw)
        message = format_validation_error(exc.value)
        assert "2x" not in message and "Hunter" not in message and "auth.y" not in message
        assert (
            "auth.<an unknown key, not shown since it may hold or be part of a password" in message
        )

    @pytest.mark.parametrize(
        "body",
        [
            "connection: {host: nas, password: a,Hunter2x}\nmodules: {filestation: {}}\n",
            "connection: {host: nas}\nmodules: {filestation: {}}\nlogging: {level: info}\n"
            "auth: {username: a}\nextra: {password: a,Hunter2x}\n",
            "connection: {host: nas}\nmodules: {filestation: {}}\nlogging: {file: a,Hunter2x}\n",
            "connection: {host: nas}\nmodules: {filestation: {enabled: true, x: a,Hunter2x}}\n",
        ],
        ids=["connection", "top-level", "logging", "module"],
    )
    def test_a_comma_split_value_is_not_named_in_any_section(self, body: str) -> None:
        import yaml
        from pydantic import ValidationError

        from mcp_synology.core.config import format_validation_error

        raw = yaml.safe_load("schema_version: 1\n" + body)
        if "extra" in raw:  # a whole flow-style document: `{…, password: a,Hunter2x}`
            raw.update(raw.pop("extra"))
        with pytest.raises(ValidationError) as exc:
            AppConfig(**raw)
        message = format_validation_error(exc.value)
        assert "Hunter" not in message
        assert "<an unknown key, not shown since it may hold or be part of a password" in message

    def test_a_valueless_near_miss_under_a_module_is_named(self) -> None:
        from pydantic import ValidationError

        from mcp_synology.core.config import format_validation_error

        raw = _minimal_raw()
        raw["modules"] = {"filestation": {"permision": None}}
        with pytest.raises(ValidationError) as exc:
            AppConfig(**raw)
        assert "  - modules.filestation.permision: Extra inputs are not permitted" in (
            format_validation_error(exc.value)
        )

    def test_a_near_miss_in_another_case_is_named(self) -> None:
        """`USERNAME` under auth is a near-miss of `username` in any case: named."""
        from pydantic import ValidationError

        from mcp_synology.core.config import format_validation_error

        raw = _minimal_raw()
        raw["auth"] = {"USERNAME": "me"}
        with pytest.raises(ValidationError) as exc:
            AppConfig(**raw)
        assert "  - auth.USERNAME: Extra inputs are not permitted" in (
            format_validation_error(exc.value)
        )

    def test_a_non_text_key_in_module_settings_is_not_shown(self) -> None:
        """`{x: Hunter,2024}` in settings splits into a key 2024, which pydantic reports as
        `settings.2024.[key]`: the mapping is named, never the key."""
        import yaml
        from pydantic import ValidationError

        from mcp_synology.core.config import format_validation_error

        raw = yaml.safe_load(
            "schema_version: 1\nconnection: {host: nas}\n"
            "modules: {filestation: {settings: {x: Hunter,2024}}}\n"
        )
        with pytest.raises(ValidationError) as exc:
            AppConfig(**raw)
        message = format_validation_error(exc.value)
        assert "2024" not in message and "Hunter" not in message
        assert "modules.filestation.settings.<a key that isn't text, not shown" in message

    @pytest.mark.parametrize(
        "line",
        ["\ufeffschema_version: *x\n", '"schema_version": *x\n', "'schema_version': *x\n"],
        ids=["bom", "double-quoted", "single-quoted"],
    )
    def test_yaml_error_names_a_bom_or_quoted_key(self, tmp_path: Any, line: str) -> None:
        import yaml

        from mcp_synology.core.config import format_yaml_error, load_config

        path = tmp_path / "c.yaml"
        path.write_text(line + "connection: {host: nas}\n", encoding="utf-8")
        with pytest.raises(yaml.YAMLError) as exc:
            load_config(path)
        assert "on the `schema_version` line. " in format_yaml_error(exc.value)

    def test_an_unknown_key_with_a_value_is_still_named(self) -> None:
        from pydantic import ValidationError

        from mcp_synology.core.config import format_validation_error

        raw = _minimal_raw()
        raw["totally_unknown"] = "x"
        raw["connection"]["proxy"] = "http://p"
        with pytest.raises(ValidationError) as exc:
            AppConfig(**raw)
        message = format_validation_error(exc.value)
        assert "  - totally_unknown: Extra inputs are not permitted" in message
        assert "  - connection.proxy: Extra inputs are not permitted" in message

    @pytest.mark.parametrize("fragment", ["20245678", "3.14159", "2024-01-02"])
    @pytest.mark.parametrize("section", ["auth", "connection", "logging"])
    def test_a_fragment_that_isnt_text_is_not_named(self, fragment: str, section: str) -> None:
        """QA round 1: YAML reads `{password: Summer,2024}`'s second half as a number or a
        date key, which pydantic reports as `invalid_key`, not as an unknown key."""
        import yaml
        from pydantic import ValidationError

        from mcp_synology.core.config import format_validation_error

        body = {
            "auth": "auth: {username: u, password: Summer,%s}\nconnection: {host: nas}\n",
            "connection": "connection: {host: nas, password: Summer,%s}\n",
            "logging": "connection: {host: nas}\nlogging: {file: Summer,%s}\n",
        }[section] % fragment
        raw = yaml.safe_load("schema_version: 1\nmodules: {filestation: {}}\n" + body)
        with pytest.raises(ValidationError) as exc:
            AppConfig(**raw)
        message = format_validation_error(exc.value)
        assert fragment not in message and "2024" not in message and "3.14" not in message
        assert "<a key that isn't text, not shown" in message

    def test_a_misspelled_auth_key_is_still_named(self) -> None:
        from pydantic import ValidationError

        from mcp_synology.core.config import format_validation_error

        raw = _minimal_raw()
        raw["auth"] = {"usrname": "me", "pasword": "x", "device_ids": "y"}
        with pytest.raises(ValidationError) as exc:
            AppConfig(**raw)
        message = format_validation_error(exc.value)
        for key in ("usrname", "pasword", "device_ids"):
            assert f"  - auth.{key}: Extra inputs are not permitted" in message

    def test_a_block_context_does_not_point_to_line_1(self, tmp_path: Any) -> None:
        # A misindented key: PyYAML's context is the whole document's block mapping.
        import yaml

        from mcp_synology.core.config import format_yaml_error, load_config

        path = tmp_path / "c.yaml"
        path.write_text(
            "schema_version: 1\nconnection: {host: nas}\nauth:\n  username: me\n"
            " password: Hunter2x\n"
        )
        with pytest.raises(yaml.YAMLError) as exc:
            load_config(path)
        message = format_yaml_error(exc.value)
        assert "starts at line 1" not in message and "Hunter" not in message
        assert message.startswith("The config file isn't valid YAML at line 5")

    def test_a_refused_character_says_where(self, tmp_path: Any) -> None:
        import yaml

        from mcp_synology.core.config import format_yaml_error, load_config

        path = tmp_path / "c.yaml"
        path.write_text("schema_version: 1\nauth:\n  password: Hun\x07ter2x\n")
        with pytest.raises(yaml.YAMLError) as exc:
            load_config(path)
        message = format_yaml_error(exc.value)
        assert "ter2x" not in message
        assert "(character 40 of the file)" in message

    def test_a_derived_instance_id_is_not_quoted(self) -> None:
        """A host written as a URL can carry a password; the instance_id derived from it
        isn't quoted."""
        raw: dict[str, Any] = {
            "schema_version": 1,
            "connection": {"host": "https://admin:Hunter2x@nas"},
            "modules": {"filestation": {"enabled": True}},
        }
        with pytest.raises(ValueError) as exc:
            AppConfig(**raw)
        assert "hunter" not in str(exc.value).casefold()
        assert "The instance_id derived from connection.host contains invalid" in str(exc.value)

    def test_yaml_error_without_a_location(self) -> None:
        import yaml

        from mcp_synology.core.config import format_yaml_error

        assert format_yaml_error(yaml.YAMLError("Hunter2x")).startswith(
            "The config file isn't valid YAML. The text there isn't shown"
        )

    def test_yaml_key_lookup_without_text_or_past_the_end(self) -> None:
        from types import SimpleNamespace

        from mcp_synology.core.config import _key_on_line

        assert _key_on_line(SimpleNamespace(buffer=None, line=0)) is None
        assert _key_on_line(SimpleNamespace(buffer="a: 1\n", line=5)) is None

    def test_too_deep_a_config(self, tmp_path: Any) -> None:
        import yaml

        from mcp_synology.core.config import format_yaml_error, load_config

        path = tmp_path / "c.yaml"
        path.write_text("a: " + "[" * 20000 + "]" * 20000 + "\n")
        with pytest.raises(yaml.YAMLError) as exc:
            load_config(path)
        assert format_yaml_error(exc.value).startswith(
            "The config file is nested too deeply for YAML to read."
        )

    def test_a_set_is_not_shown(self) -> None:
        from pydantic import ValidationError

        from mcp_synology.core.config import format_validation_error

        raw = _minimal_raw()
        raw["connection"] = {"hunter2"}
        with pytest.raises(ValidationError) as exc:
            AppConfig(**raw)
        assert "hunter2" not in format_validation_error(exc.value)

    def test_a_plain_value_is_still_shown(self) -> None:
        from pydantic import ValidationError

        from mcp_synology.core.config import format_validation_error

        raw = _minimal_raw()
        raw["connection"]["timeout"] = 9999
        with pytest.raises(ValidationError) as exc:
            AppConfig(**raw)
        assert "connection.timeout:" in format_validation_error(exc.value)
        assert "(got 9999)" in format_validation_error(exc.value)

    def test_invalid_instance_id_characters(self) -> None:
        raw = _minimal_raw()
        raw["instance_id"] = "bad_chars!"
        with pytest.raises(ValueError, match="invalid characters"):
            AppConfig(**raw)

    def test_invalid_permission_value(self) -> None:
        raw = _minimal_raw()
        raw["modules"]["filestation"]["permission"] = "superadmin"
        with pytest.raises(ValueError):
            AppConfig(**raw)

    def test_module_defaults(self) -> None:
        mod = ModuleConfig()
        assert mod.enabled is True
        assert mod.permission == "read"
        assert mod.settings == {}

    def test_connection_defaults(self) -> None:
        conn = ConnectionConfig(host="test")
        assert conn.https is False
        assert conn.verify_ssl is True
        assert conn.timeout == 30

    def test_logging_defaults(self) -> None:
        log = LoggingConfig()
        assert log.level == "info"
        assert log.file is None


class TestEnabledModules:
    """#168: a config whose modules are all ``enabled: false`` would start with 0 tools."""

    def test_empty_modules_rejected(self) -> None:
        raw = _minimal_raw()
        raw["modules"] = {}
        with pytest.raises(ValueError, match="At least one module must be configured"):
            AppConfig(**raw)

    def test_every_module_disabled_rejected(self) -> None:
        raw = _minimal_raw()
        raw["modules"] = {"filestation": {"enabled": False}, "system": {"enabled": False}}
        with pytest.raises(ValueError, match="No module is enabled") as exc:
            AppConfig(**raw)
        assert "filestation, system" in str(exc.value)
        assert "filestation: {enabled: true}" in str(exc.value)

    def test_one_enabled_among_disabled_accepted(self) -> None:
        raw = _minimal_raw()
        raw["modules"] = {"filestation": {"enabled": False}, "system": {"enabled": True}}
        config = AppConfig(**raw)
        assert config.modules["system"].enabled


class TestCredentialWarnings:
    """#167: only a password is a secret; a username alone gets no plaintext warning."""

    @staticmethod
    def _load(tmp_path: Path, auth: dict[str, str], env: dict[str, str] | None = None) -> None:
        raw = _minimal_raw()
        raw["auth"] = auth
        path = tmp_path / "config.yaml"
        path.write_text(yaml.safe_dump(raw))
        clean = {k: v for k, v in os.environ.items() if not k.startswith("SYNOLOGY_")}
        with patch.dict(os.environ, {**clean, **(env or {})}, clear=True):
            load_config(str(path))

    def test_username_only_no_warning(
        self, tmp_path: Path, caplog: pytest.LogCaptureFixture
    ) -> None:
        caplog.set_level(logging.WARNING, logger="mcp_synology.core.config")
        self._load(tmp_path, {"username": "nobody"})
        assert "Plaintext" not in caplog.text
        assert "environment variable" not in caplog.text

    def test_plain_http_is_a_warning(
        self, tmp_path: Path, caplog: pytest.LogCaptureFixture
    ) -> None:
        """#193: credentials in cleartext is a WARNING (it was INFO)."""
        raw = _minimal_raw()
        raw["connection"] = {"host": "192.168.1.100", "https": False}
        path = tmp_path / "config.yaml"
        path.write_text(yaml.safe_dump(raw))
        caplog.set_level(logging.WARNING, logger="mcp_synology.core.config")
        load_config(str(path))
        record = next(r for r in caplog.records if "HTTPS is disabled" in r.getMessage())
        assert record.levelno == logging.WARNING
        assert "docs/https.md" in record.getMessage()

    def test_https_config_has_no_plain_http_warning(
        self, tmp_path: Path, caplog: pytest.LogCaptureFixture
    ) -> None:
        raw = _minimal_raw()
        raw["connection"] = {"host": "192.168.1.100", "https": True}
        path = tmp_path / "config.yaml"
        path.write_text(yaml.safe_dump(raw))
        caplog.set_level(logging.DEBUG, logger="mcp_synology.core.config")
        load_config(str(path))
        assert "HTTPS is disabled" not in caplog.text

    def test_password_in_file_warns(self, tmp_path: Path, caplog: pytest.LogCaptureFixture) -> None:
        caplog.set_level(logging.WARNING, logger="mcp_synology.core.config")
        self._load(tmp_path, {"username": "nobody", "password": "s3cret"})
        assert "Plaintext password found in config file" in caplog.text

    def test_password_from_env_gets_env_wording(
        self, tmp_path: Path, caplog: pytest.LogCaptureFixture
    ) -> None:
        caplog.set_level(logging.WARNING, logger="mcp_synology.core.config")
        self._load(tmp_path, {"username": "nobody"}, {"SYNOLOGY_PASSWORD": "s3cret"})
        assert "SYNOLOGY_PASSWORD environment variable" in caplog.text
        assert "Plaintext" not in caplog.text

    def test_username_from_env_only_no_warning(
        self, tmp_path: Path, caplog: pytest.LogCaptureFixture
    ) -> None:
        caplog.set_level(logging.WARNING, logger="mcp_synology.core.config")
        self._load(tmp_path, {}, {"SYNOLOGY_USERNAME": "nobody"})
        assert "Plaintext" not in caplog.text
        assert "environment variable" not in caplog.text


class TestDeriveInstanceId:
    def test_ip_address(self) -> None:
        assert _derive_instance_id("192.168.1.100") == "192-168-1-100"

    def test_hostname(self) -> None:
        assert _derive_instance_id("nas.local") == "nas"

    def test_simple_hostname(self) -> None:
        assert _derive_instance_id("mynas") == "mynas"


class TestEnvOverrides:
    def test_host_override(self) -> None:
        raw: dict[str, Any] = {}
        with patch.dict(os.environ, {"SYNOLOGY_HOST": "10.0.0.1"}):
            result = _merge_env_overrides(raw)
        assert result["connection"]["host"] == "10.0.0.1"

    def test_port_coerced_to_int(self) -> None:
        raw: dict[str, Any] = {"connection": {}}
        with patch.dict(os.environ, {"SYNOLOGY_PORT": "5001"}):
            result = _merge_env_overrides(raw)
        assert result["connection"]["port"] == 5001

    def test_https_coerced_to_bool(self) -> None:
        raw: dict[str, Any] = {"connection": {}}
        with patch.dict(os.environ, {"SYNOLOGY_HTTPS": "true"}):
            result = _merge_env_overrides(raw)
        assert result["connection"]["https"] is True

    def test_update_notices_override(self) -> None:
        raw: dict[str, Any] = {}
        with patch.dict(os.environ, {"SYNOLOGY_UPDATE_NOTICES": "security"}):
            result = _merge_env_overrides(raw)
        assert result["update_notices"] == "security"

    def test_env_does_not_override_when_not_set(self) -> None:
        raw: dict[str, Any] = {"connection": {"host": "original"}}
        env = {k: v for k, v in os.environ.items() if not k.startswith("SYNOLOGY_")}
        with patch.dict(os.environ, env, clear=True):
            result = _merge_env_overrides(raw)
        assert result["connection"]["host"] == "original"


class TestDiscoverConfigPath:
    def test_explicit_path(self, tmp_path: Path) -> None:
        config_file = tmp_path / "config.yaml"
        config_file.write_text("schema_version: 1\n")
        result = discover_config_path(str(config_file))
        assert result == config_file

    def test_explicit_path_not_found(self) -> None:
        with pytest.raises(FileNotFoundError, match="not found"):
            discover_config_path("/nonexistent/config.yaml")

    def test_env_var_path(self, tmp_path: Path) -> None:
        config_file = tmp_path / "config.yaml"
        config_file.write_text("schema_version: 1\n")
        with patch.dict(os.environ, {"MCP_SYNOLOGY_CONFIG": str(config_file)}):
            result = discover_config_path()
        assert result == config_file


class TestLoadConfig:
    def test_load_minimal(self, tmp_path: Path) -> None:
        config_file = tmp_path / "config.yaml"
        config_file.write_text(
            "schema_version: 1\n"
            "connection:\n"
            "  host: 192.168.1.100\n"
            "modules:\n"
            "  filestation:\n"
            "    enabled: true\n"
        )
        config = load_config(config_file)
        assert config.connection is not None
        assert config.connection.host == "192.168.1.100"
        assert "filestation" in config.modules


class TestTopLevelAllowedLocalPaths:
    """#257: one allowed_local_paths for every module, at the top level."""

    def test_accepts_home_relative_entries(self) -> None:
        config = AppConfig(**{**_minimal_raw(), "allowed_local_paths": ["~/x", "/srv/in"]})
        assert config.allowed_local_paths == ["~/x", "/srv/in"]

    def test_unset_by_default(self) -> None:
        assert AppConfig(**_minimal_raw()).allowed_local_paths is None

    @pytest.mark.parametrize("value", [["relative/dir"], []])
    def test_rejects_relative_or_empty(self, value: list[str]) -> None:
        with pytest.raises(ValidationError, match="allowed_local_paths"):
            AppConfig(**{**_minimal_raw(), "allowed_local_paths": value})

    def test_unknown_top_level_key_still_rejected(self) -> None:
        with pytest.raises(ValidationError):
            AppConfig(**{**_minimal_raw(), "allowed_local_path": ["/x"]})


class TestUpdateNotices:
    """Per config, since configs may run different installs (#314)."""

    def test_defaults_to_all(self) -> None:
        assert AppConfig(**_minimal_raw()).update_notices == "all"

    @pytest.mark.parametrize("value", ["all", "security", "off"])
    def test_accepts_the_three_settings(self, value: str) -> None:
        assert AppConfig(**{**_minimal_raw(), "update_notices": value}).update_notices == value

    def test_rejects_anything_else(self) -> None:
        with pytest.raises(ValidationError, match="update_notices"):
            AppConfig(**{**_minimal_raw(), "update_notices": "weekly"})

    @pytest.mark.parametrize(("written", "expected"), [("off", "off"), ("on", "all")])
    def test_unquoted_yaml_booleans(self, tmp_path: Path, written: str, expected: str) -> None:
        # YAML 1.1 reads an unquoted `off` as false; the README and the notice write it
        # unquoted (QA round 1 on #433).
        config_file = tmp_path / "config.yaml"
        config_file.write_text(
            "schema_version: 1\n"
            f"update_notices: {written}\n"
            "connection:\n"
            "  host: 192.168.1.100\n"
            "modules:\n"
            "  filestation:\n"
            "    enabled: true\n"
        )
        assert load_config(config_file).update_notices == expected
