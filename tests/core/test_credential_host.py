"""A stored credential is tied to its host (#536).

The keyring is a real in-memory backend, not a mock, so what `setup` writes is what a later
login reads. Files go through `load_config`, since which host may be recorded depends on how
the config was found.
"""

from __future__ import annotations

import asyncio
import importlib.util
import io
import os
import signal
import subprocess
import sys
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, patch

import httpx
import keyring
import pytest
import respx
from click.testing import CliRunner, Result
from keyring.errors import KeyringError, KeyringLocked, NoKeyringError, PasswordDeleteError
from mcp.server.mcpserver.exceptions import ToolError

from mcp_synology.cli.check import _asked_to_send, _stdin_is_a_terminal
from mcp_synology.cli.main import main
from mcp_synology.cli.setup import _attempt_login, _may_replace_entry, _store_keyring
from mcp_synology.core.auth import AuthManager, check_route, how_to_record, resolve_credentials
from mcp_synology.core.client import DsmClient
from mcp_synology.core.config import AppConfig, comparable_host, comparable_target, load_config
from mcp_synology.core.errors import AuthenticationError, CredentialHostError, SynologyError
from mcp_synology.core.formatting import synology_error_response
from tests.memory_keyring import MemoryKeyring


@pytest.fixture
def store(request: pytest.FixtureRequest, monkeypatch: pytest.MonkeyPatch) -> MemoryKeyring:
    backend = MemoryKeyring()
    previous = keyring.get_keyring()
    keyring.set_keyring(backend)
    request.addfinalizer(lambda: keyring.set_keyring(previous))
    for var in (
        "SYNOLOGY_HOST",
        "SYNOLOGY_PORT",
        "SYNOLOGY_HTTPS",
        "SYNOLOGY_INSTANCE_ID",
        "MCP_SYNOLOGY_CONFIG",
    ):
        monkeypatch.delenv(var, raising=False)
    for var in ("SYNOLOGY_USERNAME", "SYNOLOGY_PASSWORD", "SYNOLOGY_DEVICE_ID"):
        monkeypatch.delenv(var, raising=False)
    return backend


async def _logs_in(*args: Any, **_kwargs: Any) -> dict[str, Any]:
    """Stands in for `_setup_login` or `_connect_and_login` when DSM accepts the login."""
    store = next(arg for arg in args if callable(arg))
    return {"success": True, "keyring_ok": store(None)}


def _record(host: str, port: int = 5000) -> str:
    """As an entry records it, for a config with no `port` and no `https` (5000)."""
    return f"{host}:{port}" if host.strip() else host


def _entry(store: MemoryKeyring, instance: str, host: str | None = None) -> None:
    service = f"mcp-synology/{instance}"
    store.values[(service, "username")] = "admin"
    store.values[(service, "password")] = "hunter2"
    if host is not None:
        store.values[(service, "host")] = _record(host)


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
        assert "They were set up for 'nas.home:5000'" in result.output
        assert "hunter2" not in result.output

    def test_check_quotes_a_recorded_host_that_could_forge_a_line(
        self, store: MemoryKeyring, tmp_path: Path
    ) -> None:
        store.values[("mcp-synology/nas", "username")] = "admin"
        store.values[("mcp-synology/nas", "password")] = "hunter2"
        store.values[("mcp-synology/nas", "host")] = "x\nError: forged"
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

    @pytest.mark.parametrize("key", ["username", "password"])
    def test_a_username_or_password_alone_from_the_keyring_refuses(
        self, store: MemoryKeyring, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, key: str
    ) -> None:
        service = "mcp-synology/nas"
        store.values[(service, key)] = "from-keyring"
        store.values[(service, "host")] = "nas.home"
        for name, var in (("username", "USERNAME"), ("password", "PASSWORD")):
            if name != key:
                monkeypatch.setenv(f"SYNOLOGY_{var}", "from-env")
        with pytest.raises(AuthenticationError, match="are for another host"):
            resolve_credentials(load_config(_file(tmp_path, "nas.office")))

    @pytest.mark.parametrize(
        "failure",
        [KeyringError("locked"), RuntimeError("a backend's own type")],
        ids=["keyring error", "a backend's own type"],
    )
    def test_a_keyring_that_cant_answer_about_the_host_refuses(
        self,
        store: MemoryKeyring,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        failure: Exception,
    ) -> None:
        """Its own message, not "no credentials found", which the read's handler would give.
        QA round 1 on #559: a backend's own type escaped as itself, with a traceback."""
        _entry(store, "nas")
        real = store.get_password

        def fails(service: str, key: str) -> Any:
            if key == "host":
                raise failure
            return real(service, key)

        monkeypatch.setattr(store, "get_password", fails)
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


def _check(path: Path, answer: str = "y\n") -> Result:
    """`mcp-synology check` against a stub DSM that accepts the login."""
    with (
        patch("mcp_synology.core.client.DsmClient.query_api_info", AsyncMock(return_value=None)),
        patch("mcp_synology.core.client.DsmClient.request", AsyncMock(return_value={"sid": "s"})),
        patch("mcp_synology.cli.check._stdin_is_a_terminal", return_value=True),
    ):
        result = CliRunner().invoke(main, ["check", "--config", str(path)], input=answer)
    assert result.exit_code == 0, result.output
    return result


def _log_in(config: AppConfig, error: Exception | None = None) -> AuthManager:
    """One login against a stub DSM that answers with a session, or raises ``error``."""
    client = DsmClient(base_url="http://nas:5000")
    auth = AuthManager(config, client)
    with patch.object(client, "request", AsyncMock(return_value={"sid": "s"}, side_effect=error)):
        asyncio.run(auth.login())
    return auth


