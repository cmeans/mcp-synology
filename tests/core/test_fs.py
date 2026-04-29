"""Tests for core/fs.py — atomic_write_text."""

from __future__ import annotations

from pathlib import Path
from unittest.mock import patch

import pytest

from mcp_synology.core.fs import atomic_write_text


class TestAtomicWriteText:
    def test_writes_content(self, tmp_path: Path) -> None:
        path = tmp_path / "out.yaml"
        atomic_write_text(path, "hello\n")
        assert path.read_text(encoding="utf-8") == "hello\n"

    def test_creates_missing_parent_dirs(self, tmp_path: Path) -> None:
        path = tmp_path / "deep" / "nested" / "dir" / "out.yaml"
        atomic_write_text(path, "hi\n")
        assert path.read_text(encoding="utf-8") == "hi\n"

    def test_overwrites_existing_file(self, tmp_path: Path) -> None:
        path = tmp_path / "out.yaml"
        path.write_text("old contents\n", encoding="utf-8")
        atomic_write_text(path, "new contents\n")
        assert path.read_text(encoding="utf-8") == "new contents\n"

    def test_no_temp_file_left_behind_on_success(self, tmp_path: Path) -> None:
        path = tmp_path / "out.yaml"
        atomic_write_text(path, "hi\n")
        # Only the target file should exist; .tmp sibling should be gone.
        assert path.exists()
        assert not (tmp_path / "out.yaml.tmp").exists()
        assert sorted(p.name for p in tmp_path.iterdir()) == ["out.yaml"]

    def test_replace_failure_cleans_up_tmp_and_raises(self, tmp_path: Path) -> None:
        """If `Path.replace` raises mid-write, the temp file is cleaned up."""
        path = tmp_path / "out.yaml"

        # Patch Path.replace globally so the rename fails AFTER the .tmp is
        # written. Verify (a) the original exception propagates, (b) the
        # .tmp sibling is removed, (c) no partial file at the target.
        original_replace = Path.replace

        def fail_replace(self: Path, *args: object, **kwargs: object) -> None:
            raise OSError("simulated rename failure")

        with (
            patch.object(Path, "replace", fail_replace),
            pytest.raises(OSError, match="simulated rename failure"),
        ):
            atomic_write_text(path, "hi\n")

        # Sanity: original_replace is what we restored implicitly.
        assert Path.replace is original_replace

        # The target file must NOT exist (no torn write at the canonical path).
        assert not path.exists()
        # The temp file must NOT linger.
        assert not (tmp_path / "out.yaml.tmp").exists()
        # And the directory must be empty.
        assert list(tmp_path.iterdir()) == []

    def test_replace_failure_does_not_clobber_existing_target(self, tmp_path: Path) -> None:
        """Existing target file is preserved if rename fails — no torn write."""
        path = tmp_path / "out.yaml"
        path.write_text("important previous contents\n", encoding="utf-8")

        def fail_replace(self: Path, *args: object, **kwargs: object) -> None:
            raise OSError("simulated rename failure")

        with (
            patch.object(Path, "replace", fail_replace),
            pytest.raises(OSError, match="simulated rename failure"),
        ):
            atomic_write_text(path, "new contents that should not appear\n")

        # Existing file untouched — atomicity guarantee.
        assert path.read_text(encoding="utf-8") == "important previous contents\n"
        assert not (tmp_path / "out.yaml.tmp").exists()

    def test_custom_encoding(self, tmp_path: Path) -> None:
        path = tmp_path / "out.yaml"
        atomic_write_text(path, "héllo\n", encoding="latin-1")
        assert path.read_bytes() == "héllo\n".encode("latin-1")
