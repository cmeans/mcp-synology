"""Tests for scripts/sync-server-json.py.

The script is the load-bearing piece of the version-centralization story
(issue #11): pyproject.toml is the single source of truth, and this script
propagates the version into server.json. Anything that breaks the lossless
round-trip — for example, someone adding `sort_keys=True` to `json.dumps` —
would silently break the no-op invariant the CI guard relies on.

These tests cover:
- happy-path read of pyproject and server.json
- every error path (missing file, malformed file, missing version, wrong shape)
- collect_versions / apply_version correctness, including immutability of input
- serialize() matches the existing 2-space + trailing newline format
- main() exit codes for the three CI-relevant flows: clean check, drifted
  check, drifted write (which then makes the next check clean)
- the lossless round-trip invariant: sync a clean tree → byte-identical output
"""

from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path
from typing import TYPE_CHECKING, Any

import pytest

if TYPE_CHECKING:
    from types import ModuleType

# scripts/sync-server-json.py uses a hyphen, so it can't be imported as a
# normal package. Load it via importlib so the test file can call its
# functions directly instead of shelling out to subprocess for every check.
_SCRIPT_PATH = Path(__file__).resolve().parents[2] / "scripts" / "sync-server-json.py"


def _load_script() -> ModuleType:
    spec = importlib.util.spec_from_file_location("sync_server_json", _SCRIPT_PATH)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules["sync_server_json"] = module
    spec.loader.exec_module(module)
    return module


sync = _load_script()


# ---------- read_pyproject_version ----------


def test_read_pyproject_version_happy_path(tmp_path: Path) -> None:
    pyproject = tmp_path / "pyproject.toml"
    pyproject.write_text('[project]\nname = "x"\nversion = "1.2.3"\n')
    assert sync.read_pyproject_version(pyproject) == "1.2.3"


def test_read_pyproject_version_missing_file(tmp_path: Path) -> None:
    with pytest.raises(SystemExit) as exc:
        sync.read_pyproject_version(tmp_path / "nope.toml")
    assert "not found" in str(exc.value)


def test_read_pyproject_version_malformed_toml(tmp_path: Path) -> None:
    pyproject = tmp_path / "pyproject.toml"
    pyproject.write_text("this is not = valid = toml [")
    with pytest.raises(SystemExit) as exc:
        sync.read_pyproject_version(pyproject)
    assert "not valid TOML" in str(exc.value)


def test_read_pyproject_version_missing_version_key(tmp_path: Path) -> None:
    pyproject = tmp_path / "pyproject.toml"
    pyproject.write_text('[project]\nname = "x"\n')
    with pytest.raises(SystemExit) as exc:
        sync.read_pyproject_version(pyproject)
    assert "[project].version not found" in str(exc.value)


def test_read_pyproject_version_non_string_version(tmp_path: Path) -> None:
    pyproject = tmp_path / "pyproject.toml"
    pyproject.write_text('[project]\nname = "x"\nversion = 123\n')
    with pytest.raises(SystemExit) as exc:
        sync.read_pyproject_version(pyproject)
    assert "not a string" in str(exc.value)


# ---------- load_server_json ----------


def _write_server_json(path: Path, data: dict[str, Any]) -> None:
    path.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")


def test_load_server_json_happy_path(tmp_path: Path) -> None:
    server = tmp_path / "server.json"
    _write_server_json(server, {"version": "1.0.0", "packages": []})
    loaded = sync.load_server_json(server)
    assert loaded == {"version": "1.0.0", "packages": []}


def test_load_server_json_missing_file(tmp_path: Path) -> None:
    with pytest.raises(SystemExit) as exc:
        sync.load_server_json(tmp_path / "nope.json")
    assert "not found" in str(exc.value)


def test_load_server_json_malformed(tmp_path: Path) -> None:
    server = tmp_path / "server.json"
    server.write_text("{ this is not valid json")
    with pytest.raises(SystemExit) as exc:
        sync.load_server_json(server)
    assert "not valid JSON" in str(exc.value)


def test_load_server_json_top_level_must_be_object(tmp_path: Path) -> None:
    server = tmp_path / "server.json"
    server.write_text('["not", "an", "object"]')
    with pytest.raises(SystemExit) as exc:
        sync.load_server_json(server)
    assert "must be a JSON object" in str(exc.value)


# ---------- collect_versions ----------


def test_collect_versions_top_level_and_packages() -> None:
    server = {
        "version": "1.0.0",
        "packages": [
            {"identifier": "pkg-a", "version": "1.0.0"},
            {"identifier": "pkg-b", "version": "2.0.0"},
        ],
    }
    assert sync.collect_versions(server) == {
        "top_level": "1.0.0",
        "packages[0]": "1.0.0",
        "packages[1]": "2.0.0",
    }


def test_collect_versions_handles_missing_packages() -> None:
    assert sync.collect_versions({"version": "1.0.0"}) == {"top_level": "1.0.0"}


def test_collect_versions_handles_empty_packages() -> None:
    assert sync.collect_versions({"version": "1.0.0", "packages": []}) == {"top_level": "1.0.0"}


def test_collect_versions_skips_non_dict_packages() -> None:
    server = {"version": "1.0.0", "packages": ["not-a-dict"]}
    assert sync.collect_versions(server) == {"top_level": "1.0.0"}


# ---------- apply_version ----------


def test_apply_version_updates_top_level_and_all_packages() -> None:
    server = {
        "version": "0.5.0",
        "packages": [
            {"identifier": "a", "version": "0.5.0"},
            {"identifier": "b", "version": "0.5.0"},
        ],
    }
    updated = sync.apply_version(server, "0.6.0")
    assert updated["version"] == "0.6.0"
    assert updated["packages"][0]["version"] == "0.6.0"
    assert updated["packages"][1]["version"] == "0.6.0"


