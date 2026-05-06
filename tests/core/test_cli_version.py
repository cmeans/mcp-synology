"""Tests for cli/version.py — version checking, PyPI queries, auto-upgrade, revert.

This file targets the largest single coverage gap remaining after PR #16:
cli/version.py was at 27% (93/127 statements missing). The module has no
direct unit tests because it talks to PyPI via stdlib `urllib.request`
(not httpx), reads/writes a YAML file under `~/.local/state`, and shells
out to `uv` or `pipx` for upgrade and revert. Each of those external
boundaries is mockable with `monkeypatch` + `unittest.mock.patch`.

Coverage target per #14: cli/version.py 27% → 90%+.
"""

from __future__ import annotations

import json
import subprocess
from typing import TYPE_CHECKING, Any
from unittest.mock import MagicMock, patch

import click
import pytest
import yaml

from mcp_synology.cli import version as v

if TYPE_CHECKING:
    from pathlib import Path


# ---------- _get_current_version ----------


class TestGetCurrentVersion:
    def test_returns_importlib_version_when_package_installed(self) -> None:
        with patch("importlib.metadata.version", return_value="9.9.9"):
            assert v._get_current_version() == "9.9.9"

    def test_falls_back_to_module_version_on_package_not_found(self) -> None:
        from importlib.metadata import PackageNotFoundError

        with patch("importlib.metadata.version", side_effect=PackageNotFoundError("nope")):
            result = v._get_current_version()
        # Falls back to mcp_synology.__version__
        from mcp_synology import __version__

        assert result == __version__


# ---------- _get_latest_pypi_version ----------


class TestGetLatestPypiVersion:
    @staticmethod
    def _fake_response(payload: dict[str, Any]) -> MagicMock:
        body = json.dumps(payload).encode("utf-8")
        resp = MagicMock()
        resp.read.return_value = body
        resp.__enter__.return_value = resp
        resp.__exit__.return_value = None
        return resp

    def test_returns_version_on_success(self) -> None:
        resp = self._fake_response({"info": {"version": "1.2.3"}})
        with patch("mcp_synology.cli.version.urlopen", return_value=resp):
            assert v._get_latest_pypi_version() == "1.2.3"

    def test_returns_none_on_oserror(self) -> None:
        with patch("mcp_synology.cli.version.urlopen", side_effect=OSError("network down")):
            assert v._get_latest_pypi_version() is None

    def test_returns_none_on_keyerror_in_response(self) -> None:
        resp = self._fake_response({"unexpected": "shape"})
        with patch("mcp_synology.cli.version.urlopen", return_value=resp):
            assert v._get_latest_pypi_version() is None

    def test_returns_none_on_invalid_json(self) -> None:
        resp = MagicMock()
        resp.read.return_value = b"not json at all"
        resp.__enter__.return_value = resp
        resp.__exit__.return_value = None
        with patch("mcp_synology.cli.version.urlopen", return_value=resp):
            assert v._get_latest_pypi_version() is None


# ---------- _version_tuple ----------


class TestVersionTuple:
    def test_simple(self) -> None:
        assert v._version_tuple("1.2.3") == (1, 2, 3)

    def test_two_part(self) -> None:
        assert v._version_tuple("0.5") == (0, 5)

    def test_invalid_returns_none(self) -> None:
        # Pre-#45 returned `(0,)` sentinel, which silently compared less than
        # every real version. Now returns None so callers must handle parse
        # failure explicitly (see _check_for_update).
        assert v._version_tuple("not.a.version") is None

    def test_empty_returns_none(self) -> None:
        # int("") raises ValueError
        assert v._version_tuple("") is None


# ---------- _detect_installer ----------


class TestDetectInstaller:
    def test_uv_path(self) -> None:
        with patch("shutil.which", return_value="/home/me/.local/share/uv/tools/mcp-synology"):
            assert v._detect_installer() == "uv"

    def test_pipx_path(self) -> None:
        with patch("shutil.which", return_value="/home/me/.local/pipx/venvs/mcp-synology/bin/x"):
            assert v._detect_installer() == "pipx"

    def test_unknown_path(self) -> None:
        with patch("shutil.which", return_value="/usr/local/bin/mcp-synology"):
            assert v._detect_installer() is None

    def test_not_on_path(self) -> None:
        with patch("shutil.which", return_value=None):
            assert v._detect_installer() is None


# ---------- _load_global_state / _save_global_state ----------


