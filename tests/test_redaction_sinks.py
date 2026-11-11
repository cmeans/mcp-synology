"""Guard tests for #514: one redaction point per output sink.

Text leaves the server three ways, and each has one function every write goes through:

- **model-facing:** tool replies and errors (`server._SynologyServer.call_tool`), the server
  instructions (`create_server`) and progress messages (`core.formatting.report_progress`);
- **terminal:** `cli.output.say`;
- **logs:** `cli.logging_.RedactingFormatter`, on every handler the CLI sets up.

The canary tests plant credentials at each sink and check none comes out. The structural tests
keep every write going through these functions, so a new output path is covered without a new
test: they fail on a `click.echo`, `click.secho` or `print`, a `ToolError(` or
`report_progress(` call, or a logging handler or formatter set up anywhere else.
"""

from __future__ import annotations

import ast
import logging
import sys
from pathlib import Path
from typing import TYPE_CHECKING, Any

import httpx
import pytest
import respx
from click.testing import CliRunner
from mcp.client import Client
from mcp.server.mcpserver import Context  # noqa: TC002 - the SDK reads the annotation at runtime
from mcp.server.mcpserver.exceptions import ToolError
from mcp.types import TextContent

from mcp_synology.cli import logging_, main
from mcp_synology.cli.output import say, visible
from mcp_synology.core.errors import ErrorCode
from mcp_synology.core.formatting import error_response, report_progress
from mcp_synology.server import REPLY_LABEL, _SynologyServer, create_server
from tests.conftest import make_test_config

if TYPE_CHECKING:
    from collections.abc import Iterator

# Credentials as they could reach a sink: userinfo in a URL, a query item, a JSON member.
CANARY = "CANARYsecret514"
LEAKS = [
    f"https://admin:{CANARY}@nas.example:5001/webapi/entry.cgi",
    f"https://nas.example/webapi/entry.cgi?api=SYNO.API.Auth&passwd={CANARY}",
    f'{{"passwd": "{CANARY}", "account": "admin"}}',
]


def _text(result: Any) -> str:
    return " ".join(getattr(c, "text", "") for c in result.content)


# --- model-facing ---


@pytest.mark.parametrize("leak", LEAKS)
async def test_a_reply_never_carries_a_credential(
    leak: str, caplog: pytest.LogCaptureFixture
) -> None:
    server = _SynologyServer("canary")

    @server.tool(name="leaky")
    async def leaky() -> list[TextContent]:
        # Two text blocks: every block is scrubbed, not only the first.
        return [
            TextContent(type="text", text="first block"),
            TextContent(type="text", text=f"DSM said: {leak}"),
        ]

    with caplog.at_level(logging.WARNING, logger="mcp_synology.server"):
        async with Client(server) as client:
            result = await client.call_tool("leaky", {})
    text = _text(result)
    assert text.startswith(REPLY_LABEL)
    assert CANARY not in text
    # A credential reaching the sink means a call site let it through: logged, never quoted.
    warnings = [
        r.getMessage() for r in caplog.records if "looked like a credential" in r.getMessage()
    ]
    assert warnings == [
        "The reply of leaky held text that looked like a credential, masked there (#514); if it "
        "was one, a call site let it through."
    ]
    assert CANARY not in caplog.text


# File and folder names that look like query items: the model must be able to repeat them, so a
# reply, error or progress message keeps them (maintainer, 2026-11-10).
NAMES = ["download.php?id=7&token=a1b2c3.zip", "notes;password=old.txt", "a&sid=1.pdf"]


async def test_a_reply_keeps_file_names_that_look_like_query_items(
    caplog: pytest.LogCaptureFixture,
) -> None:
    server = _SynologyServer("names")

    @server.tool(name="listing")
    async def listing() -> str:
        return "\n".join(f"/video/{name}" for name in NAMES)

    with caplog.at_level(logging.WARNING, logger="mcp_synology.server"):
        async with Client(server) as client:
            text = _text(await client.call_tool("listing", {}))
    assert all(name in text for name in NAMES), text
    assert "looked like a credential" not in caplog.text


