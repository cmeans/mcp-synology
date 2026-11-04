"""Guard tests for #511: one validation pass, so `check` and `serve` agree on every config.

A new way a config can fail goes in BAD_CONFIGS. Each one must stop `check`, `serve` and
`setup --config` with the same message, no traceback and none of the secret it holds.
"""

from __future__ import annotations

import ast
import logging
import re
from pathlib import Path

import pytest
from click.testing import CliRunner

import mcp_synology.cli
from mcp_synology.cli import logging_
from mcp_synology.cli.main import main
from mcp_synology.core.config import ENV_VAR_MAP, load_config
from mcp_synology.modules import ModuleSettings
from mcp_synology.server import _MODULE_REGISTRY, create_server, module_settings, validate_config

SECRET = "Hunter2x"
GOOD_HEAD = "schema_version: 1\nconnection: {host: nas.local, https: true}\n"
REPO = Path(__file__).resolve().parents[2]

BAD_CONFIGS: dict[str, tuple[str, str]] = {
    # name: (the file, text the error must have)
    "repeated_key": (
        f"{GOOD_HEAD}auth:\n  password: {SECRET}\n  password: other\nmodules: {{filestation: }}\n",
        "`password` twice",
    ),
    "colon_less_key": (
        f"{GOOD_HEAD}auth: {{username: me, password: a,{SECRET}}}\nmodules: {{filestation: }}\n",
        "no value inside",
    ),
    "not_a_mapping": ("- a\n- b\n", "must be a YAML mapping"),
    "bad_yaml": (f"schema_version: 1\nauth: {{password: *{SECRET}\n", "isn't valid YAML"),
    "unknown_key": (f"{GOOD_HEAD}modules: {{filestation: }}\npasword: {SECRET}\n", "pasword"),
    "host_with_port": (
        f"schema_version: 1\nconnection: {{host: 'admin:{SECRET}@nas:5001'}}\n"
        "modules: {filestation: }\n",
        "connection.host",
    ),
    "port_range": (
        "schema_version: 1\nconnection: {host: nas, port: 70000}\nmodules: {filestation: }\n",
        "connection.port",
    ),
    "empty_log_file": (
        f"{GOOD_HEAD}logging: {{file: ''}}\nmodules: {{filestation: }}\n",
        "logging.file",
    ),
    "no_module": (GOOD_HEAD, "modules"),
    "filestation_timeout": (
        f"{GOOD_HEAD}modules: {{filestation: {{settings: {{async_timeout: 5}}}}}}\n",
        "modules.filestation.settings.async_timeout",
    ),
    "filestation_choice": (
        f"{GOOD_HEAD}modules: {{filestation: {{settings: {{file_type_indicator: Emoji}}}}}}\n",
        "modules.filestation.settings.file_type_indicator",
    ),
    "filestation_empty_dir": (
        f"{GOOD_HEAD}modules: {{filestation: {{settings: {{default_download_dir: ''}}}}}}\n",
        "modules.filestation.settings.default_download_dir",
    ),
    "filestation_bool_number": (
        f"{GOOD_HEAD}modules: {{filestation: {{settings: {{search_poll_interval: yes}}}}}}\n",
        "modules.filestation.settings.search_poll_interval",
    ),
    "photos_timeout": (
        f"{GOOD_HEAD}modules: {{photos: {{settings: {{task_timeout: 9999}}}}}}\n",
        "modules.photos.settings.task_timeout",
    ),
    "photos_bool_number": (
        f"{GOOD_HEAD}modules: {{photos: {{settings: {{task_timeout: on}}}}}}\n",
        "modules.photos.settings.task_timeout",
    ),
    "photos_paths_string": (
        f"{GOOD_HEAD}modules: {{photos: {{settings: {{allowed_local_paths: /tmp}}}}}}\n",
        "modules.photos.settings.allowed_local_paths",
    ),
    "downloadstation_relative": (
        f"{GOOD_HEAD}modules: {{downloadstation: {{settings: {{allowed_local_paths: [rel]}}}}}}\n",
        "modules.downloadstation.settings.allowed_local_paths",
    ),
}


