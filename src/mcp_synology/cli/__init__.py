"""CLI package: serve, setup, check subcommands (click-based).

Re-exports for backward compatibility:
- server.py imports: _check_for_update, _load_global_state, _save_global_state
- __main__.py imports: main
"""

from __future__ import annotations

from mcp_synology.cli.main import main
from mcp_synology.cli.setup import _CONFIG_DIR, _store_keyring
from mcp_synology.cli.version import (
    _check_for_update,
    _load_global_state,
    _save_global_state,
    _with_global_state_lock,
)

__all__ = [
    "_CONFIG_DIR",
    "_check_for_update",
    "_load_global_state",
    "_save_global_state",
    "_store_keyring",
    "_with_global_state_lock",
    "main",
]