class TestGlobalState:
    def test_load_returns_empty_when_file_missing(self, tmp_path: Path) -> None:
        with patch("pathlib.Path.home", return_value=tmp_path):
            assert v._load_global_state() == {}

    def test_load_returns_yaml_contents(self, tmp_path: Path) -> None:
        state_dir = tmp_path / ".local" / "state" / "mcp-synology"
        state_dir.mkdir(parents=True)
        (state_dir / "global.yaml").write_text("auto_upgrade: true\nrunning_version: 0.5.0\n")
        with patch("pathlib.Path.home", return_value=tmp_path):
            assert v._load_global_state() == {
                "auto_upgrade": True,
                "running_version": "0.5.0",
            }

    def test_load_returns_empty_on_yaml_error(self, tmp_path: Path) -> None:
        state_dir = tmp_path / ".local" / "state" / "mcp-synology"
        state_dir.mkdir(parents=True)
        (state_dir / "global.yaml").write_text("not: valid: yaml: at: all")
        with patch("pathlib.Path.home", return_value=tmp_path):
            assert v._load_global_state() == {}

    def test_load_returns_empty_when_yaml_is_null(self, tmp_path: Path) -> None:
        """Empty YAML file (yaml.safe_load returns None) → {}."""
        state_dir = tmp_path / ".local" / "state" / "mcp-synology"
        state_dir.mkdir(parents=True)
        (state_dir / "global.yaml").write_text("")
        with patch("pathlib.Path.home", return_value=tmp_path):
            assert v._load_global_state() == {}

    def test_save_creates_directory_and_file(self, tmp_path: Path) -> None:
        with patch("pathlib.Path.home", return_value=tmp_path):
            v._save_global_state({"running_version": "1.0.0", "auto_upgrade": True})

        state_file = tmp_path / ".local" / "state" / "mcp-synology" / "global.yaml"
        assert state_file.exists()
        loaded = yaml.safe_load(state_file.read_text())
        assert loaded == {"running_version": "1.0.0", "auto_upgrade": True}
        assert state_file.read_text().startswith("# Auto-generated by mcp-synology.\n")

    def test_save_then_load_round_trips(self, tmp_path: Path) -> None:
        original = {"latest_known_version": "0.6.0", "previous_version": "0.5.0"}
        with patch("pathlib.Path.home", return_value=tmp_path):
            v._save_global_state(original)
            assert v._load_global_state() == original


# ---------- _with_global_state_lock ----------


class TestGlobalStateLock:
    def test_concurrent_writers_preserve_both_updates(self, tmp_path: Path) -> None:
        """Regression test for #93 — load/mutate/save under the lock prevents lost updates.

        Two threads each increment a distinct counter 50 times. Without the lock,
        interleaved load/save sequences cause lost updates and at least one
        counter ends below 50. With the lock, both counters reach exactly 50.
        """
        import threading

        with patch("pathlib.Path.home", return_value=tmp_path):
            v._save_global_state({"counter_a": 0, "counter_b": 0})

            def writer(key: str) -> None:
                for _ in range(50):
                    with v._with_global_state_lock():
                        state = v._load_global_state()
                        state[key] = state.get(key, 0) + 1
                        v._save_global_state(state)

            t1 = threading.Thread(target=writer, args=("counter_a",))
            t2 = threading.Thread(target=writer, args=("counter_b",))
            t1.start()
            t2.start()
            t1.join()
            t2.join()

            final = v._load_global_state()

        assert final["counter_a"] == 50
        assert final["counter_b"] == 50

    def test_lock_creates_state_dir_if_missing(self, tmp_path: Path) -> None:
        """Lockfile location is `.../mcp-synology/global.yaml.lock`; the parent
        dir must be created on first use even if no save has happened yet."""
        with patch("pathlib.Path.home", return_value=tmp_path), v._with_global_state_lock():
            pass
        lock_dir = tmp_path / ".local" / "state" / "mcp-synology"
        assert lock_dir.is_dir()
        assert (lock_dir / "global.yaml.lock").exists()


# ---------- _check_for_update ----------


