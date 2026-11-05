"""A stored credential is tied to its host (#536).

The keyring is a real in-memory backend, not a mock, so what `setup` writes is what a later
login reads. Files go through `load_config`, since which host may be recorded depends on how
the config was found.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, patch

import keyring
import pytest
from click.testing import CliRunner
from keyring.errors import KeyringError
from mcp.server.mcpserver.exceptions import ToolError

from mcp_synology.cli.main import main
from mcp_synology.cli.setup import _may_replace_entry, _store_keyring
from mcp_synology.core.auth import AuthManager, resolve_credentials
from mcp_synology.core.client import DsmClient
from mcp_synology.core.config import comparable_host, load_config
from mcp_synology.core.errors import AuthenticationError
from mcp_synology.core.formatting import synology_error_response
from tests.memory_keyring import MemoryKeyring


@pytest.fixture
def store(request: pytest.FixtureRequest, monkeypatch: pytest.MonkeyPatch) -> MemoryKeyring:
    backend = MemoryKeyring()
    previous = keyring.get_keyring()
    keyring.set_keyring(backend)
    request.addfinalizer(lambda: keyring.set_keyring(previous))
    for var in ("SYNOLOGY_HOST", "SYNOLOGY_INSTANCE_ID", "MCP_SYNOLOGY_CONFIG"):
        monkeypatch.delenv(var, raising=False)
    for var in ("SYNOLOGY_USERNAME", "SYNOLOGY_PASSWORD", "SYNOLOGY_DEVICE_ID"):
        monkeypatch.delenv(var, raising=False)
    return backend


def _entry(store: MemoryKeyring, instance: str, host: str | None = None) -> None:
    service = f"mcp-synology/{instance}"
    store.values[(service, "username")] = "admin"
    store.values[(service, "password")] = "hunter2"
    if host is not None:
        store.values[(service, "host")] = host


def _file(tmp_path: Path, host: str | None, instance: str | None = None) -> Path:
    lines = ["schema_version: 1"]
    if instance:
        lines.append(f"instance_id: {instance}")
    if host:
        lines.append(f"connection: {{host: {host}}}")
    lines.append("modules: {filestation: {}}")
    path = tmp_path / "named.yaml"
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return path


class TestAMismatchRefuses:
    def test_two_hosts_with_the_same_first_label(
        self, store: MemoryKeyring, tmp_path: Path
    ) -> None:
        """Route 1: no attacker and no variable. Each derives `nas`."""
        _entry(store, "nas", "nas.home")
        config = load_config(_file(tmp_path, "nas.office"))
        assert config.instance_id == "nas"
        with pytest.raises(AuthenticationError) as exc:
            resolve_credentials(config)
        assert "are for another host" in str(exc.value)
        assert "'nas.office'" in str(exc.value)
        assert "mcp-synology setup --config" in (exc.value.suggestion or "")
        # An `env` block's author mustn't learn the real NAS's name from the reply.
        assert "nas.home" not in str(exc.value) + (exc.value.suggestion or "")
        assert "hunter2" not in str(exc.value)

    def test_check_says_which_host_the_entry_is_for(
        self, store: MemoryKeyring, tmp_path: Path
    ) -> None:
        """The maintainer, on #536: it's someone at their own terminal, not the model."""
        _entry(store, "nas", "nas.home")
        with patch(
            "mcp_synology.core.client.DsmClient.query_api_info", AsyncMock(return_value=None)
        ):
            result = CliRunner().invoke(
                main, ["check", "--config", str(_file(tmp_path, "nas.office"))]
            )
        assert result.exit_code == 1
        assert "They were set up for 'nas.home'" in result.output
        assert "hunter2" not in result.output

    def test_check_quotes_a_recorded_host_that_could_forge_a_line(
        self, store: MemoryKeyring, tmp_path: Path
    ) -> None:
        _entry(store, "nas", "x\nError: forged")
        with patch(
            "mcp_synology.core.client.DsmClient.query_api_info", AsyncMock(return_value=None)
        ):
            result = CliRunner().invoke(
                main, ["check", "--config", str(_file(tmp_path, "nas.office"))]
            )
        assert "'x\\nError: forged'" in result.output

    def test_check_has_nothing_to_add_with_no_recorded_host(
        self, store: MemoryKeyring, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _entry(store, "nas")
        monkeypatch.setenv("SYNOLOGY_INSTANCE_ID", "nas")
        with patch(
            "mcp_synology.core.client.DsmClient.query_api_info", AsyncMock(return_value=None)
        ):
            result = CliRunner().invoke(
                main, ["check", "--config", str(_file(tmp_path, "nas.home"))]
            )
        assert "don't say which host" in result.output
        assert "They were set up for" not in result.output

    def test_a_tool_error_doesnt_name_the_recorded_host(
        self, store: MemoryKeyring, tmp_path: Path
    ) -> None:
        """What reaches the model: `recorded_host` stays out of the envelope."""
        _entry(store, "nas", "nas.home")
        with pytest.raises(AuthenticationError) as exc:
            resolve_credentials(load_config(_file(tmp_path, "nas.office")))
        with pytest.raises(ToolError) as tool:
            synology_error_response("List files", exc.value)
        assert "nas.home" not in str(tool.value)
        assert "nas.office" in str(tool.value)

    @pytest.mark.parametrize(
        ("env", "instance"),
        [
            ({"SYNOLOGY_HOST": "other.example", "SYNOLOGY_INSTANCE_ID": "home-nas"}, "home-nas"),
            ({"SYNOLOGY_HOST": "nas.other.example"}, "nas"),
        ],
        ids=["route 2, both variables", "route 3, the same first label"],
    )
    def test_a_host_from_a_variable_with_no_file(
        self,
        store: MemoryKeyring,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        env: dict[str, str],
        instance: str,
    ) -> None:
        monkeypatch.chdir(tmp_path)
        _entry(store, instance, "home-nas.lan")
        for name, value in env.items():
            monkeypatch.setenv(name, value)
        with pytest.raises(AuthenticationError, match="are for another host"):
            resolve_credentials(load_config())

    @pytest.mark.parametrize(
        "connection", ["", "connection: {https: true}\n"], ids=["no section", "with a section"]
    )
    def test_a_variable_over_a_file_that_names_the_entry(
        self,
        store: MemoryKeyring,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        connection: str,
    ) -> None:
        """Route 4, and QA's case on #535."""
        _entry(store, "home-nas", "home-nas.lan")
        path = tmp_path / "c.yaml"
        path.write_text(
            "schema_version: 1\ninstance_id: home-nas\n"
            f"{connection}modules: {{filestation: {{}}}}\n",
            encoding="utf-8",
        )
        monkeypatch.setenv("SYNOLOGY_HOST", "other.example")
        with pytest.raises(AuthenticationError, match="are for another host"):
            resolve_credentials(load_config(path))

    def test_a_file_named_by_the_variable(
        self, store: MemoryKeyring, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Route 5."""
        _entry(store, "home-nas", "home-nas.lan")
        monkeypatch.setenv("MCP_SYNOLOGY_CONFIG", str(_file(tmp_path, "other.example", "home-nas")))
        with pytest.raises(AuthenticationError, match="are for another host"):
            resolve_credentials(load_config())

    async def test_no_login_request_is_sent(self, store: MemoryKeyring, tmp_path: Path) -> None:
        _entry(store, "nas", "nas.home")
        config = load_config(_file(tmp_path, "nas.office"))
        client = DsmClient(base_url="http://nas.office:5000")
        client.request = AsyncMock()  # type: ignore[method-assign]
        with pytest.raises(AuthenticationError):
            await AuthManager(config, client).login()
        client.request.assert_not_called()

    @pytest.mark.parametrize("key", ["username", "password", "device_id"])
    def test_any_one_value_from_the_keyring_refuses(
        self, store: MemoryKeyring, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, key: str
    ) -> None:
        """A stale device token goes to DSM as well, so it's tied to the host too."""
        service = "mcp-synology/nas"
        store.values[(service, key)] = "from-keyring"
        store.values[(service, "host")] = "nas.home"
        for name, var in (("username", "USERNAME"), ("password", "PASSWORD")):
            if name != key:
                monkeypatch.setenv(f"SYNOLOGY_{var}", "from-env")
        with pytest.raises(AuthenticationError, match="are for another host"):
            resolve_credentials(load_config(_file(tmp_path, "nas.office")))

    @pytest.mark.parametrize("failing", ["get_password", "set_password"])
    def test_a_keyring_that_cant_answer_about_the_host_refuses(
        self, store: MemoryKeyring, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, failing: str
    ) -> None:
        """The read's own handler would otherwise carry on with what it had already read."""
        _entry(store, "nas")
        real = getattr(store, failing)

        def fails(service: str, key: str, *rest: str) -> Any:
            if key == "host":
                raise KeyringError("locked")
            return real(service, key, *rest)

        monkeypatch.setattr(store, failing, fails)
        with pytest.raises(AuthenticationError, match="Couldn't check which host"):
            resolve_credentials(load_config(_file(tmp_path, "nas.home")))


class TestWhatStillWorks:
    def test_the_recorded_host_logs_in(self, store: MemoryKeyring, tmp_path: Path) -> None:
        _entry(store, "nas", "nas.home")
        assert resolve_credentials(load_config(_file(tmp_path, "nas.home")))[:2] == (
            "admin",
            "hunter2",
        )

    @pytest.mark.parametrize(
        ("recorded", "host"),
        [("nas.home", "NAS.home"), ("xn--bcher-kva.example", "bücher.example")],
    )
    def test_another_spelling_of_the_host_is_the_same_host(
        self, store: MemoryKeyring, tmp_path: Path, recorded: str, host: str
    ) -> None:
        path = tmp_path / "c.yaml"
        path.write_text(
            f"schema_version: 1\ninstance_id: nas\nconnection: {{host: {host}}}\n"
            "modules: {filestation: {}}\n",
            encoding="utf-8",
        )
        _entry(store, "nas", recorded)
        assert resolve_credentials(load_config(path))[1] == "hunter2"

    def test_credentials_from_the_environment_need_no_record(
        self, store: MemoryKeyring, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        """The usual Docker shape, even beside another host's entry."""
        monkeypatch.chdir(tmp_path)
        _entry(store, "nas", "nas.home")
        monkeypatch.setenv("SYNOLOGY_HOST", "nas.office")
        monkeypatch.setenv("SYNOLOGY_USERNAME", "u")
        monkeypatch.setenv("SYNOLOGY_PASSWORD", "p")
        assert resolve_credentials(load_config()) == ("u", "p", None)

    def test_a_password_in_the_config_file_needs_no_record(
        self, store: MemoryKeyring, tmp_path: Path
    ) -> None:
        path = tmp_path / "c.yaml"
        path.write_text(
            "schema_version: 1\nconnection: {host: nas.office}\n"
            "auth: {username: u, password: p}\nmodules: {filestation: {}}\n",
            encoding="utf-8",
        )
        _entry(store, "other")
        assert resolve_credentials(load_config(path)) == ("u", "p", None)
        assert ("mcp-synology/nas", "host") not in store.values


class TestAnEntryWithNoRecordedHost:
    def test_named_with_config_records_it(self, store: MemoryKeyring, tmp_path: Path) -> None:
        _entry(store, "nas")
        assert resolve_credentials(load_config(_file(tmp_path, "NAS.home")))[1] == "hunter2"
        assert store.values[("mcp-synology/nas", "host")] == "nas.home"

    def test_the_default_location_records_it(
        self, store: MemoryKeyring, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        monkeypatch.chdir(tmp_path)
        home_config = Path.home() / ".config" / "mcp-synology" / "config.yaml"
        home_config.parent.mkdir(parents=True)
        home_config.write_text(
            "schema_version: 1\nconnection: {host: nas.home}\nmodules: {filestation: {}}\n",
            encoding="utf-8",
        )
        _entry(store, "nas")
        assert resolve_credentials(load_config())[1] == "hunter2"
        assert store.values[("mcp-synology/nas", "host")] == "nas.home"

    def test_then_another_host_is_refused(self, store: MemoryKeyring, tmp_path: Path) -> None:
        _entry(store, "nas")
        resolve_credentials(load_config(_file(tmp_path, "nas.home")))
        with pytest.raises(AuthenticationError, match="are for another host"):
            resolve_credentials(load_config(_file(tmp_path, "nas.office")))

    @pytest.mark.parametrize(
        "var",
        ["SYNOLOGY_HOST", "SYNOLOGY_INSTANCE_ID"],
    )
    def test_a_variable_doesnt_get_to_record_it(
        self, store: MemoryKeyring, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, var: str
    ) -> None:
        _entry(store, "nas")
        monkeypatch.setenv(var, "nas")
        with pytest.raises(AuthenticationError, match="don't say which host"):
            resolve_credentials(load_config(_file(tmp_path, "nas.home")))
        assert ("mcp-synology/nas", "host") not in store.values

    def test_a_file_named_by_the_variable_doesnt_record_it(
        self, store: MemoryKeyring, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _entry(store, "nas")
        monkeypatch.setenv("MCP_SYNOLOGY_CONFIG", str(_file(tmp_path, "nas.home")))
        with pytest.raises(AuthenticationError, match="don't say which host"):
            resolve_credentials(load_config())

    def test_env_only_doesnt_record_it(
        self, store: MemoryKeyring, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        monkeypatch.chdir(tmp_path)
        _entry(store, "nas")
        monkeypatch.setenv("SYNOLOGY_HOST", "nas.home")
        with pytest.raises(AuthenticationError, match="don't say which host"):
            resolve_credentials(load_config())


class TestSetup:
    def test_store_records_the_host_as_compared(self, store: MemoryKeyring) -> None:
        assert _store_keyring("mcp-synology/nas", "admin", "pw", "NAS.home")
        assert store.values[("mcp-synology/nas", "host")] == comparable_host("NAS.home")

    def test_the_host_is_written_after_the_password(self, store: MemoryKeyring) -> None:
        """Stopped partway, the old record over a new password refuses the next login."""
        order: list[str] = []
        real = store.set_password

        def recording(service: str, key: str, value: str) -> None:
            order.append(key)
            real(service, key, value)

        with patch.object(store, "set_password", recording):
            _store_keyring("mcp-synology/nas", "admin", "pw", "nas.home")
        assert order == ["username", "password", "host"]

    def test_another_hosts_device_token_is_dropped(self, store: MemoryKeyring) -> None:
        _entry(store, "nas", "nas.home")
        store.values[("mcp-synology/nas", "device_id")] = "token"
        _store_keyring("mcp-synology/nas", "admin", "pw", "nas.office")
        assert ("mcp-synology/nas", "device_id") not in store.values

    def test_the_same_hosts_device_token_stays(self, store: MemoryKeyring) -> None:
        _entry(store, "nas", "nas.home")
        store.values[("mcp-synology/nas", "device_id")] = "token"
        _store_keyring("mcp-synology/nas", "admin", "pw", "NAS.home")
        assert store.values[("mcp-synology/nas", "device_id")] == "token"

    @pytest.mark.parametrize(
        ("recorded", "stored", "asks"),
        [
            (None, False, False),
            ("nas.home", True, False),
            ("nas.office", True, True),
            (None, True, True),
        ],
        ids=["nothing stored", "the same host", "another host", "no host recorded"],
    )
    def test_when_it_asks(
        self, store: MemoryKeyring, recorded: str | None, stored: bool, asks: bool
    ) -> None:
        if stored:
            _entry(store, "nas", recorded)
        with patch("click.confirm", return_value=False) as confirm:
            assert _may_replace_entry("mcp-synology/nas", "NAS.home", "nas") is not asks
        assert confirm.called is asks

    def test_declining_for_a_config_writes_nothing(
        self, store: MemoryKeyring, tmp_path: Path
    ) -> None:
        _entry(store, "nas", "nas.home")
        before = dict(store.values)
        result = CliRunner().invoke(
            main, ["setup", "--config", str(_file(tmp_path, "nas.office"))], input="n\n"
        )
        assert "already holds credentials for another host" in result.output
        assert "DSM username" not in result.output
        assert store.values == before

    def test_declining_interactively_writes_nothing(
        self, store: MemoryKeyring, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.chdir(tmp_path)
        monkeypatch.setattr("mcp_synology.cli.setup._CONFIG_DIR", tmp_path / "configs")
        _entry(store, "nas", "nas.home")
        before = dict(store.values)
        result = CliRunner().invoke(main, ["setup"], input="nas.office\ny\n5001\nread\n\nn\nn\n")
        assert "already holds credentials for another host" in result.output
        assert "DSM username" not in result.output
        assert store.values == before
        assert not (tmp_path / "configs" / "nas.yaml").exists()

    def test_an_existing_config_is_asked_about_before_the_credentials(
        self, store: MemoryKeyring, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Declining used to leave the keyring already written."""
        monkeypatch.chdir(tmp_path)
        configs = tmp_path / "configs"
        configs.mkdir()
        (configs / "nas.yaml").write_text("x\n", encoding="utf-8")
        monkeypatch.setattr("mcp_synology.cli.setup._CONFIG_DIR", configs)
        result = CliRunner().invoke(main, ["setup"], input="nas.home\ny\n5001\nread\n\nn\nn\n")
        assert "already exists. Overwrite?" in result.output
        assert "DSM username" not in result.output
        assert store.values == {}
