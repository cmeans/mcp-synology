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
    host_problems,
    load_config,
    parse_config_text,
    parse_env_value,
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
            "schema_version: 1\nconnection: {host: nas}\nauth:\n  Hunter2x: 1\n  Hunter2x: 2\n",
        )
        assert "Hunter2x" not in message
        assert message.startswith("The config file has a key twice in one section (lines 4 and 5)")

    @pytest.mark.parametrize(
        ("text", "key", "where"),
        [
            # A pasted second module block (pre-QA on #532): settings and module names
            # hold no credentials, and the warnings already name them.
            (
                "modules:\n  filestation:\n  filestation:\n    permission: write\n",
                "filestation",
                "lines 2 and 3",
            ),
            (
                "modules:\n  filestation:\n    settings:\n      zz_custom: 1\n      zz_custom: 2\n",
                "zz_custom",
                "lines 4 and 5",
            ),
        ],
    )
    def test_a_repeated_module_or_setting_is_named(self, text: str, key: str, where: str) -> None:
        with pytest.raises(yaml.YAMLError) as exc:
            parse_config_text(text)
        assert format_yaml_error(exc.value).startswith(
            f"The config file has `{key}` twice in one section ({where})"
        )

    @pytest.mark.parametrize(
        ("text", "where"),
        [
            # Block style: there's no {…}, and the alias's marks are its anchor's (pre-QA
            # on #532 found "in the {…} at line 4" here).
            (
                "k: &k host\nconnection:\n  *k : a\n  host: b\n",
                "in the section that starts at line 3",
            ),
            (
                "k: &k host\nconnection:\n  host: a\n  *k : b\n",
                "in the section that starts at line 3",
            ),
        ],
    )
    def test_a_repeat_by_alias_in_a_block_names_the_sections_line(
        self, text: str, where: str
    ) -> None:
        with pytest.raises(yaml.YAMLError) as exc:
            parse_config_text(text)
        assert f"`host` twice in one section ({where})" in format_yaml_error(exc.value)

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

    def test_a_repeated_alias_key_names_the_mappings_line(self) -> None:
        """Pre-QA r4: the alias's own marks are its anchor's, on another line. QA round 1
        on #532: its column is then the mapping's, so no columns are given."""
        with pytest.raises(yaml.YAMLError) as exc:
            parse_config_text("x: &k https\nconnection: {*k: true, https: false}\n")
        assert "`https` twice in one section (in the {…} at line 2)" in format_yaml_error(exc.value)

    def test_a_key_repeated_by_alias_twice_gets_no_columns(self) -> None:
        """QA round 1 on #532: it said "columns 4 and 4"."""
        with pytest.raises(yaml.YAMLError) as exc:
            parse_config_text("x: &k host\nm: {*k : 1, *k : 2}\n")
        message = format_yaml_error(exc.value)
        assert "`host` twice in one section (in the {…} at line 2)" in message
        assert "columns" not in message

    def test_two_merge_keys_in_one_mapping_are_refused(self) -> None:
        """QA round 1 on #532: the later `<<` silently won."""
        with pytest.raises(yaml.YAMLError) as exc:
            parse_config_text("b: &b {host: x}\nc: &c {host: y}\nconnection:\n  <<: *b\n  <<: *c\n")
        assert format_yaml_error(exc.value).startswith(
            "The config file has `<<` twice in one section (lines 4 and 5), and YAML would "
            "silently let the later one win. Merge both in one, later one first, since the "
            "first in a list wins: `<<: [*later, *earlier]`."
        )

    def test_following_the_merge_advice_keeps_the_result(self) -> None:
        """QA round 2 on #532: `<<: [*a, *b]` turned HTTPS off where the two lines had it on."""
        anchors = "a: &a {https: false}\nb: &b {https: true}\nc:\n"
        as_written = yaml.safe_load(anchors + "  <<: *a\n  <<: *b\n")["c"]
        assert parse_config_text(anchors + "  <<: [*b, *a]\n")["c"] == as_written == {"https": True}

    def test_two_merge_keys_on_one_line_give_the_columns(self) -> None:
        """QA round 2 on #532: it said "lines 3 and 3"."""
        with pytest.raises(yaml.YAMLError) as exc:
            parse_config_text("a: &a {x: 1}\nb: &b {y: 2}\nc: {<<: *a, <<: *b}\n")
        assert "`<<` twice in one section (line 3, columns 5 and 13)" in format_yaml_error(
            exc.value
        )

    @pytest.mark.parametrize(
        ("text", "where"),
        [
            ("connection: {&k host: a, *k : b}\n", "in the {…} at line 1"),
            (
                "connection:\n  &k host: a\n  https: true\n  *k : b\n",
                "in the section that starts at line 2",
            ),
            # After an alias to an anchor outside the mapping, whose marks are further back.
            ("x: &o z\nm: {&k host: 2, *o : 3, *k : 4}\n", "in the {…} at line 2"),
            # The same, with that entry's value an alias too, so nothing in it moves forward.
            ("x: &o z\ny: &v w\nm: {&k host: 2, *o : *v, *k : 4}\n", "in the {…} at line 3"),
            # An anchor inside an earlier value (pre-QA r6 on #532).
            ("connection: {a: {&k host: 1}, *k : 2, host: nas}\n", "in the {…} at line 1"),
            (
                "connection:\n  a:\n    &k host: 1\n  *k : 2\n  host: nas\n",
                "in the section that starts at line 2",
            ),
        ],
    )
    def test_an_alias_to_an_anchor_in_the_same_mapping(self, text: str, where: str) -> None:
        """QA round 2 on #532: "columns 14 and 14", or "line 2" for a repeat on line 4."""
        with pytest.raises(yaml.YAMLError) as exc:
            parse_config_text(text)
        message = format_yaml_error(exc.value)
        assert f"twice in one section ({where})" in message
        assert "columns" not in message

    def test_keys_after_a_value_keep_their_own_places(self) -> None:
        """The check for an anchor inside a value mustn't catch a key written after one."""
        for text, where in (
            ("c:\n  a:\n    b: 1\n  x: 1\n  x: 2\n", "lines 4 and 5"),
            ("c:\n  a:\n  - 1\n  x: 1\n  x: 2\n", "lines 4 and 5"),
            ("c: {a: 1,b: 2,b: 3}\n", "line 1, columns 10 and 15"),
            ("e: &e\nc: {a: *e, b: 1, b: 2}\n", "line 2, columns 12 and 18"),
        ):
            with pytest.raises(yaml.YAMLError) as exc:
                parse_config_text(text)
            assert f"twice in one section ({where})" in format_yaml_error(exc.value), text

    @pytest.mark.parametrize("entries", ["*m : *x, *m : *x", "*m : *x, <<: *x", "<<: *x, *m : *x"])
    def test_two_merge_keys_by_alias_get_no_columns(self, entries: str) -> None:
        """Pre-QA r6 on #532: as for any other key, "columns 4 and 4"."""
        with pytest.raises(yaml.YAMLError) as exc:
            parse_config_text(f"a: &m <<\nb: &x {{q: 1}}\nc: {{{entries}}}\n")
        assert "`<<` twice in one section (in the {…} at line 3)" in format_yaml_error(exc.value)

    def test_a_repeated_key_in_a_mapping_in_a_list_is_refused(self) -> None:
        """Pre-QA r6 on #532: the walk into lists was unpinned."""
        with pytest.raises(yaml.YAMLError) as exc:
            parse_config_text("allowed_local_paths: [{a: 1, a: 2}]\n")
        assert "a key twice in one section" in format_yaml_error(exc.value)

    def test_a_repeated_key_directly_under_a_module_isnt_named_by_the_settings_rule(self) -> None:
        """Pre-QA r6 on #532: only a key in `settings` is named, not one beside it."""
        with pytest.raises(yaml.YAMLError) as exc:
            parse_config_text(
                "modules:\n  x:\n    permission:\n      Hunter2x: 1\n      Hunter2x: 2\n"
            )
        assert "Hunter2x" not in format_yaml_error(exc.value)

    @pytest.mark.parametrize(
        "key",
        ['"a\\n[ERROR] forged"', '"a\\r[ERROR] forged"', '"a\\e[31m[ERROR] forged"'],
    )
    def test_a_repeated_settings_key_that_could_forge_a_line_isnt_named(self, key: str) -> None:
        """QA rounds 2 and 3 on #532: a quoted key can hold a line break or an escape."""
        with pytest.raises(yaml.YAMLError) as exc:
            parse_config_text(
                f"modules:\n  filestation:\n    settings:\n      {key}: 1\n      {key}: 2\n"
            )
        message = format_yaml_error(exc.value)
        assert message.startswith("The config file has a key twice in one section (lines 4 and 5)")
        assert "forged" not in message

    def test_a_dotted_settings_key_is_named(self) -> None:
        with pytest.raises(yaml.YAMLError) as exc:
            parse_config_text("modules:\n  x:\n    settings:\n      a-b.c_d: 1\n      a-b.c_d: 2\n")
        assert format_yaml_error(exc.value).startswith("The config file has `a-b.c_d` twice")

    def test_a_key_at_a_settings_like_path_under_auth_stays_hidden(self) -> None:
        """The rule names keys only under `modules`: any other may be a password's fragment."""
        with pytest.raises(yaml.YAMLError) as exc:
            parse_config_text("auth:\n  x:\n    settings:\n      Hunter2x: 1\n      Hunter2x: 2\n")
        assert "Hunter2x" not in format_yaml_error(exc.value)

    def test_a_key_spelled_equals_is_text(self) -> None:
        """QA round 2 on #532: the check built the key before PyYAML rewrites its tag, so
        `{=: 1}`, which YAML loads, was "isn't valid YAML"."""
        assert parse_config_text("a: {=: 1}\n") == yaml.safe_load("a: {=: 1}\n") == {"a": {"=": 1}}
        with pytest.raises(yaml.YAMLError) as exc:
            parse_config_text("a: {=: 1, =: 2}\n")
        assert "a key twice in one section" in format_yaml_error(exc.value)

    def test_one_merge_key_with_a_list_is_fine(self) -> None:
        raw = parse_config_text("b: &b {host: x}\nc: &c {host: y}\nconnection:\n  <<: [*b, *c]\n")
        assert raw["connection"] == {"host": "x"}

    def test_a_repeat_on_one_line_gives_the_columns(self, tmp_path: Path) -> None:
        message = _yaml_message(
            tmp_path, "schema_version: 1\nconnection: {host: nas, https: true, https: no}\n"
        )
        assert "`https` twice in one section (line 2, columns 25 and 38)" in message

    @pytest.mark.parametrize(
        "text",
        [
            "a: &a [*a]\n",
            "a: &a {b: *a}\n",
            # Each level doubles the paths to the bottom: without the visited set, the
            # walk would take 2**25 steps.
            "l0: &l0 [x, y]\n"
            + "".join(f"l{i}: &l{i} [*l{i - 1}, *l{i - 1}]\n" for i in range(1, 26)),
        ],
    )
    def test_shared_and_recursive_nodes_are_checked_once(self, text: str) -> None:
        """Pre-QA r3: the walk over the composed YAML visits each node once, so a
        recursive anchor doesn't hang and an alias chain doesn't explode."""
        assert parse_config_text(text) is not None

    @pytest.mark.parametrize(
        "text", ["x: &e\ny: {a: *e}\n", "x: {a: &z , b: *z}\n", "x: &k a\ny: {*k: }\n"]
    )
    def test_an_alias_to_an_empty_value_is_not_a_colon_less_key(self, text: str) -> None:
        """Pre-QA r3: the alias's node was written earlier, so its marks aren't this
        entry's."""
        parse_config_text(text)

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
        assert parse_config_text("a: {b : }\n") == {"a": {"b": None}}  # pre-QA r7 on #532

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

    def test_under_modules_the_advice_keeps_the_module(self, tmp_path: Path) -> None:
        """QA round 2 on #532: "leave it out" drops the module, and the form it gives loads."""
        head = "schema_version: 1\nconnection: {host: nas}\n"
        message = _yaml_message(tmp_path, head + "modules: {filestation, photos}\n")
        assert message.endswith(
            "to list a module with its defaults, give it a colon: `{filestation: , photos: }`."
        )
        assert "leave it out" not in message
        config = load_config(_write(tmp_path, head + "modules: {filestation: , photos: }\n"))
        assert sorted(config.modules) == ["filestation", "photos"]

    def test_elsewhere_the_advice_is_to_leave_it_out(self, tmp_path: Path) -> None:
        message = _yaml_message(tmp_path, "schema_version: 1\nconnection: {host: nas, https}\n")
        assert message.endswith("to leave a setting empty, leave it out.")


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

    @pytest.mark.parametrize("first", ["nas", "bücher", "xn--bcher-kva"])
    def test_a_digit_outside_ascii_isnt_a_number(self, first: str) -> None:
        """QA round 3 on #532: httpx sends `nas.१२३` as a name, and one name got two
        answers, since `.host` decodes only a first `xn--` part."""
        assert host_problems(f"{first}.१२३") == []

    @pytest.mark.parametrize("host", ["nas.123", "0x7f.1", "nas.0x"])
    def test_an_ascii_number_at_the_end_is_still_refused(self, host: str) -> None:
        assert host_problems(host) != []

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

    @pytest.mark.parametrize(
        "host",
        [
            "192.168.1",
            "10.1",
            "127.1",
            "0x7f.0.0.1",
            "2130706433",
            "1.2.3.4.5",
            "nas.0x1",
            "nas.42",
            # IDNA maps these dots to `.` before resolving (pre-QA r4).
            "192。168。1",
            "127．1",
            "192.168.1。",
            "127｡1",
        ],
    )
    def test_a_short_or_numeric_ipv4_address_is_refused(self, host: str) -> None:
        """Pre-QA r3: the resolver expands `192.168.1` to 192.168.0.1, the router, so the
        password went there. A host whose last part is a number must be a whole IPv4
        address."""
        from mcp_synology.core.config import host_problems

        assert host_problems(host) == [
            "ends in a number, so it's read as an IPv4 address, but it isn't a whole one: "
            "write all four numbers (192.168.1.100), or a name whose last part isn't a number"
        ]

    @pytest.mark.parametrize("host", ["192.168.1.100.", "192.168.1.100。"])
    def test_a_dot_after_an_ipv4_address_is_refused(self, host: str) -> None:
        """Pre-QA r4: it resolves nowhere, and derived the instance ID `192`."""
        from mcp_synology.core.config import host_problems

        assert host_problems(host) == ["has a dot after the IPv4 address: drop it"]

    @pytest.mark.parametrize("host", ["192.168.1.100", "nas.local.", "nas1", "1nas.local"])
    def test_a_whole_ipv4_address_or_a_name_with_digits_loads(self, host: str) -> None:
        from mcp_synology.core.config import host_problems

        assert host_problems(host) == []

    def test_a_tilde_is_refused(self) -> None:
        from mcp_synology.core.config import host_problems

        assert host_problems("na~s") == ["has a character a host name can't have"]

    @pytest.mark.parametrize(
        ("kwargs", "form"),
        [({}, '"[fd00::1]", in quotes'), ({"ipv6_form": "[fd00::1]"}, "brackets, [fd00::1]")],
    )
    def test_the_ipv6_advice_fits_its_source(self, kwargs: dict[str, str], form: str) -> None:
        """Quotes only in the config file: typed into setup or an env var, they'd be part
        of the host (pre-QA r3)."""
        from mcp_synology.core.config import host_problems

        assert form in host_problems("fd00::1", **kwargs)[0]
        assert form.split(", ")[-1] in host_problems("[nas]", **kwargs)[0]

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

    @pytest.mark.parametrize("host", ["xn--", "xn--zz", "xn--a", "XN--ZZ"])
    def test_a_malformed_a_label_is_refused_not_raised(self, host: str) -> None:
        """QA round 1 on #532: reading `url.host` decoded the `xn--` label, and idna's own
        error (a UnicodeError, its text decoded from the host) escaped. Pre-QA after it:
        such a host meets every rule the general advice states, so it gets its own."""
        from mcp_synology.core.config import host_problems

        assert host_problems(host) == [
            "starts with a part beginning xn--, which isn't valid punycode: write the name "
            "as it's spelled, or copy its xn-- form exactly"
        ]

    @pytest.mark.parametrize("from_env", [False, True])
    def test_a_malformed_a_label_is_a_config_error_naming_no_text(self, from_env: bool) -> None:
        raw: dict[str, Any] = {"schema_version": 1, "modules": {"filestation": {}}}
        if from_env:
            with pytest.raises(EnvVarError) as env_exc:
                parse_env_value("SYNOLOGY_HOST", "connection.host", "xn--a")
            message = str(env_exc.value)
            assert message.startswith("SYNOLOGY_HOST (it sets connection.host) must be")
        else:
            raw["connection"] = {"host": "xn--a"}
            with pytest.raises(ValidationError) as exc:
                AppConfig.model_validate(raw)
            message = format_validation_error(exc.value)
        assert "which isn't valid punycode" in message
        assert "Codepoint" not in message
        assert "\\x80" not in message

    @pytest.mark.parametrize(
        ("host", "ok"),
        [
            ("[fe80::1%eth0]", True),
            ("[fe80::1%en0.1_a~b-c]", True),
            ("[fe80::1%x:80]", False),
            ("[fe80::1%a\\b]", False),
        ],
    )
    def test_an_ipv6_zone_takes_only_unreserved_characters(self, host: str, ok: bool) -> None:
        """QA round 1 on #532: ipaddress takes any zone text."""
        from mcp_synology.core.config import host_problems

        assert host_problems(host) == (
            []
            if ok
            else [
                "has an IPv6 zone (after %) with characters other than letters, digits, "
                "'.', '_', '~' and '-'"
            ]
        )

    def test_a_part_longer_than_63_characters_is_refused(self) -> None:
        """QA round 1 on #532: the error states this rule, which httpx doesn't check."""
        from mcp_synology.core.config import host_problems

        assert host_problems("a" * 63 + ".local") == []
        (problem,) = host_problems("a" * 64 + ".local")
        assert problem.startswith("isn't a valid host name or IP address")

    @pytest.mark.parametrize(
        ("host", "ok"),
        [
            ("xn--mnchen-3ya", True),
            ("Bücher.local", True),
            ("a" * 63, True),
            # 64 characters as sent; `.host` decodes a first `xn--` part to 58.
            ("xn--" + ("ü" * 58).encode("punycode").decode(), False),
            ("xn--" + ("ü" * 57).encode("punycode").decode(), True),
            ("é" * 64, False),
        ],
    )
    def test_the_63_character_rule_measures_the_host_as_sent(self, host: str, ok: bool) -> None:
        """QA round 2 on #532: a 67-character `xn--` label passed."""
        from mcp_synology.core.config import host_problems

        assert (host_problems(host) == []) is ok

    @pytest.mark.parametrize("value", ["12345", "192.168", "true", "2024-01-02"])
    def test_a_host_yaml_didnt_read_as_text_says_so(self, tmp_path: Path, value: str) -> None:
        """QA round 2 on #532: with the value hidden, all that was left was "should be a
        valid string", though the value looks like text in the file."""
        message = _validation_message(
            tmp_path, f"schema_version: 1\nconnection: {{host: {value}}}\n" + _MODULES
        )
        assert "isn't text: YAML read it as a number, date or true/false" in message
        assert "put it in quotes" in message
        assert value not in message.replace("true/false", "")

    def test_a_longer_list_as_a_host_isnt_called_an_ipv6_address(self, tmp_path: Path) -> None:
        """Pre-QA r6 on #532: the advice is for the one address `[fd00::1]` reads as."""
        message = _validation_message(
            tmp_path, "schema_version: 1\nconnection:\n  host: [a, b]\n" + _MODULES
        )
        assert "IPv6" not in message

    def test_an_unbracketed_ipv6_variable_gets_no_quotes_advice(self) -> None:
        """Pre-QA r6 on #532: quotes are for the file's form."""
        with pytest.raises(EnvVarError) as exc:
            parse_env_value("SYNOLOGY_HOST", "connection.host", "fd00::1")
        assert "write it in square brackets, [fd00::1]." in str(exc.value)

    def test_a_blank_host_is_empty(self, tmp_path: Path) -> None:
        """QA round 2 on #532: `host: ""` said so, and `host:` said "a valid string"."""
        message = _validation_message(
            tmp_path, "schema_version: 1\nconnection:\n  host:\n" + _MODULES
        )
        assert "must be a bare host name or IP address, and it is empty." in message

    @pytest.mark.parametrize(
        ("kwargs", "says"),
        [
            ({}, "user names and passwords go under auth or in the keyring"),
            (
                {"credentials_setting": "are asked for after these questions"},
                "user names and passwords are asked for after these questions",
            ),
        ],
    )
    def test_the_credentials_advice_names_its_source(
        self, kwargs: dict[str, str], says: str
    ) -> None:
        """QA round 1 on #532: setup and SYNOLOGY_HOST got the file's advice."""
        from mcp_synology.core.config import host_problems

        assert says in host_problems("admin@nas", **kwargs)[0]

    def test_synology_host_gets_the_env_credentials_advice(self) -> None:
        with pytest.raises(EnvVarError) as exc:
            parse_env_value("SYNOLOGY_HOST", "connection.host", "admin@nas")
        assert "go in SYNOLOGY_USERNAME and SYNOLOGY_PASSWORD" in str(exc.value)

    @pytest.mark.parametrize(
        ("host", "derived"),
        [("nas．local", "nas"), ("nas。local", "nas"), ("192｡168｡1｡100", "192-168-1-100")],
    )
    def test_ideographic_and_full_width_dots_split_the_derived_id(
        self, host: str, derived: str
    ) -> None:
        """QA round 1 on #532: host_problems accepts them (IDNA maps them to `.`), but
        the instance ID split on ASCII dots only, and blamed the host's characters."""
        config = AppConfig.model_validate(
            {"schema_version": 1, "connection": {"host": host}, "modules": {"filestation": {}}}
        )
        assert config.instance_id == derived


