"""A stored credential is tied to its host, against a real DSM (#536): `setup` records the host,
`check` logs in with it, and another host's config gets a refusal, not a login.

The keyring is in memory: vdsm tests otherwise keep the real one.

Run: ``uv run pytest tests/vdsm/test_vdsm_credential_host.py -m vdsm --no-cov --dsm-version 7.2.2``
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

import keyring
import pytest
from click.testing import CliRunner

from mcp_synology.cli.main import main
from tests.memory_keyring import MemoryKeyring
from tests.vdsm.golden_image import load_golden_meta

if TYPE_CHECKING:
    from pathlib import Path

pytestmark = pytest.mark.vdsm

_INSTANCE = "vdsm-credential-host"


def _config(path: Path, host: str, port: int) -> str:
    path.write_text(
        f"schema_version: 1\ninstance_id: {_INSTANCE}\n"
        f"connection: {{host: {host}, port: {port}, https: false}}\n"
        "modules: {filestation: {}}\n",
        encoding="utf-8",
    )
    return str(path)


def test_setup_records_the_host_and_another_host_is_refused(
    vdsm_container: Any,
    dsm_version: str,
    tmp_path: Path,
    request: pytest.FixtureRequest,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    store = MemoryKeyring()
    previous = keyring.get_keyring()
    keyring.set_keyring(store)
    request.addfinalizer(lambda: keyring.set_keyring(previous))
    for var in (
        "SYNOLOGY_HOST",
        "SYNOLOGY_INSTANCE_ID",
        "SYNOLOGY_USERNAME",
        "SYNOLOGY_PASSWORD",
        "SYNOLOGY_DEVICE_ID",
        "MCP_SYNOLOGY_CONFIG",
    ):
        monkeypatch.delenv(var, raising=False)
    monkeypatch.setenv("HOME", str(tmp_path))  # the state file stays out of the real one

    meta = load_golden_meta(dsm_version)
    port = int(vdsm_container.base_url.rsplit(":", 1)[1])
    runner = CliRunner()
    home_nas = _config(tmp_path / "a.yaml", "localhost", port)
    other = _config(tmp_path / "b.yaml", "127.0.0.1", port)

    def run(*args: str, stdin: str | None = None) -> tuple[int, str]:
        """Exit code and output. CI's --log-cli-level resumes pytest's capture around each log
        record, which puts sys.stdout back mid-command, so the rest of the output lands there."""
        result = runner.invoke(main, list(args), input=stdin)
        captured = capsys.readouterr()
        return result.exit_code, result.output + captured.out + captured.err

    code, output = run(
        "setup", "--config", home_nas, stdin=f"{meta['test_user']}\n{meta['test_password']}\n"
    )
    assert code == 0, output
    assert "Login successful" in output
    assert store.values[(f"mcp-synology/{_INSTANCE}", "host")] == "localhost"

    code, output = run("check", "--config", home_nas)
    assert "Authentication successful!" in output, output

    code, output = run("check", "--config", other)
    assert code == 1, output
    assert "are for another host" in output
    assert "They were set up for 'localhost'" in output

    # Run again for another host: asked first, and declining changes nothing.
    before = dict(store.values)
    code, output = run("setup", "--config", other, stdin="n\n")
    assert "already holds credentials for another host" in output
    assert store.values == before

    # Accepted, the record moves, and the first config is the one refused.
    code, output = run(
        "setup", "--config", other, stdin=f"y\n{meta['test_user']}\n{meta['test_password']}\n"
    )
    assert "Login successful" in output, output
    assert store.values[(f"mcp-synology/{_INSTANCE}", "host")] == "127.0.0.1"
    assert "Authentication successful!" in run("check", "--config", other)[1]
    assert "are for another host" in run("check", "--config", home_nas)[1]
