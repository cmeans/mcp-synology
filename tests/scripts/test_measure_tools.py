"""Tests for scripts/measure_tools.py (offline parts and skip paths only; no network)."""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path
from typing import TYPE_CHECKING, Any

import pytest

if TYPE_CHECKING:
    from types import ModuleType

SCRIPT = Path(__file__).resolve().parents[2] / "scripts" / "measure_tools.py"


@pytest.fixture(scope="module")
def mt() -> ModuleType:
    spec = importlib.util.spec_from_file_location("measure_tools", SCRIPT)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules["measure_tools"] = module
    spec.loader.exec_module(module)
    return module


def test_offline_config_uses_test_net_and_all_modules(mt: ModuleType, tmp_path: Path) -> None:
    text = mt.write_offline_config(tmp_path).read_text()
    assert "host: 192.0.2.1" in text
    for module in ("filestation", "system", "downloadstation", "photos"):
        assert module in text
    assert "password" not in text


def test_measure_size_counts_all_48_tools(mt: ModuleType, tmp_path: Path) -> None:
    size = mt.measure_size(mt.write_offline_config(tmp_path))
    assert size["tools"] == 48
    assert size["total"] == size["schemas"] + size["descriptions"] + size["instructions"]
    assert size["schema_titles"] == 0


def test_run_external_reports_missing_executable(mt: ModuleType) -> None:
    result = mt.run_external(["definitely-not-a-real-binary-xyz", "--version"])
    assert result["status"] == "unavailable"
    assert "not found" in result["reason"]


def test_run_external_reports_failure(mt: ModuleType) -> None:
    result = mt.run_external([sys.executable, "-c", "import sys; sys.exit(3)"])
    assert result["status"] == "unavailable"
    assert "exit 3" in result["reason"]


def test_claude_tokens_skipped_without_key(
    mt: ModuleType, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    assert mt.claude_tokens(mt.write_offline_config(tmp_path)) == {
        "status": "skipped",
        "reason": "ANTHROPIC_API_KEY not set",
    }


def test_compare_reports_deltas(mt: ModuleType) -> None:
    old: dict[str, Any] = {
        "size": {"total": 20809},
        "claude_tokens": {"claude-sonnet-5-5": 9127},
        "mcpscore": {"score": 77, "max_score": 92},
    }
    new: dict[str, Any] = {
        "size": {"total": 15730},
        "claude_tokens": {"claude-sonnet-5-5": 6879},
        "mcpscore": {"score": 77, "max_score": 92},
    }
    text = mt.compare(old, new)
    assert "20809 -> 15730 (-5079, -24%)" in text
    assert "9127 -> 6879" in text
    assert "mcpscore: 77/92 -> 77/92" in text


def test_claude_token_errors_keep_the_message(
    mt: ModuleType, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """QA round 1 (#164): a 404 for a retired model ID and a 401 for a bad key must be
    distinguishable, not both `unavailable: HTTPError`."""
    monkeypatch.setenv("ANTHROPIC_API_KEY", "test-key")

    def fail(*_: object, **__: object) -> dict[str, Any]:
        raise RuntimeError("HTTP Error 404: Not Found")

    monkeypatch.setattr(mt, "_post_json", fail)
    counts = mt.claude_tokens(mt.write_offline_config(tmp_path))
    assert all("404: Not Found" in str(v) for v in counts.values()), counts


def test_no_external_also_skips_tdqs_score(mt: ModuleType, monkeypatch: pytest.MonkeyPatch) -> None:
    """QA round 1 (#164): --no-external wins over --tdqs-score; nothing is launched."""
    monkeypatch.setenv("ANTHROPIC_API_KEY", "test-key")  # else score skips before launching
    launched: list[list[str]] = []
    monkeypatch.setattr(mt, "run_external", lambda argv, env=None: launched.append(argv) or {})
    monkeypatch.setattr(mt, "claude_tokens", lambda config: {"status": "skipped"})
    out = Path(mt.REPO) / ".cache" / "measure" / "test-no-external.json"
    try:
        assert mt.main(["--no-external", "--tdqs-score", "--out", str(out)]) == 0
        assert launched == []
    finally:
        out.unlink(missing_ok=True)
