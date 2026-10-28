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
from datetime import datetime
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
    def test_simple_versions_order_numerically(self) -> None:
        assert v._version_tuple("0.9.0") < v._version_tuple("0.10.0")  # type: ignore[operator]
        assert v._version_tuple("1.2.3") == v._version_tuple("1.2.3")

    @pytest.mark.parametrize(
        ("older", "newer"),
        [
            # #411: an rc install must see the final release as newer.
            ("0.10.0rc11", "0.10.0"),
            ("0.10.0rc9", "0.10.0rc11"),
            ("0.10.0a1", "0.10.0b1"),
            ("0.10.0b2", "0.10.0rc1"),
            ("0.11.0.dev3", "0.11.0a1"),
            ("0.10.0", "0.10.0.post1"),
            ("0.10.0.post1", "0.10.1"),
            ("0.9.9", "0.10.0rc1"),
        ],
    )
    def test_pep440_ordering(self, older: str, newer: str) -> None:
        assert v._is_newer(newer, older)
        assert not v._is_newer(older, newer)

    def test_two_part_is_not_a_version(self) -> None:
        assert v._version_tuple("0.5") is None

    def test_is_newer_false_when_either_does_not_parse(self) -> None:
        assert not v._is_newer("latest", "0.10.0")
        assert not v._is_newer("0.10.0", "dev")
        assert not v._is_newer(None, "0.10.0")

    def test_invalid_returns_none(self) -> None:
        # Pre-#45 returned `(0,)` sentinel, which silently compared less than
        # every real version. Now returns None so callers must handle parse
        # failure explicitly (see _check_for_update).
        assert v._version_tuple("not.a.version") is None

    def test_empty_returns_none(self) -> None:
        # int("") raises ValueError
        assert v._version_tuple("") is None


# ---------- _detect_installer ----------


@pytest.fixture
def _no_venv(monkeypatch: pytest.MonkeyPatch) -> None:
    """Run as a system interpreter (not a venv), so _detect_installer consults PATH."""
    monkeypatch.setattr("sys.prefix", "/usr")
    monkeypatch.setattr("sys.base_prefix", "/usr")


@pytest.mark.usefixtures("_no_venv")
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

    @pytest.mark.parametrize(
        ("prefix", "installer"),
        [
            ("/home/me/.local/share/uv/tools/mcp-synology", "uv"),
            ("/home/me/.cache/uv/archive-v0/AbCdEf", "uvx"),
            ("/home/me/.local/pipx/venvs/mcp-synology", "pipx"),
        ],
    )
    def test_running_interpreter_wins_over_path(self, prefix: str, installer: str) -> None:
        # An MCP client may start the server by full path; PATH can hold another copy.
        with (
            patch("sys.prefix", prefix),
            patch("shutil.which", return_value="/usr/local/bin/mcp-synology"),
        ):
            assert v._detect_installer() == installer

    def test_windows_uvx_cache(self) -> None:
        prefix = "C:\\Users\\me\\AppData\\Local\\uv\\cache\\archive-v0\\AbCd"
        with patch("sys.prefix", prefix):
            assert v._detect_installer() == "uvx"

    def test_a_plain_venv_ignores_path(self) -> None:
        # A venv isn't the uv tool copy that happens to be on PATH.
        with (
            patch("sys.prefix", "/home/me/project/.venv"),
            patch("shutil.which", return_value="/home/me/.local/share/uv/tools/mcp-synology/x"),
        ):
            assert v._detect_installer() is None


