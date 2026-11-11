"""The one way the CLI writes to the terminal (#514).

Every line the CLI prints itself goes through `say`. It does two things before styling:
- runs `redact_secrets` in its names mode, so a credential that reaches a message (in a DSM
  error, a URL or a config value) is masked, while a local path such as
  ``/home/b/nas;sid=2/config.yaml``, which the user may copy, is kept (a ``key=value`` item is
  masked only inside a URL; the log file and stderr logging get the full pass);
- writes control characters and terminal escapes visibly as ``\\xNN``, other than tab, line
  feed and carriage return, so a message can't send a terminal escape (#486). A carriage
  return or line feed still moves to a new line or column 0: values from a config file and the
  NAS are escaped where they come from (``repr`` in config errors, `core.client._loads`).

Styling comes after, from the caller's ``fg``, so the escaping never touches the
CLI's own colors. Not through it: a prompt's text (`click.prompt`, `click.confirm`), which the
CLI builds from its own words and values escaped at the source (``repr``, `core.client._loads`);
click's own rendering of a `ClickException`; and Python's traceback for an unhandled crash.
``tests/test_redaction_sinks.py`` fails on any other terminal write in ``src/`` (``click.echo``,
``print``, a stream write, an exit with a message, a click exception, …: CLAUDE.md's class
entry point bullet lists them); a stream held in a variable can't be told apart from another
object's ``write``, so that one is review's to catch.
"""

from __future__ import annotations

import re

import click

from mcp_synology.core.formatting import FORGING_RANGES
from mcp_synology.core.redaction import redact_secrets

# What a reply may not carry as it is (#604: C0 and C1 controls, ESC and the 8-bit CSI among
# them, bidi controls, line and paragraph separators, tag characters, lone surrogates), but
# tab, line feed and carriage return, which a terminal line needs.
_CONTROL = re.compile(
    "[" + "".join(f"\\U{low:08x}-\\U{high:08x}" for low, high in FORGING_RANGES) + "]"
)
_KEPT = frozenset("\t\n\r")


def _escaped(char: str) -> str:
    if char in _KEPT:
        return char
    code = ord(char)
    return (
        f"\\x{code:02x}"
        if code < 0x100
        else f"\\u{code:04x}"
        if code < 0x10000
        else f"\\U{code:08x}"
    )


def visible(text: str) -> str:
    """``text`` with each character `formatting.forges` refuses, other than tab, LF and CR,
    written as ``\\xNN`` or ``\\uNNNN``."""
    return _CONTROL.sub(lambda m: _escaped(m.group()), text)


def say(
    message: object = "",
    *,
    fg: str | None = None,
    err: bool = False,
    nl: bool = True,
) -> None:
    """Write ``message`` to stdout (or stderr with ``err``), redacted, with control characters
    made visible, then colored ``fg``."""
    text = visible(redact_secrets("" if message is None else str(message), names=True))
    click.echo(click.style(text, fg=fg) if fg is not None else text, err=err, nl=nl)
