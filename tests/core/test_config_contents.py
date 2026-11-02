"""What a config file may contain (#511 PR 2): duplicate keys, keys with no value,
empty sections, the host and port, empty IDs and file paths, and the same rules for
the environment variables that set those fields."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

import pytest
import yaml
from pydantic import ValidationError

from mcp_synology.core.config import (
    AppConfig,
    EnvVarError,
    format_validation_error,
    format_yaml_error,
    load_config,
    parse_config_text,
)

if TYPE_CHECKING:
    from pathlib import Path

_MODULES = "modules:\n  filestation:\n    enabled: true\n"


def _write(tmp_path: Path, text: str) -> Path:
    path = tmp_path / "config.yaml"
    path.write_text(text, encoding="utf-8")
    return path


def _yaml_message(tmp_path: Path, text: str) -> str:
    with pytest.raises(yaml.YAMLError) as exc:
        load_config(_write(tmp_path, text))
    return format_yaml_error(exc.value)


def _validation_message(tmp_path: Path, text: str) -> str:
    with pytest.raises(ValidationError) as exc:
        load_config(_write(tmp_path, text))
    return format_validation_error(exc.value)


@pytest.fixture(autouse=True)
def _no_env(monkeypatch: pytest.MonkeyPatch) -> None:
    for name in (
        "SYNOLOGY_HOST",
        "SYNOLOGY_PORT",
        "SYNOLOGY_HTTPS",
        "SYNOLOGY_USERNAME",
        "SYNOLOGY_PASSWORD",
        "SYNOLOGY_DEVICE_ID",
        "SYNOLOGY_INSTANCE_ID",
        "SYNOLOGY_LOG_LEVEL",
        "SYNOLOGY_UPDATE_NOTICES",
        "MCP_SYNOLOGY_CONFIG",
    ):
        monkeypatch.delenv(name, raising=False)


class TestDuplicateKeys:
    """#464: YAML kept the last of two equal keys without a word."""

    def test_a_repeated_https_is_refused_naming_the_key_and_both_lines(
        self, tmp_path: Path
    ) -> None:
        message = _yaml_message(
            tmp_path,
            "schema_version: 1\nconnection:\n  host: nas\n  https: true\n  https: false\n"
            + _MODULES,
        )
        assert message.startswith("The config file has `https` twice in one section")
        assert "(lines 4 and 5)" in message

    def test_a_repeated_password_is_refused_without_its_values(self, tmp_path: Path) -> None:
        message = _yaml_message(
            tmp_path,
            "schema_version: 1\nconnection: {host: nas}\nauth:\n  password: Hunter2x\n"
            "  password: Spring2y\n" + _MODULES,
        )
        assert "`password` twice" in message
        assert "Hunter2x" not in message
        assert "Spring2y" not in message

    def test_a_repeated_section_whose_last_copy_is_empty(self, tmp_path: Path) -> None:
        """#464 comment: the empty last `connection:` used to drop `https: true`."""
        message = _yaml_message(
            tmp_path,
            "schema_version: 1\nconnection: {host: nas, https: true}\n"
            + _MODULES
            + "connection:\n",
        )
        assert "`connection` twice" in message
        assert "(lines 2 and 6)" in message

    def test_a_key_the_config_doesnt_define_is_not_named(self, tmp_path: Path) -> None:
        """A key that isn't the config's own may be a password's fragment."""
        message = _yaml_message(
            tmp_path,
            "schema_version: 1\nconnection: {host: nas}\nmodules:\n  filestation:\n"
            "    settings:\n      Hunter2x: 1\n      Hunter2x: 2\n",
        )
        assert "Hunter2x" not in message
        assert message.startswith("The config file has a key twice in one section (lines 6 and 7)")

    def test_a_comma_split_that_repeats_a_real_key(self, tmp_path: Path) -> None:
        """#464 comment: `{username: me, password: Spring,username}` loaded username=None."""
        message = _yaml_message(
            tmp_path,
            "schema_version: 1\nconnection: {host: nas}\n"
            "auth: {username: me, password: Spring,username}\n" + _MODULES,
        )
        assert "Spring" not in message

    def test_a_merge_key_may_be_overridden(self) -> None:
        """`<<: *base` then the same key is YAML's override, not a repeat."""
        raw = parse_config_text(
            "base: &b {https: true, port: 5001}\nconn:\n  <<: *b\n  https: false\n"
        )
        assert raw["conn"] == {"https": False, "port": 5001}

    def test_a_merged_anchor_reused_by_alias_is_not_a_repeat(self) -> None:
        """Pre-QA r2: SafeLoader flattens a merge in place, so the alias's second visit saw
        the override as a repeat."""
        raw = parse_config_text("a: &a {x: 1}\nc: {<<: &b {<<: *a, x: 2}}\nd: *b\n")
        assert raw["d"] == {"x": 2}

    def test_the_same_key_in_two_sections_is_fine(self) -> None:
        raw = parse_config_text("a: {x: 1}\nb: {x: 2}\n")
        assert raw == {"a": {"x": 1}, "b": {"x": 2}}

    def test_an_unhashable_key_still_gets_yamls_own_error(self) -> None:
        with pytest.raises(yaml.YAMLError):
            parse_config_text("? [a, b]\n: 1\n")


