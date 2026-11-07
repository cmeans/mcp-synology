"""Shared response formatters (table, key-value, status, tree, error)."""

from __future__ import annotations

import datetime
import json
import re
import unicodedata
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, NoReturn

if TYPE_CHECKING:
    from mcp_synology.core.errors import ErrorCode, SynologyError


# Unicode's Bidi_Control characters (UAX #9): they reorder how the text around them reads.
_BIDI_CONTROLS = frozenset(
    "\u061c\u200e\u200f\u202a\u202b\u202c\u202d\u202e\u2066\u2067\u2068\u2069"
)
# Tag characters carry text a model reads and a person doesn't see (OWASP's prompt injection
# cheat sheet).
_TAGS = re.compile("[\U000e0000-\U000e007f]")


def needs_quoting(text: str) -> bool:
    """Whether ``text`` from the NAS can't go into a reply as it is (#604): a control character
    (a newline would start a line of its own that reads as the server's), a line or paragraph
    separator, a bidi control or a tag character."""
    return any(
        unicodedata.category(c) in ("Cc", "Zl", "Zp") or c in _BIDI_CONTROLS for c in text
    ) or bool(_TAGS.search(text))


def forges(c: str) -> bool:
    """A character a name may not carry into a reply as it is: ``needs_quoting``'s, but a tab,
    which can't start a line and is part of names DSM keeps exactly (#488)."""
    if c == "\t":
        return False
    return (
        unicodedata.category(c) in ("Cc", "Zl", "Zp") or c in _BIDI_CONTROLS or bool(_TAGS.match(c))
    )


def _escape_char(c: str) -> str:
    o = ord(c)
    named = {"\n": "\\n", "\r": "\\r"}.get(c)
    if named:
        return named
    return f"\\x{o:02x}" if o <= 0xFF else f"\\u{o:04x}" if o <= 0xFFFF else f"\\U{o:08x}"


_ESCAPE = re.compile(r"\\(n|r|x[0-9a-f]{2}|u[0-9a-f]{4}|U[0-9a-f]{8})")


def escape_controls(text: str) -> str:
    """``text`` with each character a name may not carry into a reply written as an escape
    (``\\n``, ``\\x1b``, ``\\u202e``), and the rest as it is (#604)."""
    if not any(forges(c) for c in text):
        return text
    return "".join(_escape_char(c) if forges(c) else c for c in text)


def unescape_controls(text: str) -> str:
    """``escape_controls`` undone, for a reader that looks at the characters themselves (Photos'
    EXIF values, to tell binary from text). Its result goes into a reply only escaped again.
    Text that already held such a backslash sequence is read as that character."""

    def back(m: re.Match[str]) -> str:
        code = m.group(1)
        return {"n": "\n", "r": "\r"}.get(code) or chr(int(code[1:], 16))

    return _ESCAPE.sub(back, text)


def escape_nas_text(value: Any) -> Any:
    """A DSM reply with every string in it, keys too, passed through ``escape_controls`` (#604).

    Text from the NAS reaches replies in many places, so it's escaped once, where every reply
    is read (``DsmClient``), rather than at each. A name holding a newline would otherwise
    write a line of its own into a reply, which reads as the server's. The cost: an item
    with such a name is shown escaped and can't be acted on through the tools (DSM answers
    that it isn't there), until it's renamed on the NAS; the tools never create one."""
    if isinstance(value, str):
        return escape_controls(value)
    if isinstance(value, list):
        return [escape_nas_text(v) for v in value]
    if isinstance(value, dict):
        return {escape_nas_text(k): escape_nas_text(v) for k, v in value.items()}
    return value


def reply_text(value: object) -> str:
    """``value`` as a reply shows text from the NAS: as is, or quoted with every character that
    doesn't print escaped (``repr``), when it holds one ``needs_quoting`` names (#604). The MCP
    spec says servers MUST sanitize tool outputs. A name with one of those is quoted whole, as
    GNU ``ls`` and git quote such a file name, so it can't make a line of its own. Text that
    merely reads like an instruction is left alone: filtering wording isn't a boundary."""
    text = str(value)
    return repr(text) if needs_quoting(text) else text


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

    # Every cell may hold text from the NAS (#604).
    rows = [[reply_text(cell) for cell in row] for row in rows]
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
        # A value may be text from the NAS (#604).
        lines.append(f"  {key + ':':<{max_key_len + 1}}  {reply_text(value)}")

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
            lines.append(f"{prefix}{connector}{reply_text(node.name)}")
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
    from mcp.server.mcpserver.exceptions import ToolError

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


_SIZE_UNITS = ["B", "KiB", "MiB", "GiB", "TiB", "PiB"]


def format_size(size_bytes: int) -> str:
    """Format a byte count as a human-readable string in binary units (KiB = 1024 bytes).

    Examples:
        0 -> "0 B"
        1024 -> "1 KiB"
        1536 -> "1.5 KiB"
        1073741824 -> "1 GiB"
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
    """Format a Unix epoch timestamp as a human-readable UTC datetime string.

    Returns format: YYYY-MM-DD HH:MM:SS UTC. The zone is spelled out because an
    unlabeled UTC time reads as local time (#137). UTC rather than local keeps
    the output independent of the machine the server runs on, and matches
    search_files, which treats naive input dates as UTC.
    """
    dt = datetime.datetime.fromtimestamp(epoch, tz=datetime.UTC)
    return dt.strftime("%Y-%m-%d %H:%M:%S UTC")


_DAY_NAMES = ["Sun", "Mon", "Tue", "Wed", "Thu", "Fri", "Sat"]

# DSM's weekly plans: day-major Sun..Sat, 24 chars per day, one char per hour. Verified
# for Download Station's schedule from DSM's SYNO.ux.ScheduleTable widget (#123); Storage
# Manager's scrubbing, reclamation and deduplication windows are 168 characters too, and
# read the same way (inferred; storage-module-spec.md).
SCHEDULE_PLAN_LENGTH = 7 * 24


def weekly_plan_grid(plan: str, glyphs: dict[str, str], legend: str) -> str:
    """Render a DSM weekly plan as a 7 × 24 text grid.

    ``glyphs`` maps each plan character to its cell, and ``legend`` explains them.
    A malformed plan is rendered rather than rejected: missing or unrecognized
    cells show as '?' and a note names the problem.
    """
    hour_header = "     " + " ".join(f"{h:02d}" for h in range(24))
    lines = [hour_header]
    for day_idx, name in enumerate(_DAY_NAMES):
        day_slice = plan[day_idx * 24 : (day_idx + 1) * 24].ljust(24, "?")
        # Two spaces between cells: each cell sits under its 2-digit hour label.
        cells = "  ".join(glyphs.get(ch, "?") for ch in day_slice)
        lines.append(f"{name}  {cells}")

    lines.append("")
    lines.append(legend)
    lines.append("Hours are the NAS's local time.")
    if len(plan) != SCHEDULE_PLAN_LENGTH:
        lines.append(
            f"Note: stored plan is malformed ({len(plan)} chars, expected "
            f"{SCHEDULE_PLAN_LENGTH}); '?' marks unreadable hours."
        )
    elif set(plan) - set(glyphs):
        valid = "/".join(sorted(glyphs))
        lines.append(
            f"Note: stored plan is malformed (contains values other than {valid}); "
            "'?' marks unreadable hours."
        )
    return "\n".join(lines)