class TestCheckForUpdate:
    def test_force_bypasses_cache_and_fetches(self) -> None:
        state: dict[str, Any] = {
            "last_version_check": "2200-01-01T00:00:00+00:00",  # far in the future
            "latest_known_version": "0.0.1",
        }
        with (
            patch("mcp_synology.cli.version._get_latest_pypi_version", return_value="9.9.9"),
            patch("mcp_synology.cli.version._get_current_version", return_value="0.5.0"),
        ):
            assert v._check_for_update(state, force=True) == "9.9.9"
        assert state["latest_known_version"] == "9.9.9"

    def test_cache_valid_and_newer_returns_cached(self) -> None:
        from datetime import UTC, datetime

        state: dict[str, Any] = {
            "last_version_check": datetime.now(tz=UTC).isoformat(),
            "latest_known_version": "9.9.9",
        }
        with (
            patch("mcp_synology.cli.version._get_latest_pypi_version") as fetch,
            patch("mcp_synology.cli.version._get_current_version", return_value="0.5.0"),
        ):
            assert v._check_for_update(state) == "9.9.9"
        fetch.assert_not_called()  # cache hit, no network call

    def test_cache_valid_and_not_newer_returns_none(self) -> None:
        from datetime import UTC, datetime

        state: dict[str, Any] = {
            "last_version_check": datetime.now(tz=UTC).isoformat(),
            "latest_known_version": "0.5.0",
        }
        with (
            patch("mcp_synology.cli.version._get_latest_pypi_version") as fetch,
            patch("mcp_synology.cli.version._get_current_version", return_value="0.5.0"),
        ):
            assert v._check_for_update(state) is None
        fetch.assert_not_called()

    def test_cache_stale_triggers_fetch(self) -> None:
        state: dict[str, Any] = {
            "last_version_check": "2000-01-01T00:00:00+00:00",  # very old
            "latest_known_version": "0.0.1",
        }
        with (
            patch("mcp_synology.cli.version._get_latest_pypi_version", return_value="0.6.0"),
            patch("mcp_synology.cli.version._get_current_version", return_value="0.5.0"),
        ):
            assert v._check_for_update(state) == "0.6.0"

    def test_cache_corrupt_timestamp_falls_through_to_fetch(self) -> None:
        state: dict[str, Any] = {
            "last_version_check": "not a timestamp",
            "latest_known_version": "0.0.1",
        }
        with (
            patch("mcp_synology.cli.version._get_latest_pypi_version", return_value="0.6.0"),
            patch("mcp_synology.cli.version._get_current_version", return_value="0.5.0"),
        ):
            assert v._check_for_update(state) == "0.6.0"

    def test_pypi_returns_none_returns_none(self) -> None:
        state: dict[str, Any] = {}
        with (
            patch("mcp_synology.cli.version._get_latest_pypi_version", return_value=None),
            patch("mcp_synology.cli.version._get_current_version", return_value="0.5.0"),
        ):
            assert v._check_for_update(state) is None
        # Even on failure the timestamp gets updated (so we don't hammer PyPI)
        assert "last_version_check" in state

    def test_pypi_returns_same_version(self) -> None:
        state: dict[str, Any] = {}
        with (
            patch("mcp_synology.cli.version._get_latest_pypi_version", return_value="0.5.0"),
            patch("mcp_synology.cli.version._get_current_version", return_value="0.5.0"),
        ):
            assert v._check_for_update(state) is None
        assert state["latest_known_version"] == "0.5.0"

    def test_pypi_returns_newer_version(self) -> None:
        state: dict[str, Any] = {}
        with (
            patch("mcp_synology.cli.version._get_latest_pypi_version", return_value="9.9.9"),
            patch("mcp_synology.cli.version._get_current_version", return_value="0.5.0"),
        ):
            assert v._check_for_update(state) == "9.9.9"


# ---------- _do_auto_upgrade ----------