class TestKeyWithoutValue:
    """#464 comment: `{password: Spring,device_id}` split off `device_id` with no value,
    and the config loaded with the password cut short."""

    def test_a_flow_key_with_no_colon_is_refused(self, tmp_path: Path) -> None:
        message = _yaml_message(
            tmp_path,
            "schema_version: 1\nconnection: {host: nas}\n"
            "auth: {username: me, password: Spring,device_id}\n" + _MODULES,
        )
        assert message.startswith("The config file has a key with no value inside {…} (line 3)")
        assert "Spring" not in message
        assert "device_id" not in message
        assert "quote that value" in message

    def test_a_missing_space_after_the_colon_gets_that_advice(self, tmp_path: Path) -> None:
        """`{host:nas}` is one colon-less key in YAML (pre-QA r2)."""
        message = _yaml_message(
            tmp_path, "schema_version: 1\nconnection: {host:nas, https:true}\n" + _MODULES
        )
        assert "A colon needs a space after it (`host: nas`, not `host:nas`)" in message

    def test_a_flow_key_with_a_colon_and_no_value_is_fine(self) -> None:
        assert parse_config_text("a: {b: , c: 1}\n") == {"a": {"b": None, "c": 1}}
        assert parse_config_text("a: {b:}\n") == {"a": {"b": None}}

    @pytest.mark.parametrize(
        "text",
        ["auth: {password: Spr,ing #x: y\n}\n", "a: {a: 1, b # note: x\n}\n"],
    )
    def test_a_colon_in_a_comment_doesnt_count(self, text: str) -> None:
        """Pre-QA r1: a colon inside a comment let the fragment through."""
        with pytest.raises(yaml.YAMLError) as exc:
            parse_config_text(text)
        assert "a key with no value inside {…}" in format_yaml_error(exc.value)

    def test_block_style_is_unaffected(self) -> None:
        assert parse_config_text("a:\n  b:\n  c: 1\n") == {"a": {"b": None, "c": 1}}