@pytest.fixture(autouse=True)
def _isolated(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    for var in (*ENV_VAR_MAP, "MCP_SYNOLOGY_CONFIG"):
        monkeypatch.delenv(var, raising=False)
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("PYTHON_KEYRING_BACKEND", "keyring.backends.null.Keyring")


@pytest.fixture(autouse=True)
def _restore_logging() -> object:
    root = logging.getLogger()
    handlers, level = list(root.handlers), root.level
    yield
    for handler in list(root.handlers):
        if handler not in handlers:
            root.removeHandler(handler)
            handler.close()
    root.setLevel(level)
    logging_._early_records = None


def _run(*args: str) -> tuple[int, str, BaseException | None]:
    result = CliRunner().invoke(main, list(args))
    return result.exit_code, result.stderr + result.stdout, result.exception


def _only_the_error(output: str) -> str:
    """The `Error:` block: logging lines around it may differ per command."""
    lines = output.splitlines()
    start = next(i for i, line in enumerate(lines) if line.startswith("Error:"))
    end = start + 1
    while end < len(lines) and lines[end].startswith(("  - ", "Config file:", "The config file")):
        end += 1
    return "\n".join(lines[start:end])


@pytest.mark.parametrize("name", sorted(BAD_CONFIGS))
def test_every_command_refuses_a_bad_config_the_same_way(name: str, tmp_path: Path) -> None:
    text, expected = BAD_CONFIGS[name]
    path = tmp_path / "bad.yaml"
    path.write_text(text, encoding="utf-8")

    errors = []
    for command in (["check"], ["serve"], ["setup"]):
        code, output, exception = _run(*command, "--config", str(path))
        assert code == 1, (command, output)
        assert isinstance(exception, SystemExit), (command, exception)
        assert "Traceback" not in output
        assert SECRET not in output, (command, output)
        errors.append(_only_the_error(output))

    assert errors[0] == errors[1] == errors[2]
    assert expected in errors[0]
    assert repr(str(path)) in errors[0], "the error names the file"


class TestResources:
    def test_a_config_path_that_is_a_folder_is_a_clean_error(self, tmp_path: Path) -> None:
        """#483."""
        code, output, _ = _run("check", "--config", str(tmp_path))
        assert code == 1
        assert "Traceback" not in output
        assert "can't be read" in output
        assert repr(str(tmp_path)) in output

    def test_a_config_file_without_permission_is_a_clean_error(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """#483. Through the variable: click's own check catches this for `--config`."""
        path = tmp_path / "locked.yaml"
        path.write_text(f"{GOOD_HEAD}modules: {{filestation: }}\n", encoding="utf-8")
        path.chmod(0)
        try:
            path.read_text()
        except PermissionError:
            pass
        else:
            pytest.skip("running as a user who ignores file modes")
        monkeypatch.setenv("MCP_SYNOLOGY_CONFIG", str(path))
        code, output, _ = _run("serve")
        assert code == 1
        assert "Traceback" not in output
        assert "Permission denied" in output
        assert repr(str(path)) in output

    def test_the_log_files_folder_is_created(self, tmp_path: Path) -> None:
        """#498: examples/config-power-user.yaml names one a fresh machine doesn't have."""
        log = tmp_path / "new" / "folder" / "server.log"
        path = tmp_path / "ok.yaml"
        path.write_text(
            f"{GOOD_HEAD}logging: {{file: {log}}}\nmodules: {{filestation: }}\n", encoding="utf-8"
        )
        validate_config(path)
        assert log.exists()

    def test_a_log_file_that_cant_be_opened_is_a_clean_error(self, tmp_path: Path) -> None:
        """#498: a folder at that path."""
        path = tmp_path / "bad.yaml"
        path.write_text(
            f"{GOOD_HEAD}logging: {{file: {tmp_path}}}\nmodules: {{filestation: }}\n",
            encoding="utf-8",
        )
        code, output, _ = _run("check", "--config", str(path))
        assert code == 1
        assert "Traceback" not in output
        assert "logging.file" in output
        assert "can't be opened" in output

    def test_an_instructions_file_that_isnt_utf8_falls_back(
        self, tmp_path: Path, caplog: pytest.LogCaptureFixture
    ) -> None:
        """#498: `serve` crashed on the decode error; the byte isn't quoted."""
        instructions = tmp_path / "i.md"
        instructions.write_bytes(b"caf\xe9 secret text")
        path = tmp_path / "ok.yaml"
        path.write_text(
            f"{GOOD_HEAD}instructions_file: {instructions}\nmodules: {{filestation: }}\n",
            encoding="utf-8",
        )
        config = validate_config(path)
        with caplog.at_level(logging.WARNING, logger="mcp_synology.server"):
            server = create_server(config)
        assert "isn't valid UTF-8" in caplog.text
        assert "secret text" not in caplog.text
        assert "\\xe9" not in caplog.text
        assert "secret text" not in (server.instructions or "")
        assert "SERVER IDENTITY" in (server.instructions or "")

    def test_check_reports_an_unreadable_instructions_file(self, tmp_path: Path) -> None:
        """#498: `check` says what `serve` would do, and carries on."""
        path = tmp_path / "ok.yaml"
        missing = tmp_path / "none.md"
        path.write_text(
            f"{GOOD_HEAD}instructions_file: {missing}\nmodules: {{filestation: }}\n",
            encoding="utf-8",
        )
        result = CliRunner().invoke(main, ["check", "--config", str(path)])
        assert "instructions_file" in result.stderr
        assert "using the built-in text" in result.stderr


class TestEveryProblemTogether:
    def test_repeated_keys_in_file_order(self, tmp_path: Path) -> None:
        """QA round 2 on #532: one per run, last first, each round a client restart."""
        path = tmp_path / "bad.yaml"
        path.write_text(
            "schema_version: 1\n"  # 1
            "connection:\n"  # 2
            "  host: nas\n"  # 3
            "  https: true\n"  # 4
            "  https: false\n"  # 5
            "auth:\n"  # 6
            "  username: a\n"  # 7
            "  username: b\n"  # 8
            "modules:\n"  # 9
            "  filestation:\n"  # 10
            "  filestation:\n",  # 11
            encoding="utf-8",
        )
        _, output, _ = _run("check", "--config", str(path))
        places = [output.index(f"lines {pair}") for pair in ("4 and 5", "7 and 8", "10 and 11")]
        assert places == sorted(places)

    def test_model_level_problems_are_each_listed_and_counted(self, tmp_path: Path) -> None:
        path = tmp_path / "bad.yaml"
        path.write_text("schema_version: 99\nmodules: {}\n", encoding="utf-8")
        _, output, _ = _run("check", "--config", str(path))
        assert "(3 error(s))" in output
        assert "schema_version is 99" in output
        assert "connection.host is required" in output
        assert "At least one module" in output

    def test_every_modules_settings_are_reported(self, tmp_path: Path) -> None:
        path = tmp_path / "bad.yaml"
        path.write_text(
            f"{GOOD_HEAD}modules:\n"
            "  filestation: {settings: {async_timeout: 5, file_type_indicator: Emoji}}\n"
            "  photos: {settings: {task_timeout: 9999}}\n",
            encoding="utf-8",
        )
        _, output, _ = _run("check", "--config", str(path))
        assert "(3 error(s))" in output
        assert "modules.filestation.settings.async_timeout" in output
        assert "modules.filestation.settings.file_type_indicator" in output
        assert "modules.photos.settings.task_timeout" in output


class TestNamesTheSource:
    def test_a_file_error_names_the_file_at_any_log_level(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """QA round 2 on #532: only the INFO `Config:` line named it."""
        monkeypatch.setenv("SYNOLOGY_LOG_LEVEL", "error")
        path = tmp_path / "bad.yaml"
        path.write_text("schema_version: 1\nschema_version: 1\n", encoding="utf-8")
        _, output, _ = _run("check", "--config", str(path))
        assert f"Config file: {str(path)!r}" in output

    def test_a_path_with_a_newline_cant_forge_a_line(self, tmp_path: Path) -> None:
        folder = tmp_path / "a\nError: forged"
        folder.mkdir()
        path = folder / "bad.yaml"
        path.write_text("schema_version: 1\nschema_version: 1\n", encoding="utf-8")
        _, output, _ = _run("check", "--config", str(path))
        assert "\nError: forged" not in output

    def test_an_env_var_error_doesnt_blame_the_file(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("SYNOLOGY_PORT", "abc")
        path = tmp_path / "ok.yaml"
        path.write_text(f"{GOOD_HEAD}modules: {{filestation: }}\n", encoding="utf-8")
        _, output, _ = _run("check", "--config", str(path))
        assert "SYNOLOGY_PORT" in output
        assert "Config file:" not in output

    def test_synology_host_is_named_when_it_breaks_the_derived_id(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """QA round 2 on #532: the reader opened a file that looks fine."""
        monkeypatch.setenv("SYNOLOGY_HOST", "my_nas")
        path = tmp_path / "ok.yaml"
        path.write_text(f"{GOOD_HEAD}modules: {{filestation: }}\n", encoding="utf-8")
        _, output, _ = _run("check", "--config", str(path))
        assert "SYNOLOGY_HOST gives no usable instance_id" in output

    def test_the_files_host_is_named_when_it_breaks_the_derived_id(self, tmp_path: Path) -> None:
        path = tmp_path / "bad.yaml"
        path.write_text(
            "schema_version: 1\nconnection: {host: my_nas}\nmodules: {filestation: }\n",
            encoding="utf-8",
        )
        _, output, _ = _run("check", "--config", str(path))
        assert "connection.host gives no usable instance_id" in output


class TestLoadTimeLogsReachTheFile:
    """QA round 2 on #535: under an MCP client stderr may not be visible, and the security
    warnings logged while the config loads went only there."""

    def _config(self, tmp_path: Path, https: str = "false") -> tuple[Path, Path]:
        log = tmp_path / "server.log"
        path = tmp_path / "ok.yaml"
        path.write_text(
            f"schema_version: 1\nconnection: {{host: nas.local, https: {https}}}\n"
            f"logging: {{file: {log}}}\nmodules: {{filestation: }}\n",
            encoding="utf-8",
        )
        return path, log

    def _load(self, path: Path) -> None:
        logging_._init_early_logging()
        config = validate_config(path)
        logging_._configure_logging(config.logging.level, config.logging.file)

    def test_a_security_warning_reaches_the_file(self, tmp_path: Path) -> None:
        path, log = self._config(tmp_path)
        self._load(path)
        assert "HTTPS is disabled" in log.read_text(encoding="utf-8")

    def test_at_any_log_level(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("SYNOLOGY_LOG_LEVEL", "error")
        path, log = self._config(tmp_path)
        self._load(path)
        assert "HTTPS is disabled" in log.read_text(encoding="utf-8")

    def test_an_override_warning_reaches_the_file(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("SYNOLOGY_HOST", "127.0.0.2")
        path, log = self._config(tmp_path)
        self._load(path)
        assert "SYNOLOGY_HOST overrides connection.host" in log.read_text(encoding="utf-8")

    def test_the_source_line_follows_the_level(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        path, log = self._config(tmp_path)
        # `basicConfig`, which sets this in a real run, does nothing under pytest's handlers.
        logging.getLogger().setLevel(logging.INFO)
        self._load(path)
        assert "Config: " in log.read_text(encoding="utf-8")

        log.unlink()
        root = logging.getLogger()
        for handler in list(root.handlers):
            if isinstance(handler, logging.FileHandler):
                root.removeHandler(handler)
                handler.close()
        monkeypatch.setenv("SYNOLOGY_LOG_LEVEL", "error")
        root.setLevel(logging.ERROR)
        self._load(path)
        assert "Config: " not in log.read_text(encoding="utf-8")

    def test_each_record_reaches_the_file_once(self, tmp_path: Path) -> None:
        path, log = self._config(tmp_path)
        self._load(path)
        assert log.read_text(encoding="utf-8").count("HTTPS is disabled") == 1

    def test_the_buffer_is_capped_and_let_go(self, tmp_path: Path) -> None:
        logging_._init_early_logging()
        logger = logging.getLogger("mcp_synology.test")
        for i in range(logging_._EARLY_RECORDS_CAP + 5):
            logger.warning("record %d", i)
        early = logging_._early_records
        assert early is not None
        assert len(early.records) == logging_._EARLY_RECORDS_CAP
        logging_._configure_logging("info")
        assert logging_._early_records is None
        assert early not in logging.getLogger().handlers


class TestModuleSettings:
    def test_every_settings_schema_has_the_shared_rules(self) -> None:
        """A new module's settings get them, or this fails."""
        for name, module in _MODULE_REGISTRY.items():
            schema = module.MODULE_INFO.settings_schema
            if schema is not None:
                assert issubclass(schema, ModuleSettings), name

    def test_every_module_with_a_schema_is_validated(self) -> None:
        """validate_config covers each one (#511's module coverage test)."""
        for name, module in _MODULE_REGISTRY.items():
            schema = module.MODULE_INFO.settings_schema
            config = load_config_from({name: {"settings": {}}})
            settings = module_settings(config)
            assert name in settings
            if schema is None:
                assert settings[name] is None
            else:
                assert isinstance(settings[name], schema)

    def test_a_disabled_or_unknown_modules_settings_arent_read(self) -> None:
        config = load_config_from(
            {
                "system": {},
                "filestation": {"enabled": False, "settings": {"async_timeout": 5}},
                "newer_module": {"settings": {"async_timeout": 5}},
            }
        )
        assert set(module_settings(config)) == {"system"}

    def test_an_unknown_setting_stays_a_warning(self) -> None:
        config = load_config_from({"filestation": {"settings": {"delete_timout": 60}}})
        assert "filestation" in module_settings(config)

    def test_true_false_is_refused_in_every_number_setting(self) -> None:
        checked = 0
        for name, module in _MODULE_REGISTRY.items():
            schema = module.MODULE_INFO.settings_schema
            for field, info in (schema.model_fields if schema else {}).items():
                if "int" in str(info.annotation) or "float" in str(info.annotation):
                    config = load_config_from({name: {"settings": {field: True}}})
                    with pytest.raises(ValueError, match="not true or false"):
                        module_settings(config)
                    checked += 1
        assert checked >= 9  # File Station's timeouts and poll interval, Photos' timeout

    def test_a_real_boolean_setting_still_takes_true(self) -> None:
        config = load_config_from({"filestation": {"settings": {"hide_recycle_in_listings": True}}})
        assert module_settings(config)["filestation"].hide_recycle_in_listings is True  # type: ignore[union-attr]

    def test_a_shown_value_is_only_a_plain_one(self) -> None:
        config = load_config_from(
            {"filestation": {"settings": {"file_type_indicator": {"nested": SECRET}}}}
        )
        with pytest.raises(ValueError) as exc:
            module_settings(config)
        assert SECRET not in str(exc.value)


def load_config_from(modules: dict[str, object]) -> object:
    from mcp_synology.core.config import AppConfig

    return AppConfig.model_validate(
        {"schema_version": 1, "connection": {"host": "nas"}, "modules": modules}
    )


class TestOnePath:
    def test_the_cli_loads_a_config_only_through_validate_config(self) -> None:
        """A command that called load_config itself would accept what `serve` refuses."""
        cli = Path(mcp_synology.cli.__file__).parent
        callers = {}
        for path in cli.glob("*.py"):
            for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
                if isinstance(node, ast.Call):
                    name = getattr(node.func, "id", getattr(node.func, "attr", ""))
                    if name in ("load_config", "validate_config"):
                        callers.setdefault(name, set()).add(path.name)
        assert callers == {"validate_config": {"startup.py"}}

    def test_load_config_still_loads_a_good_file(self, tmp_path: Path) -> None:
        path = tmp_path / "ok.yaml"
        path.write_text(f"{GOOD_HEAD}modules: {{filestation: }}\n", encoding="utf-8")
        assert load_config(path).instance_id == "nas"
        assert validate_config(path).instance_id == "nas"


class TestEnvTable:
    def test_the_readme_lists_every_variable_and_only_those(self) -> None:
        readme = (REPO / "README.md").read_text(encoding="utf-8")
        table = set(re.findall(r"^\| `(SYNOLOGY_[A-Z_]+)` \|", readme, re.M))
        assert table == set(ENV_VAR_MAP)
