"""Tests for modules/filestation/local_paths.py — where upload/download may touch this machine."""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

from mcp_synology.modules.filestation.local_paths import (
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
            "mcp_synology.modules.filestation.local_paths.default_downloads_dir",
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