class TestEmptySections:
    """#492, #475: an empty section is the same as one left out."""

    @pytest.mark.parametrize("section", ["auth:\n", "logging:\n"])
    def test_an_empty_auth_or_logging_loads_without_env(self, tmp_path: Path, section: str) -> None:
        config = load_config(
            _write(tmp_path, "schema_version: 1\nconnection: {host: nas}\n" + section + _MODULES)
        )
        assert config.auth.password is None
        assert config.logging.level == "info"

    def test_an_empty_module_entry_is_enabled_with_defaults(self, tmp_path: Path) -> None:
        config = load_config(
            _write(
                tmp_path, "schema_version: 1\nconnection: {host: nas}\nmodules:\n  filestation:\n"
            )
        )
        assert config.modules["filestation"].enabled is True
        assert config.modules["filestation"].permission == "read"

    def test_every_registered_module_takes_an_empty_entry(self) -> None:
        """The rule is ModuleConfig's, so it holds for every module, including ones
        added later (maintainer, 2026-11-02; documented in the README's Modules)."""
        from mcp_synology.server import _MODULE_REGISTRY

        names = sorted(_MODULE_REGISTRY)
        text = "schema_version: 1\nconnection: {host: nas}\nmodules:\n" + "".join(
            f"  {name}:\n" for name in names
        )
        config = AppConfig.model_validate(parse_config_text(text))
        assert sorted(config.modules) == names
        for module in config.modules.values():
            assert (module.enabled, module.permission, module.settings) == (True, "read", {})

    def test_empty_settings_are_no_settings(self, tmp_path: Path) -> None:
        config = load_config(
            _write(
                tmp_path,
                "schema_version: 1\nconnection: {host: nas}\nmodules:\n  filestation:\n"
                "    settings:\n",
            )
        )
        assert config.modules["filestation"].settings == {}

    def test_empty_modules_says_a_module_is_needed(self, tmp_path: Path) -> None:
        message = _validation_message(
            tmp_path, "schema_version: 1\nconnection: {host: nas}\nmodules:\n"
        )
        assert "At least one module must be configured" in message