class TestUpgradeHint:
    @pytest.mark.parametrize(
        ("installer", "expected"),
        [
            ("uv", "run `uv tool upgrade mcp-synology`, then restart your MCP client"),
            ("pipx", "run `pipx upgrade mcp-synology`, then restart your MCP client"),
            ("uvx", "quit your MCP client, run `uv cache clean mcp-synology`, then start it again"),
        ],
    )
    def test_per_installer(self, installer: str, expected: str) -> None:
        with patch("mcp_synology.cli.version._detect_installer", return_value=installer):
            assert v._upgrade_hint().startswith(expected)

    def test_a_uv_made_venv_gets_uv_pip(self, tmp_path: Path) -> None:
        # uv venvs have no pip.
        (tmp_path / "pyvenv.cfg").write_text("home = /usr/bin\nuv = 0.9.2\n")
        with (
            patch("mcp_synology.cli.version._detect_installer", return_value=None),
            patch("sys.prefix", str(tmp_path)),
            patch("sys.executable", "/opt/venv/bin/python"),
        ):
            assert v._upgrade_hint() == (
                "run `uv pip install -U mcp-synology --python /opt/venv/bin/python`, "
                "then restart your MCP client"
            )

    def test_otherwise_pip_in_this_interpreter(self, tmp_path: Path) -> None:
        (tmp_path / "pyvenv.cfg").write_text("home = /usr/bin\n")
        with (
            patch("mcp_synology.cli.version._detect_installer", return_value=None),
            patch("sys.prefix", str(tmp_path)),
            patch("sys.executable", "/opt/venv/bin/python"),
        ):
            assert v._upgrade_hint() == (
                "run `/opt/venv/bin/python -m pip install -U mcp-synology`, "
                "then restart your MCP client"
            )


class TestUpdateNotice:
    def test_for_the_model_says_to_mention_it_once(self) -> None:
        with (
            patch("mcp_synology.cli.version._get_current_version", return_value="0.8.0"),
            patch("mcp_synology.cli.version._detect_installer", return_value="uv"),
        ):
            text = v.update_notice("0.10.0")
        assert text == (
            "A new version of mcp-synology is available: 0.10.0 (running 0.8.0). "
            "To upgrade, run `uv tool upgrade mcp-synology`, then restart your MCP client. "
            "What's new: https://github.com/cmeans/mcp-synology/releases/tag/v0.10.0. "
            "To stop these notices, set `update_notices: security` or `off` in this "
            "connection's config. Mention this to the user once; don't repeat it."
        )

    def test_security_and_person_facing(self) -> None:
        with patch("mcp_synology.cli.version._detect_installer", return_value="uv"):
            text = v.update_notice("0.10.1", security=True, for_model=False)
        assert text.startswith("A security release of mcp-synology is available: 0.10.1")
        assert "Mention this" not in text
        assert "update_notices" not in text


class TestIsSecurityRelease:
    def _resp(self, body: object) -> MagicMock:
        resp = MagicMock()
        resp.read.return_value = json.dumps(body).encode()
        resp.__enter__ = MagicMock(return_value=resp)
        resp.__exit__ = MagicMock(return_value=False)
        return resp

    def test_marked_in_the_release_body(self) -> None:
        body = {"body": "**Security release:** fixes a token leak.\n\n### Fixed\n- ..."}
        with patch("mcp_synology.cli.version.urlopen", return_value=self._resp(body)) as op:
            assert v._is_security_release("0.10.1") is True
        assert op.call_args.args[0].full_url.endswith("/releases/tags/v0.10.1")

    def test_ordinary_release(self) -> None:
        with patch("mcp_synology.cli.version.urlopen", return_value=self._resp({"body": "x"})):
            assert v._is_security_release("0.10.1") is False

    def test_unreachable_is_unknown(self) -> None:
        # The conftest makes urlopen raise OSError in every unit test.
        assert v._is_security_release("0.10.1") is None


