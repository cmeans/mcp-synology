"""A relative `instructions_file`, `logging.file` or `default_download_dir` is from the config
file's folder (#553)."""

from __future__ import annotations

import logging
import os
import threading
from pathlib import Path
from typing import Any

import pytest

from mcp_synology.cli.logging_ import _configure_logging
from mcp_synology.core.config import AppConfig, _config_has_a_folder, load_config
from mcp_synology.server import _read_instructions_file, validate_config


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


def test_a_download_folder_is_from_the_config_files_folder(
    tmp_path: Path, working_folder: Path
) -> None:
    """Pre-QA on #553: it also becomes an allowed local folder, wherever the server started."""
    home = tmp_path / "home-config"
    path = _file(home, "")
    path.write_text(
        "schema_version: 1\nconnection: {host: nas}\n"
        "modules: {filestation: {settings: {default_download_dir: dl}}}\n",
        encoding="utf-8",
    )
    settings = load_config(path).modules["filestation"].settings
    assert settings["default_download_dir"] == os.path.join(os.path.realpath(home), "dl")
    assert validate_config(path) is not None


def _load_from_a_pipe(tmp_path: Path, text: str, *, then_remove: bool = False) -> AppConfig:
    pipe = tmp_path / "pipe"
    os.mkfifo(pipe)

    def feed() -> None:
        pipe.write_text(text, encoding="utf-8")
        if then_remove:
            pipe.unlink()

    # daemon: a regression that doesn't read the pipe mustn't keep pytest from exiting.
    writer = threading.Thread(target=feed, daemon=True)
    writer.start()
    try:
        return load_config(pipe)
    finally:
        writer.join(timeout=5)


_HEAD = "schema_version: 1\nconnection: {host: nas}\n"


@pytest.mark.skipif(not hasattr(os, "mkfifo"), reason="no named pipes here")
@pytest.mark.parametrize(
    ("line", "setting"),
    [
        ("modules: {filestation: }\ninstructions_file: notes.md", "instructions_file"),
        ("modules: {filestation: }\nlogging: {file: x.log}", "logging.file"),
        (
            "modules: {filestation: {settings: {default_download_dir: dl}}}",
            "default_download_dir",
        ),
    ],
)
@pytest.mark.parametrize("then_remove", [False, True], ids=["pipe", "pipe its writer removed"])
def test_a_pipe_has_no_folder_to_find_a_relative_path_from(
    tmp_path: Path, line: str, setting: str, then_remove: bool, _pipe_timeout: None
) -> None:
    """Pre-QA on #553: `--config <(generate)` looked under /dev/fd, and the log's error said to
    fix a path nobody wrote. Pre-QA on #561: a pipe already removed counted as having a folder,
    and the error didn't end by naming the config file, as every other one does."""
    with pytest.raises(ValueError, match=f"{setting} .* has no folder") as exc:
        _load_from_a_pipe(tmp_path, f"{_HEAD}{line}\n", then_remove=then_remove)
    assert any(note.startswith("Config file:") for note in exc.value.__notes__)


@pytest.mark.skipif(not hasattr(os, "mkfifo"), reason="no named pipes here")
@pytest.mark.parametrize(
    "modules",
    [
        "{system: , filestation: {enabled: false, settings: {default_download_dir: dl}}}",
        "{filestation: , photos: {enabled: false, settings: {default_download_dir: dl}}}",
        "{filestation: , photos: {settings: {default_download_dir: dl}}}",
        "{filestation: , nosuch: {settings: {default_download_dir: dl}}}",
    ],
    ids=["disabled File Station", "disabled module", "another module", "unknown module"],
)
def test_a_setting_that_isnt_read_isnt_refused_on_a_pipe(
    tmp_path: Path, modules: str, _pipe_timeout: None
) -> None:
    """Pre-QA on #561: every module's was read, so a pipe config failed on one never loaded."""
    assert _load_from_a_pipe(tmp_path, f"{_HEAD}modules: {modules}\n") is not None


@pytest.mark.skipif(not hasattr(os, "mkfifo"), reason="no named pipes here")
def test_every_relative_path_on_a_pipe_is_reported_at_once(
    tmp_path: Path, _pipe_timeout: None
) -> None:
    """Pre-QA on #561: only the first, so a user fixed one and failed again."""
    with pytest.raises(ValueError) as exc:
        _load_from_a_pipe(
            tmp_path,
            f"{_HEAD}modules: {{filestation: {{settings: {{default_download_dir: dl}}}}}}\n"
            "instructions_file: notes.md\nlogging: {file: x.log}\n",
        )
    for setting in ("instructions_file", "logging.file", "default_download_dir"):
        assert f"{setting} " in str(exc.value)