class TestHost:
    """#476, #471, #504 item 1: `connection.host` is a bare host name or IP address, and a
    rejected host is never shown (it can hold a password)."""

    @pytest.mark.parametrize(
        "host",
        ["nas", "nas.local", "nas.local.", "192.168.1.100", "[fd00::1]", "nås.example", "my_nas"],
    )
    def test_bare_hosts_load(self, host: str) -> None:
        raw: dict[str, Any] = {
            "schema_version": 1,
            "instance_id": "x",
            "connection": {"host": host},
            "modules": {"filestation": {}},
        }
        assert AppConfig.model_validate(raw).connection.host == host  # type: ignore[union-attr]

    @pytest.mark.parametrize(
        ("host", "advice"),
        [
            ("https://admin:Hunter2x@nas", "drop the http:// or https://"),
            ("admin:Hunter2x@nas", "user names and passwords go under auth"),
            ("nas.local:5001", "set connection.port"),
            ("nas.local/dsm", "no path"),
            ("fd00::1", "square brackets"),
            ("nas local", "a space or an invisible character"),
            (" nas", "a space or an invisible character"),
            ("nas\u200b", "a space or an invisible character"),
            ("nas?x", "a character a host name can't have"),
            (".nas", "an empty part"),
            ("nas..local", "an empty part"),
            ("[nas]", "square brackets hold only an IPv6 address"),
            ("[fd00::1", "square brackets hold only an IPv6 address"),
            ("nas]", "square brackets hold only an IPv6 address"),
            ("", "is empty"),
            ("  ", "is empty"),
        ],
    )
    def test_a_host_that_isnt_bare_is_refused_unquoted(self, host: str, advice: str) -> None:
        raw: dict[str, Any] = {
            "schema_version": 1,
            "connection": {"host": host},
            "modules": {"filestation": {}},
        }
        with pytest.raises(ValidationError) as exc:
            AppConfig.model_validate(raw)
        message = format_validation_error(exc.value)
        assert "connection.host:" in message
        assert advice in message
        assert "Hunter2x" not in message
        if "[" in host:
            assert "has a port" not in message
        if host.strip():
            assert repr(host) not in message
            assert "(got" not in message

    def test_scheme_and_credentials_are_both_reported(self) -> None:
        raw: dict[str, Any] = {
            "schema_version": 1,
            "connection": {"host": "https://admin:Hunter2x@nas"},
            "modules": {"filestation": {}},
        }
        with pytest.raises(ValidationError) as exc:
            AppConfig.model_validate(raw)
        message = format_validation_error(exc.value)
        assert "drop the http:// or https://" in message
        assert "user names and passwords go under auth" in message
        assert "hunter2x" not in message.casefold()

    @pytest.mark.parametrize(
        ("host", "kinds"),
        [
            ("[fd00::1]:5001", ["port"]),
            ("https://[fd00::1]", ["scheme"]),
            ("https://[fd00::1]:5001/x", ["scheme", "path", "port"]),
            ("admin:pw@[fd00::1]", ["user name"]),
            ("[fe80::1%eth0]", []),
            ("[nas]:5001", ["has square brackets", "port"]),
        ],
    )
    def test_a_bracketed_ipv6_address_gets_only_whats_wrong_around_it(
        self, host: str, kinds: list[str]
    ) -> None:
        """Pre-QA r1: `[fd00::1]` is the right form, so `[fd00::1]:5001` gets the port
        advice, not a complaint about its brackets."""
        from mcp_synology.core.config import host_problems

        assert [p.split(":")[0].removeprefix("has a ") for p in host_problems(host)] == kinds

    @pytest.mark.parametrize("host", ["999.1.1.1", "1.2.3.999", "01.2.3.4", "Ⓝas", "ä" * 70])
    def test_a_host_the_url_library_refuses_is_refused_here(self, host: str) -> None:
        """Pre-QA r2: these looked bare but crashed later with httpx.InvalidURL."""
        from mcp_synology.core.config import host_problems

        (problem,) = host_problems(host)
        assert problem.startswith("isn't a valid host name or IP address")

    @pytest.mark.parametrize("text", ["host: [fd00::1]\n", "host: [fd00::1]\nport: 5001\n"])
    def test_an_unquoted_bracketed_ipv6_address_says_to_quote_it(self, text: str) -> None:
        """Unquoted, `[fd00::1]` is a YAML list (pre-QA r2)."""
        raw = {
            "schema_version": 1,
            "connection": parse_config_text(text),
            "modules": {"filestation": {}},
        }
        with pytest.raises(ValidationError) as exc:
            AppConfig.model_validate(raw)
        message = format_validation_error(exc.value)
        assert 'an IPv6 address in square brackets needs quotes, "[fd00::1]"' in message
        assert "fd00::1]'" not in message

    def test_an_empty_host_gets_no_hiding_note(self) -> None:
        from mcp_synology.core.config import host_message, host_problems

        assert host_message(host_problems("")) == (
            "must be a bare host name or IP address, and it is empty."
        )

    @pytest.mark.parametrize(
        ("kwargs", "says"),
        [
            ({}, "for HTTPS, set https: true"),
            ({"https_setting": "set SYNOLOGY_HTTPS=true"}, "for HTTPS, set SYNOLOGY_HTTPS=true"),
        ],
    )
    def test_the_https_advice_names_its_source(self, kwargs: dict[str, str], says: str) -> None:
        from mcp_synology.core.config import host_problems

        assert says in host_problems("http://nas", **kwargs)[0]


class TestPort:
    """#504 item 1: `port: 70000` ended in a traceback."""

    @pytest.mark.parametrize("port", [0, -1, 65536, 70000])
    def test_out_of_range(self, tmp_path: Path, port: int) -> None:
        message = _validation_message(
            tmp_path, f"schema_version: 1\nconnection: {{host: nas, port: {port}}}\n" + _MODULES
        )
        assert "connection.port:" in message

    @pytest.mark.parametrize("port", [1, 5001, 65535])
    def test_in_range(self, tmp_path: Path, port: int) -> None:
        config = load_config(
            _write(
                tmp_path,
                f"schema_version: 1\nconnection: {{host: nas, port: {port}}}\n" + _MODULES,
            )
        )
        assert config.connection is not None
        assert config.connection.port == port