@pytest.mark.parametrize("leak", LEAKS)
async def test_an_error_never_carries_a_credential(leak: str) -> None:
    server = _SynologyServer("canary")

    @server.tool(name="leaky")
    async def leaky() -> str:
        raise ToolError(f"Can't reach {leak}")

    @server.tool(name="structured")
    async def structured() -> str:
        # As real tools fail: error_response's JSON envelope, whose message escapes the quotes
        # of JSON inside it.
        error_response(ErrorCode.UNAVAILABLE, f"DSM said {leak}", retryable=False)

    async with Client(server) as client:
        for tool in ("leaky", "structured"):
            result = await client.call_tool(tool, {})
            assert result.is_error
            assert CANARY not in _text(result), tool


# Inside error_response's JSON envelope a line break is escaped, so only the walk over the
# envelope's strings sees a header or a wrapped link at a line's start (pre-QA r2 on #514 PR 2).
ENVELOPE_LEAKS = [f"\nX-SYNO-Token: {CANARY}", f"\nthunder://{CANARY}"]


@pytest.mark.parametrize("leak", ENVELOPE_LEAKS)
async def test_an_error_envelope_is_redacted_string_by_string(leak: str) -> None:
    server = _SynologyServer("canary")

    @server.tool(name="structured")
    async def structured() -> str:
        error_response(ErrorCode.UNAVAILABLE, f"DSM said {leak}", retryable=False)

    async with Client(server) as client:
        result = await client.call_tool("structured", {})
    assert result.is_error
    assert CANARY not in _text(result)


