"""Tests for core/local_paths.py — where tools may read and write on this machine."""

from __future__ import annotations

import os
import sys
from pathlib import Path
from typing import Any

import pytest

from mcp_synology.core.local_paths import (
    allowed_local_roots,
    default_downloads_dir,
    outside_allowed_roots,
)


class TestUserDirsFileThatCantBeRead:
    """QA round 2 on #541: every command reads this file, so it can't hang or crash them."""

    @pytest.mark.parametrize("kind", ["device", "pipe"])
    def test_a_device_or_pipe_means_no_setting(
        self, tmp_path: Path, run_capped: Any, kind: str
    ) -> None:
        dirs_file = tmp_path / "user-dirs.dirs"
        if kind == "pipe":
            os.mkfifo(dirs_file)
        else:
            dirs_file.symlink_to("/dev/zero")
        result = run_capped(
            "-c",
            "import os, sys\n"
            "os.environ['XDG_CONFIG_HOME'] = sys.argv[1]\n"
            "from mcp_synology.core.local_paths import _xdg_downloads_dir\n"
            "print(_xdg_downloads_dir())",
            str(tmp_path),
        )
        assert result.returncode == 0, result.stderr
        assert result.stdout.strip() == "None"

    def test_bytes_that_arent_utf8_mean_no_setting(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        (tmp_path / "user-dirs.dirs").write_bytes(b'XDG_DOWNLOAD_DIR="/data/t\xe9l\xe9"\n')
        monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path))
        assert default_downloads_dir(platform="linux") == Path.home() / "Downloads"