class TestEmptyValues:
    """#485 and the #511 comment: an empty value that names nothing is an error, not
    "unset"."""

    @pytest.mark.parametrize("value", ['""', '"  "'])
    def test_an_empty_instance_id(self, tmp_path: Path, value: str) -> None:
        with pytest.raises(ValueError, match="instance_id is empty"):
            load_config(
                _write(
                    tmp_path,
                    f"schema_version: 1\ninstance_id: {value}\nconnection: {{host: nas}}\n"
                    + _MODULES,
                )
            )

    def test_instance_id_is_lowered(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        """It names the state folder and keyring entry (pre-QA r2: no test pinned it)."""
        path = _write(
            tmp_path, "schema_version: 1\ninstance_id: MyNAS\nconnection: {host: nas}\n" + _MODULES
        )
        assert load_config(path).instance_id == "mynas"
        monkeypatch.setenv("SYNOLOGY_INSTANCE_ID", "Office-NAS")
        assert load_config(path).instance_id == "office-nas"

    @pytest.mark.parametrize(
        "line", ["logging:\n  file:\n", "instructions_file:\n", "logging: {file: }\n"]
    )
    def test_a_key_left_blank_is_unset(self, tmp_path: Path, line: str) -> None:
        """YAML reads a key with nothing after it as null: not set, like an empty section
        (maintainer decision 3; only "" names an empty path)."""
        config = load_config(
            _write(tmp_path, "schema_version: 1\nconnection: {host: nas}\n" + line + _MODULES)
        )
        assert config.logging.file is None
        assert config.instructions_file is None

    @pytest.mark.parametrize(
        ("line", "where"),
        [
            ('logging: {file: ""}\n', "logging.file:"),
            ('logging: {file: "  "}\n', "logging.file:"),
            ('instructions_file: ""\n', "instructions_file:"),
        ],
    )
    def test_an_empty_file_path(self, tmp_path: Path, line: str, where: str) -> None:
        message = _validation_message(
            tmp_path, "schema_version: 1\nconnection: {host: nas}\n" + line + _MODULES
        )
        assert where in message
        assert "is empty" in message


class TestEnvVarsFollowTheFieldRules:
    """An env var that breaks its field's own rule is an EnvVarError naming the variable,
    never quoting the value (PR 1's spec left these to PR 2)."""

    _FILE = "schema_version: 1\nconnection: {host: nas}\n" + _MODULES

    @pytest.mark.parametrize(
        ("name", "value", "says"),
        [
            ("SYNOLOGY_HOST", "https://admin:Hunter2x@nas", "drop the http:// or https://"),
            ("SYNOLOGY_HOST", "nas:5001", "set SYNOLOGY_PORT"),
            ("SYNOLOGY_PORT", "70000", "a whole number from 1 to 65535"),
            ("SYNOLOGY_PORT", "0", "a whole number from 1 to 65535"),
            ("SYNOLOGY_INSTANCE_ID", "Hunter2x!", "lowercase letters, digits and hyphens"),
        ],
    )
    def test_a_value_breaking_the_rule(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        name: str,
        value: str,
        says: str,
    ) -> None:
        monkeypatch.setenv(name, value)
        with pytest.raises(EnvVarError) as exc:
            load_config(_write(tmp_path, self._FILE))
        message = str(exc.value)
        assert message.startswith(name)
        assert says in message
        assert value not in message
        assert "Hunter2x" not in message

    def test_env_only_mode_checks_the_host_too(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("SYNOLOGY_HOST", "admin:Hunter2x@nas")
        monkeypatch.chdir("/")
        monkeypatch.setenv("HOME", "/nonexistent-home-for-test")
        with pytest.raises(EnvVarError, match="^SYNOLOGY_HOST"):
            load_config()
