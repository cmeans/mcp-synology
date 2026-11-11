"""The batch failure formatter as `DeleteAdapter` drives it, for tests of its text (#512 PR 3).

`failure_error` gets its pieces from the adapter; this calls the same steps with a real
`DeleteAdapter`'s wording, so a change to that wording shows here too.
"""

from __future__ import annotations

from typing import Any

from mcp_synology.modules.filestation.batch import Item, _with_not_started, _with_progress
from mcp_synology.modules.filestation.operations import DeleteAdapter, _already_done

_ADAPTER = DeleteAdapter(
    None,  # type: ignore[arg-type]  # the wording needs no client
    [],
    paths_arg=[],
    recursive=True,
    recycle_bin_status=None,
)


def with_progress_so_far(error: Any, failed: str, done: list[str], rest: list[str]) -> Any:
    """A delete's failure of ``failed`` after ``done``, with ``rest`` not started."""
    out = _with_progress(
        error,
        failed,
        _already_done("Delete", done),
        _ADAPTER.retry_advice(),
        _ADAPTER.check_clause(Item(failed, None)),
    )
    return _with_not_started(out, rest)
