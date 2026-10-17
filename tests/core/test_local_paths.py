"""Tests for core/local_paths.py — where tools may read and write on this machine."""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

from mcp_synology.core.local_paths import (
    allowed_local_roots,
    default_downloads_dir,
    outside_allowed_roots,
)


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