class TestAnEntryWithNoRecordedHost:
    def test_named_with_config_records_it(self, store: MemoryKeyring, tmp_path: Path) -> None:
        _entry(store, "nas")
        auth = _log_in(load_config(_file(tmp_path, "NAS.home")))
        assert store.values[("mcp-synology/nas", "host")] == "nas.home:5000"
        assert auth.recorded_host == "nas.home:5000"

    def test_finding_the_credentials_records_nothing(
        self, store: MemoryKeyring, tmp_path: Path
    ) -> None:
        _entry(store, "nas")
        assert resolve_credentials(load_config(_file(tmp_path, "nas.home")))[1] == "hunter2"
        assert ("mcp-synology/nas", "host") not in store.values

    @pytest.mark.parametrize(
        "error",
        [
            AuthenticationError("wrong password", code=400),
            AuthenticationError("disabled", code=401),
            httpx.ConnectError("refused"),
        ],
        ids=["wrong password", "disabled", "host down"],
    )
    def test_a_login_dsm_doesnt_accept_leaves_no_record(
        self, store: MemoryKeyring, tmp_path: Path, error: Exception
    ) -> None:
        """Pre-QA on #559: another NAS refusing the password pinned the entry to it, and the real
        NAS's config was then refused, with `check` saying the entry was "set up for" the other."""
        _entry(store, "nas")
        (tmp_path / "other").mkdir()
        with pytest.raises(type(error)):
            _log_in(load_config(_file(tmp_path / "other", "nas.office")), error)
        assert ("mcp-synology/nas", "host") not in store.values
        _log_in(load_config(_file(tmp_path, "nas.home")))
        assert store.values[("mcp-synology/nas", "host")] == "nas.home:5000"

    def test_a_remembered_refusal_leaves_no_record(
        self, store: MemoryKeyring, tmp_path: Path
    ) -> None:
        """A refusal DSM isn't asked about again, in the same process."""
        _entry(store, "nas")
        config = load_config(_file(tmp_path, "nas.office"))
        client = DsmClient(base_url="http://nas:5000")
        auth = AuthManager(config, client)
        refused = AsyncMock(side_effect=AuthenticationError("wrong password", code=400))
        with patch.object(client, "request", refused):
            for _ in range(2):
                with pytest.raises(AuthenticationError):
                    asyncio.run(auth.login())
        assert refused.await_count == 1
        assert ("mcp-synology/nas", "host") not in store.values

    @pytest.mark.parametrize("code", [403, 404, 406])
    def test_dsm_asking_for_a_second_factor_leaves_no_record(
        self, store: MemoryKeyring, tmp_path: Path, code: int
    ) -> None:
        """Pre-QA on #559: that it asks only once the password is right has no capture behind it,
        and a false record pins the wrong host. `setup` is what these accounts run next, and it
        records."""
        _entry(store, "nas")
        with pytest.raises(AuthenticationError):
            _log_in(
                load_config(_file(tmp_path, "nas.home")),
                AuthenticationError("2FA", code=code),
            )
        assert ("mcp-synology/nas", "host") not in store.values

    def test_a_config_password_gets_advice_that_works(
        self, store: MemoryKeyring, tmp_path: Path
    ) -> None:
        """Pre-QA on #559: a refusal from `check` said to run `check`, forever, as a login using
        the file's password can't record."""
        _entry(store, "nas")
        path = tmp_path / "c.yaml"
        path.write_text(
            "schema_version: 1\nconnection: {host: nas.home}\nauth: {password: p}\n"
            "modules: {filestation: {}}\n",
            encoding="utf-8",
        )
        with pytest.raises(CredentialHostError) as exc:
            resolve_credentials(load_config(path))
        assert "remove auth.password from the config and run" in (
            exc.value.terminal_suggestion or ""
        )

    def test_a_username_with_no_stored_password_gets_only_setup(
        self, store: MemoryKeyring, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """`check` at a terminal would find no password to record with."""
        store.values[("mcp-synology/nas", "username")] = "admin"
        monkeypatch.setenv("SYNOLOGY_PASSWORD", "p")
        with pytest.raises(AuthenticationError) as exc:
            resolve_credentials(load_config(_file(tmp_path, "nas.home")))
        assert "mcp-synology check" not in (exc.value.suggestion or "")
        assert "mcp-synology setup" in (exc.value.suggestion or "")

    def test_a_config_password_doesnt_read_as_the_variables(
        self, store: MemoryKeyring, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The variable's value lands in `auth.password`, but there's nothing in the file to
        remove."""
        _entry(store, "nas")
        monkeypatch.setenv("SYNOLOGY_PASSWORD", "p")
        assert "remove auth.password" not in how_to_record(load_config(_file(tmp_path, "nas.home")))

    def test_a_reply_with_no_session_leaves_no_record(
        self, store: MemoryKeyring, tmp_path: Path
    ) -> None:
        """Pre-QA on #559: only a session shows DSM accepted the password."""
        _entry(store, "nas")
        client = DsmClient(base_url="http://nas:5000")
        auth = AuthManager(load_config(_file(tmp_path, "nas.home")), client)
        with (
            patch.object(client, "request", AsyncMock(return_value={})),
            pytest.raises(AuthenticationError),
        ):
            asyncio.run(auth.login())
        assert ("mcp-synology/nas", "host") not in store.values

    def test_check_mentions_an_entry_holding_only_a_token(
        self, store: MemoryKeyring, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        store.values[("mcp-synology/nas", "device_id")] = "token"
        monkeypatch.setenv("SYNOLOGY_USERNAME", "u")
        monkeypatch.setenv("SYNOLOGY_PASSWORD", "p")
        assert "still has no recorded host" in _check(_file(tmp_path, "nas.home")).output

    def test_a_token_with_no_credentials_gets_no_token_warning(
        self, store: MemoryKeyring, tmp_path: Path, caplog: pytest.LogCaptureFixture
    ) -> None:
        store.values[("mcp-synology/nas", "device_id")] = "token"
        with caplog.at_level("WARNING"), pytest.raises(AuthenticationError, match="No credentials"):
            resolve_credentials(load_config(_file(tmp_path, "nas.home")))
        assert not any("Not sending the device token" in m for m in caplog.messages)

    def test_a_token_that_cant_be_stored_says_so(
        self,
        store: MemoryKeyring,
        monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        """Pre-QA on #559: a traceback, and the session was never logged out. #454: the login
        writes nothing, so the store says so."""
        real_set = store.set_password

        def fails(service: str, key: str, value: str) -> None:
            if key == "device_id":
                raise RuntimeError("a backend's own type")
            real_set(service, key, value)

        monkeypatch.setattr(store, "set_password", fails)
        client = AsyncMock()
        client.request = AsyncMock(
            side_effect=[SynologyError("2FA", code=403), {"sid": "s", "did": "token"}, {}]
        )
        with patch("click.prompt", return_value="123456"):
            result = asyncio.run(_attempt_login(client, "admin", "pw"))
        assert result["success"]
        assert result["device_id"] == "token"
        assert ("mcp-synology/nas", "device_id") not in store.values
        with pytest.raises(SystemExit) as stopped:
            _store_keyring("mcp-synology/nas", "admin", "pw", "nas", 5000, "token")
        assert stopped.value.code == 1
        out = capsys.readouterr().err
        assert "2FA device token couldn't be stored" in out
        assert "2FA bootstrap complete" not in out

    def test_a_host_recorded_meanwhile_stays(self, store: MemoryKeyring, tmp_path: Path) -> None:
        """A second process got there during this login's round trip."""
        _entry(store, "nas")
        config = load_config(_file(tmp_path, "nas.office"))
        client = DsmClient(base_url="http://nas:5000")
        auth = AuthManager(config, client)

        async def other_process_records(*_args: Any, **_kwargs: Any) -> dict[str, str]:
            store.values[("mcp-synology/nas", "host")] = "nas.home:5000"
            return {"sid": "s"}

        with patch.object(client, "request", other_process_records):
            asyncio.run(auth.login())
        assert store.values[("mcp-synology/nas", "host")] == "nas.home:5000"
        assert auth.recorded_host is None

    def test_a_record_that_cant_be_written_doesnt_undo_the_login(
        self,
        store: MemoryKeyring,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        """QA round 1 on #559: a backend's own type. The password has gone by then, so the next
        login tries again."""
        _entry(store, "nas")

        def fails(_service: str, _key: str, _value: str) -> None:
            raise RuntimeError("a backend's own type")

        monkeypatch.setattr(store, "set_password", fails)
        with caplog.at_level("WARNING"):
            auth = _log_in(load_config(_file(tmp_path, "nas.home")))
        assert auth.recorded_host is None
        assert any("Couldn't record this host" in m for m in caplog.messages)

    def test_a_username_alone_doesnt_settle_whose_entry_it_is(
        self, store: MemoryKeyring, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Pre-QA on #559: with SYNOLOGY_PASSWORD set, a login using none of the entry's password
        pinned it."""
        _entry(store, "nas")
        monkeypatch.setenv("SYNOLOGY_PASSWORD", "p")
        with pytest.raises(AuthenticationError, match="don't say which host"):
            _log_in(load_config(_file(tmp_path, "nas.home")))
        assert ("mcp-synology/nas", "host") not in store.values

    def test_a_username_with_no_password_anywhere_says_so(
        self, store: MemoryKeyring, tmp_path: Path
    ) -> None:
        """Pre-QA on #559: nothing is sent, so it isn't a host question, and nothing's recorded."""
        store.values[("mcp-synology/nas", "username")] = "admin"
        with pytest.raises(AuthenticationError, match="No credentials found"):
            _log_in(load_config(_file(tmp_path, "nas.home")))
        assert ("mcp-synology/nas", "host") not in store.values

    def test_check_says_it_recorded_it(self, store: MemoryKeyring, tmp_path: Path) -> None:
        """Pre-QA on #559: the upgrade advice sends people to `check`, which gave no sign."""
        _entry(store, "nas")
        result = _check(_file(tmp_path, "nas.home"))
        assert "is now recorded for nas.home:5000" in result.output

    @pytest.mark.parametrize("answer", ["n\n", "\n", ""], ids=["no", "the default", "no person"])
    def test_check_sends_nothing_unless_told_yes(
        self, store: MemoryKeyring, tmp_path: Path, answer: str
    ) -> None:
        """QA round 3 on #559: on a full path it sent the stored password and token and pinned
        the entry, with none of the advice's question shown (maintainer, 2026-11-05: ask first)."""
        _entry(store, "nas")
        request = AsyncMock(return_value={"sid": "s"})
        with (
            patch(
                "mcp_synology.core.client.DsmClient.query_api_info", AsyncMock(return_value=None)
            ),
            patch("mcp_synology.core.client.DsmClient.request", request),
            patch("mcp_synology.cli.check._stdin_is_a_terminal", return_value=True),
        ):
            result = CliRunner().invoke(
                main, ["check", "--config", str(_file(tmp_path, "nas.office"))], input=answer
            )
        assert result.exit_code == 1
        assert "and any 2FA token to 'nas.office:5000' over plain HTTP" in result.output
        assert "Nothing was sent" in result.output
        request.assert_not_awaited()
        assert ("mcp-synology/nas", "host") not in store.values

    def test_a_pipe_answering_yes_sends_nothing(self, store: MemoryKeyring, tmp_path: Path) -> None:
        """Pre-QA on #559: `printf 'y' | check` sent it, and a pipe left open waited forever."""
        _entry(store, "nas")
        request = AsyncMock(return_value={"sid": "s"})
        with (
            patch(
                "mcp_synology.core.client.DsmClient.query_api_info", AsyncMock(return_value=None)
            ),
            patch("mcp_synology.core.client.DsmClient.request", request),
        ):
            result = CliRunner().invoke(
                main, ["check", "--config", str(_file(tmp_path, "nas.office"))], input="y\n"
            )
        assert result.exit_code == 1
        assert "this needs someone at a terminal to answer" in result.stderr
        assert "Nothing was sent" in result.output
        request.assert_not_awaited()

    @pytest.mark.parametrize("stdin", [None, "closed"], ids=["none", "closed"])
    def test_stdin_closed_is_no_terminal(
        self, monkeypatch: pytest.MonkeyPatch, stdin: str | None
    ) -> None:
        """QA round 4 on #559: `check <&-`, as under cron, was a traceback."""
        handle = None
        if stdin:
            handle = io.StringIO()
            handle.close()
        monkeypatch.setattr(sys, "stdin", handle)
        assert _stdin_is_a_terminal() is False

    def test_the_question_goes_to_stderr(self, store: MemoryKeyring, tmp_path: Path) -> None:
        """QA round 4 on #559: `check > log` must still show it."""
        _entry(store, "nas")
        with (
            patch(
                "mcp_synology.core.client.DsmClient.query_api_info", AsyncMock(return_value=None)
            ),
            patch("mcp_synology.cli.check._stdin_is_a_terminal", return_value=True),
        ):
            result = CliRunner().invoke(
                main, ["check", "--config", str(_file(tmp_path, "nas.office"))], input="n\n"
            )
        assert "Send its stored password" in result.stderr
        assert "Send its stored password" not in result.stdout

    def test_the_question_puts_back_the_interrupt_handler(
        self, store: MemoryKeyring, tmp_path: Path
    ) -> None:
        """Pre-QA on #559: asyncio's own handler cancels the login cleanly."""
        _entry(store, "nas")
        config = load_config(_file(tmp_path, "nas.office"))

        def handler(*_: object) -> None:
            """Not Python's default, which is what the question sets."""

        before = signal.signal(signal.SIGINT, handler)
        try:
            with (
                patch("mcp_synology.cli.check._stdin_is_a_terminal", return_value=True),
                patch("click.confirm", return_value=True),
            ):
                assert _asked_to_send(config, "nas") is True
            assert signal.getsignal(signal.SIGINT) is handler
        finally:
            signal.signal(signal.SIGINT, before)

    def test_a_keyring_that_fails_the_first_read_cant_skip_the_question(
        self, store: MemoryKeyring, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Pre-QA on #559: the question's own read failed, and the login's read then recorded.
        Secret Service does this when someone dismisses its unlock dialog."""
        _entry(store, "nas")
        real = store.get_password
        calls = iter([KeyringLocked("dismissed")])

        def fails_once(service: str, key: str) -> str | None:
            for error in calls:
                raise error
            return real(service, key)

        monkeypatch.setattr(store, "get_password", fails_once)
        request = AsyncMock(return_value={"sid": "s"})
        with (
            patch(
                "mcp_synology.core.client.DsmClient.query_api_info", AsyncMock(return_value=None)
            ),
            patch("mcp_synology.core.client.DsmClient.request", request),
        ):
            result = CliRunner().invoke(
                main, ["check", "--config", str(_file(tmp_path, "nas.office"))]
            )
        assert result.exit_code == 1
        request.assert_not_awaited()
        assert ("mcp-synology/nas", "host") not in store.values

    def test_one_ctrl_c_leaves_the_question(self, store: MemoryKeyring, tmp_path: Path) -> None:
        """Pre-QA on #559: asyncio holds Ctrl-C back from a blocking prompt until the second."""
        _entry(store, "nas")
        seen: list[object] = []

        def confirm(*_args: object, **_kwargs: object) -> bool:
            seen.append(signal.getsignal(signal.SIGINT))
            return False

        with (
            patch(
                "mcp_synology.core.client.DsmClient.query_api_info", AsyncMock(return_value=None)
            ),
            patch("mcp_synology.cli.check._stdin_is_a_terminal", return_value=True),
            patch("click.confirm", confirm),
        ):
            CliRunner().invoke(main, ["check", "--config", str(_file(tmp_path, "nas.office"))])
        assert seen == [signal.default_int_handler]

    def test_a_password_with_no_username_isnt_asked_about(
        self, store: MemoryKeyring, tmp_path: Path
    ) -> None:
        """A login that can't work doesn't need the question, and with none asked, it can't
        record: the same refusal as a read that failed."""
        store.values[("mcp-synology/nas", "password")] = "hunter2"
        with (
            patch(
                "mcp_synology.core.client.DsmClient.query_api_info", AsyncMock(return_value=None)
            ),
            patch("mcp_synology.cli.check._stdin_is_a_terminal", return_value=True),
        ):
            result = CliRunner().invoke(
                main, ["check", "--config", str(_file(tmp_path, "nas.home"))]
            )
        assert result.exit_code == 1
        assert "don't say which host" in result.output
        assert "Send its stored password" not in result.output

    def test_check_doesnt_ask_about_a_recorded_entry(
        self, store: MemoryKeyring, tmp_path: Path
    ) -> None:
        _entry(store, "nas", "nas.home")
        assert "Send its stored password" not in _check(_file(tmp_path, "nas.home"), "").output

    def test_check_doesnt_ask_when_the_login_wont_use_the_entry(
        self, store: MemoryKeyring, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _entry(store, "nas")
        monkeypatch.setenv("SYNOLOGY_PASSWORD", "p")
        monkeypatch.setenv("SYNOLOGY_USERNAME", "u")
        assert "Send its stored password" not in _check(_file(tmp_path, "nas.home"), "").output

    def test_check_says_when_it_didnt_use_the_entry(
        self, store: MemoryKeyring, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Pre-QA on #559: "Authentication successful!" and nothing recorded, so the server stayed
        refused."""
        _entry(store, "nas")
        monkeypatch.setenv("SYNOLOGY_USERNAME", "u")
        monkeypatch.setenv("SYNOLOGY_PASSWORD", "p")
        result = _check(_file(tmp_path, "nas.home"))
        assert "still has no recorded host" in result.output
        assert ("mcp-synology/nas", "host") not in store.values

    def test_check_has_nothing_to_say_about_a_recorded_entry(
        self, store: MemoryKeyring, tmp_path: Path
    ) -> None:
        _entry(store, "nas", "nas.home")
        result = _check(_file(tmp_path, "nas.home"))
        assert "recorded" not in result.output

    def test_the_default_location_doesnt_record_it(
        self, store: MemoryKeyring, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        """Pre-QA on #536: it's found through HOME, which an `env` block can set, so a planted
        `config.yaml` chose the host and got the password."""
        monkeypatch.chdir(tmp_path)
        home_config = Path.home() / ".config" / "mcp-synology" / "config.yaml"
        home_config.parent.mkdir(parents=True)
        home_config.write_text(
            "schema_version: 1\nconnection: {host: nas.home}\nmodules: {filestation: {}}\n",
            encoding="utf-8",
        )
        _entry(store, "nas")
        with pytest.raises(AuthenticationError, match="don't say which host"):
            resolve_credentials(load_config())
        assert ("mcp-synology/nas", "host") not in store.values

    def test_the_default_location_logs_in_once_recorded(
        self, store: MemoryKeyring, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        monkeypatch.chdir(tmp_path)
        home_config = Path.home() / ".config" / "mcp-synology" / "config.yaml"
        home_config.parent.mkdir(parents=True)
        home_config.write_text(
            "schema_version: 1\nconnection: {host: nas.home}\nmodules: {filestation: {}}\n",
            encoding="utf-8",
        )
        _entry(store, "nas", "nas.home")
        assert resolve_credentials(load_config())[1] == "hunter2"

    def test_a_config_value_expanded_through_home_doesnt_record_it(
        self, store: MemoryKeyring, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        """Pre-QA on #536: `--config ~/...` in a client's args, with only HOME set in `env`."""
        monkeypatch.setenv("HOME", str(tmp_path))
        monkeypatch.setenv("USERPROFILE", str(tmp_path))
        _file(tmp_path, "nas.home")
        _entry(store, "nas")
        with pytest.raises(AuthenticationError, match="don't say which host"):
            resolve_credentials(load_config("~/named.yaml"))
        assert ("mcp-synology/nas", "host") not in store.values

    @pytest.mark.parametrize("value", ["./named.yaml", "named.yaml", "sub/../named.yaml", "./~/n"])
    def test_a_relative_config_doesnt_record_it(
        self, store: MemoryKeyring, monkeypatch: pytest.MonkeyPatch, tmp_path: Path, value: str
    ) -> None:
        """QA round 1 on #559: the client's working folder chose the file, as in a cloned repo,
        and the password went to the host it named. The route #555 closed."""
        monkeypatch.chdir(tmp_path)
        monkeypatch.setenv("HOME", str(tmp_path))
        monkeypatch.setenv("USERPROFILE", str(tmp_path))
        (tmp_path / "sub").mkdir()
        _file(tmp_path, "nas.home")
        (tmp_path / "n").write_text((tmp_path / "named.yaml").read_text(), encoding="utf-8")
        _entry(store, "nas")
        with pytest.raises(AuthenticationError, match="don't say which host"):
            resolve_credentials(load_config(value))
        assert ("mcp-synology/nas", "host") not in store.values

    def test_the_refusal_doesnt_spell_out_the_first_use_conditions(
        self, store: MemoryKeyring, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """QA round 1 on #559: a model that can edit a client's config could follow them."""
        _entry(store, "nas")
        monkeypatch.setenv("SYNOLOGY_INSTANCE_ID", "nas")
        with pytest.raises(AuthenticationError) as exc:
            resolve_credentials(load_config(_file(tmp_path, "nas.home")))
        assert "starting with" not in str(exc.value)
        assert "only a config" not in str(exc.value)

    def test_the_advice_for_an_old_entry_keeps_the_device_token(
        self, store: MemoryKeyring, tmp_path: Path
    ) -> None:
        """QA round 1 on #559: `setup` removes the token, and `check` records the host without
        that. Refused here by a relative `--config`, which is the case that advice is for."""
        _entry(store, "nas")
        config = load_config(_file(tmp_path, "nas.home"))
        config._host_may_be_recorded = False
        with pytest.raises(CredentialHostError) as exc:
            resolve_credentials(config)
        advice = exc.value.terminal_suggestion or ""
        assert "'mcp-synology check --config <full path to your config file>'" in advice
        assert "keeps any 2FA token" in advice
        assert "mcp-synology setup --config" in advice

    def test_the_advice_asks_whose_credentials_these_are(
        self, store: MemoryKeyring, tmp_path: Path
    ) -> None:
        """QA round 2 on #559: `nas.office` is the right NAS for its own config while the entry
        holds `nas.home`'s password, and following "if this is the right NAS" sent it there."""
        _entry(store, "nas")
        config = load_config(_file(tmp_path, "nas.office"))
        config._host_may_be_recorded = False
        with pytest.raises(CredentialHostError) as exc:
            resolve_credentials(config)
        advice = exc.value.terminal_suggestion or ""
        route = advice.index("mcp-synology check")
        assert advice.index("If you know these are the credentials for 'nas.office:5000'") < route
        assert "It asks before sending the stored password there" in advice
        assert "Otherwise, give this NAS its own instance_id in the config, then run" in advice
        assert "leaves this entry alone" in advice
        assert "If it's the wrong NAS, check connection.host" in advice

    def test_a_tool_reply_names_no_command_that_records_it(
        self, store: MemoryKeyring, tmp_path: Path
    ) -> None:
        """QA round 2 on #559: `check` needs no person, and the reply for a config in a folder the
        model was pointed at told it to name that file by its full path."""
        _entry(store, "nas")
        config = load_config(_file(tmp_path, "nas.home"))
        config._host_may_be_recorded = False
        with pytest.raises(CredentialHostError) as exc:
            resolve_credentials(config)
        with pytest.raises(ToolError) as tool:
            synology_error_response("List files", exc.value)
        reply = str(tool.value)
        assert "mcp-synology check" not in reply
        assert "full path" not in reply
        assert "ask the user to give it its own instance_id in the config, then run" in reply
        assert "https://github.com/cmeans/mcp-synology/blob/main/docs/credentials.md#" in reply

    def test_check_gives_the_terminal_advice(
        self, store: MemoryKeyring, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _entry(store, "nas")
        monkeypatch.chdir(tmp_path)
        _file(tmp_path, "nas.home")
        with patch(
            "mcp_synology.core.client.DsmClient.query_api_info", AsyncMock(return_value=None)
        ):
            result = CliRunner().invoke(main, ["check", "--config", "named.yaml"])
        assert result.exit_code == 1
        assert "If you know these are the credentials for 'nas.home:5000'" in result.output
        assert "mcp-synology check --config <full path" in result.output

    def test_a_recorded_mismatch_separates_an_old_address_from_another_nas(
        self, store: MemoryKeyring, tmp_path: Path
    ) -> None:
        """Pre-QA on #559: "run setup" alone replaces the other NAS's entry, and its token."""
        _entry(store, "nas", "nas.home")
        with pytest.raises(CredentialHostError) as exc:
            resolve_credentials(load_config(_file(tmp_path, "nas.office")))
        advice = exc.value.terminal_suggestion or ""
        assert "this NAS's old address, run 'mcp-synology setup --config" in advice
        assert "and replace the entry" in advice
        assert "If it's another NAS, give this one its own instance_id" in advice
        assert "nas.home" not in advice  # `check` prints it before this, and logs share the text
        assert "mcp-synology check" not in advice

    @pytest.mark.parametrize(
        ("variables", "named"),
        [
            ({"SYNOLOGY_HTTPS": "true"}, "the same SYNOLOGY_HTTPS and no SYNOLOGY_PASSWORD"),
            ({}, "at a terminal, with no SYNOLOGY_PASSWORD"),
        ],
    )
    def test_the_check_route_keeps_the_connection(
        self,
        store: MemoryKeyring,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        variables: dict[str, str],
        named: str,
    ) -> None:
        """Pre-QA on #559: with SYNOLOGY_HTTPS left out at the terminal, the stored password went
        over plain HTTP."""
        _entry(store, "nas")
        for variable, value in variables.items():
            monkeypatch.setenv(variable, value)
        config = load_config(_file(tmp_path, "nas.home"))
        assert named in (check_route(config) or "")

    def test_changing_the_instance_id_doesnt_keep_the_variable(
        self, store: MemoryKeyring, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """ "Its own instance_id" with "the same SYNOLOGY_INSTANCE_ID" would contradict itself."""
        _entry(store, "nas")
        monkeypatch.setenv("SYNOLOGY_INSTANCE_ID", "nas")
        with pytest.raises(CredentialHostError) as exc:
            resolve_credentials(load_config(_file(tmp_path, "nas.home")))
        assert "the same SYNOLOGY_INSTANCE_ID" not in (exc.value.terminal_suggestion or "")
        # At a terminal, without it, `setup` would ask about this same entry (pre-QA on #559).
        assert "with that SYNOLOGY_INSTANCE_ID" in (exc.value.terminal_suggestion or "")
        assert "Give this NAS its own instance_id with SYNOLOGY_INSTANCE_ID" in (
            exc.value.terminal_suggestion or ""
        )

    def test_declining_says_how_with_the_instance_variable_set(
        self,
        store: MemoryKeyring,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        """Pre-QA on #559: "in a config file" is overridden by the variable, so following it asked
        about the same entry again."""
        _entry(store, "nas", "nas.home")
        monkeypatch.setenv("SYNOLOGY_INSTANCE_ID", "nas")
        config = load_config(_file(tmp_path, "nas.office"))
        with patch("click.confirm", return_value=False):
            _may_replace_entry("mcp-synology/nas", "nas.office", 5000, "nas", config)
        assert "its own instance_id with SYNOLOGY_INSTANCE_ID" in capsys.readouterr().out

    def test_declining_with_no_route_doesnt_say_keep_both(
        self,
        store: MemoryKeyring,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        """Pre-QA on #559: wrong when both are the same NAS."""
        _entry(store, "nas")
        monkeypatch.setenv("SYNOLOGY_HOST", "nas.office")
        config = load_config(_file(tmp_path, "nas.office"))
        with patch("click.confirm", return_value=False):
            _may_replace_entry("mcp-synology/nas", "nas.office", 5000, "nas", config)
        out = capsys.readouterr().out
        assert "To keep both" not in out
        assert "run this again and answer yes" in out
        assert "If they're another NAS's, give this NAS its own instance_id" in out

    @pytest.mark.parametrize(
        ("variables", "named"),
        [
            (["SYNOLOGY_HOST"], "the same SYNOLOGY_HOST"),
            (["SYNOLOGY_INSTANCE_ID"], "its own instance_id with SYNOLOGY_INSTANCE_ID"),
            (
                ["SYNOLOGY_HOST", "SYNOLOGY_INSTANCE_ID"],
                "with that SYNOLOGY_INSTANCE_ID and the same SYNOLOGY_HOST, which asks",
            ),
        ],
    )
    def test_the_advice_with_a_variable_says_setup_needs_it_too(
        self,
        store: MemoryKeyring,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        variables: list[str],
        named: str,
    ) -> None:
        """QA round 1 on #559: `setup` at a terminal stored for the file's host or instance, and
        the server was refused again with the same text. `check` wouldn't record it there either,
        so it isn't offered."""
        _entry(store, "nas")
        for variable in variables:
            monkeypatch.setenv(variable, "nas")
        with pytest.raises(AuthenticationError) as exc:
            resolve_credentials(load_config(_file(tmp_path, "nas.home")))
        suggestion = exc.value.suggestion or ""
        assert named in suggestion
        assert "mcp-synology check" not in suggestion

    @pytest.mark.parametrize("variable", ["SYNOLOGY_PORT", "SYNOLOGY_HTTPS"])
    def test_the_advice_keeps_the_rest_of_the_connection(
        self, store: MemoryKeyring, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, variable: str
    ) -> None:
        """QA round 2 on #559: `setup` with the host alone couldn't connect."""
        monkeypatch.chdir(tmp_path)
        _entry(store, "nas")
        monkeypatch.setenv("SYNOLOGY_HOST", "nas.home")
        monkeypatch.setenv(variable, "5001" if variable == "SYNOLOGY_PORT" else "true")
        with pytest.raises(AuthenticationError) as exc:
            resolve_credentials(load_config())
        assert f"the same SYNOLOGY_HOST and {variable}" in (exc.value.suggestion or "")

    def test_config_wins_over_the_variable(
        self, store: MemoryKeyring, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """--config names the file, so MCP_SYNOLOGY_CONFIG beside it changes nothing."""
        _entry(store, "nas")
        monkeypatch.setenv("MCP_SYNOLOGY_CONFIG", str(tmp_path / "elsewhere.yaml"))
        _log_in(load_config(_file(tmp_path, "nas.home")))
        assert store.values[("mcp-synology/nas", "host")] == "nas.home:5000"

    @pytest.mark.parametrize("var", ["SYNOLOGY_HOST", "SYNOLOGY_INSTANCE_ID"])
    def test_a_blank_variable_counts_as_unset(
        self, store: MemoryKeyring, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, var: str
    ) -> None:
        _entry(store, "nas")
        monkeypatch.setenv(var, "   ")
        assert resolve_credentials(load_config(_file(tmp_path, "nas.home")))[1] == "hunter2"

    def test_a_blank_recorded_host_counts_as_no_record(
        self, store: MemoryKeyring, tmp_path: Path
    ) -> None:
        _entry(store, "nas", "   ")
        _log_in(load_config(_file(tmp_path, "nas.home")))
        assert store.values[("mcp-synology/nas", "host")] == "nas.home:5000"

    def test_an_equal_synology_host_doesnt_record_it_either(
        self, store: MemoryKeyring, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _entry(store, "nas")
        monkeypatch.setenv("SYNOLOGY_HOST", "nas.home")
        with pytest.raises(AuthenticationError, match="don't say which host"):
            resolve_credentials(load_config(_file(tmp_path, "nas.home")))

    def test_then_another_host_is_refused(self, store: MemoryKeyring, tmp_path: Path) -> None:
        _entry(store, "nas")
        _log_in(load_config(_file(tmp_path, "nas.home")))
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


class TestADeviceTokenAlone:
    """Credentials from elsewhere log in as before, but the token doesn't go to another host
    (maintainer, 2026-11-05)."""

    @staticmethod
    def _token(store: MemoryKeyring, host: str | None) -> None:
        store.values[("mcp-synology/nas", "device_id")] = "token"
        if host:
            store.values[("mcp-synology/nas", "host")] = f"{host}:5000"

    @staticmethod
    def _creds_from_env(monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("SYNOLOGY_USERNAME", "u")
        monkeypatch.setenv("SYNOLOGY_PASSWORD", "p")

    def test_for_another_host_its_left_out(
        self,
        store: MemoryKeyring,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        self._token(store, "nas.home")
        self._creds_from_env(monkeypatch)
        config = load_config(_file(tmp_path, "nas.office"))
        with caplog.at_level("WARNING"):
            assert resolve_credentials(config) == ("u", "p", None)
        assert any("Not sending the device token" in m for m in caplog.messages)
        assert not [m for m in caplog.messages if "nas.home" in m or "token'" in m]

    def test_for_this_host_its_sent(
        self, store: MemoryKeyring, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        self._token(store, "nas.home")
        self._creds_from_env(monkeypatch)
        assert resolve_credentials(load_config(_file(tmp_path, "nas.home"))) == ("u", "p", "token")

    def test_with_no_record_its_left_out_and_nothing_is_recorded(
        self, store: MemoryKeyring, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """QA round 1 on #559: this pinned another NAS's password to whatever host the config
        named, and refused its real config, on a login that used none of it."""
        self._token(store, None)
        _entry(store, "nas")
        self._creds_from_env(monkeypatch)
        assert resolve_credentials(load_config(_file(tmp_path, "nas.home"))) == ("u", "p", None)
        assert ("mcp-synology/nas", "host") not in store.values

    def test_another_spelling_of_the_host_still_gets_the_token(
        self, store: MemoryKeyring, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        self._token(store, "nas.home")
        self._creds_from_env(monkeypatch)
        assert resolve_credentials(load_config(_file(tmp_path, "NAS.Home")))[2] == "token"

    def test_the_warning_says_how_to_get_it_sent(
        self,
        store: MemoryKeyring,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        """Pre-QA on #559: the login's own error is DSM's, so this is the only mention of it."""
        self._token(store, None)
        self._creds_from_env(monkeypatch)
        monkeypatch.setenv("SYNOLOGY_HOST", "nas.home")
        with caplog.at_level("WARNING"):
            resolve_credentials(load_config(_file(tmp_path, "nas.home")))
        warning = next(m for m in caplog.messages if "Not sending the device token" in m)
        # `check` can't record it with the variable set, or with no password stored.
        assert "mcp-synology check" not in warning
        assert "with the same SYNOLOGY_HOST" in warning

    @pytest.mark.parametrize(
        ("host", "routes_to_check"),
        [(None, True), ("nas.other", False)],
        ids=["no record", "another host's"],
    )
    def test_the_warning_names_only_a_route_that_works(
        self,
        store: MemoryKeyring,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        caplog: pytest.LogCaptureFixture,
        host: str | None,
        routes_to_check: bool,
    ) -> None:
        """Pre-QA on #559: `check` can't re-record an entry for another host, and refuses."""
        _entry(store, "nas", host)
        self._token(store, host)
        self._creds_from_env(monkeypatch)
        with caplog.at_level("WARNING"):
            resolve_credentials(load_config(_file(tmp_path, "nas.home")))
        warning = next(m for m in caplog.messages if "Not sending the device token" in m)
        assert ("mcp-synology check" in warning) is routes_to_check
        assert "mcp-synology setup" in warning

    def test_a_backends_own_error_on_the_first_read_isnt_a_traceback(
        self, store: MemoryKeyring, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Pre-QA on #559: the class QA round 1 fixed in the new helpers."""
        self._creds_from_env(monkeypatch)
        monkeypatch.delenv("SYNOLOGY_PASSWORD")

        def fails(_service: str, _key: str) -> None:
            raise RuntimeError("a backend's own type")

        monkeypatch.setattr(store, "get_password", fails)
        with pytest.raises(AuthenticationError, match="No credentials found"):
            resolve_credentials(load_config(_file(tmp_path, "nas.home")))

    def test_it_warns_once_per_config(
        self,
        store: MemoryKeyring,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        """QA round 1 on #559: the server resolves on every call, so 5 calls gave 5 warnings."""
        self._token(store, "nas.home")
        self._creds_from_env(monkeypatch)
        config = load_config(_file(tmp_path, "nas.office"))
        with caplog.at_level("WARNING"):
            for _ in range(3):
                resolve_credentials(config)
        assert sum("Not sending the device token" in m for m in caplog.messages) == 1

    def test_a_backends_own_error_leaves_it_out(
        self, store: MemoryKeyring, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """QA round 1 on #559: only `KeyringError` and `OSError` were caught."""
        self._token(store, "nas.home")
        self._creds_from_env(monkeypatch)
        real = store.get_password

        def fails(service: str, key: str) -> str | None:
            if key == "host":
                raise RuntimeError("a backend's own type")
            return real(service, key)

        monkeypatch.setattr(store, "get_password", fails)
        assert resolve_credentials(load_config(_file(tmp_path, "nas.home"))) == ("u", "p", None)

    def test_with_no_record_and_env_only_its_left_out(
        self, store: MemoryKeyring, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The usual Docker shape beside an old token: main sent it."""
        monkeypatch.chdir(tmp_path)
        self._token(store, None)
        self._creds_from_env(monkeypatch)
        monkeypatch.setenv("SYNOLOGY_HOST", "nas.home")
        assert resolve_credentials(load_config()) == ("u", "p", None)

    def test_a_token_from_the_environment_isnt_checked(
        self,
        store: MemoryKeyring,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        """It's the one used, so the keyring's is neither sent nor reported."""
        self._token(store, "nas.home")
        self._creds_from_env(monkeypatch)
        monkeypatch.setenv("SYNOLOGY_DEVICE_ID", "env-token")
        with caplog.at_level("WARNING"):
            config = load_config(_file(tmp_path, "nas.office"))
            assert resolve_credentials(config) == ("u", "p", "env-token")
        assert not [m for m in caplog.messages if "device token" in m]

    def test_a_login_with_no_password_records_nothing(
        self, store: MemoryKeyring, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        self._token(store, None)
        monkeypatch.setenv("SYNOLOGY_USERNAME", "u")
        with pytest.raises(AuthenticationError, match="No credentials"):
            resolve_credentials(load_config(_file(tmp_path, "nas.home")))
        assert ("mcp-synology/nas", "host") not in store.values

    def test_a_login_with_no_username_records_nothing(
        self, store: MemoryKeyring, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        self._token(store, None)
        monkeypatch.setenv("SYNOLOGY_PASSWORD", "p")
        with pytest.raises(AuthenticationError, match="No credentials"):
            resolve_credentials(load_config(_file(tmp_path, "nas.home")))
        assert ("mcp-synology/nas", "host") not in store.values

    @pytest.mark.parametrize("error", [KeyringError("locked"), OSError("gone")], ids=["key", "os"])
    def test_a_keyring_that_cant_answer_leaves_it_out(
        self,
        store: MemoryKeyring,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        error: Exception,
    ) -> None:
        self._token(store, "nas.home")
        self._creds_from_env(monkeypatch)
        real = store.get_password

        def fails(service: str, key: str) -> str | None:
            if key == "host":
                raise error
            return real(service, key)

        monkeypatch.setattr(store, "get_password", fails)
        assert resolve_credentials(load_config(_file(tmp_path, "nas.home")))[2] is None

    def test_a_device_token_beside_a_keyring_password_refuses(
        self, store: MemoryKeyring, tmp_path: Path
    ) -> None:
        _entry(store, "nas", "nas.home")
        store.values[("mcp-synology/nas", "device_id")] = "token"
        with pytest.raises(AuthenticationError, match="are for another host"):
            resolve_credentials(load_config(_file(tmp_path, "nas.office")))


class TestSetup:
    def test_store_records_the_host_as_compared(self, store: MemoryKeyring) -> None:
        assert _store_keyring("mcp-synology/nas", "admin", "pw", "NAS.home", 5000)
        assert store.values[("mcp-synology/nas", "host")] == comparable_target("NAS.home", 5000)

    def test_a_new_host_is_written_before_the_new_password(self, store: MemoryKeyring) -> None:
        order: list[str] = []
        real = store.set_password

        def recording(service: str, key: str, value: str) -> None:
            order.append(key)
            real(service, key, value)

        with patch.object(store, "set_password", recording):
            _store_keyring("mcp-synology/nas", "admin", "pw", "nas.home", 5000)
        assert order == ["host", "username", "password"]

    @pytest.mark.parametrize("old_host", ["nas.home", None], ids=["another host", "no record"])
    def test_a_stop_partway_leaves_nothing_to_send(
        self,
        store: MemoryKeyring,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        old_host: str | None,
    ) -> None:
        """Pre-QA on #536: with no old record, a new password stayed with no host, and the next
        config with that first label recorded its own and got that password."""
        _entry(store, "nas", old_host)
        real = store.set_password

        def fails(service: str, key: str, value: str) -> None:
            if key == "username":
                raise KeyringError("locked")
            real(service, key, value)

        monkeypatch.setattr(store, "set_password", fails)
        with pytest.raises(SystemExit) as stopped:
            _store_keyring("mcp-synology/nas", "admin", "new-pw", "nas.office", 5000)
        assert stopped.value.code == 1
        assert ("mcp-synology/nas", "password") not in store.values
        assert store.values[("mcp-synology/nas", "host")] == "nas.office:5000"

    def test_the_same_host_keeps_the_old_password_until_the_new_one(
        self, store: MemoryKeyring
    ) -> None:
        _entry(store, "nas", "nas.home")
        _store_keyring("mcp-synology/nas", "admin", "new-pw", "NAS.home", 5000)
        assert store.values[("mcp-synology/nas", "password")] == "new-pw"

    def test_another_hosts_device_token_is_dropped(self, store: MemoryKeyring) -> None:
        _entry(store, "nas", "nas.home")
        store.values[("mcp-synology/nas", "device_id")] = "token"
        _store_keyring("mcp-synology/nas", "admin", "pw", "nas.office", 5000)
        assert ("mcp-synology/nas", "device_id") not in store.values

    @pytest.mark.parametrize(
        "refusal",
        [PasswordDeleteError("denied"), RuntimeError("prompt dismissed")],
        ids=["the same error as no such password", "a backend's own type"],
    )
    def test_a_delete_that_doesnt_take_stops_with_the_old_record(
        self,
        store: MemoryKeyring,
        monkeypatch: pytest.MonkeyPatch,
        tmp_path: Path,
        refusal: Exception,
    ) -> None:
        """Pre-QA on #559: the old device token stayed, under the new host, and went to it."""
        _entry(store, "nas", "nas.home")
        store.values[("mcp-synology/nas", "device_id")] = "OLD-TOKEN"

        def refuses(_service: str, _key: str) -> None:
            raise refusal

        monkeypatch.setattr(store, "delete_password", refuses)
        assert not _store_keyring("mcp-synology/nas", "admin", "new-pw", "nas.office", 5000)
        assert store.values[("mcp-synology/nas", "host")] == "nas.home:5000"
        assert store.values[("mcp-synology/nas", "password")] == "hunter2"
        with pytest.raises(AuthenticationError, match="are for another host"):
            resolve_credentials(load_config(_file(tmp_path, "nas.office")))

    def test_a_padded_record_reads_the_same_everywhere(
        self, store: MemoryKeyring, tmp_path: Path
    ) -> None:
        """Pre-QA on #559: `setup` called it the same host, then deleted the token, and the login
        refused it."""
        _entry(store, "nas")
        store.values[("mcp-synology/nas", "host")] = "nas.home:5000\n"
        store.values[("mcp-synology/nas", "device_id")] = "token"
        with patch("click.confirm") as confirm:
            assert _may_replace_entry("mcp-synology/nas", "nas.home", 5000, "nas")
        confirm.assert_not_called()
        assert _store_keyring("mcp-synology/nas", "admin", "pw", "nas.home", 5000)
        assert store.values[("mcp-synology/nas", "device_id")] == "token"
        assert resolve_credentials(load_config(_file(tmp_path, "nas.home")))[2] == "token"

    def test_an_entry_with_only_a_device_token_is_asked_about(self, store: MemoryKeyring) -> None:
        store.values[("mcp-synology/nas", "device_id")] = "token"
        with patch("click.confirm", return_value=False) as confirm:
            assert not _may_replace_entry("mcp-synology/nas", "nas.office", 5000, "nas")
        assert "with no host recorded" in confirm.call_args.args[0]

    def test_a_blank_record_reads_as_no_record(self, store: MemoryKeyring) -> None:
        _entry(store, "nas", "   ")
        with patch("click.confirm", return_value=False) as confirm:
            _may_replace_entry("mcp-synology/nas", "nas.office", 5000, "nas")
        assert "with no host recorded" in confirm.call_args.args[0]

    def test_a_record_with_no_credentials_isnt_asked_about(self, store: MemoryKeyring) -> None:
        """Pre-QA on #536: left by removing the rest, it said it "holds credentials"."""
        store.values[("mcp-synology/nas", "host")] = "nas.home:5000"
        with patch("click.confirm") as confirm:
            assert _may_replace_entry("mcp-synology/nas", "nas.office", 5000, "nas")
        confirm.assert_not_called()

    @pytest.mark.parametrize(
        "failure",
        [KeyringLocked("locked"), OSError("no bus"), RuntimeError("a backend's own type")],
        ids=["locked", "os error", "a backend's own type"],
    )
    def test_a_keyring_that_fails_to_read_stops_with_nothing_changed(
        self,
        store: MemoryKeyring,
        monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture[str],
        failure: Exception,
    ) -> None:
        """QA round 1 on #559: it may work again by the write, and another host's credentials
        were replaced with no question asked."""
        _entry(store, "nas", "nas.home")
        real = store.get_password
        calls = iter([failure])

        def fails_once(service: str, key: str) -> str | None:
            for error in calls:
                raise error
            return real(service, key)

        monkeypatch.setattr(store, "get_password", fails_once)
        with patch("click.confirm") as confirm, pytest.raises(SystemExit) as stopped:
            _may_replace_entry("mcp-synology/nas", "nas.office", 5000, "nas")
        assert stopped.value.code == 1  # pre-QA on #559: a script saw success
        confirm.assert_not_called()
        assert "nothing was changed" in capsys.readouterr().out
        assert store.values[("mcp-synology/nas", "password")] == "hunter2"

    def test_no_keyring_at_all_isnt_asked_about(
        self, store: MemoryKeyring, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Docker, CI: writing it reports the problem, with the variables to set instead."""

        def fails(_service: str, _key: str) -> None:
            raise NoKeyringError("no backend")

        monkeypatch.setattr(store, "get_password", fails)
        with patch("click.confirm") as confirm:
            assert _may_replace_entry("mcp-synology/nas", "nas.office", 5000, "nas")
        confirm.assert_not_called()

    def test_a_stop_partway_leaves_no_old_username(
        self, store: MemoryKeyring, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """QA round 1 on #559: with SYNOLOGY_PASSWORD set, it went to the new host."""
        _entry(store, "nas", "nas.home")
        real = store.set_password

        def fails(service: str, key: str, value: str) -> None:
            if key == "username":
                raise KeyringError("locked")
            real(service, key, value)

        monkeypatch.setattr(store, "set_password", fails)
        with pytest.raises(SystemExit) as stopped:
            _store_keyring("mcp-synology/nas", "new-admin", "pw", "nas.office", 5000)
        assert stopped.value.code == 1
        assert ("mcp-synology/nas", "username") not in store.values

    def test_a_failed_replace_says_what_is_gone(
        self,
        store: MemoryKeyring,
        monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        """QA round 1 on #559: it said only "Keyring not available"."""
        _entry(store, "nas", "nas.home")
        store.values[("mcp-synology/nas", "device_id")] = "token"

        def fails(_service: str, _key: str, _value: str) -> None:
            raise KeyringError("read-only")

        monkeypatch.setattr(store, "set_password", fails)
        with pytest.raises(SystemExit) as stopped:
            _store_keyring("mcp-synology/nas", "admin", "pw", "nas.office", 5000)
        assert stopped.value.code == 1
        out = capsys.readouterr().out
        assert "Already removed from this entry: device_id, password, username." in out

    def test_a_failed_write_with_nothing_removed_doesnt_claim_any(
        self,
        store: MemoryKeyring,
        monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        _entry(store, "nas", "nas.home")

        def fails(_service: str, _key: str, _value: str) -> None:
            raise KeyringError("read-only")

        monkeypatch.setattr(store, "set_password", fails)
        assert not _store_keyring("mcp-synology/nas", "admin", "pw", "nas.home", 5000)
        assert "Already removed" not in capsys.readouterr().out

    @pytest.mark.parametrize(
        ("recorded", "says", "doesnt"),
        [
            ("nas.home", "To keep both", "mcp-synology check"),
            (None, "If these are this NAS's own credentials, run 'mcp-synology check", "To keep"),
        ],
        ids=["another host", "no host recorded"],
    )
    def test_declining_gives_advice_that_fits(
        self,
        store: MemoryKeyring,
        tmp_path: Path,
        capsys: pytest.CaptureFixture[str],
        recorded: str | None,
        says: str,
        doesnt: str,
    ) -> None:
        """QA round 1 on #559: "keep both" is wrong when an old entry is this NAS's own."""
        _entry(store, "nas", recorded)
        config = load_config(_file(tmp_path, "nas.office"))
        with patch("click.confirm", return_value=False):
            assert not _may_replace_entry("mcp-synology/nas", "nas.office", 5000, "nas", config)
        out = capsys.readouterr().out
        assert says in out
        assert doesnt not in out

    def test_declining_names_no_route_that_cant_work(
        self,
        store: MemoryKeyring,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        """QA round 2 on #559: `check` with a token and no password says "No credentials found",
        with SYNOLOGY_HOST set it's refused again, and interactive setup has no file to name."""
        store.values[("mcp-synology/nas", "device_id")] = "token"
        config = load_config(_file(tmp_path, "nas.office"))
        with patch("click.confirm", return_value=False):
            _may_replace_entry("mcp-synology/nas", "nas.office", 5000, "nas", config)
        assert "mcp-synology check" not in capsys.readouterr().out
        _entry(store, "nas")
        with patch("click.confirm", return_value=False):
            _may_replace_entry("mcp-synology/nas", "nas.office", 5000, "nas")
        assert "mcp-synology check" not in capsys.readouterr().out
        monkeypatch.setenv("SYNOLOGY_HOST", "nas.office")
        config = load_config(_file(tmp_path, "nas.office"))
        with patch("click.confirm", return_value=False):
            _may_replace_entry("mcp-synology/nas", "nas.office", 5000, "nas", config)
        assert "mcp-synology check" not in capsys.readouterr().out

    def test_a_write_that_doesnt_take_isnt_reported_as_recorded(
        self,
        store: MemoryKeyring,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        """QA round 2 on #559: `check` said "now recorded" with nothing there."""
        _entry(store, "nas")
        monkeypatch.setattr(store, "set_password", lambda *_args: None)
        with caplog.at_level("WARNING"):
            auth = _log_in(load_config(_file(tmp_path, "nas.home")))
        assert auth.recorded_host is None
        assert any("Couldn't record this host" in m for m in caplog.messages)

    def test_a_failed_read_isnt_called_a_mismatch(
        self,
        store: MemoryKeyring,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        """QA round 2 on #559: it said "isn't recorded", with advice that can remove the token, and
        used up the one warning, so a real mismatch later said nothing."""
        store.values[("mcp-synology/nas", "device_id")] = "token"
        store.values[("mcp-synology/nas", "host")] = "nas.other:5000"
        monkeypatch.setenv("SYNOLOGY_USERNAME", "u")
        monkeypatch.setenv("SYNOLOGY_PASSWORD", "p")
        config = load_config(_file(tmp_path, "nas.home"))
        real = store.get_password

        def fails(service: str, key: str) -> str | None:
            if key == "host":
                raise RuntimeError("a backend's own type")
            return real(service, key)

        with caplog.at_level("WARNING"), monkeypatch.context() as patched:
            patched.setattr(store, "get_password", fails)
            for _ in range(3):
                assert resolve_credentials(config)[2] is None
        assert sum("couldn't read which host" in m for m in caplog.messages) == 1
        assert not any("isn't recorded for this host" in m for m in caplog.messages)
        with caplog.at_level("WARNING"):
            assert resolve_credentials(config)[2] is None
        assert any("isn't recorded for this host" in m for m in caplog.messages)

    def test_accepting_replaces_it(self, store: MemoryKeyring, tmp_path: Path) -> None:
        _entry(store, "nas", "nas.home")
        with patch("mcp_synology.cli.setup._setup_login", _logs_in):
            result = CliRunner().invoke(
                main,
                ["setup", "--config", str(_file(tmp_path, "nas.office"))],
                input="y\nadmin2\nnew-pw\n",
            )
        assert result.exit_code == 0, result.output
        assert store.values[("mcp-synology/nas", "host")] == "nas.office:5000"
        assert store.values[("mcp-synology/nas", "password")] == "new-pw"

    def test_env_only_advice_names_plain_setup(
        self, store: MemoryKeyring, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Pre-QA on #536: an env-only user has no config file to name."""
        monkeypatch.chdir(tmp_path)
        _entry(store, "nas", "nas.home")
        monkeypatch.setenv("SYNOLOGY_HOST", "nas.office")
        with pytest.raises(AuthenticationError) as exc:
            resolve_credentials(load_config())
        assert "'mcp-synology setup'" in (exc.value.suggestion or "")
        assert "--config" not in (exc.value.suggestion or "")

    def test_a_keyring_that_fails_on_the_host_read_gets_advice_that_works(
        self, store: MemoryKeyring, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Pre-QA on #536: it said to run `setup`, which then couldn't store anything."""
        _entry(store, "nas")
        real = store.get_password

        def fails(service: str, key: str) -> str | None:
            if key == "host":
                raise OSError("locked")
            return real(service, key)

        monkeypatch.setattr(store, "get_password", fails)
        with pytest.raises(AuthenticationError) as exc:
            resolve_credentials(load_config(_file(tmp_path, "nas.home")))
        assert "OS keyring is unlocked" in (exc.value.suggestion or "")
        assert "setup" not in (exc.value.suggestion or "")

    def test_the_same_hosts_device_token_stays(self, store: MemoryKeyring) -> None:
        _entry(store, "nas", "nas.home")
        store.values[("mcp-synology/nas", "device_id")] = "token"
        _store_keyring("mcp-synology/nas", "admin", "pw", "NAS.home", 5000)
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
            assert _may_replace_entry("mcp-synology/nas", "NAS.home", 5000, "nas") is not asks
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


def _migrate_script() -> Any:
    path = Path(__file__).parents[2] / "scripts" / "migrate-from-synology-mcp.py"
    spec = importlib.util.spec_from_file_location("migrate_from_synology_mcp", path)
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class TestTheMigrationScript:
    """Pre-QA on #559: it wrote around the record, so the check passed the old NAS's secrets."""

    @staticmethod
    def _old(store: MemoryKeyring) -> None:
        store.values[("synology-mcp/nas", "username")] = "homeuser"
        store.values[("synology-mcp/nas", "password")] = "home-pw"

    def test_an_entry_recorded_for_a_host_is_left_alone(self, store: MemoryKeyring) -> None:
        self._old(store)
        store.values[("mcp-synology/nas", "host")] = "nas.office:5000"
        assert _migrate_script().migrate_keyring({"nas"}, dry_run=False) == 0
        assert ("mcp-synology/nas", "password") not in store.values

    def test_an_entry_with_no_record_takes_them_and_stays_unrecorded(
        self, store: MemoryKeyring
    ) -> None:
        self._old(store)
        assert _migrate_script().migrate_keyring({"nas"}, dry_run=False) == 1
        assert store.values[("mcp-synology/nas", "password")] == "home-pw"
        assert ("mcp-synology/nas", "host") not in store.values

    def test_a_failed_read_of_the_new_entry_copies_nothing(
        self, store: MemoryKeyring, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """QA round 3 on #559: it failed open, and overwrote an entry recorded for a host."""
        self._old(store)
        real = store.get_password

        def fails(service: str, key: str) -> str | None:
            if service == "mcp-synology/nas" and key == "host":
                raise RuntimeError("a backend's own type")
            return real(service, key)

        monkeypatch.setattr(store, "get_password", fails)
        assert _migrate_script().migrate_keyring({"nas"}, dry_run=False) == 0
        assert ("mcp-synology/nas", "password") not in store.values

    def test_a_blank_record_is_no_record(self, store: MemoryKeyring) -> None:
        self._old(store)
        store.values[("mcp-synology/nas", "host")] = "   "
        assert _migrate_script().migrate_keyring({"nas"}, dry_run=False) == 1

    def test_cleanup_keeps_what_was_skipped(self, store: MemoryKeyring) -> None:
        """QA round 3 on #559: the old NAS's credentials then existed nowhere."""
        self._old(store)
        store.values[("mcp-synology/nas", "host")] = "nas.office:5000"
        store.values[("mcp-synology/nas", "password")] = "office-pw"
        _migrate_script().cleanup_keyring({"nas"}, dry_run=False)
        assert store.values[("synology-mcp/nas", "password")] == "home-pw"

    def test_cleanup_removes_what_was_migrated_and_recorded_since(
        self, store: MemoryKeyring
    ) -> None:
        self._old(store)
        script = _migrate_script()
        script.migrate_keyring({"nas"}, dry_run=False)
        store.values[("mcp-synology/nas", "host")] = "nas.home:5000"
        script.cleanup_keyring({"nas"}, dry_run=False)
        assert ("synology-mcp/nas", "password") not in store.values

    def test_cleanup_keeps_it_when_the_new_entry_cant_be_read(
        self, store: MemoryKeyring, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        self._old(store)
        real = store.get_password

        def fails(service: str, key: str) -> str | None:
            if service == "mcp-synology/nas":
                raise RuntimeError("a backend's own type")
            return real(service, key)

        monkeypatch.setattr(store, "get_password", fails)
        _migrate_script().cleanup_keyring({"nas"}, dry_run=False)
        assert store.values[("synology-mcp/nas", "password")] == "home-pw"

    def test_cleanup_keeps_it_after_a_read_that_failed_during_the_copy(
        self, store: MemoryKeyring, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Pre-QA on #559: the copy skipped it, cleanup's read then worked and saw no record, and
        the only copy was deleted."""
        self._old(store)
        script = _migrate_script()
        real = store.get_password

        def fails(service: str, key: str) -> str | None:
            if service == "mcp-synology/nas":
                raise RuntimeError("a backend's own type")
            return real(service, key)

        with monkeypatch.context() as patched:
            patched.setattr(store, "get_password", fails)
            assert script.migrate_keyring({"nas"}, dry_run=False) == 0
        script.cleanup_keyring({"nas"}, dry_run=False)
        assert store.values[("synology-mcp/nas", "password")] == "home-pw"

    def test_a_dry_run_previews_keeping_it_for_a_recorded_entry(
        self, store: MemoryKeyring, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """Migration skips an entry recorded for a host, so nothing would be copied there."""
        self._old(store)
        store.values[("mcp-synology/nas", "host")] = "nas.office:5000"
        _migrate_script().cleanup_keyring({"nas"}, dry_run=True)
        assert "KEEP  keyring synology-mcp/nas" in capsys.readouterr().out

    def test_it_says_nothing_about_a_value_already_copied(
        self, store: MemoryKeyring, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """QA round 6 on #559: every rerun would have reported a value that's the same."""
        self._old(store)
        script = _migrate_script()
        script.migrate_keyring({"nas"}, dry_run=False)
        capsys.readouterr()
        script.migrate_keyring({"nas"}, dry_run=False)
        assert "holds a different value" not in capsys.readouterr().out

    def test_it_says_when_it_left_a_different_value(
        self, store: MemoryKeyring, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """Pre-QA on #559: the entry was left in silence."""
        self._old(store)
        store.values[("mcp-synology/nas", "password")] = "new-pw"
        _migrate_script().migrate_keyring({"nas"}, dry_run=False)
        assert "SKIP  keyring mcp-synology/nas/password: holds a different value" in (
            capsys.readouterr().out
        )

    def test_a_dry_run_previews_the_delete_of_what_would_be_copied(
        self, store: MemoryKeyring, capsys: pytest.CaptureFixture[str]
    ) -> None:
        self._old(store)
        _migrate_script().cleanup_keyring({"nas"}, dry_run=True)
        assert "DELETE keyring synology-mcp/nas/password" in capsys.readouterr().out

    def test_it_doesnt_say_all_migrated_after_skipping_one(
        self,
        store: MemoryKeyring,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        """QA round 4 on #559: `_any_skipped` was right, and nothing held the line that uses it."""
        self._old(store)
        store.values[("mcp-synology/nas", "host")] = "nas.office:5000"
        store.values[("mcp-synology/nas", "password")] = "office-pw"
        monkeypatch.setenv("HOME", str(tmp_path))
        (tmp_path / ".local" / "state" / "synology-mcp" / "nas").mkdir(parents=True)
        monkeypatch.setattr(sys, "argv", ["migrate", "--apply"])
        _migrate_script().main()
        out = capsys.readouterr().out
        assert "SKIP  keyring mcp-synology/nas" in out
        assert "all keyring entries already migrated" not in out


class TestQaRoundThree:
    @pytest.mark.parametrize(
        ("spelling", "recorded"),
        [
            ("nas.home.", "nas.home"),
            ("[fd00:0::1]", "[fd00::1]"),
            ("[FD00::1]", "[fd00::1]"),
            ("[fd00::1]", "[fd00::1]"),
        ],
    )
    def test_spellings_of_one_host_compare_equal(self, spelling: str, recorded: str) -> None:
        assert comparable_host(spelling) == recorded

    def test_the_route_warns_about_plain_http(self, store: MemoryKeyring, tmp_path: Path) -> None:
        """A terminal can't see a client's env block, so this goes by the config it can read."""
        _entry(store, "nas")
        assert "goes over plain HTTP" in (
            check_route(load_config(_file(tmp_path, "nas.home"))) or ""
        )

    def test_the_route_names_a_username_only_in_a_variable(
        self, store: MemoryKeyring, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Pre-QA on #559: followed at a terminal without it, `check` refused again."""
        store.values[("mcp-synology/nas", "password")] = "hunter2"
        monkeypatch.setenv("SYNOLOGY_USERNAME", "u")
        route = check_route(load_config(_file(tmp_path, "nas.home"))) or ""
        assert "with the same SYNOLOGY_USERNAME and no SYNOLOGY_PASSWORD" in route

    def test_the_route_doesnt_name_a_username_the_entry_holds(
        self, store: MemoryKeyring, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """QA round 5 on #559."""
        _entry(store, "nas")
        monkeypatch.setenv("SYNOLOGY_USERNAME", "u")
        route = check_route(load_config(_file(tmp_path, "nas.home"))) or ""
        assert "mcp-synology check" in route
        assert "SYNOLOGY_USERNAME" not in route

    def test_a_failed_read_of_the_username_gives_no_route(
        self, store: MemoryKeyring, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """QA round 5 on #559: it was outside the `try`, so `how_to_record` raised."""
        store.values[("mcp-synology/nas", "password")] = "hunter2"
        monkeypatch.setenv("SYNOLOGY_USERNAME", "u")
        config = load_config(_file(tmp_path, "nas.home"))
        real = store.get_password

        def fails(service: str, key: str) -> str | None:
            if key == "username":
                raise RuntimeError("a backend's own type")
            return real(service, key)

        monkeypatch.setattr(store, "get_password", fails)
        assert check_route(config) is None

    def test_setup_with_stdin_closed_says_so(self, tmp_path: Path) -> None:
        """Pre-QA on #559: a traceback. `sys.stdin` is None, which `CliRunner` can't arrange."""
        path = _file(tmp_path, "nas.home")
        done = subprocess.run(
            [
                "bash",
                "-c",
                f"exec '{sys.executable}' -c "
                "'from mcp_synology.cli.main import main; main()' "
                f"setup --config '{path}' <&-",
            ],
            env={
                **os.environ,
                "HOME": str(tmp_path),
                "PYTHON_KEYRING_BACKEND": "keyring.backends.null.Keyring",
                "DBUS_SESSION_BUS_ADDRESS": "",
            },
            capture_output=True,
            text=True,
            check=False,
        )
        assert done.returncode == 1
        assert "stdin is closed" in done.stderr
        assert "Traceback" not in done.stderr

    def test_the_route_is_quiet_on_https(self, store: MemoryKeyring, tmp_path: Path) -> None:
        _entry(store, "nas")
        path = tmp_path / "c.yaml"
        path.write_text(
            "schema_version: 1\nconnection: {host: nas.home, https: true}\n"
            "modules: {filestation: {}}\n",
            encoding="utf-8",
        )
        assert "plain HTTP" not in (check_route(load_config(path)) or "")

    def test_no_route_for_a_password_with_no_username(
        self, store: MemoryKeyring, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        store.values[("mcp-synology/nas", "password")] = "hunter2"
        assert check_route(load_config(_file(tmp_path, "nas.home"))) is None
        monkeypatch.setenv("SYNOLOGY_USERNAME", "u")
        assert check_route(load_config(_file(tmp_path, "nas.home"))) is not None

    def test_a_refused_password_from_elsewhere_isnt_told_about_the_entry(
        self, store: MemoryKeyring, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """QA round 4 on #559: the login never used the entry."""
        _entry(store, "nas")
        monkeypatch.setenv("SYNOLOGY_USERNAME", "u")
        monkeypatch.setenv("SYNOLOGY_PASSWORD", "p")
        with (
            patch(
                "mcp_synology.core.client.DsmClient.query_api_info", AsyncMock(return_value=None)
            ),
            patch(
                "mcp_synology.core.client.DsmClient.request",
                AsyncMock(side_effect=AuthenticationError("wrong password", code=400)),
            ),
        ):
            result = CliRunner().invoke(
                main, ["check", "--config", str(_file(tmp_path, "nas.office"))]
            )
        assert result.exit_code == 1
        assert "If these are another NAS's credentials" not in result.output

    def test_a_refused_login_on_an_unrecorded_entry_offers_its_own_id(
        self, store: MemoryKeyring, tmp_path: Path
    ) -> None:
        _entry(store, "nas")
        with (
            patch(
                "mcp_synology.core.client.DsmClient.query_api_info", AsyncMock(return_value=None)
            ),
            patch(
                "mcp_synology.core.client.DsmClient.request",
                AsyncMock(side_effect=AuthenticationError("wrong password", code=400)),
            ),
            patch("mcp_synology.cli.check._stdin_is_a_terminal", return_value=True),
        ):
            result = CliRunner().invoke(
                main, ["check", "--config", str(_file(tmp_path, "nas.office"))], input="y\n"
            )
        assert result.exit_code == 1
        assert "Authentication failed" in result.output
        assert "If these are another NAS's credentials, give this NAS its own" in result.output

    def test_interactive_setup_records_the_host_and_doesnt_ask_again(
        self, store: MemoryKeyring, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Pre-QA on #559: the newcomer's main path, which nothing pinned."""
        monkeypatch.chdir(tmp_path)
        first = "NAS.Home\ny\n5001\nread\n\nn\nu\npw\n"
        # The second run finds the file the first wrote, so it asks about that alone.
        for answers in (first, "NAS.Home\ny\n5001\nread\n\nn\ny\nu\npw\n"):
            with (
                patch("mcp_synology.cli.setup._CONFIG_DIR", tmp_path / "config"),
                patch(
                    "mcp_synology.core.config.discover_config_path",
                    side_effect=FileNotFoundError,
                ),
                patch("mcp_synology.cli.setup._connect_and_login", _logs_in),
            ):
                result = CliRunner().invoke(main, ["setup"], input=answers)
            assert store.values[("mcp-synology/nas", "host")] == "nas.home:5001", result.output
            assert "already holds" not in result.output


def _port_file(tmp_path: Path, port: int | None, *, https: bool = False) -> Path:
    connection = ["host: nas.home"]
    if port is not None:
        connection.append(f"port: {port}")
    if https:
        connection.append("https: true")
    path = tmp_path / "named.yaml"
    path.write_text(
        "schema_version: 1\n"
        f"connection: {{{', '.join(connection)}}}\n"
        "modules: {filestation: {}}\n",
        encoding="utf-8",
    )
    return path


class TestAnEntryIsTiedToItsPort:
    """#562: two NAS behind one name on different ports share a derived instance ID, and one's
    password went to the other."""

    def test_another_port_is_refused(self, store: MemoryKeyring, tmp_path: Path) -> None:
        _entry(store, "nas", "nas.home")
        with pytest.raises(CredentialHostError) as exc:
            resolve_credentials(load_config(_port_file(tmp_path, 18103)))
        assert "are for another port on this host" in str(exc.value)
        assert "'nas.home:18103'" in str(exc.value)
        assert exc.value.recorded_host == "nas.home:5000"
        assert "hunter2" not in str(exc.value)

    def test_the_same_port_is_accepted(self, store: MemoryKeyring, tmp_path: Path) -> None:
        store.values[("mcp-synology/nas", "username")] = "admin"
        store.values[("mcp-synology/nas", "password")] = "hunter2"
        store.values[("mcp-synology/nas", "host")] = "nas.home:18103"
        config = load_config(_port_file(tmp_path, 18103))
        assert resolve_credentials(config)[1] == "hunter2"

    @pytest.mark.parametrize(
        ("port", "https", "record"),
        [
            (None, False, "nas.home:5000"),
            (5000, False, "nas.home:5000"),
            (None, True, "nas.home:5001"),
            (5001, True, "nas.home:5001"),
        ],
        ids=["http default", "http named", "https default", "https named"],
    )
    def test_the_default_port_compares_equal_with_it_written_out(
        self,
        store: MemoryKeyring,
        tmp_path: Path,
        port: int | None,
        https: bool,
        record: str,
    ) -> None:
        store.values[("mcp-synology/nas", "username")] = "admin"
        store.values[("mcp-synology/nas", "password")] = "hunter2"
        store.values[("mcp-synology/nas", "host")] = record
        config = load_config(_port_file(tmp_path, port, https=https))
        assert resolve_credentials(config)[1] == "hunter2"

    def test_switching_to_https_changes_the_default_port(
        self, store: MemoryKeyring, tmp_path: Path
    ) -> None:
        _entry(store, "nas", "nas.home")
        with pytest.raises(CredentialHostError):
            resolve_credentials(load_config(_port_file(tmp_path, None, https=True)))

    def test_a_host_named_like_the_start_of_the_record_is_another_host(
        self, store: MemoryKeyring, tmp_path: Path
    ) -> None:
        store.values[("mcp-synology/nas", "username")] = "admin"
        store.values[("mcp-synology/nas", "password")] = "hunter2"
        store.values[("mcp-synology/nas", "host")] = "nas.homelab:5000"
        with pytest.raises(CredentialHostError) as exc:
            resolve_credentials(load_config(_port_file(tmp_path, None)))
        assert "are for another host" in str(exc.value)

    def test_check_asks_about_the_port_and_says_what_it_recorded(
        self, store: MemoryKeyring, tmp_path: Path
    ) -> None:
        _entry(store, "nas")
        with (
            patch("mcp_synology.cli.check._stdin_is_a_terminal", return_value=True),
            patch(
                "mcp_synology.core.client.DsmClient.query_api_info", AsyncMock(return_value=None)
            ),
            patch(
                "mcp_synology.core.auth.AuthManager._send_login",
                AsyncMock(return_value={"sid": "s", "did": ""}),
            ),
            patch("mcp_synology.core.auth.AuthManager.logout", AsyncMock(return_value=None)),
        ):
            result = CliRunner().invoke(
                main, ["check", "--config", str(_port_file(tmp_path, 18103))], input="y\n"
            )
        assert "'nas.home:18103'" in result.output
        assert "is now recorded for nas.home:18103." in result.output

    def test_an_ipv6_host_records_its_port(self) -> None:
        assert comparable_target("[fd00:0::1]", 5001) == "[fd00::1]:5001"

    def test_a_token_for_another_port_isnt_sent(
        self, store: MemoryKeyring, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        store.values[("mcp-synology/nas", "device_id")] = "token"
        store.values[("mcp-synology/nas", "host")] = "nas.home:5000"
        monkeypatch.setenv("SYNOLOGY_USERNAME", "u")
        monkeypatch.setenv("SYNOLOGY_PASSWORD", "p")
        config = load_config(_port_file(tmp_path, 18103))
        assert resolve_credentials(config) == ("u", "p", None)


class TestSetupForAnotherPort:
    def test_it_asks_first(self, store: MemoryKeyring) -> None:
        _entry(store, "nas", "nas.home")
        with patch("click.confirm", return_value=False) as confirm:
            assert not _may_replace_entry("mcp-synology/nas", "nas.home", 18103, "nas")
        assert "for another port on this host" in confirm.call_args.args[0]
        assert "Replace it for nas.home:18103?" in confirm.call_args.args[0]

    def test_setup_with_a_config_uses_its_port(self, store: MemoryKeyring, tmp_path: Path) -> None:
        _entry(store, "nas", "nas.home")
        with patch("mcp_synology.cli.setup._setup_login", _logs_in):
            declined = CliRunner().invoke(
                main, ["setup", "--config", str(_port_file(tmp_path, 18103))], input="n\n"
            )
            assert "nas.home:18103" in declined.output
            assert "for another port on this host" in declined.output
            assert store.values[("mcp-synology/nas", "host")] == "nas.home:5000"
            CliRunner().invoke(
                main,
                ["setup", "--config", str(_port_file(tmp_path, 18103))],
                input="y\nadmin\npw\n",
            )
        assert store.values[("mcp-synology/nas", "host")] == "nas.home:18103"

    def test_a_host_named_like_the_start_of_the_record_is_asked_as_another_host(
        self, store: MemoryKeyring
    ) -> None:
        _entry(store, "nas", "nas.homelab")
        with patch("click.confirm", return_value=False) as confirm:
            _may_replace_entry("mcp-synology/nas", "nas.home", 5000, "nas")
        assert "for another host" in confirm.call_args.args[0]

    def test_the_same_port_isnt_asked_about(self, store: MemoryKeyring) -> None:
        _entry(store, "nas", "nas.home")
        with patch("click.confirm") as confirm:
            assert _may_replace_entry("mcp-synology/nas", "nas.home", 5000, "nas")
        confirm.assert_not_called()

    def test_the_old_device_token_doesnt_carry_over(self, store: MemoryKeyring) -> None:
        _entry(store, "nas", "nas.home")
        store.values[("mcp-synology/nas", "device_id")] = "token"
        assert _store_keyring("mcp-synology/nas", "admin", "pw", "nas.home", 18103)
        assert ("mcp-synology/nas", "device_id") not in store.values
        assert store.values[("mcp-synology/nas", "host")] == "nas.home:18103"


class TestARecordWithNoPort:
    """From #559 before this: not shipped, but on any machine that ran it."""

    @staticmethod
    def _old_entry(store: MemoryKeyring) -> None:
        _entry(store, "nas")
        store.values[("mcp-synology/nas", "host")] = "nas.home"
        store.values[("mcp-synology/nas", "device_id")] = "token"

    def test_first_use_records_the_port_and_keeps_the_token(
        self, store: MemoryKeyring, tmp_path: Path
    ) -> None:
        self._old_entry(store)
        auth = _log_in(load_config(_port_file(tmp_path, 18103)))
        assert store.values[("mcp-synology/nas", "host")] == "nas.home:18103"
        assert auth.recorded_host == "nas.home:18103"
        assert store.values[("mcp-synology/nas", "device_id")] == "token"

    def test_it_isnt_taken_to_match_without_a_full_path(
        self, store: MemoryKeyring, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        self._old_entry(store)
        monkeypatch.chdir(tmp_path)
        _port_file(tmp_path, None)
        with pytest.raises(CredentialHostError) as exc:
            resolve_credentials(load_config("named.yaml"))
        assert "don't say which host and port" in str(exc.value)

    def test_another_hosts_record_is_still_another_host(
        self, store: MemoryKeyring, tmp_path: Path
    ) -> None:
        _entry(store, "nas")
        store.values[("mcp-synology/nas", "host")] = "nas.office"
        with pytest.raises(CredentialHostError) as exc:
            resolve_credentials(load_config(_port_file(tmp_path, None)))
        assert "are for another host" in str(exc.value)

    def test_setup_asks(self, store: MemoryKeyring) -> None:
        self._old_entry(store)
        with patch("click.confirm", return_value=False) as confirm:
            assert not _may_replace_entry("mcp-synology/nas", "nas.home", 5000, "nas")
        assert "with no port recorded" in confirm.call_args.args[0]

    def test_declining_offers_the_way_that_records_it(
        self, store: MemoryKeyring, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        self._old_entry(store)
        config = load_config(_port_file(tmp_path, None))
        with patch("click.confirm", return_value=False):
            assert not _may_replace_entry("mcp-synology/nas", "nas.home", 5000, "nas", config)
        assert "mcp-synology check --config <full path" in capsys.readouterr().out


def test_synology_port_stops_a_first_use_from_recording(
    store: MemoryKeyring, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """It chooses which NAS behind a name gets the password, as SYNOLOGY_HOST does."""
    _entry(store, "nas")
    monkeypatch.setenv("SYNOLOGY_PORT", "18103")
    config = load_config(_port_file(tmp_path, None))
    assert not config.host_may_be_recorded
    with pytest.raises(CredentialHostError):
        resolve_credentials(config)
    assert check_route(config) is None


class TestTheWordingOfAPortRefusal:
    def test_what_a_person_is_told(self, store: MemoryKeyring, tmp_path: Path) -> None:
        _entry(store, "nas", "nas.home")
        with pytest.raises(CredentialHostError) as exc:
            resolve_credentials(load_config(_port_file(tmp_path, 18103)))
        terminal = exc.value.terminal_suggestion or ""
        assert "If the host and port they were set up for are this NAS's old address" in terminal
        assert "connection.port" in terminal
        assert "SYNOLOGY_PORT" in terminal

    def test_what_a_model_is_told(self, store: MemoryKeyring, tmp_path: Path) -> None:
        _entry(store, "nas", "nas.home")
        with pytest.raises(CredentialHostError) as exc:
            resolve_credentials(load_config(_port_file(tmp_path, 18103)))
        assert "a new address or port for the same NAS" in (exc.value.suggestion or "")

    @pytest.mark.parametrize("https", [False, True], ids=["http", "https"])
    def test_the_route_warns_that_https_can_change_the_port(
        self, store: MemoryKeyring, tmp_path: Path, https: bool
    ) -> None:
        _entry(store, "nas")
        route = check_route(load_config(_port_file(tmp_path, None, https=https))) or ""
        assert "can change the port recorded" in route


_INFO = {
    "success": True,
    "data": {
        "SYNO.API.Auth": {"path": "entry.cgi", "minVersion": 1, "maxVersion": 7},
        "SYNO.DSM.Info": {"path": "entry.cgi", "minVersion": 1, "maxVersion": 2},
    },
}
_WRONG_PASSWORD = {"success": False, "error": {"code": 400}}
_NEEDS_A_CODE = {"success": False, "error": {"code": 403}}
_LOGGED_IN = {"success": True, "data": {"sid": "the-sid"}}
_LOGGED_IN_WITH_TOKEN = {"success": True, "data": {"sid": "the-sid", "did": "new-token"}}


def _dsm(*logins: dict[str, Any], hostname: Any = None, info: Any = None) -> None:
    """A DSM at nas.home:5000 giving these answers to login POSTs, in order."""
    answers = iter(logins)
    respx.get("http://nas.home:5000/webapi/query.cgi").mock(
        return_value=httpx.Response(200, json=_INFO)
    )
    respx.post("http://nas.home:5000/webapi/entry.cgi").mock(
        side_effect=lambda _request: httpx.Response(200, json=next(answers))
    )

    def reads(request: httpx.Request) -> httpx.Response:
        if request.url.params.get("api") == "SYNO.DSM.Info":
            if isinstance(hostname, Exception):
                raise hostname
            if info is not None:
                return httpx.Response(200, json=info)
            return httpx.Response(200, json={"success": True, "data": {"hostname": hostname or ""}})
        return httpx.Response(200, json={"success": True, "data": {}})

    respx.get("http://nas.home:5000/webapi/entry.cgi").mock(side_effect=reads)


class TestTheKeyringIsWrittenOnlyAfterTheLogin:
    """#454. Through the real login code, with DSM mocked at HTTP. Each flow: `setup --config`
    and the interactive one, whose answers end in the username and password."""

    SERVICE = "mcp-synology/nas"
    ANSWERS = "nas.home\nn\n\nread\n\n"

    def _old(self, store: MemoryKeyring, host: str = "nas.home") -> dict[tuple[str, str], str]:
        _entry(store, "nas", host)
        store.values[(self.SERVICE, "device_id")] = "old-token"
        return dict(store.values)

    def _with_config(self, tmp_path: Path, answers: str) -> Result:
        path = _file(tmp_path, "nas.home")
        return CliRunner().invoke(main, ["setup", "--config", str(path)], input=answers)

    def _interactive(self, tmp_path: Path, answers: str) -> Result:
        with (
            patch("mcp_synology.cli.setup._CONFIG_DIR", tmp_path / "config"),
            patch("mcp_synology.core.config.discover_config_path", side_effect=FileNotFoundError),
        ):
            return CliRunner().invoke(main, ["setup"], input=self.ANSWERS + answers)

    @respx.mock
    @pytest.mark.parametrize("flow", ["_with_config", "_interactive"])
    def test_a_mistyped_password_leaves_the_entry_as_it_was(
        self, flow: str, store: MemoryKeyring, tmp_path: Path
    ) -> None:
        before = self._old(store)
        _dsm(_WRONG_PASSWORD)
        result = getattr(self, flow)(tmp_path, "admin\ntypo\n")
        assert result.exit_code == 1
        assert isinstance(result.exception, SystemExit)
        assert "Login failed" in result.output
        assert "Nothing was changed." in result.output
        assert "Credentials stored" not in result.output
        assert store.values == before

    @respx.mock
    @pytest.mark.parametrize("flow", ["_with_config", "_interactive"])
    @pytest.mark.parametrize(
        "reply",
        [
            {"success": True, "data": {}},
            {"success": True},
            {"success": True, "data": []},
            {"success": True, "data": {"sid": ""}},
            {"success": True, "data": None},
            {"success": True, "data": 5},
            {"success": True, "data": {"sid": 123}},
            {"success": True, "data": {"sid": ["x"]}},
            {"success": True, "data": {"sid": True}},
            {"success": True, "data": {"sid": "   "}},
        ],
    )
    def test_a_reply_with_no_session_isnt_a_login(
        self, flow: str, reply: dict[str, Any], store: MemoryKeyring, tmp_path: Path
    ) -> None:
        """A proxy or the wrong host can answer `success`. A mistyped password mustn't replace a
        working one on the strength of that."""
        before = self._old(store)
        _dsm(reply)
        result = getattr(self, flow)(tmp_path, "admin\ntypo\n")
        assert result.exit_code == 1
        assert isinstance(result.exception, SystemExit)
        assert "no session ID" in result.output
        assert "Login successful" not in result.output
        assert store.values == before

    @respx.mock
    def test_accepting_a_replacement_and_then_failing_loses_nothing(
        self, store: MemoryKeyring, tmp_path: Path
    ) -> None:
        """Pre-QA on #536: the old password and 2FA token went, with the record."""
        before = self._old(store, "nas.office")
        _dsm(_WRONG_PASSWORD)
        result = self._with_config(tmp_path, "y\nadmin\ntypo\n")
        assert "Login failed" in result.output
        assert store.values == before

    @respx.mock
    def test_a_wrong_code_leaves_the_entry_as_it_was(
        self, store: MemoryKeyring, tmp_path: Path
    ) -> None:
        before = self._old(store)
        _dsm(_NEEDS_A_CODE, {"success": False, "error": {"code": 404}})
        result = self._with_config(tmp_path, "admin\nnew-pw\n000000\n")
        assert "Login failed" in result.output
        assert store.values == before

    @respx.mock
    def test_an_unreachable_nas_records_nothing(self, store: MemoryKeyring, tmp_path: Path) -> None:
        """Pre-QA on #536: a mistyped host got recorded, so the retry asked about replacing."""
        respx.get("http://nas.home:5000/webapi/query.cgi").mock(
            side_effect=httpx.ConnectError("refused")
        )
        result = self._with_config(tmp_path, "admin\npw\n")
        assert result.exit_code == 1
        assert store.values == {}

    @respx.mock
    @pytest.mark.parametrize("flow", ["_with_config", "_interactive"])
    def test_a_login_that_works_stores_everything_with_its_new_token(
        self, flow: str, store: MemoryKeyring, tmp_path: Path
    ) -> None:
        self._old(store)
        _dsm(_NEEDS_A_CODE, _LOGGED_IN_WITH_TOKEN)
        result = getattr(self, flow)(tmp_path, "me\nnew-pw\n123456\n")
        assert result.exit_code == 0, result.output
        assert "2FA bootstrap complete!" in result.output
        assert store.values[(self.SERVICE, "username")] == "me"
        assert store.values[(self.SERVICE, "password")] == "new-pw"
        assert store.values[(self.SERVICE, "device_id")] == "new-token"
        assert store.values[(self.SERVICE, "host")] == "nas.home:5000"

    @respx.mock
    def test_another_username_with_no_new_token_clears_the_old_one(
        self, store: MemoryKeyring, tmp_path: Path
    ) -> None:
        """The stored token is the other account's trust."""
        self._old(store)
        _dsm(_LOGGED_IN)
        result = self._with_config(tmp_path, "someone-else\npw\n")
        assert result.exit_code == 0, result.output
        assert (self.SERVICE, "device_id") not in store.values
        assert store.values[(self.SERVICE, "username")] == "someone-else"

    @respx.mock
    def test_the_same_username_with_no_new_token_keeps_it(
        self, store: MemoryKeyring, tmp_path: Path
    ) -> None:
        self._old(store)
        _dsm(_LOGGED_IN)
        result = self._with_config(tmp_path, "admin\nnew-pw\n")
        assert result.exit_code == 0, result.output
        assert store.values[(self.SERVICE, "device_id")] == "old-token"
        assert store.values[(self.SERVICE, "password")] == "new-pw"

    @respx.mock
    def test_a_token_on_a_plain_login_isnt_stored(
        self, store: MemoryKeyring, tmp_path: Path
    ) -> None:
        """Only a code login asks for one, so a plain reply can't replace the stored token."""
        self._old(store)
        _dsm(_LOGGED_IN_WITH_TOKEN)
        result = self._with_config(tmp_path, "admin\nnew-pw\n")
        assert result.exit_code == 0, result.output
        assert "2FA bootstrap" not in result.output
        assert store.values[(self.SERVICE, "device_id")] == "old-token"

    @respx.mock
    def test_a_delete_that_doesnt_take_stops_the_store(
        self, store: MemoryKeyring, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Another account's token mustn't stay beside the new username."""
        before = self._old(store)
        monkeypatch.setattr(store, "delete_password", lambda *_args: None)
        _dsm(_LOGGED_IN)
        result = self._with_config(tmp_path, "someone-else\npw\n")
        assert "Couldn't store these in the keyring." in result.output
        assert store.values == before

    @respx.mock
    def test_a_failed_write_says_what_already_changed(
        self, store: MemoryKeyring, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        self._old(store)
        real_set = store.set_password

        def refuses_the_password(service: str, key: str, value: str) -> None:
            if key == "password":
                raise KeyringError("locked")
            real_set(service, key, value)

        monkeypatch.setattr(store, "set_password", refuses_the_password)
        _dsm(_LOGGED_IN)
        result = self._with_config(tmp_path, "someone-else\npw\n")
        assert "This entry is as it was." in result.output
        assert store.values[(self.SERVICE, "username")] == "admin"
        assert store.values[(self.SERVICE, "device_id")] == "old-token"
        assert result.exit_code == 0, result.output

    @respx.mock
    def test_a_write_that_fails_with_no_way_back_is_reported_and_exits_1(
        self, store: MemoryKeyring, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        self._old(store)
        real_set = store.set_password
        written: list[str] = []

        def one_write_only(service: str, key: str, value: str) -> None:
            if written:
                raise KeyringError("locked")
            written.append(key)
            real_set(service, key, value)

        monkeypatch.setattr(store, "set_password", one_write_only)
        _dsm(_LOGGED_IN)
        result = self._with_config(tmp_path, "someone-else\npw\n")
        assert result.exit_code == 1
        assert isinstance(result.exception, SystemExit)
        assert "new username with the old password" in result.output
        assert store.values[(self.SERVICE, "device_id")] == "old-token"

    @respx.mock
    def test_a_refused_username_write_keeps_the_token(
        self, store: MemoryKeyring, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """QA round 1 on #583: the token was deleted before the username was written."""
        before = self._old(store)

        def refuses(*_args: str) -> None:
            raise KeyringError("locked")

        monkeypatch.setattr(store, "set_password", refuses)
        _dsm(_LOGGED_IN)
        result = self._with_config(tmp_path, "someone-else\npw\n")
        assert result.exit_code == 0, result.output
        assert store.values == before

    @respx.mock
    def test_a_refused_host_write_after_removing_the_old_entry_exits_1(
        self, store: MemoryKeyring, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        self._old(store, "nas.office")

        def refuses(*_args: str) -> None:
            raise KeyringError("locked")

        monkeypatch.setattr(store, "set_password", refuses)
        _dsm(_LOGGED_IN)
        result = self._with_config(tmp_path, "y\nadmin\npw\n")
        assert result.exit_code == 1
        assert "Already removed from this entry:" in result.output

    @respx.mock
    @pytest.mark.parametrize(
        "reply",
        [
            {"success": True, "data": {"sid": "the-sid"}},
            {"success": True, "data": {"sid": "the-sid", "did": ""}},
            {"success": True, "data": {"sid": "the-sid", "did": "  "}},
            {"success": True, "data": {"sid": "the-sid", "did": 12345}},
            {"success": True, "data": []},
        ],
    )
    def test_a_code_login_with_no_usable_token_changes_nothing(
        self, reply: dict[str, Any], store: MemoryKeyring, tmp_path: Path
    ) -> None:
        before = self._old(store)
        _dsm(_NEEDS_A_CODE, reply)
        result = self._with_config(tmp_path, "someone-else\npw\n123456\n")
        assert result.exit_code == 1
        assert "Nothing was changed." in result.output
        assert "2FA bootstrap" not in result.output
        assert store.values == before

    @respx.mock
    @pytest.mark.parametrize(
        ("code", "advice"),
        [
            (400, "Check username and password."),
            (404, "fresh code"),
            (407, "Wait a few minutes"),
            (408, "Auto Block"),
        ],
    )
    def test_a_failed_login_gives_its_advice(
        self, code: int, advice: str, store: MemoryKeyring, tmp_path: Path
    ) -> None:
        _dsm({"success": False, "error": {"code": code}})
        result = self._with_config(tmp_path, "admin\ntypo\n")
        assert result.exit_code == 1
        assert advice in result.output
        assert ("counts toward DSM's Auto Block" in result.output) is (code in (400, 404))

    @respx.mock
    def test_an_unreachable_nas_says_nothing_changed(
        self, store: MemoryKeyring, tmp_path: Path
    ) -> None:
        respx.get("http://nas.home:5000/webapi/query.cgi").mock(
            side_effect=httpx.ConnectError("refused")
        )
        result = self._with_config(tmp_path, "admin\npw\n")
        assert result.exit_code == 1
        assert "Nothing was changed." in result.output

    @respx.mock
    def test_no_config_is_mentioned_when_the_token_stops_a_bare_setup(
        self, store: MemoryKeyring, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        self._old(store)
        real_set = store.set_password

        def refuses_the_token(service: str, key: str, value: str) -> None:
            if key == "device_id":
                raise KeyringError("locked")
            real_set(service, key, value)

        monkeypatch.setattr(store, "set_password", refuses_the_token)
        _dsm(_NEEDS_A_CODE, _LOGGED_IN_WITH_TOKEN)
        result = self._interactive(tmp_path, "someone-else\npw\n123456\n")
        assert result.exit_code == 1
        assert "the old account's token was removed" in result.output
        assert "No config was written." in result.output

    @respx.mock
    @pytest.mark.parametrize(
        ("failure", "shown"),
        [
            ({"hostname": httpx.ReadTimeout("slow")}, "ReadTimeout"),
            ({"hostname": httpx.ConnectError("gone")}, "ConnectError"),
            ({"hostname": httpx.RemoteProtocolError("gone")}, "RemoteProtocolError"),
            ({"hostname": httpx.TooManyRedirects("loop")}, "TooManyRedirects"),
            ({"info": {"success": True, "data": []}}, "AttributeError"),
            ({"info": {"success": True, "data": "text"}}, "AttributeError"),
            ({"info": {"success": True, "data": None}}, "AttributeError"),
            ({"info": {"success": True, "data": 5}}, "AttributeError"),
        ],
    )
    def test_a_failed_hostname_read_doesnt_lose_a_login_that_worked(
        self, failure: dict[str, Any], shown: str, store: MemoryKeyring, tmp_path: Path
    ) -> None:
        """The code is spent and the login proved, and the hostname only suggests an alias."""
        _dsm(_NEEDS_A_CODE, _LOGGED_IN_WITH_TOKEN, **failure)
        result = self._interactive(tmp_path, "me\npw\n123456\n")
        assert result.exit_code == 0, result.output
        assert f"Couldn't read the NAS's hostname ({shown})." in result.output
        assert store.values[(self.SERVICE, "password")] == "pw"
        assert store.values[(self.SERVICE, "device_id")] == "new-token"
        assert (tmp_path / "config" / "nas.yaml").exists()

    @respx.mock
    @pytest.mark.parametrize("flow", ["_with_config", "_interactive"])
    def test_a_new_token_that_cant_be_stored_ends_setup(
        self, flow: str, store: MemoryKeyring, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Another account's token doesn't stay beside the new username, and nothing offers
        variables that couldn't log in without the token."""
        self._old(store)
        real_set = store.set_password

        def refuses_the_token(service: str, key: str, value: str) -> None:
            if key == "device_id":
                raise KeyringError("locked")
            real_set(service, key, value)

        monkeypatch.setattr(store, "set_password", refuses_the_token)
        _dsm(_NEEDS_A_CODE, _LOGGED_IN_WITH_TOKEN)
        result = getattr(self, flow)(tmp_path, "someone-else\npw\n123456\n")
        assert result.exit_code == 1
        assert "The username and password are stored" in result.output
        assert "2FA device token couldn't be stored" in result.output
        assert "SYNOLOGY_PASSWORD" not in result.output
        assert "Credentials stored" not in result.output
        assert (self.SERVICE, "device_id") not in store.values
        assert store.values[(self.SERVICE, "username")] == "someone-else"
        assert not (tmp_path / "config" / "nas.yaml").exists()
        assert respx.calls.last.request.url.params.get("method") == "logout"

    @respx.mock
    def test_a_keyring_that_refuses_writes_still_gets_a_login_and_a_file(
        self, store: MemoryKeyring, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        def refuses(*_args: str) -> None:
            raise KeyringError("locked")

        monkeypatch.setattr(store, "set_password", refuses)
        _dsm(_LOGGED_IN)
        result = self._interactive(tmp_path, "me\npw\n")
        assert result.exit_code == 0, result.output
        assert "Login successful!" in result.output
        assert "Couldn't store these in the keyring." in result.output
        assert "With those variables set, test it" in result.output
        assert result.output.count("SYNOLOGY_PASSWORD") == 2  # the lines, then the client's snippet
        assert (tmp_path / "config" / "nas.yaml").exists()

    @respx.mock
    def test_a_keyring_that_refuses_writes_says_what_to_set_with_a_config(
        self, store: MemoryKeyring, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        def refuses(*_args: str) -> None:
            raise KeyringError("locked")

        monkeypatch.setattr(store, "set_password", refuses)
        _dsm(_LOGGED_IN)
        result = self._with_config(tmp_path, "me\npw\n")
        assert result.exit_code == 0, result.output
        assert "With those variables set, test it" in result.output

    @pytest.mark.parametrize("sid", [None, 123, ["x"], True, "   "])
    def test_the_servers_login_refuses_the_same_replies(self, sid: Any, tmp_path: Path) -> None:
        """QA round 1 on #583: `check` and `serve` crashed on one, and returned 123 on another."""
        config = load_config(_file(tmp_path, "nas.home"))
        client = DsmClient(base_url="http://nas.home:5000")
        auth = AuthManager(config, client)
        with (
            patch.dict(os.environ, {"SYNOLOGY_USERNAME": "u", "SYNOLOGY_PASSWORD": "p"}),
            patch.object(client, "request", AsyncMock(return_value=sid)),
            pytest.raises(AuthenticationError, match="no session ID"),
        ):
            asyncio.run(auth.login())
        with (
            patch.dict(os.environ, {"SYNOLOGY_USERNAME": "u", "SYNOLOGY_PASSWORD": "p"}),
            patch.object(client, "request", AsyncMock(return_value={"sid": sid})),
            pytest.raises(AuthenticationError, match="no session ID"),
        ):
            asyncio.run(auth.login())

    def test_no_keyring_at_all_skips_the_login(self, store: MemoryKeyring, tmp_path: Path) -> None:
        """There's nowhere to keep what it would prove (#481), and no NAS is contacted here."""
        with patch("mcp_synology.core.auth.no_keyring_here", return_value=True):
            result = self._interactive(tmp_path, "me\npw\n")
        assert result.exit_code == 0, result.output
        assert "Connecting to NAS" not in result.output
        assert (tmp_path / "config" / "nas.yaml").exists()