async def test_the_update_notice_is_scrubbed_with_the_reply(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Text added after a tool ran (the update notice, from a release's notes) goes through
    the sink too, on a reply and on an error."""
    server = _SynologyServer("canary")
    notices = iter([f"\n\nUpdate: see {LEAKS[0]}", f"\n\nUpdate: see {LEAKS[1]}"])

    async def take() -> str:
        return next(notices)

    monkeypatch.setattr(server, "_take_update_notice", take)

    @server.tool(name="ok")
    async def ok() -> str:
        return "done"

    @server.tool(name="fails")
    async def fails() -> str:
        raise ToolError("no")

    async with Client(server) as client:
        for tool in ("ok", "fails"):
            text = _text(await client.call_tool(tool, {}))
            assert "Update: see" in text, tool
            assert CANARY not in text, tool


async def test_a_progress_message_never_carries_a_credential() -> None:
    server = _SynologyServer("canary")

    @server.tool(name="slow")
    async def slow(ctx: Context) -> str:
        for leak in LEAKS:
            await report_progress(ctx, 1, 2, f"copying from {leak}")
        return "done"

    messages: list[str] = []

    async def on_progress(progress: float, total: float | None, message: str | None) -> None:
        messages.append(message or "")

    async with Client(server) as client:
        await client.call_tool("slow", {}, progress_callback=on_progress)
    assert len(messages) == len(LEAKS)
    assert not any(CANARY in m for m in messages)


def test_the_instructions_never_carry_a_credential(caplog: pytest.LogCaptureFixture) -> None:
    config = make_test_config(custom_instructions=" ".join(LEAKS))
    with caplog.at_level(logging.WARNING, logger="mcp_synology.server"):
        instructions = create_server(config).instructions or ""
    assert instructions
    assert CANARY not in instructions
    # The user's own text is the likely source, so the WARNING points at the config.
    assert [
        r.getMessage() for r in caplog.records if "looked like a credential" in r.getMessage()
    ] == [
        "The server instructions held text that looked like a credential, masked there (#514); "
        "check custom_instructions and instructions_file in the config."
    ]


# --- terminal ---


@pytest.mark.parametrize("leak", LEAKS)
@pytest.mark.parametrize("err", [False, True])
def test_the_cli_never_prints_a_credential(
    leak: str, err: bool, capsys: pytest.CaptureFixture[str]
) -> None:
    say(f"Error: {leak}", fg="red", err=err)
    out, errout = capsys.readouterr()
    assert CANARY not in out + errout
    assert "Error:" in (errout if err else out)


@pytest.mark.parametrize(
    "path", ["/home/b/nas;sid=2/config.yaml", "C:\\Users\\a;token=1\\c.yaml", "/x/a&passwd=1.yaml"]
)
def test_the_cli_keeps_local_paths_that_look_like_query_items(
    path: str, capsys: pytest.CaptureFixture[str]
) -> None:
    """The user copies a path `setup` prints: a `key=value` item is masked only in a URL."""
    say(f"Config written to {path}")
    assert capsys.readouterr().out == f"Config written to {path}\n"


def test_the_cli_shows_control_characters_visibly(capsys: pytest.CaptureFixture[str]) -> None:
    """#486: a key from a config file can't send a terminal escape; tab, LF and CR are kept."""
    say("key 'ho\x1b[31mst\x07' is unknown\tand\r\nnext")
    assert capsys.readouterr().out == "key 'ho\\x1b[31mst\\x07' is unknown\tand\r\nnext\n"
    assert visible("\x00\x7f") == "\\x00\\x7f"


# --- logs ---


@pytest.fixture
def fresh_root_logging() -> Iterator[None]:
    """The root logger as the CLI sets it up, restored after the test."""
    root = logging.getLogger()
    handlers, level = list(root.handlers), root.level
    for h in handlers:
        root.removeHandler(h)
    try:
        yield
    finally:
        for h in list(root.handlers):
            root.removeHandler(h)
            h.close()
        for h in handlers:
            root.addHandler(h)
        root.setLevel(level)
        logging_._take_early_records()


def test_logs_never_carry_a_credential(
    fresh_root_logging: None, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    log_file = tmp_path / "server.log"
    # As at a CLI start: no handler on the root logger, pytest's own included (it adds one for
    # the test's call phase, after the fixture ran), or basicConfig would set nothing up.
    root = logging.getLogger()
    for h in list(root.handlers):
        root.removeHandler(h)
    logging_._init_early_logging(verbose=True)
    log = logging.getLogger("mcp_synology.canary")
    log.debug("before the file opens: %s", LEAKS[0])
    logging_._configure_logging("debug", str(log_file))
    for leak in LEAKS:
        log.warning("request failed: %s", leak)
    try:
        raise RuntimeError(f"connection to {LEAKS[0]} refused")
    except RuntimeError:
        log.exception("a traceback")
    for h in logging.getLogger().handlers:
        h.flush()
    written = log_file.read_text(encoding="utf-8")
    assert "a traceback" in written and "RuntimeError" in written
    assert "before the file opens" in written
    assert CANARY not in written
    assert CANARY not in capsys.readouterr().err


@respx.mock
def test_check_at_debug_never_shows_a_credential(
    fresh_root_logging: None, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """End to end, as #514 asked: `check` at debug level (`logging.level`) with a log file,
    the password from the environment, and DSM's connection failing with a credential in the
    error's text. No canary reaches stdout, stderr or the log file."""
    root = logging.getLogger()
    for h in list(root.handlers):
        root.removeHandler(h)
    log_file = tmp_path / "check.log"
    config = tmp_path / "config.yaml"
    config.write_text(
        "schema_version: 1\n"
        "connection:\n  host: 127.0.0.1\n  port: 5000\n  https: false\n  timeout: 5\n"
        "auth:\n  username: admin\n"
        f"logging:\n  level: debug\n  file: {log_file}\n"
        "modules:\n  filestation:\n    enabled: true\n",
        encoding="utf-8",
    )
    respx.get("http://127.0.0.1:5000/webapi/query.cgi").mock(
        return_value=httpx.Response(
            200,
            json={
                "success": True,
                "data": {"SYNO.API.Auth": {"path": "entry.cgi", "minVersion": 1, "maxVersion": 7}},
            },
        )
    )
    respx.post("http://127.0.0.1:5000/webapi/entry.cgi").mock(
        side_effect=httpx.ReadError(f"reset by {LEAKS[0]} after {LEAKS[1]}")
    )
    result = CliRunner().invoke(
        main, ["check", "-c", str(config)], env={"SYNOLOGY_PASSWORD": CANARY}
    )
    assert result.exit_code == 1, result.output
    assert "dropped it" in result.output
    # The error was shown, masked, not dropped.
    assert "***@nas.example" in result.output
    for h in logging.getLogger().handlers:
        h.flush()
    written = log_file.read_text(encoding="utf-8")
    assert "SYNO.API.Auth" in written
    assert CANARY not in result.output
    assert CANARY not in written
    assert CANARY not in capsys.readouterr().err


# --- structural ---

SRC = Path(__file__).resolve().parents[1] / "src"
_TERMINAL = "mcp_synology/cli/output.py"
_ERRORS = {"mcp_synology/core/formatting.py", "mcp_synology/server.py"}
_PROGRESS = "mcp_synology/core/formatting.py"
_LOGGING = "mcp_synology/cli/logging_.py"


def _calls() -> Iterator[tuple[str, ast.Call]]:
    for path in sorted(SRC.rglob("*.py")):
        rel = path.relative_to(SRC).as_posix()
        for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
            if isinstance(node, ast.Call):
                yield rel, node


def _dotted(func: ast.expr) -> str:
    if isinstance(func, ast.Name):
        return func.id
    if isinstance(func, ast.Attribute):
        return f"{_dotted(func.value)}.{func.attr}"
    return ""


def _aliases(tree: ast.Module) -> dict[str, str]:
    """Names a module imports under another name, mapped to what they are
    (`from click import echo as e` → `e: click.echo`)."""
    found: dict[str, str] = {}
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and node.module:
            for alias in node.names:
                found[alias.asname or alias.name] = f"{node.module}.{alias.name}"
        elif isinstance(node, ast.Import):
            for alias in node.names:
                if alias.asname:
                    found[alias.asname] = alias.name
    return found


# What writes to the terminal without `say`, by name: a stream held in a variable
# (`out = sys.stderr; out.write(...)`) can't be told apart from any other object's `write`.
_WRITERS = {
    "click.echo",
    "click.secho",
    "click.utils.echo",
    "print",
    "click.get_text_stream",
    "os.write",
    "traceback.print_exc",
    "traceback.print_exception",
    "traceback.print_stack",
    "traceback.print_tb",
    "traceback.print_last",
    "warnings.showwarning",
} | {
    f"sys.{stream}{part}.write"
    for stream in ("stdout", "stderr", "__stdout__", "__stderr__")
    for part in ("", ".buffer")
}
_LOG_SETUP = {"basicConfig", "dictConfig", "fileConfig", "Formatter"}


def test_every_output_goes_through_its_sink() -> None:
    found: list[str] = []
    for path in sorted(SRC.rglob("*.py")):
        rel = path.relative_to(SRC).as_posix()
        tree = ast.parse(path.read_text(encoding="utf-8"))
        aliases = _aliases(tree)
        for call in (n for n in ast.walk(tree) if isinstance(n, ast.Call)):
            name = _dotted(call.func)
            head, _, tail = name.partition(".")
            resolved = f"{aliases[head]}.{tail}".rstrip(".") if head in aliases else name
            last = resolved.rsplit(".", 1)[-1]
            here = f"{rel}:{call.lineno} {name}("
            if (resolved in _WRITERS or name in _WRITERS) and rel != _TERMINAL:
                found.append(here)
            if last == "ToolError" and rel not in _ERRORS:
                found.append(here)
            if (
                last == "report_progress"
                and resolved != "mcp_synology.core.formatting.report_progress"
                and name != "report_progress"
                and rel != _PROGRESS
            ):
                found.append(f"{here} (use core.formatting.report_progress)")
            if (
                (last.endswith("Handler") or last in _LOG_SETUP)
                and rel != _LOGGING
                and last != "VersionedHandler"
            ):
                found.append(here)
            if (
                name == "getattr"
                and len(call.args) >= 2
                and isinstance(call.args[1], ast.Constant)
                and call.args[1].value in ("report_progress", "echo", "secho", "write")
            ):
                found.append(f"{here} getattr {call.args[1].value}")
    assert found == [], "\n".join(found)


def test_every_log_handler_uses_the_redacting_formatter() -> None:
    tree = ast.parse((SRC / _LOGGING).read_text(encoding="utf-8"))
    formatters = [
        _dotted(c.args[0].func)
        for c in ast.walk(tree)
        if isinstance(c, ast.Call)
        and _dotted(c.func).endswith("setFormatter")
        and c.args
        and isinstance(c.args[0], ast.Call)
    ]
    assert formatters and set(formatters) == {"RedactingFormatter"}
    # basicConfig takes its handlers, which carry the formatter: never format= or stream=.
    for c in ast.walk(tree):
        if isinstance(c, ast.Call) and _dotted(c.func) == "logging.basicConfig":
            assert {k.arg for k in c.keywords} == {"level", "handlers"}


def test_the_canaries_hold_credentials_the_pass_masks() -> None:
    """Each canary holds a credential that `redact_secrets` masks, so a sink test that finds
    none out has the pass to thank, not a canary that was never there."""
    from mcp_synology.core.redaction import redact_secrets

    for leak in LEAKS:
        assert CANARY in leak
        assert CANARY not in redact_secrets(leak)
    assert sys.modules["mcp_synology.cli.output"].redact_secrets is redact_secrets