class TestDoAutoUpgrade:
    @staticmethod
    def _fake_completed(returncode: int, stderr: str = "") -> subprocess.CompletedProcess[str]:
        return subprocess.CompletedProcess(
            args=["fake"], returncode=returncode, stdout="", stderr=stderr
        )

    def test_uv_installer_success(self, tmp_path: Path) -> None:
        with (
            patch("pathlib.Path.home", return_value=tmp_path),
            patch("mcp_synology.cli.version._detect_installer", return_value="uv"),
            patch("mcp_synology.cli.version._get_current_version", return_value="0.5.0"),
            patch("subprocess.run", return_value=self._fake_completed(0)) as run,
        ):
            assert v._do_auto_upgrade() is True
            saved = v._load_global_state()
        cmd = run.call_args.args[0]
        assert cmd[:3] == ["uv", "tool", "install"]
        assert "mcp-synology@latest" in cmd
        assert saved["previous_version"] == "0.5.0"

    def test_pipx_installer_success(self, tmp_path: Path) -> None:
        with (
            patch("pathlib.Path.home", return_value=tmp_path),
            patch("mcp_synology.cli.version._detect_installer", return_value="pipx"),
            patch("mcp_synology.cli.version._get_current_version", return_value="0.5.0"),
            patch("subprocess.run", return_value=self._fake_completed(0)),
        ):
            assert v._do_auto_upgrade() is True
            saved = v._load_global_state()
        assert saved["previous_version"] == "0.5.0"

    def test_unknown_installer_returns_false(self, tmp_path: Path) -> None:
        with (
            patch("pathlib.Path.home", return_value=tmp_path),
            patch("mcp_synology.cli.version._detect_installer", return_value=None),
            patch("mcp_synology.cli.version._get_current_version", return_value="0.5.0"),
        ):
            assert v._do_auto_upgrade() is False
            assert "previous_version" not in v._load_global_state()

    def test_subprocess_failure_returns_false(self, tmp_path: Path) -> None:
        with (
            patch("pathlib.Path.home", return_value=tmp_path),
            patch("mcp_synology.cli.version._detect_installer", return_value="uv"),
            patch("mcp_synology.cli.version._get_current_version", return_value="0.5.0"),
            patch("subprocess.run", return_value=self._fake_completed(1, stderr="boom")),
        ):
            assert v._do_auto_upgrade() is False
            assert "previous_version" not in v._load_global_state()


# ---------- _validate_version_string ----------


class TestValidateVersionString:
    @pytest.mark.parametrize(
        "value",
        [
            "0.5.0",
            "0.5.1",
            "0.5.1-rc1",
            "0.5.0a1",
            "1.2.3.post4",
            "0.5.1.dev1",
            "1.2.3-alpha.1",
            "10.20.30",
        ],
    )
    def test_accepts_valid_versions(self, value: str) -> None:
        v._validate_version_string(value)  # no exception

    @pytest.mark.parametrize(
        "value",
        [
            "latest",
            "1.2",  # too few segments
            "",
            " ",
            "   ",
            "1.0.0; whatever",
            "1.0.0 --extra",
            "1.2.3 ",  # trailing whitespace
            " 1.2.3",  # leading whitespace
            "1.2.3-",  # trailing separator with no suffix
            "v1.2.3",  # leading letter prefix
            "1.2.3/../etc/passwd",
            "==1.2.3",
        ],
    )
    def test_rejects_invalid_versions(self, value: str) -> None:
        with pytest.raises(click.ClickException) as exc:
            v._validate_version_string(value)
        # Exception message names the bad value and the expected shape
        assert "Invalid version string" in exc.value.message
        assert "MAJOR.MINOR.PATCH" in exc.value.message


# ---------- _do_revert ----------


