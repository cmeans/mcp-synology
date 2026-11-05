"""A relative `instructions_file` or `logging.file` is from the config file's folder (#553)."""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any

import pytest

from mcp_synology.core.config import load_config
from mcp_synology.server import _read_instructions_file


def _file(folder: Path, extra: str) -> Path:
    folder.mkdir(parents=True, exist_ok=True)
    path = folder / "c.yaml"
    path.write_text(
        f"schema_version: 1\nconnection: {{host: nas}}\nmodules: {{filestation: }}\n{extra}\n",
        encoding="utf-8",
    )
    return path


@pytest.fixture
def working_folder(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    here = tmp_path / "opened-repo"
    here.mkdir()
    monkeypatch.chdir(here)
    return here


@pytest.mark.parametrize(
    ("line", "value"),
    [
        ("instructions_file: notes.md", lambda c: c.instructions_file),
        ("logging: {file: logs/syn.log}", lambda c: c.logging.file),
    ],
    ids=["instructions_file", "logging.file"],
)
def test_a_relative_path_is_from_the_config_files_folder(
    tmp_path: Path, working_folder: Path, line: str, value: Any
) -> None:
    home = tmp_path / "home-config"
    assert Path(value(load_config(_file(home, line)))).parent.is_relative_to(home)


def test_a_folder_you_opened_cant_supply_the_instructions(
    tmp_path: Path, working_folder: Path
) -> None:
    """The issue's case: the planted text reached the model."""
    (working_folder / "notes.md").write_text("PLANTED-FROM-FOLDER", encoding="utf-8")
    home = tmp_path / "home-config"
    config = load_config(_file(home, "instructions_file: notes.md"))
    text, problem = _read_instructions_file(config)
    assert text is None
    assert problem is not None
    (home / "notes.md").write_text("mine", encoding="utf-8")
    assert _read_instructions_file(config)[0] == "mine"


def test_it_doesnt_depend_on_where_the_server_starts(
    tmp_path: Path, working_folder: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = _file(tmp_path / "home-config", "logging: {file: syn.log}")
    first = load_config(path).logging.file
    other = tmp_path / "elsewhere"
    other.mkdir()
    monkeypatch.chdir(other)
    assert load_config(path).logging.file == first


def test_a_relative_config_flag_gives_an_absolute_path(working_folder: Path) -> None:
    _file(working_folder, "logging: {file: syn.log}")
    file = load_config("./c.yaml").logging.file
    assert file is not None
    assert os.path.isabs(file)
    assert Path(file).parent.samefile(working_folder)


@pytest.mark.parametrize(
    "value",
    ["~/syn.log", "~nosuchuser553/x.log"],
    ids=["home", "an unknown user keeps its own error"],
)
def test_a_tilde_isnt_relative(tmp_path: Path, working_folder: Path, value: str) -> None:
    assert load_config(_file(tmp_path, f"logging: {{file: {value}}}")).logging.file == value


def test_an_absolute_path_stays(tmp_path: Path, working_folder: Path) -> None:
    target = tmp_path / "abs" / "syn.log"
    config = load_config(_file(tmp_path, f"logging: {{file: {target}}}"))
    assert config.logging.file == str(target)


def test_a_config_named_in_a_folder_keeps_that_folders_files(working_folder: Path) -> None:
    """Naming it is the consent, and its own folder is where its files are."""
    (working_folder / "notes.md").write_text("mine", encoding="utf-8")
    config = load_config(_file(working_folder, "instructions_file: notes.md"))
    assert _read_instructions_file(config)[0] == "mine"
