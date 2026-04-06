"""Logging configuration for CLI commands."""

from __future__ import annotations

import logging
import os
import sys

_LOG_FORMAT = "%(asctime)s [%(levelname)s] %(name)s: %(message)s"


def _init_early_logging(*, verbose: bool = False) -> None:
    """Configure logging early so config discovery/loading is visible.

    Checks --verbose flag first, then SYNOLOGY_LOG_LEVEL env var.
    Falls back to INFO. Will be reconfigured after config is loaded
    if the config specifies a different level.
    """
    if verbose:
        level = logging.DEBUG
    else:
        env_level = os.environ.get("SYNOLOGY_LOG_LEVEL", "info").upper()
        level = getattr(logging, env_level, logging.INFO)
    logging.basicConfig(level=level, format=_LOG_FORMAT, stream=sys.stderr)


def _configure_logging(level: str, log_file: str | None = None) -> None:
    """Reconfigure logging from loaded config values."""
    root = logging.getLogger()
    root.setLevel(getattr(logging, level.upper(), logging.INFO))

    if log_file:
        file_handler = logging.FileHandler(os.path.expanduser(log_file), encoding="utf-8")
        file_handler.setFormatter(logging.Formatter(_LOG_FORMAT))
        root.addHandler(file_handler)