class TestDoRevert:
    @staticmethod
    def _fake_completed(returncode: int, stderr: str = "") -> subprocess.CompletedProcess[str]:
        return subprocess.CompletedProcess(
            args=["fake"], returncode=returncode, stdout="", stderr=stderr
        )

    def test_revert_with_no_state_and_no_explicit(self, tmp_path: Path) -> None:
        """No previous_version recorded and no explicit version → message + return."""
        with (
            patch("pathlib.Path.home", return_value=tmp_path),
            patch("mcp_synology.cli.version._get_current_version", return_value="0.5.0"),
            patch("subprocess.run") as run,
        ):
            v._do_revert(None)
        run.assert_not_called()

    def test_revert_with_target_version_uv_installer(self, tmp_path: Path) -> None:
        with (
            patch("pathlib.Path.home", return_value=tmp_path),
            patch("mcp_synology.cli.version._get_current_version", return_value="0.5.0"),
            patch("mcp_synology.cli.version._detect_installer", return_value="uv"),
            patch("subprocess.run", return_value=self._fake_completed(0)) as run,
        ):
            v._do_revert("0.4.1")
        cmd = run.call_args.args[0]
        assert cmd[:4] == ["uv", "tool", "install", "--force"]
        assert any("==0.4.1" in arg for arg in cmd)

    def test_revert_with_target_version_pipx_installer(self, tmp_path: Path) -> None:
        with (
            patch("pathlib.Path.home", return_value=tmp_path),
            patch("mcp_synology.cli.version._get_current_version", return_value="0.5.0"),
            patch("mcp_synology.cli.version._detect_installer", return_value="pipx"),
            patch("subprocess.run", return_value=self._fake_completed(0)) as run,
        ):
            v._do_revert("0.4.1")
        cmd = run.call_args.args[0]
        assert cmd[:3] == ["pipx", "install", "--force"]
        assert any("==0.4.1" in arg for arg in cmd)

    def test_revert_with_unknown_installer(self, tmp_path: Path) -> None:
        with (
            patch("pathlib.Path.home", return_value=tmp_path),
            patch("mcp_synology.cli.version._get_current_version", return_value="0.5.0"),
            patch("mcp_synology.cli.version._detect_installer", return_value=None),
            patch("subprocess.run") as run,
        ):
            v._do_revert("0.4.1")
        run.assert_not_called()

    def test_revert_uses_previous_version_from_state(self, tmp_path: Path) -> None:
        # Pre-populate state with a previous_version
        state_dir = tmp_path / ".local" / "state" / "mcp-synology"
        state_dir.mkdir(parents=True)
        (state_dir / "global.yaml").write_text("previous_version: 0.4.1\n")

        with (
            patch("pathlib.Path.home", return_value=tmp_path),
            patch("mcp_synology.cli.version._get_current_version", return_value="0.5.0"),
            patch("mcp_synology.cli.version._detect_installer", return_value="uv"),
            patch("subprocess.run", return_value=self._fake_completed(0)) as run,
        ):
            v._do_revert(None)
        cmd = run.call_args.args[0]
        assert any("==0.4.1" in arg for arg in cmd)

    def test_revert_to_current_version_is_noop(self, tmp_path: Path) -> None:
        with (
            patch("pathlib.Path.home", return_value=tmp_path),
            patch("mcp_synology.cli.version._get_current_version", return_value="0.4.1"),
            patch("subprocess.run") as run,
        ):
            v._do_revert("0.4.1")
        run.assert_not_called()

    def test_revert_subprocess_failure(self, tmp_path: Path) -> None:
        with (
            patch("pathlib.Path.home", return_value=tmp_path),
            patch("mcp_synology.cli.version._get_current_version", return_value="0.5.0"),
            patch("mcp_synology.cli.version._detect_installer", return_value="uv"),
            patch("subprocess.run", return_value=self._fake_completed(1, stderr="bad")),
        ):
            # Just verify it doesn't crash; the function returns None either way
            v._do_revert("0.4.1")

    def test_revert_rejects_invalid_explicit_version(self, tmp_path: Path) -> None:
        """Malformed --revert <VER> raises ClickException and never shells out."""
        with (
            patch("pathlib.Path.home", return_value=tmp_path),
            patch("mcp_synology.cli.version._get_current_version", return_value="0.5.0"),
            patch("subprocess.run") as run,
            pytest.raises(click.ClickException),
        ):
            v._do_revert("latest")
        run.assert_not_called()

    def test_revert_rejects_invalid_state_previous_version(self, tmp_path: Path) -> None:
        """Corrupt previous_version in global state is rejected, not handed to pip."""
        state_dir = tmp_path / ".local" / "state" / "mcp-synology"
        state_dir.mkdir(parents=True)
        (state_dir / "global.yaml").write_text("previous_version: '1.0.0; rm -rf /'\n")

        with (
            patch("pathlib.Path.home", return_value=tmp_path),
            patch("mcp_synology.cli.version._get_current_version", return_value="0.5.0"),
            patch("subprocess.run") as run,
            pytest.raises(click.ClickException),
        ):
            v._do_revert(None)
        run.assert_not_called()

    def test_revert_success_clears_previous_and_disables_auto_upgrade(self, tmp_path: Path) -> None:
        state_dir = tmp_path / ".local" / "state" / "mcp-synology"
        state_dir.mkdir(parents=True)
        (state_dir / "global.yaml").write_text("previous_version: 0.4.1\nauto_upgrade: true\n")

        with (
            patch("pathlib.Path.home", return_value=tmp_path),
            patch("mcp_synology.cli.version._get_current_version", return_value="0.5.0"),
            patch("mcp_synology.cli.version._detect_installer", return_value="uv"),
            patch("subprocess.run", return_value=self._fake_completed(0)),
        ):
            v._do_revert(None)

        loaded = yaml.safe_load((state_dir / "global.yaml").read_text())
        assert loaded["previous_version"] is None
        assert loaded["auto_upgrade"] is False
