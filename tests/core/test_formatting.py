"""Tests for core/formatting.py — all shared formatters."""

import json

import pytest
from mcp.server.fastmcp.exceptions import ToolError

from mcp_synology.core.errors import (
    FileStationError,
    PathNotFoundError,
    SynologyError,
)
from mcp_synology.core.formatting import (
    TreeNode,
    error_response,
    format_error,
    format_key_value,
    format_size,
    format_status,
    format_table,
    format_timestamp,
    format_tree,
    synology_error_response,
)


class TestFormatTable:
    def test_basic_table(self) -> None:
        result = format_table(
            headers=["Name", "Size"],
            rows=[["file.txt", "1 KB"], ["image.png", "2 MB"]],
        )
        assert "Name" in result
        assert "file.txt" in result
        assert "image.png" in result

    def test_table_with_title(self) -> None:
        result = format_table(
            headers=["Name"],
            rows=[["test"]],
            title="My Files",
        )
        assert result.startswith("My Files")
        assert "===" in result

    def test_empty_table(self) -> None:
        result = format_table(headers=["Name"], rows=[])
        assert "No items" in result

    def test_column_alignment(self) -> None:
        result = format_table(
            headers=["A", "B"],
            rows=[["short", "x"], ["a very long value", "y"]],
        )
        lines = result.strip().split("\n")
        # Header and separator should be aligned
        assert len(lines) >= 4


class TestFormatKeyValue:
    def test_basic_pairs(self) -> None:
        result = format_key_value([("Name", "test.txt"), ("Size", "1 KB")])
        assert "Name:" in result
        assert "test.txt" in result

    def test_with_title(self) -> None:
        result = format_key_value([("Key", "Val")], title="Info")
        assert result.startswith("Info")

    def test_empty_pairs(self) -> None:
        result = format_key_value([])
        assert "No data" in result


class TestFormatStatus:
    def test_success(self) -> None:
        result = format_status("Operation completed")
        assert result.startswith("[+]")

    def test_failure(self) -> None:
        result = format_status("Operation failed", success=False)
        assert result.startswith("[!]")


class TestFormatTree:
    def test_flat_tree(self) -> None:
        nodes = [TreeNode("a"), TreeNode("b"), TreeNode("c")]
        result = format_tree(nodes)
        assert "├── a" in result
        assert "└── c" in result

    def test_nested_tree(self) -> None:
        nodes = [
            TreeNode(
                "root",
                children=[
                    TreeNode("child1"),
                    TreeNode("child2", children=[TreeNode("grandchild")]),
                ],
            )
        ]
        result = format_tree(nodes)
        assert "root" in result
        assert "child1" in result
        assert "grandchild" in result

    def test_empty_tree(self) -> None:
        result = format_tree([])
        assert "(empty)" in result

    def test_tree_with_title(self) -> None:
        result = format_tree([TreeNode("x")], title="Tree")
        assert result.startswith("Tree")


class TestFormatError:
    def test_error_with_suggestion(self) -> None:
        result = format_error("Move files", "Path not found", "Use list_files to browse")
        assert "[!]" in result
        assert "Move files" in result
        assert "Path not found" in result
        assert "Suggestion:" in result

    def test_error_without_suggestion(self) -> None:
        result = format_error("Delete", "Unknown error")
        assert "Suggestion" not in result


class TestFormatSize:
    def test_zero(self) -> None:
        assert format_size(0) == "0 B"

    def test_bytes(self) -> None:
        assert format_size(500) == "500 B"

    def test_kilobytes(self) -> None:
        assert format_size(1024) == "1 KB"
        assert format_size(1536) == "1.5 KB"

    def test_megabytes(self) -> None:
        assert format_size(1048576) == "1 MB"

    def test_gigabytes(self) -> None:
        assert format_size(1073741824) == "1 GB"

    def test_terabytes(self) -> None:
        assert format_size(1099511627776) == "1 TB"


class TestFormatTimestamp:
    def test_epoch_zero(self) -> None:
        result = format_timestamp(0)
        assert result == "1970-01-01 00:00:00"

    def test_known_timestamp(self) -> None:
        # 2025-03-15 12:00:00 UTC
        result = format_timestamp(1742040000)
        assert "2025-03-15" in result


class TestErrorResponse:
    def test_raises_tool_error(self) -> None:
        with pytest.raises(ToolError) as exc_info:
            error_response("not_found", "File not found", retryable=False)
        body = json.loads(str(exc_info.value))
        assert body["status"] == "error"
        assert body["error"]["code"] == "not_found"
        assert body["error"]["message"] == "File not found"
        assert body["error"]["retryable"] is False

    def test_includes_optional_fields(self) -> None:
        with pytest.raises(ToolError) as exc_info:
            error_response(
                "invalid_parameter",
                "Bad param",
                retryable=False,
                param="path",
                value="/bad",
                valid=["/good1", "/good2"],
                suggestion="Try a valid path.",
                help_url="https://example.com",
            )
        body = json.loads(str(exc_info.value))
        err = body["error"]
        assert err["param"] == "path"
        assert err["value"] == "/bad"
        assert err["valid"] == ["/good1", "/good2"]
        assert err["suggestion"] == "Try a valid path."
        assert err["help_url"] == "https://example.com"

    def test_omits_optional_fields_when_none(self) -> None:
        with pytest.raises(ToolError) as exc_info:
            error_response("dsm_error", "Something broke", retryable=True)
        body = json.loads(str(exc_info.value))
        err = body["error"]
        assert "param" not in err
        assert "value" not in err
        assert "valid" not in err
        assert "suggestion" not in err
        assert "help_url" not in err
        assert err["retryable"] is True


class TestSynologyErrorResponse:
    def test_maps_path_not_found(self) -> None:
        exc = PathNotFoundError("No such file", code=408, suggestion="Check path")
        with pytest.raises(ToolError) as exc_info:
            synology_error_response("List files", exc)
        body = json.loads(str(exc_info.value))
        assert body["error"]["code"] == "not_found"
        assert body["error"]["retryable"] is False
        assert "List files" in body["error"]["message"]
        assert "408" in body["error"]["message"]
        assert body["error"]["suggestion"] == "Check path"

    def test_maps_generic_synology_error(self) -> None:
        exc = SynologyError("Unknown", code=100)
        with pytest.raises(ToolError) as exc_info:
            synology_error_response("Operation", exc)
        body = json.loads(str(exc_info.value))
        assert body["error"]["code"] == "dsm_error"

    def test_maps_filestation_error(self) -> None:
        exc = FileStationError("FS error", code=401)
        with pytest.raises(ToolError) as exc_info:
            synology_error_response("Copy", exc)
        body = json.loads(str(exc_info.value))
        assert body["error"]["code"] == "filestation_error"

    def test_includes_help_url(self) -> None:
        exc = PathNotFoundError("Not found", code=408)
        with pytest.raises(ToolError) as exc_info:
            synology_error_response("Get info", exc)
        body = json.loads(str(exc_info.value))
        assert body["error"]["help_url"] is not None

    def test_no_code(self) -> None:
        exc = SynologyError("Network timeout")
        with pytest.raises(ToolError) as exc_info:
            synology_error_response("Upload", exc)
        body = json.loads(str(exc_info.value))
        # When no code, message should not contain "DSM error"
        assert "DSM error" not in body["error"]["message"]
        assert "Upload failed" in body["error"]["message"]
