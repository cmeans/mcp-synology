"""Tests for scripts/runtime_floors.py, the list CI's floor job installs (#115)."""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path
from typing import TYPE_CHECKING

import pytest

if TYPE_CHECKING:
    from types import ModuleType

_SCRIPT_PATH = Path(__file__).resolve().parents[2] / "scripts" / "runtime_floors.py"


def _load_script() -> ModuleType:
    spec = importlib.util.spec_from_file_location("runtime_floors", _SCRIPT_PATH)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules["runtime_floors"] = module
    spec.loader.exec_module(module)
    return module


script = _load_script()


@pytest.mark.parametrize(
    ("dependency", "pin"),
    [
        ("click>=8.2.2", "click==8.2.2"),
        ("mcp>=2.3,<3", "mcp==2.3"),
        ("mcp >= 2.3, < 3", "mcp==2.3"),
        ("a.b_c-d>=1!2.0,!=3,<=4", "a.b_c-d==1!2.0"),
        ("regex>=2026.9.29", "regex==2026.9.29"),
    ],
)
def test_a_floor_becomes_a_pin(dependency: str, pin: str) -> None:
    assert script.floors([dependency]) == [pin]


@pytest.mark.parametrize(
    "dependency",
    [
        "click",
        "click<9",
        "click==8.2.2",
        "click~=8.2",
        "click>8.2",
        "click[extra]>=8.2",
        "click>=8.2; python_version<'3.12'",
        "click>=8.2,>=8.3",
        "",
    ],
)
def test_a_dependency_with_no_readable_floor_is_an_error(dependency: str) -> None:
    with pytest.raises(ValueError, match="no `>=` floor") as caught:
        script.floors(["mcp>=2.3,<3", dependency])
    assert repr(dependency) in str(caught.value)


def test_the_real_pyproject_gives_one_pin_per_dependency(
    capsys: pytest.CaptureFixture[str],
) -> None:
    assert script.main() == 0
    pins = capsys.readouterr().out.splitlines()
    names = {pin.partition("==")[0] for pin in pins}
    assert len(pins) == len(names) > 0
    assert {"mcp", "httpx", "pydantic", "click"} <= names


def test_an_unreadable_dependency_fails_the_run(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    pyproject = tmp_path / "pyproject.toml"
    pyproject.write_text('[project]\ndependencies = ["click"]\n', encoding="utf-8")
    assert script.main(pyproject) == 1
    captured = capsys.readouterr()
    assert captured.out == ""
    assert "'click' has no `>=` floor" in captured.err
