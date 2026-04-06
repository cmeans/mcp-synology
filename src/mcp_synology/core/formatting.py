"""Shared response formatters (table, key-value, status, tree, error)."""

from __future__ import annotations

import datetime
from dataclasses import dataclass


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