def test_apply_version_does_not_mutate_input() -> None:
    server = {
        "version": "0.5.0",
        "packages": [{"identifier": "a", "version": "0.5.0"}],
    }
    sync.apply_version(server, "0.6.0")
    assert server["version"] == "0.5.0"
    assert server["packages"][0]["version"] == "0.5.0"


def test_apply_version_preserves_unrelated_fields() -> None:
    server = {
        "version": "0.5.0",
        "name": "io.github.example/x",
        "description": "stays put",
        "packages": [{"identifier": "a", "version": "0.5.0", "transport": {"type": "stdio"}}],
    }
    updated = sync.apply_version(server, "0.6.0")
    assert updated["name"] == "io.github.example/x"
    assert updated["description"] == "stays put"
    assert updated["packages"][0]["transport"] == {"type": "stdio"}
    assert updated["packages"][0]["identifier"] == "a"


# ---------- serialize ----------


def test_serialize_matches_expected_format() -> None:
    server = {"version": "1.0.0", "packages": [{"version": "1.0.0"}]}
    out = sync.serialize(server)
    assert out.endswith("\n")
    # 2-space indent: nested keys appear on their own lines with 4-space indent
    assert '\n  "packages": [\n    {\n      "version": "1.0.0"\n    }\n  ]\n' in out


def test_serialize_lossless_round_trip(tmp_path: Path) -> None:
    """The CI no-op guarantee: writing a synced server.json must be byte-identical."""
    original = {
        "$schema": "https://example.com/schema.json",
        "name": "io.github.example/x",
        "version": "0.5.0",
        "packages": [
            {"registryType": "pypi", "identifier": "x", "version": "0.5.0"},
        ],
    }
    server_path = tmp_path / "server.json"
    server_path.write_text(sync.serialize(original), encoding="utf-8")
    before = server_path.read_bytes()
    loaded = sync.load_server_json(server_path)
    rewritten = sync.apply_version(loaded, "0.5.0")
    server_path.write_text(sync.serialize(rewritten), encoding="utf-8")
    after = server_path.read_bytes()
    assert before == after


# ---------- main() exit codes ----------


@pytest.fixture
def fake_repo(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Stand up a tmp pyproject.toml + server.json and point the script at them."""
    pyproject = tmp_path / "pyproject.toml"
    pyproject.write_text('[project]\nname = "x"\nversion = "0.5.0"\n')
    server_json = tmp_path / "server.json"
    _write_server_json(
        server_json,
        {
            "name": "io.github.example/x",
            "version": "0.5.0",
            "packages": [{"identifier": "x", "version": "0.5.0"}],
        },
    )
    monkeypatch.setattr(sync, "PYPROJECT", pyproject)
    monkeypatch.setattr(sync, "SERVER_JSON", server_json)
    monkeypatch.setattr(sync, "REPO_ROOT", tmp_path)
    return tmp_path


def test_main_check_clean_returns_zero(
    fake_repo: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.setattr(sys, "argv", ["sync-server-json.py", "--check"])
    assert sync.main() == 0
    assert "in sync" in capsys.readouterr().out


def test_main_check_drifted_returns_one(
    fake_repo: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    server_path = fake_repo / "server.json"
    data = json.loads(server_path.read_text())
    data["version"] = "9.9.9"
    data["packages"][0]["version"] = "8.8.8"
    _write_server_json(server_path, data)

    monkeypatch.setattr(sys, "argv", ["sync-server-json.py", "--check"])
    assert sync.main() == 1
    err = capsys.readouterr().err
    assert "drift detected" in err
    assert "top_level" in err
    assert "packages[0]" in err
    assert "9.9.9" in err
    assert "8.8.8" in err


def test_main_write_no_op_when_clean(
    fake_repo: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    server_path = fake_repo / "server.json"
    before = server_path.read_bytes()

    monkeypatch.setattr(sys, "argv", ["sync-server-json.py"])
    assert sync.main() == 0
    assert "already in sync" in capsys.readouterr().out
    # No-op invariant: the file is not even rewritten when nothing drifted.
    assert server_path.read_bytes() == before


def test_main_write_fixes_drift_then_check_passes(
    fake_repo: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    server_path = fake_repo / "server.json"
    data = json.loads(server_path.read_text())
    data["version"] = "9.9.9"
    data["packages"][0]["version"] = "8.8.8"
    _write_server_json(server_path, data)

    monkeypatch.setattr(sys, "argv", ["sync-server-json.py"])
    assert sync.main() == 0
    out = capsys.readouterr().out
    assert "Updated" in out
    assert "0.5.0" in out

    fixed = json.loads(server_path.read_text())
    assert fixed["version"] == "0.5.0"
    assert fixed["packages"][0]["version"] == "0.5.0"

    monkeypatch.setattr(sys, "argv", ["sync-server-json.py", "--check"])
    assert sync.main() == 0


def test_main_propagates_pyproject_errors(fake_repo: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    (fake_repo / "pyproject.toml").unlink()
    monkeypatch.setattr(sys, "argv", ["sync-server-json.py", "--check"])
    with pytest.raises(SystemExit) as exc:
        sync.main()
    assert "not found" in str(exc.value)


def test_main_propagates_server_json_errors(
    fake_repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    (fake_repo / "server.json").write_text("{ invalid")
    monkeypatch.setattr(sys, "argv", ["sync-server-json.py", "--check"])
    with pytest.raises(SystemExit) as exc:
        sync.main()
    assert "not valid JSON" in str(exc.value)
