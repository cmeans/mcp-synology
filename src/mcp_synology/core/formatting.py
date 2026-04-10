"""Shared response formatters (table, key-value, status, tree, error)."""

from __future__ import annotations

import datetime
import json
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, NoReturn

if TYPE_CHECKING:
    from mcp_synology.core.errors import ErrorCode, SynologyError


def format_table(
    headers: list[str],
    rows: list[list[str]],
    title: str | None = None,
) -> str:
    """Format data as an aligned text table.

    Args:
        headers: Column header names.
        rows: List of rows, each a list of cell values.
        title: Optional title displayed above the table.
    """
    if not rows:
        parts: list[str] = []
        if title:
            parts.append(title)
            parts.append("=" * len(title))
        parts.append("No items to display.")
        return "\n".join(parts)

    # Calculate column widths
    all_rows = [headers, *rows]
    col_widths = [
        max(len(str(row[i])) if i < len(row) else 0 for row in all_rows)
        for i in range(len(headers))
    ]

    def format_row(cells: list[str]) -> str:
        parts = []
        for i, cell in enumerate(cells):
            width = col_widths[i] if i < len(col_widths) else 0
            parts.append(str(cell).ljust(width))
        return "  ".join(parts)

    lines: list[str] = []
    if title:
        lines.append(title)
        lines.append("=" * max(len(title), sum(col_widths) + 2 * (len(col_widths) - 1)))

    lines.append("  " + format_row(headers))
    lines.append("  " + format_row(["─" * w for w in col_widths]))
    lines.extend("  " + format_row(row) for row in rows)

    return "\n".join(lines)


def format_key_value(
    pairs: list[tuple[str, str]],
    title: str | None = None,
) -> str:
    """Format data as aligned key-value pairs.

    Args:
        pairs: List of (key, value) tuples.
        title: Optional title displayed above the pairs.
    """
    if not pairs:
        parts: list[str] = []
        if title:
            parts.append(title)
            parts.append("=" * len(title))
        parts.append("No data to display.")
        return "\n".join(parts)

    max_key_len = max(len(k) for k, _ in pairs)

    lines: list[str] = []
    if title:
        lines.append(title)
        lines.append("=" * max(len(title), max_key_len + 20))

    for key, value in pairs:
        lines.append(f"  {key + ':':<{max_key_len + 1}}  {value}")

    return "\n".join(lines)


def format_status(message: str, success: bool = True) -> str:
    """Format an operation status message.

    Args:
        message: The status message.
        success: Whether the operation succeeded.
    """
    marker = "+" if success else "!"
    return f"[{marker}] {message}"


@dataclass
class TreeNode:
    """A node in a tree structure for format_tree."""

    name: str
    children: list[TreeNode] | None = None


def format_tree(
    nodes: list[TreeNode],
    title: str | None = None,
) -> str:
    """Format data as a tree structure.

    Args:
        nodes: Top-level tree nodes.
        title: Optional title displayed above the tree.
    """
    lines: list[str] = []
    if title:
        lines.append(title)
        lines.append("=" * len(title))

    def _render(node_list: list[TreeNode], prefix: str = "") -> None:
        for i, node in enumerate(node_list):
            is_last = i == len(node_list) - 1
            connector = "└── " if is_last else "├── "
            lines.append(f"{prefix}{connector}{node.name}")
            if node.children:
                extension = "    " if is_last else "│   "
                _render(node.children, prefix + extension)

    if not nodes:
        lines.append("(empty)")
    else:
        _render(nodes)

    return "\n".join(lines)


def format_error(
    operation: str,
    error: str,
    suggestion: str | None = None,
) -> str:
    """Format an error message with optional suggestion.

    Args:
        operation: What was being attempted.
        error: The error description.
        suggestion: Optional actionable suggestion.
    """
    lines = [f"[!] {operation} failed: {error}"]
    if suggestion:
        lines.append(f"    Suggestion: {suggestion}")
    return "\n".join(lines)


def error_response(
    code: ErrorCode,
    message: str,
    *,
    retryable: bool,
    param: str | None = None,
    value: Any | None = None,
    valid: list[str] | None = None,
    suggestion: str | None = None,
    help_url: str | None = None,
) -> NoReturn:
    """Build a structured error envelope and raise ToolError.

    The MCP SDK wraps ToolError in a CallToolResult with isError=True,
    so clients get proper error signaling. The JSON envelope provides
    structured fields for smart clients alongside a human-readable message.

    ``code`` is typed as ``ErrorCode`` rather than ``str`` so a typo at a
    call site becomes a mypy error rather than a silent envelope with a
    missing ``help_url``. ``StrEnum`` members are strings at runtime, so
    JSON serialization and dict lookups still work unchanged.

    When ``help_url`` is not provided, the code is looked up in
    ``core.errors.HELP_URLS`` so every registered code gets a link
    automatically without the caller having to know the URL. Pass
    ``help_url`` explicitly to override the registered default.

    Raises:
        ToolError: always — this function never returns.
    """
    from mcp.server.fastmcp.exceptions import ToolError

    from mcp_synology.core.errors import HELP_URLS

    error: dict[str, Any] = {
        "code": code.value,
        "message": message,
        "retryable": retryable,
    }
    if param is not None:
        error["param"] = param
    if value is not None:
        error["value"] = value
    if valid is not None:
        error["valid"] = valid
    if suggestion is not None:
        error["suggestion"] = suggestion

    resolved_help_url = help_url if help_url is not None else HELP_URLS.get(code.value)
    if resolved_help_url is not None:
        error["help_url"] = resolved_help_url

    # ``default=str`` keeps a future caller from crashing the error path
    # by passing a non-JSON-serializable ``value`` (bytes, a custom object,
    # etc.). All current callers pass strings, so this is a safety net.
    raise ToolError(json.dumps({"status": "error", "error": error}, default=str))


def synology_error_response(operation: str, exc: SynologyError) -> NoReturn:
    """Convert a caught SynologyError into a structured error response.

    Maps SynologyError attributes to structured error fields and raises
    ToolError with a JSON envelope. The operation name provides context
    (e.g., "List files", "Upload").

    Raises:
        ToolError: always — this function never returns.
    """
    msg = f"{operation} failed: {exc}"
    if exc.code is not None:
        msg = f"{operation} failed (DSM error {exc.code}): {exc}"

    error_response(
        exc.error_code,
        msg,
        retryable=exc.retryable,
        suggestion=exc.suggestion,
        help_url=exc.help_url,
    )


_SIZE_UNITS = ["B", "KB", "MB", "GB", "TB", "PB"]


def format_size(size_bytes: int) -> str:
    """Format a byte count as a human-readable string (binary units).

    Examples:
        0 -> "0 B"
        1024 -> "1.0 KB"
        1536 -> "1.5 KB"
        1073741824 -> "1.0 GB"
    """
    if size_bytes == 0:
        return "0 B"

    value = float(size_bytes)
    for unit in _SIZE_UNITS:
        if abs(value) < 1024.0 or unit == _SIZE_UNITS[-1]:
            if value == int(value):
                return f"{int(value)} {unit}"
            return f"{value:.1f} {unit}"
        value /= 1024.0

    # Unreachable, but satisfies type checker
    return f"{size_bytes} B"


def format_timestamp(epoch: float) -> str:
    """Format a Unix epoch timestamp as a human-readable datetime string.

    Returns format: YYYY-MM-DD HH:MM:SS
    """
    dt = datetime.datetime.fromtimestamp(epoch, tz=datetime.UTC)
    return dt.strftime("%Y-%m-%d %H:%M:%S")
