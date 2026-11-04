"""Logging configuration for CLI commands."""

from __future__ import annotations

import logging
import os
import sys

_LOG_FORMAT = "%(asctime)s [%(levelname)s] %(name)s: %(message)s"

# More than a config load logs at any level; the cap only matters for a `setup` or `check -v`
# that never replays them.
_EARLY_RECORDS_CAP = 1000


class _EarlyRecords(logging.Handler):
    """Keeps what's logged before `logging.file` is open, to replay into it (#511 PR 3).

    The config has to load before the file can be opened, and the security warnings are
    logged while it loads: under an MCP client, where stderr may not be visible, they went
    nowhere (`SYNOLOGY_HOST=… serve` left the log file empty, QA round 2 on #535).
    """

    def __init__(self) -> None:
        super().__init__()
        self.records: list[logging.LogRecord] = []

    def emit(self, record: logging.LogRecord) -> None:
        if len(self.records) < _EARLY_RECORDS_CAP:
            self.records.append(record)


_early_records: _EarlyRecords | None = None


def _start_early_records() -> None:
    global _early_records
    root = logging.getLogger()
    if _early_records is not None:
        root.removeHandler(_early_records)
    _early_records = _EarlyRecords()
    root.addHandler(_early_records)


def _take_early_records() -> list[logging.LogRecord]:
    """Stop keeping records, and return those kept."""
    global _early_records
    if _early_records is None:
        return []
    logging.getLogger().removeHandler(_early_records)
    records = _early_records.records
    _early_records = None
    return records


def _init_early_logging(*, verbose: bool = False) -> None:
    """Configure logging early so config discovery/loading is visible.

    Checks --verbose flag first, then SYNOLOGY_LOG_LEVEL env var.
    Falls back to INFO. Will be reconfigured after config is loaded
    if the config specifies a different level.
    """
    if verbose:
        level = logging.DEBUG
    else:
        # Read as the config reads it (#511): an invalid value is INFO here, and a config
        # load that follows reports it, naming the variable (none does when discovery
        # fails first, or when setup goes on to create a config).
        from mcp_synology.core.config import ENV_VAR_MAP, EnvVarError, parse_env_value

        name = "info"
        raw = os.environ.get("SYNOLOGY_LOG_LEVEL", "")
        if raw.strip():
            try:
                name = parse_env_value("SYNOLOGY_LOG_LEVEL", ENV_VAR_MAP["SYNOLOGY_LOG_LEVEL"], raw)
            except EnvVarError:
                name = "info"
        level = getattr(logging, name.upper())
    logging.basicConfig(level=level, format=_LOG_FORMAT, stream=sys.stderr)
    _start_early_records()


def _configure_logging(level: str, log_file: str | None = None) -> None:
    """Reconfigure logging from loaded config values, and put what was logged while the
    config loaded into the file: what the level allows, and the security warnings at any
    level, as on stderr (`config._warn_always`)."""
    root = logging.getLogger()
    root.setLevel(getattr(logging, level.upper(), logging.INFO))
    early = _take_early_records()

    if log_file:
        file_handler = logging.FileHandler(os.path.expanduser(log_file), encoding="utf-8")
        file_handler.setFormatter(logging.Formatter(_LOG_FORMAT))
        for record in early:
            if record.levelno >= root.level or getattr(record, "show_always", False):
                file_handler.handle(record)
        root.addHandler(file_handler)