class TestAnnounceCooldown:
    def test_new_version_announces(self) -> None:
        assert v._should_announce({}, "0.10.0")
        state: dict[str, Any] = {}
        v._mark_announced(state, "0.9.0")
        assert v._should_announce(state, "0.10.0")

    def test_same_version_within_a_week_does_not(self) -> None:
        state: dict[str, Any] = {}
        v._mark_announced(state, "0.10.0")
        assert not v._should_announce(state, "0.10.0")
        now = datetime.fromisoformat(state["announced"][v._install_key()]["at"]).timestamp()
        assert not v._should_announce(state, "0.10.0", now=now + 6 * 86400)
        assert v._should_announce(state, "0.10.0", now=now + 7 * 86400)

    def test_recorded_per_install(self) -> None:
        # Configs may run different installs at different versions (maintainer, 2026-10-28).
        state: dict[str, Any] = {}
        with patch("sys.prefix", "/opt/install-a"):
            v._mark_announced(state, "0.10.0")
            assert not v._should_announce(state, "0.10.0")
        with patch("sys.prefix", "/opt/install-b"):
            assert v._should_announce(state, "0.10.0")

    def test_security_release_always(self) -> None:
        state: dict[str, Any] = {"latest_known_version": "0.10.1", "latest_is_security": True}
        v._mark_announced(state, "0.10.1")
        assert v._should_announce(state, "0.10.1")

    def test_unreadable_record_announces(self) -> None:
        bad = {"announced": {v._install_key(): {"version": "0.10.0", "at": "x"}}}
        assert v._should_announce(bad, "0.10.0")
        assert v._should_announce({"announced": "junk"}, "0.10.0")


class TestClaimAnnouncement:
    def test_first_claim_wins_for_an_ordinary_release(self) -> None:
        assert v.claim_announcement("0.10.0") is True
        assert v.claim_announcement("0.10.0") is False  # another connection, same install
        assert v._load_global_state()["announced"][v._install_key()]["version"] == "0.10.0"

    def test_a_security_release_is_always_claimed(self) -> None:
        v._save_global_state({"latest_known_version": "0.10.1", "latest_is_security": True})
        assert v.claim_announcement("0.10.1") is True
        assert v.claim_announcement("0.10.1") is True


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

    @pytest.mark.parametrize(
        "content", [b"- a\n- b\n", b"just a string\n", b"42\n", b"\xff\xfe not utf-8\n"]
    )
    def test_load_returns_empty_for_anything_but_a_mapping(
        self, tmp_path: Path, content: bytes
    ) -> None:
        # A bad write to the shared file mustn't stop every connection from starting
        # (QA round 1 on #433).
        state_dir = tmp_path / ".local" / "state" / "mcp-synology"
        state_dir.mkdir(parents=True)
        (state_dir / "global.yaml").write_bytes(content)
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
        # A failure is retried hourly, not stamped as the day's check (QA round 1 on #433)
        assert "last_version_check" not in state
        with patch("mcp_synology.cli.version._get_latest_pypi_version") as pypi:
            assert v._check_for_update(state) is None
        pypi.assert_not_called()

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

    def test_rc_install_sees_the_final_release(self) -> None:
        # #411: 0.10.0rc11 couldn't be parsed, so an rc install never heard of 0.10.0.
        state: dict[str, Any] = {}
        with (
            patch("mcp_synology.cli.version._get_latest_pypi_version", return_value="0.10.0"),
            patch("mcp_synology.cli.version._get_current_version", return_value="0.10.0rc11"),
        ):
            assert v._check_for_update(state) == "0.10.0"

    def test_does_not_ask_github(self) -> None:
        # Auto-upgrade needs no security flag; the notices use refresh_update_check.
        state: dict[str, Any] = {}
        with (
            patch("mcp_synology.cli.version._get_latest_pypi_version", return_value="9.9.9"),
            patch("mcp_synology.cli.version._get_current_version", return_value="0.5.0"),
            patch("mcp_synology.cli.version._is_security_release") as sec,
        ):
            assert v._check_for_update(state) == "9.9.9"
        sec.assert_not_called()