class TestPort:
    """#504 item 1: `port: 70000` ended in a traceback."""

    @pytest.mark.parametrize(
        ("line", "where"),
        [
            ("connection: {host: nas, port: yes}\n", "connection.port:"),
            ("connection: {host: nas, timeout: on}\n", "connection.timeout:"),
        ],
    )
    def test_true_or_false_isnt_a_number(self, tmp_path: Path, line: str, where: str) -> None:
        """Pre-QA r3: pydantic read `port: yes` as port 1 and `timeout: on` as 1 s."""
        message = _validation_message(tmp_path, "schema_version: 1\n" + line + _MODULES)
        assert f"{where} Value error, must be a whole number, not true or false" in message

    def test_schema_version_true_isnt_1(self, tmp_path: Path) -> None:
        message = _validation_message(
            tmp_path, "schema_version: true\nconnection: {host: nas}\n" + _MODULES
        )
        assert "schema_version: Value error, must be a whole number" in message

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

    @pytest.mark.parametrize("value", ["123", "1.5", "true", "2024-01-02"])
    def test_an_instance_id_yaml_didnt_read_as_text_says_so(
        self, tmp_path: Path, value: str
    ) -> None:
        """QA round 2 on #532, as for the host."""
        message = _validation_message(
            tmp_path,
            f"schema_version: 1\ninstance_id: {value}\nconnection: {{host: nas}}\n" + _MODULES,
        )
        assert "isn't text: YAML read it as a number, date or true/false" in message
        assert value not in message.replace("true/false", "")

    @pytest.mark.parametrize("key", ["username", "password", "device_id"])
    @pytest.mark.parametrize("value", ["123456", "1.5", "true", "2024-01-02"])
    def test_a_credential_yaml_didnt_read_as_text_says_so(
        self, tmp_path: Path, key: str, value: str
    ) -> None:
        """Pre-QA r6 on #532: `password: 123456` said only "should be a valid string"."""
        message = _validation_message(
            tmp_path,
            f"schema_version: 1\nconnection: {{host: nas}}\nauth: {{{key}: {value}}}\n" + _MODULES,
        )
        assert f"auth.{key}" in message
        assert "isn't text: YAML read it as a number, date or true/false" in message
        assert value not in message.replace("true/false", "")

    def test_a_blank_or_quoted_credential_still_loads(self, tmp_path: Path) -> None:
        config = load_config(
            _write(
                tmp_path,
                "schema_version: 1\nconnection: {host: nas}\n"
                'auth: {username: , password: "123456"}\n' + _MODULES,
            )
        )
        assert config.auth.username is None
        assert config.auth.password == "123456"

    def test_a_blank_instance_id_is_still_not_set(self, tmp_path: Path) -> None:
        config = load_config(
            _write(
                tmp_path, "schema_version: 1\ninstance_id:\nconnection: {host: nas}\n" + _MODULES
            )
        )
        assert config.instance_id == "nas"

    @pytest.mark.parametrize(("length", "ok"), [(63, True), (64, False), (400, False)])
    def test_an_instance_id_has_a_host_parts_limit(
        self, tmp_path: Path, length: int, ok: bool
    ) -> None:
        """QA round 2 on #532: a very long one crashed setup with the keyring written."""
        from mcp_synology.core.config import instance_id_problem

        value = "a" * length
        assert (instance_id_problem(value) is None) is ok
        text = f"schema_version: 1\ninstance_id: {value}\nconnection: {{host: nas}}\n" + _MODULES
        if ok:
            assert load_config(_write(tmp_path, text)).instance_id == value
            return
        message = _validation_message(tmp_path, text)
        assert "is longer than 63 characters" in message
        assert value not in message
        with pytest.raises(EnvVarError) as exc:
            parse_env_value("SYNOLOGY_INSTANCE_ID", "instance_id", value)
        assert "is longer than 63 characters" in str(exc.value)

    def test_an_instance_id_of_another_type_isnt_quoted(self, tmp_path: Path) -> None:
        """Pre-QA on #532: decision 4 hid a character error's value, but `instance_id: 123`
        still showed "(got 123)"."""
        message = _validation_message(
            tmp_path,
            "schema_version: 1\ninstance_id: 735719\nconnection: {host: nas}\n" + _MODULES,
        )
        assert "instance_id" in message
        assert "735719" not in message

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