def test_standard_input_has_no_folder(tmp_path: Path) -> None:
    """Pre-QA on #561: `--config /dev/stdin < c.yaml`, whose folder is `/dev`."""
    real = tmp_path / "real.yaml"
    real.write_text(f"{_HEAD}modules: {{filestation: }}\nlogging: {{file: x.log}}\n", "utf-8")
    saved = os.dup(0)
    fd = os.open(real, os.O_RDONLY)
    try:
        os.dup2(fd, 0)
        with pytest.raises(ValueError, match="logging.file .* has no folder"):
            load_config("/dev/stdin")
    finally:
        os.dup2(saved, 0)
        os.close(saved)
        os.close(fd)


@pytest.mark.parametrize(
    "folder", ["/dev", "/dev/fd", "/proc/123/fd"], ids=["dev", "dev fd", "proc fd"]
)
def test_a_folder_that_only_holds_descriptors_isnt_one(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, folder: str
) -> None:
    """Pre-QA on #561: `/dev/fd` is a real folder on macOS and BSD, and only Linux runs here."""
    real = tmp_path / "real.yaml"
    real.write_text("{}\n", "utf-8")
    monkeypatch.setattr(os.path, "realpath", lambda _path: folder)
    assert _config_has_a_folder(real) is False


def test_a_real_folder_under_dev_stays_one(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    real = tmp_path / "real.yaml"
    real.write_text("{}\n", "utf-8")
    monkeypatch.setattr(os.path, "realpath", lambda _path: "/dev/shm/x")
    assert _config_has_a_folder(real) is True


@pytest.mark.skipif(not os.path.exists("/dev/fd"), reason="no /dev/fd here")
def test_a_redirected_file_has_no_folder_either(tmp_path: Path) -> None:
    """Pre-QA on #561: `--config /dev/fd/3 3< c.yaml` is a regular file, so a relative log path
    became `/proc/<pid>/fd/x.log`, and the error said to fix that."""
    real = tmp_path / "real.yaml"
    real.write_text(f"{_HEAD}modules: {{filestation: }}\nlogging: {{file: x.log}}\n", "utf-8")
    fd = os.open(real, os.O_RDONLY)
    try:
        with pytest.raises(ValueError, match="logging.file .* has no folder"):
            load_config(f"/dev/fd/{fd}")
    finally:
        os.close(fd)


def test_a_working_folder_with_no_name_still_loads(
    working_folder: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """QA round 1 on #561: `realpath` needs the working folder's name, which a chroot or a deleted
    folder doesn't give, and this was a traceback at startup before #541."""
    _file(working_folder, "logging: {file: syn.log}")

    def no_name() -> str:
        raise FileNotFoundError(2, "No such file or directory")

    monkeypatch.setattr(os, "getcwd", no_name)
    assert load_config("c.yaml").logging.file == "syn.log"


def test_a_folder_reached_through_a_link_is_where_the_kernel_finds_it(
    tmp_path: Path, working_folder: Path
) -> None:
    """Pre-QA on #553: `link/../c.yaml` read the instructions from the real parent, and the log
    handler collapsed `..` as text, into the working folder."""
    real = tmp_path / "real" / "sub"
    real.mkdir(parents=True)
    (working_folder / "link").symlink_to(real)
    _file(tmp_path / "real", "logging: {file: syn.log}")
    assert load_config("link/../c.yaml").logging.file == os.path.join(
        os.path.realpath(tmp_path / "real"), "syn.log"
    )


def test_a_link_to_the_config_file_is_that_links_folder(
    tmp_path: Path, working_folder: Path
) -> None:
    path = _file(tmp_path / "dotfiles", "instructions_file: notes.md")
    (working_folder / "c.yaml").symlink_to(path)
    assert load_config("c.yaml").instructions_file == os.path.join(
        os.path.realpath(working_folder), "notes.md"
    )


def test_the_log_says_where_it_went(tmp_path: Path, caplog: pytest.LogCaptureFixture) -> None:
    """Pre-QA on #553: an upgrade moves it, and nothing named the file."""
    root = logging.getLogger()
    before = list(root.handlers)
    target = tmp_path / "syn.log"
    with caplog.at_level("INFO"):
        _configure_logging("info", str(target))
    for handler in [h for h in root.handlers if h not in before]:
        root.removeHandler(handler)
        handler.close()
    assert f"Logging to {str(target)!r}" in caplog.messages