class TestRefreshUpdateCheck:
    def test_newer_version_and_its_security_flag(self) -> None:
        with (
            patch("mcp_synology.cli.version._get_latest_pypi_version", return_value="9.9.9"),
            patch("mcp_synology.cli.version._get_current_version", return_value="0.5.0"),
            patch("mcp_synology.cli.version._is_security_release", return_value=True) as sec,
        ):
            assert v.refresh_update_check()[:2] == ("9.9.9", True)
            assert v.refresh_update_check(force=True)[:2] == ("9.9.9", True)
        sec.assert_called_once_with("9.9.9")  # asked once per version
        assert v._load_global_state()["latest_is_security"] is True

    def test_flag_resolved_even_by_an_install_that_is_current(self) -> None:
        # An older install sharing the cache mustn't miss it (pre-QA round 2).
        with (
            patch("mcp_synology.cli.version._get_latest_pypi_version", return_value="9.9.9"),
            patch("mcp_synology.cli.version._get_current_version", return_value="9.9.9"),
            patch("mcp_synology.cli.version._is_security_release", return_value=True),
        ):
            assert v.refresh_update_check()[:2] == (None, False)
        with patch("mcp_synology.cli.version._get_current_version", return_value="0.5.0"):
            assert v.refresh_update_check()[:2] == ("9.9.9", True)  # from the cache

    def test_unreachable_github_is_asked_again_hourly(self) -> None:
        with (
            patch("mcp_synology.cli.version._get_latest_pypi_version", return_value="9.9.9"),
            patch("mcp_synology.cli.version._get_current_version", return_value="0.5.0"),
        ):
            assert v.refresh_update_check()[:2] == ("9.9.9", False)
            state = v._load_global_state()
            assert "latest_is_security" not in state
            assert "security_checked_at" in state
            with patch("mcp_synology.cli.version._is_security_release") as sec:
                v.refresh_update_check()  # within the hour: not asked
            sec.assert_not_called()
            state["security_checked_at"] = "2000-01-01T00:00:00+00:00"
            v._save_global_state(state)
            with patch("mcp_synology.cli.version._is_security_release", return_value=True) as sec:
                assert v.refresh_update_check()[:2] == ("9.9.9", True)
            sec.assert_called_once_with("9.9.9")
        assert "security_checked_at" not in v._load_global_state()

    def test_unreachable_pypi_is_asked_again_hourly(self) -> None:
        # Offline at start mustn't skip PyPI for a day (QA round 1 on #433).
        with patch("mcp_synology.cli.version._get_latest_pypi_version", return_value=None):
            assert v.refresh_update_check()[:2] == (None, False)
        state = v._load_global_state()
        assert "last_version_check" not in state
        assert "version_check_failed_at" in state
        with patch("mcp_synology.cli.version._get_latest_pypi_version") as pypi:
            v.refresh_update_check()  # within the hour: not asked
        pypi.assert_not_called()
        state["version_check_failed_at"] = "2000-01-01T00:00:00+00:00"
        v._save_global_state(state)
        with (
            patch("mcp_synology.cli.version._get_latest_pypi_version", return_value="9.9.9"),
            patch("mcp_synology.cli.version._get_current_version", return_value="0.5.0"),
            patch("mcp_synology.cli.version._is_security_release", return_value=False),
        ):
            assert v.refresh_update_check()[:2] == ("9.9.9", False)
        state = v._load_global_state()
        assert "version_check_failed_at" not in state
        assert "last_version_check" in state
        with patch("mcp_synology.cli.version._get_latest_pypi_version") as pypi:
            v.refresh_update_check()  # checked today: not asked
        pypi.assert_not_called()

    def test_never_moves_the_cached_version_backwards(self) -> None:
        # Another process stored 9.9.9 (and its flag) between this one's lock sections,
        # while this one's PyPI CDN node still said 9.9.8 (pre-QA round 3).
        real_load = v._load_global_state
        calls = {"n": 0}

        def _load() -> dict[str, Any]:
            calls["n"] += 1
            if calls["n"] == 2:
                v._save_global_state({"latest_known_version": "9.9.9", "latest_is_security": True})
            return real_load()

        with (
            patch("mcp_synology.cli.version._load_global_state", side_effect=_load),
            patch("mcp_synology.cli.version._get_latest_pypi_version", return_value="9.9.8"),
            patch("mcp_synology.cli.version._get_current_version", return_value="0.5.0"),
            patch("mcp_synology.cli.version._is_security_release", return_value=False),
        ):
            assert v.refresh_update_check(force=True)[:2] == ("9.9.9", True)
        state = real_load()
        assert (state["latest_known_version"], state["latest_is_security"]) == ("9.9.9", True)

    def test_a_newer_latest_resets_the_security_flag(self) -> None:
        v._save_global_state({"latest_known_version": "9.9.8", "latest_is_security": True})
        with (
            patch("mcp_synology.cli.version._get_latest_pypi_version", return_value="9.9.9"),
            patch("mcp_synology.cli.version._get_current_version", return_value="0.5.0"),
            patch("mcp_synology.cli.version._is_security_release", return_value=False),
        ):
            assert v.refresh_update_check(force=True)[:2] == ("9.9.9", False)

    def test_network_runs_outside_the_lock(self) -> None:
        # A connection claiming an announcement mustn't wait on another's PyPI or GitHub
        # round trip: while each network call runs, the lock can be taken (pre-QA round 2).
        import fcntl

        held: list[bool] = []

        def _probe(*_args: Any, **_kwargs: Any) -> str:
            path = v._global_state_path().parent / "global.yaml.lock"
            with path.open("a+") as fh:
                try:
                    fcntl.flock(fh.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                    fcntl.flock(fh.fileno(), fcntl.LOCK_UN)
                    held.append(False)
                except BlockingIOError:
                    held.append(True)
            return "9.9.9"

        with (
            patch("mcp_synology.cli.version._get_latest_pypi_version", side_effect=_probe),
            patch("mcp_synology.cli.version._get_current_version", return_value="0.5.0"),
            patch("mcp_synology.cli.version._is_security_release", side_effect=_probe),
        ):
            v.refresh_update_check()
        assert held == [False, False]


class TestCheckAndDescribeUpdate:
    def test_person_facing_notice(self) -> None:
        with (
            patch("mcp_synology.cli.version._get_latest_pypi_version", return_value="9.9.9"),
            patch("mcp_synology.cli.version._get_current_version", return_value="0.5.0"),
            patch("mcp_synology.cli.version._detect_installer", return_value="uv"),
        ):
            text = v._check_and_describe_update()
        assert text is not None
        assert text.startswith("A new version of mcp-synology is available: 9.9.9 (running 0.5.0)")
        assert "Mention this" not in text

    def test_none_when_up_to_date(self) -> None:
        with (
            patch("mcp_synology.cli.version._get_latest_pypi_version", return_value="0.5.0"),
            patch("mcp_synology.cli.version._get_current_version", return_value="0.5.0"),
        ):
            assert v._check_and_describe_update() is None


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
            "0.6.0rc1",
            "0.7.0.dev1",
            "1.2.3a1.post2.dev3",
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
            "1.2.3\n",  # `$` would allow a trailing newline; fullmatch doesn't
            "1.2.3--rc1",  # two separators in a row
            "1.2.3..",
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

    @pytest.mark.parametrize(
        "value",
        [
            "0.0.0" + "a" * 5000 + "!",
            "0.0.0" + "0" * 5000 + "!",
            "0.0.0a" + ".a1" * 3000 + "!",
            "0.0.0" + "-a" * 3000 + "-",
        ],
    )
    def test_long_invalid_input_fails_fast(self, value: str) -> None:
        """#206 (CodeQL py/redos): the old pattern took ~5 s at 26 characters and
        doubled with each one; these are thousands of characters long."""
        import time

        start = time.perf_counter()
        with pytest.raises(click.ClickException):
            v._validate_version_string(value)
        assert time.perf_counter() - start < 0.1
