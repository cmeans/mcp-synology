"""The one way the CLI writes to the terminal (#514).

Every line the CLI prints goes through `say`. It does two things before styling:
- runs `redact_secrets`, so a credential that reaches a message (in a DSM error, a URL or a
  config value) is masked;
- writes control characters and terminal escapes visibly as ``\\xNN`` (keeping tab, line
  feed and carriage return), so text from a config file or the NAS can't move the cursor,
  recolor the terminal or forge a line (#486).

Styling comes after, from the caller's ``fg``, so the escaping never touches the
CLI's own colors. ``tests/test_redaction_sinks.py`` fails on a ``click.echo``, ``click.secho``
or ``print`` anywhere else in ``src/``.
"""

from __future__ import annotations

import re

import click

from mcp_synology.core.redaction import redact_secrets

# C0 controls other than tab, line feed and carriage return, and DEL: ESC (\x1b) among them.
_CONTROL = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")


def visible(text: str) -> str:
    """``text`` with each control character other than tab, LF and CR written as ``\\xNN``."""
    return _CONTROL.sub(lambda m: f"\\x{ord(m.group()):02x}", text)


def say(
    message: object = "",
    *,
    fg: str | None = None,
    err: bool = False,
    nl: bool = True,
) -> None:
    """Write ``message`` to stdout (or stderr with ``err``), redacted, with control characters
    made visible, then colored ``fg``."""
    text = visible(redact_secrets("" if message is None else str(message)))
    click.echo(click.style(text, fg=fg) if fg is not None else text, err=err, nl=nl)