class TestDefaultDownloadsDir:
    def test_linux_reads_xdg_user_dirs(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        home = tmp_path / "home"
        (home / ".config").mkdir(parents=True)
        (home / ".config" / "user-dirs.dirs").write_text(
            '# comment\nXDG_DESKTOP_DIR="$HOME/Desktop"\n'
            'XDG_DOWNLOAD_DIR="$HOME/Téléchargements"\n',
            encoding="utf-8",
        )
        monkeypatch.setattr(Path, "home", lambda: home)
        monkeypatch.delenv("XDG_CONFIG_HOME", raising=False)
        assert default_downloads_dir(platform="linux") == home / "Téléchargements"

    def test_linux_honors_xdg_config_home_and_absolute_values(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        cfg = tmp_path / "cfg"
        cfg.mkdir()
        (cfg / "user-dirs.dirs").write_text('XDG_DOWNLOAD_DIR="/data/dl"\n', encoding="utf-8")
        monkeypatch.setenv("XDG_CONFIG_HOME", str(cfg))
        assert default_downloads_dir(platform="linux") == Path("/data/dl")

    def test_linux_without_user_dirs_falls_back_to_home_downloads(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(Path, "home", lambda: tmp_path)
        monkeypatch.delenv("XDG_CONFIG_HOME", raising=False)
        assert default_downloads_dir(platform="linux") == tmp_path / "Downloads"

    def test_xdg_entry_naming_home_itself_is_ignored(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """xdg-user-dirs sets a disabled directory to $HOME; confining to all of home
        would be a much wider default than the user chose, so fall back instead."""
        (tmp_path / ".config").mkdir()
        (tmp_path / ".config" / "user-dirs.dirs").write_text(
            'XDG_DOWNLOAD_DIR="$HOME/"\n', encoding="utf-8"
        )
        monkeypatch.setattr(Path, "home", lambda: tmp_path)
        monkeypatch.delenv("XDG_CONFIG_HOME", raising=False)
        assert default_downloads_dir(platform="linux") == tmp_path / "Downloads"

    @pytest.mark.parametrize(
        "contents",
        [
            'XDG_DESKTOP_DIR="$HOME/Desktop"\n',  # no Downloads entry at all
            'XDG_DOWNLOAD_DIR="Downloads"\n',  # neither $HOME-relative nor absolute
        ],
    )
    def test_unusable_xdg_entry_falls_back_to_home_downloads(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, contents: str
    ) -> None:
        (tmp_path / ".config").mkdir()
        (tmp_path / ".config" / "user-dirs.dirs").write_text(contents, encoding="utf-8")
        monkeypatch.setattr(Path, "home", lambda: tmp_path)
        monkeypatch.delenv("XDG_CONFIG_HOME", raising=False)
        assert default_downloads_dir(platform="linux") == tmp_path / "Downloads"

    def test_macos_is_home_downloads(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(Path, "home", lambda: tmp_path)
        assert default_downloads_dir(platform="darwin") == tmp_path / "Downloads"

    def test_windows_is_profile_downloads(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(Path, "home", lambda: tmp_path)
        assert default_downloads_dir(platform="win32") == tmp_path / "Downloads"


class TestAllowedLocalRoots:
    def test_default_is_the_downloads_dir(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(
            "mcp_synology.core.local_paths.default_downloads_dir",
            lambda: tmp_path / "Downloads",
        )
        assert allowed_local_roots(None, None) == [(tmp_path / "Downloads").resolve()]

    def test_configured_list_replaces_the_default_and_expands_home(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(Path, "home", lambda: tmp_path)
        monkeypatch.setenv("HOME", str(tmp_path))
        roots = allowed_local_roots(["~/media", str(tmp_path / "other")], None)
        assert roots == [(tmp_path / "media").resolve(), (tmp_path / "other").resolve()]

    def test_default_download_dir_is_always_allowed(self, tmp_path: Path) -> None:
        roots = allowed_local_roots([str(tmp_path / "a")], str(tmp_path / "dl"))
        assert (tmp_path / "dl").resolve() in roots

    def test_duplicates_are_dropped(self, tmp_path: Path) -> None:
        roots = allowed_local_roots([str(tmp_path), str(tmp_path) + "/"], str(tmp_path))
        assert roots == [tmp_path.resolve()]


class TestOutsideAllowedRoots:
    def test_path_inside_a_root_is_allowed(self, tmp_path: Path) -> None:
        assert outside_allowed_roots(str(tmp_path / "a" / "b.txt"), [tmp_path.resolve()]) is None

    def test_the_root_itself_is_allowed(self, tmp_path: Path) -> None:
        assert outside_allowed_roots(str(tmp_path), [tmp_path.resolve()]) is None

    def test_sibling_with_shared_prefix_is_refused(self, tmp_path: Path) -> None:
        root = tmp_path / "Downloads"
        assert outside_allowed_roots(str(tmp_path / "Downloads2" / "x"), [root.resolve()])

    def test_dotdot_escape_is_refused(self, tmp_path: Path) -> None:
        root = (tmp_path / "Downloads").resolve()
        assert outside_allowed_roots(str(root / ".." / "secret.txt"), [root])

    @pytest.mark.skipif(sys.platform == "win32", reason="symlinks need privileges on Windows")
    def test_symlink_leading_out_of_a_root_is_refused(self, tmp_path: Path) -> None:
        root = tmp_path / "Downloads"
        root.mkdir()
        outside = tmp_path / "outside"
        outside.mkdir()
        (root / "link").symlink_to(outside)
        assert outside_allowed_roots(str(root / "link" / "f.txt"), [root.resolve()])

    @pytest.mark.skipif(sys.platform == "win32", reason="symlinks need privileges on Windows")
    def test_root_reached_through_a_symlink_is_allowed(self, tmp_path: Path) -> None:
        real = tmp_path / "real"
        real.mkdir()
        alias = tmp_path / "alias"
        alias.symlink_to(real)
        # The root is resolved too, so a path under either name is inside it.
        roots = allowed_local_roots([str(alias)], None)
        assert outside_allowed_roots(str(real / "f.txt"), roots) is None
        assert outside_allowed_roots(str(alias / "f.txt"), roots) is None

    @pytest.mark.skipif(sys.platform == "win32", reason="'/' is one drive's root on Windows")
    def test_slash_allows_everything(self, tmp_path: Path) -> None:
        roots = allowed_local_roots(["/"], None)
        assert outside_allowed_roots(str(tmp_path / "anything"), roots) is None
        assert outside_allowed_roots("/etc/hosts", roots) is None

    def test_returns_the_resolved_path_it_refused(self, tmp_path: Path) -> None:
        root = (tmp_path / "Downloads").resolve()
        refused = outside_allowed_roots(str(root / ".." / "x"), [root])
        assert refused == (tmp_path / "x").resolve()


class TestUnresolvablePaths:
    """QA R1 (#186): Path.resolve raises on a NUL byte (all versions) and on a symlink
    loop (3.11/3.12); that must be a clear error, never a raw exception."""

    def test_nul_in_a_configured_entry_is_a_config_error(self) -> None:
        with pytest.raises(ValueError, match="allowed_local_paths"):
            allowed_local_roots(["/tmp/a\x00b"], None)

    def test_resolution_failure_at_registration_names_the_entry(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        real_resolve = Path.resolve

        def _loop(self: Path, strict: bool = False) -> Path:
            if self.name == "loop":
                raise RuntimeError(f"Symlink loop from {self!r}")  # what 3.11/3.12 raise
            return real_resolve(self, strict=strict)

        monkeypatch.setattr(Path, "resolve", _loop)
        with pytest.raises(ValueError, match="default_download_dir"):
            allowed_local_roots([str(tmp_path)], str(tmp_path / "loop"))

    def test_outside_allowed_roots_raises_value_error_for_nul(self, tmp_path: Path) -> None:
        with pytest.raises(ValueError, match="can't be resolved"):
            outside_allowed_roots(str(tmp_path / "a\x00b"), [tmp_path.resolve()])

    def test_a_path_the_locale_cant_encode_doesnt_show_the_codec_text(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Pre-QA on #541: `'ascii' codec can't encode character …` told the user nothing."""

        def _refuse(self: Path, strict: bool = False) -> Path:
            raise UnicodeEncodeError("ascii", str(self), 0, 1, "ordinal not in range(128)")

        monkeypatch.setattr(Path, "resolve", _refuse)
        with pytest.raises(ValueError, match="locale can't encode the path") as exc:
            outside_allowed_roots(str(tmp_path / "x"), [tmp_path])
        assert "codec" not in str(exc.value)


class TestAllowedLocalPathsSetting:
    """QA R1 (#186): a relative entry would resolve against the server's working
    directory, which depends on how the MCP client launched it."""

    def test_relative_entry_is_rejected(self) -> None:
        from pydantic import ValidationError

        from mcp_synology.modules.filestation import FileStationSettings

        with pytest.raises(ValidationError, match="absolute"):
            FileStationSettings(allowed_local_paths=["Downloads"])

    @pytest.mark.parametrize("field", ["allowed_local_paths", "default_download_dir"])
    def test_unknown_home_user_is_a_validation_error(self, field: str) -> None:
        """QA R2 (#186): expanduser raises RuntimeError for ~nosuchuser, which pydantic
        doesn't wrap; it must be a ValidationError naming the entry."""
        from pydantic import ValidationError

        from mcp_synology.modules.filestation import FileStationSettings

        entry = "~mcp_no_such_user_xyz/dl"
        value: object = [entry] if field == "allowed_local_paths" else entry
        with pytest.raises(ValidationError, match="mcp_no_such_user_xyz"):
            FileStationSettings(**{field: value})

    def test_home_relative_and_absolute_entries_are_accepted(self, tmp_path: Path) -> None:
        from mcp_synology.modules.filestation import FileStationSettings

        s = FileStationSettings(allowed_local_paths=["~/Downloads", str(tmp_path)])
        assert s.allowed_local_paths == ["~/Downloads", str(tmp_path)]


class TestSharedPaths:
    """#257: a top-level list shared by every module, plus each module's own."""

    def test_shared_and_module_paths_are_unioned(self, tmp_path: Path) -> None:
        s, m = tmp_path / "s", tmp_path / "m"
        roots = allowed_local_roots([str(m)], shared_paths=[str(s)])
        assert roots == [s.resolve(), m.resolve()]

    def test_shared_set_drops_the_downloads_default(self, tmp_path: Path) -> None:
        assert allowed_local_roots(None, shared_paths=[str(tmp_path)]) == [tmp_path.resolve()]

    def test_unset_shared_keeps_todays_rule(self, tmp_path: Path) -> None:
        assert allowed_local_roots(None) == [default_downloads_dir().resolve()]
        assert allowed_local_roots([str(tmp_path)]) == [tmp_path.resolve()]

    def test_default_download_dir_added_once(self, tmp_path: Path) -> None:
        s = str(tmp_path)
        assert allowed_local_roots([s], s, shared_paths=[s]) == [tmp_path.resolve()]

    def test_error_names_the_setting(self) -> None:
        with pytest.raises(ValueError) as shared:
            allowed_local_roots(None, shared_paths=["/a\x00b"])
        assert "allowed_local_paths" in str(shared.value)
        assert "filestation setting" not in str(shared.value)
        with pytest.raises(ValueError) as own:
            allowed_local_roots(["/a\x00b"], module="downloadstation")
        assert "downloadstation setting allowed_local_paths" in str(own.value)


class TestLocalPathsQuoted:
    """#697: a local path in a refusal, a config error, a tool description or the instructions
    is written as replies write one (`shown_local`): quoted when a part holds a tab, padding,
    `, ` or a character that draws nothing, so it can't hide or read as two paths."""

    @staticmethod
    def _refusal(value: str, roots: list[Path]) -> dict[str, Any]:
        import json

        from mcp.server.mcpserver.exceptions import ToolError

        from mcp_synology.core.local_paths import refuse_outside_roots

        with pytest.raises(ToolError) as exc:
            refuse_outside_roots("Op", "local_path", value, Path(value), roots, module="x")
        text = str(exc.value)
        return json.loads(text[text.index("{") :])["error"]  # type: ignore[no-any-return]

    def test_a_refused_path_and_one_folder_are_shown(self, tmp_path: Path) -> None:
        root = (tmp_path / "ok, dl").resolve()
        value = str(tmp_path / "out" / "a\tb.txt")
        err = self._refusal(value, [root])
        assert err["message"] == (
            f"Op refused: {value!r} is outside the folders this server may use on this "
            f"machine: '{root}'."
        ), err
        assert err["value"] == value  # the caller's text, as sent

    def test_two_folders_are_one_a_line(self, tmp_path: Path) -> None:
        roots = [(tmp_path / "a").resolve(), (tmp_path / "b ").resolve()]
        err = self._refusal(str(tmp_path / "c.txt"), roots)
        assert err["message"].endswith(f"on this machine:\n  {roots[0]}\n  {str(roots[1])!r}"), err

    @pytest.mark.skipif(sys.platform == "win32", reason="symlinks need privileges on Windows")
    def test_the_path_a_link_resolves_to_is_shown(self, tmp_path: Path) -> None:
        root = tmp_path / "dl"
        root.mkdir()
        outside = tmp_path / "x, y"
        outside.mkdir()
        (root / "link").symlink_to(outside)
        err = self._refusal(str(root / "link" / "f.txt"), [root.resolve()])
        assert f"(resolves to '{outside.resolve() / 'f.txt'}')" in err["message"], err

    def test_an_unknown_user_is_refused_shown(self, tmp_path: Path) -> None:
        """The refusal that hides which local users exist writes the path shown too."""
        import json

        from mcp.server.mcpserver.exceptions import ToolError

        from mcp_synology.core.local_paths import refuse_outside_roots

        value = "~no-such-user-697/a\tb.txt"
        with pytest.raises(ToolError) as exc:
            refuse_outside_roots(
                "Op",
                "local_path",
                value,
                Path(value),
                [tmp_path.resolve()],
                module="x",
                hide_unknown_users=True,
            )
        text = str(exc.value)
        message = json.loads(text[text.index("{") :])["error"]["message"]
        assert message.startswith(f"Op refused: {value!r} is outside"), message

    def test_an_unresolvable_path_is_shown_escaped(self, tmp_path: Path) -> None:
        err = self._refusal(str(tmp_path / "a\x00b"), [tmp_path.resolve()])
        assert "\\x00" in err["message"] and "\x00" not in err["message"], err

    def test_the_description_quotes_each_folder(self) -> None:
        from mcp_synology.core.local_paths import roots_for_description

        text = roots_for_description([Path("/home/u/a, b"), Path("/home/u/c ")])
        assert text == "one of the allowed local folders ('/home/u/a, b', '/home/u/c ')"

    def test_a_config_entry_is_shown(self) -> None:
        from mcp_synology.core.local_paths import check_local_path_entries

        with pytest.raises(ValueError, match="start with ~\\): 'a, b'$"):
            check_local_path_entries("allowed_local_paths", ["a, b"])


@pytest.mark.parametrize(
    ("raised", "said"),
    [
        (RuntimeError("Symlink loop from '/x/loop'"), "a symlink loop"),
        (
            OSError(40, "Too many levels of symbolic links", "/x/loop"),
            "Too many levels of symbolic links",
        ),
        (OSError("bare"), "OSError"),
    ],
)
def test_an_unresolvable_path_names_it_once(
    monkeypatch: pytest.MonkeyPatch, raised: Exception, said: str
) -> None:
    """Pre-QA r2 on #712: the error's own text repeated the path with repr (3.11/3.12)."""
    from mcp_synology.core import local_paths

    def boom(self: Path, strict: bool = False) -> Path:
        raise raised

    monkeypatch.setattr(Path, "resolve", boom)
    with pytest.raises(ValueError) as exc:
        local_paths._resolve("/x/loop")
    assert str(exc.value) == f"/x/loop can't be resolved: {said}", exc.value
