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
import json
import logging
import sys
import warnings
from pathlib import Path
from typing import TYPE_CHECKING, Any

import httpx
import pytest
import respx
from click.testing import CliRunner
from mcp.client import Client
from mcp.server.mcpserver import Context  # noqa: TC002 - the SDK reads the annotation at runtime
from mcp.server.mcpserver.exceptions import ToolError
from mcp.shared.exceptions import MCPError
from mcp.types import (
    CallToolResult,
    EmbeddedResource,
    ImageContent,
    ResourceLink,
    TextContent,
    TextResourceContents,
)

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


async def test_a_structured_reply_is_scrubbed(caplog: pytest.LogCaptureFixture) -> None:
    """No tool sends structured content today (#377), but one that asks for it is scrubbed."""
    server = _SynologyServer("canary")

    @server.tool(name="structured", structured_output=True)
    async def structured() -> dict[str, str]:
        return {"url": LEAKS[0]}

    with caplog.at_level(logging.WARNING, logger="mcp_synology.server"):
        async with Client(server) as client:
            result = await client.call_tool("structured", {})
    assert CANARY not in _text(result)
    assert CANARY not in json.dumps(result.structured_content)
    assert "The structured reply of structured" in caplog.text


async def test_other_content_blocks_and_protocol_errors_are_scrubbed() -> None:
    """No tool sends an embedded resource or a resource link, or raises an `MCPError`, today;
    one that does is scrubbed like a text reply (QA round 1 on #681)."""
    server = _SynologyServer("canary")

    @server.tool(name="blocks")
    async def blocks() -> list[Any]:
        return [
            EmbeddedResource(
                type="resource",
                resource=TextResourceContents(uri="file:///x.txt", text=f"see {LEAKS[0]}"),
            ),
            ResourceLink(
                type="resource_link",
                uri=LEAKS[1],
                name="link",
                description=f"from {LEAKS[0]}",
            ),
        ]

    @server.tool(name="protocol")
    async def protocol() -> str:
        raise MCPError(-32603, f"failed {LEAKS[0]}", {"url": LEAKS[1]})

    async with Client(server) as client:
        result = await client.call_tool("blocks", {})
        dumped = json.dumps([c.model_dump(mode="json") for c in result.content])
        assert "see https://***@nas.example" in dumped
        assert CANARY not in dumped
        with pytest.raises(MCPError) as raised:
            await client.call_tool("protocol", {})
    assert CANARY not in str(raised.value)
    assert CANARY not in json.dumps(raised.value.error.model_dump(mode="json"))


async def test_every_meta_is_scrubbed_and_base64_kept() -> None:
    """A text block's `_meta`, a `data` or `blob` key under `_meta`, and the result's `_meta`
    are text; only a block's own base64 `data` is kept (pre-QA r5 on #681)."""
    server = _SynologyServer("canary")
    image = "aGVsbG8="  # base64 a canary can't be hidden in without decoding

    @server.tool(name="meta")
    async def meta() -> CallToolResult:
        return CallToolResult(
            content=[
                TextContent(type="text", text="ok", _meta={"src": LEAKS[0]}),
                ImageContent(
                    type="image",
                    data=image,
                    mimeType="image/png",
                    _meta={"data": LEAKS[0], "blob": {"x": LEAKS[1]}},
                ),
            ],
            _meta={"origin": LEAKS[0]},
        )

    async with Client(server) as client:
        result = await client.call_tool("meta", {})
    dumped = json.dumps(result.model_dump(mode="json", by_alias=True))
    assert CANARY not in dumped
    assert image in dumped


async def test_a_protocol_error_is_labeled() -> None:
    server = _SynologyServer("canary")

    @server.tool(name="protocol")
    async def protocol() -> str:
        raise MCPError(-32603, "failed")

    async with Client(server) as client:
        with pytest.raises(MCPError) as raised:
            await client.call_tool("protocol", {})
    assert raised.value.error.message.startswith(REPLY_LABEL)


def test_an_input_request_is_scrubbed() -> None:
    """No tool asks for input today; an elicitation's text would reach the client as is."""
    from mcp_synology.server import _scrubbed_model

    request = TextContent(type="text", text=f"Sign in at {LEAKS[0]}")
    assert CANARY not in _scrubbed_model(request, "t", "input request").text


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

    @server.tool(name="braces")
    async def braces() -> str:
        # A `{` that doesn't start a JSON envelope: the text is redacted whole.
        raise ToolError(f"bad {{x}} {leak}")

    @server.tool(name="prefixed")
    async def prefixed() -> str:
        # Text before a JSON envelope is redacted too.
        raise ToolError(f"{leak} said {LEAKS[2]}")

    async with Client(server) as client:
        for tool in ("leaky", "structured", "braces", "prefixed"):
            result = await client.call_tool(tool, {})
            assert result.is_error
            assert CANARY not in _text(result), tool


# A quoted URL in a structured error's message: inside the encoded envelope its quote is `\"`,
# and scanning the encoded text again dropped the backslash, so the reply wasn't JSON any more
# (pre-QA r3 on #514 PR 2).
QUOTED_URLS = [
    f'album "https://h.example/a?sid={CANARY}": no',
    f'album "https://h.example/a;token={CANARY}"',
    f'album "https://admin:{CANARY}@h.example/a?token=x" and {LEAKS[2]}',
]


@pytest.mark.parametrize("message", QUOTED_URLS)
async def test_a_structured_error_stays_json(message: str) -> None:
    server = _SynologyServer("canary")

    @server.tool(name="structured")
    async def structured() -> str:
        error_response(ErrorCode.UNAVAILABLE, message, retryable=False)

    async with Client(server) as client:
        result = await client.call_tool("structured", {})
    text = _text(result)
    assert CANARY not in text
    envelope = json.loads(text[text.index("{") :])
    assert envelope["error"]["message"].startswith("album ")


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
    # An earlier test's CLI setup may have left warnings captured while pytest put its own
    # handler back: `captureWarnings(True)` would then change nothing.
    logging.captureWarnings(False)
    try:
        yield
    finally:
        for h in list(root.handlers):
            root.removeHandler(h)
            h.close()
        for h in handlers:
            root.addHandler(h)
        root.setLevel(level)
        logging.captureWarnings(False)
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
    # A form body outside any URL: only the full pass masks it, names mode keeps it as a name.
    # Logs get the full pass (QA round 1 on #681: a names-mode formatter passed every test).
    log.debug("form body: account=admin&passwd=%s&otp_code=123456", CANARY)
    try:
        raise RuntimeError(f"connection to {LEAKS[0]} refused")
    except RuntimeError:
        log.exception("a traceback")
    # A Python warning goes through the redacting handlers, not to stderr as it is.
    with warnings.catch_warnings():
        warnings.simplefilter("always")
        warnings.warn(f"deprecated, see {LEAKS[0]}", UserWarning, stacklevel=1)
    for h in logging.getLogger().handlers:
        h.flush()
    written = log_file.read_text(encoding="utf-8")
    assert "deprecated, see https://***@nas.example" in written
    assert "a traceback" in written and "RuntimeError" in written
    assert "before the file opens" in written
    assert "form body: account=admin&passwd=***&otp_code=***" in written
    assert CANARY not in written
    assert CANARY not in capsys.readouterr().err


@respx.mock
def test_check_at_debug_never_shows_a_credential(fresh_root_logging: None, tmp_path: Path) -> None:
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
    # The CLI's runner captured stderr's DEBUG log with the rest (capsys would read nothing:
    # the handler holds the stream it was made with).
    assert "[DEBUG]" in result.output
    assert CANARY not in result.output
    assert CANARY not in written


def test_the_sdk_keeps_the_redacting_handlers(fresh_root_logging: None) -> None:
    """`serve` sets logging up before the SDK's own `basicConfig` runs in `MCPServer`, so the
    SDK's handler is never added (upstream drift: pre-QA r3 on #514 PR 2)."""
    root = logging.getLogger()
    for h in list(root.handlers):
        root.removeHandler(h)
    logging_._init_early_logging(verbose=False)
    before = list(root.handlers)
    create_server(make_test_config())
    # The SDK added no handler, and every handler that formats uses the redacting formatter
    # (the early-records buffer formats nothing: it replays into the log file's handler).
    assert root.handlers == before
    assert any(isinstance(h.formatter, logging_.RedactingFormatter) for h in root.handlers)
    assert all(
        h.formatter is None or isinstance(h.formatter, logging_.RedactingFormatter)
        for h in root.handlers
    )


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
# Python's warnings go through logging (`logging.captureWarnings` in `cli/logging_.py`).
_WRITERS = {
    "click.echo",
    "click.secho",
    "click.utils.echo",
    "print",
    "click.get_text_stream",
    "click.get_binary_stream",
    "builtins.print",
    "os.write",
    "traceback.print_exc",
    "traceback.print_exception",
    "traceback.print_stack",
    "traceback.print_tb",
    "traceback.print_last",
    "warnings.showwarning",
} | {
    f"sys.{stream}{part}.{method}"
    for stream in ("stdout", "stderr", "__stdout__", "__stderr__")
    for part in ("", ".buffer")
    for method in ("write", "writelines")
}
_LOG_SETUP = {"basicConfig", "dictConfig", "fileConfig", "Formatter"}
# What sends text to the MCP client without a sink: the session's senders, and the context's
# log methods (QA round 1 on #681). Progress goes through `core.formatting.report_progress`.
_MCP_SENDERS = {
    "send_log_message",
    "send_progress_notification",
    "send_notification",
    "elicit",
    "elicit_url",
}
_CONTEXT_LOG = {"debug", "info", "warning", "error", "log"}
# click's exceptions print their own message, not through `say`. Each one raised on purpose is
# listed, with what its message holds.
_CLICK_ERRORS = {"ClickException", "UsageError", "BadParameter", "FileError", "BadOptionUsage"}
_KNOWN_CLICK_ERRORS = {
    # A version string the user typed, quoted with `!r`.
    ("mcp_synology/cli/version.py", "ClickException"),
}


def _output_paths(rel: str, tree: ast.Module) -> list[str]:
    """Every place in one module that writes text out other than through its sink."""
    aliases = _aliases(tree)
    found: list[str] = []

    def resolve(name: str) -> str:
        head, _, tail = name.partition(".")
        return f"{aliases[head]}.{tail}".rstrip(".") if head in aliases else name

    for node in ast.walk(tree):
        # A reference counts as much as a call: `rp = ctx.report_progress` is a way around too.
        if isinstance(node, ast.Attribute | ast.Name) and not isinstance(
            getattr(node, "ctx", None), ast.Store
        ):
            name = _dotted(node)
            resolved = resolve(name)
            last = resolved.rsplit(".", 1)[-1]
            here = f"{rel}:{node.lineno} {name}"
            if (resolved in _WRITERS or name in _WRITERS) and rel != _TERMINAL:
                found.append(here)
            if (
                isinstance(node, ast.Attribute)
                and last == "report_progress"
                and resolved != "mcp_synology.core.formatting.report_progress"
                and rel != _PROGRESS
            ):
                found.append(f"{here} (use core.formatting.report_progress)")
            if isinstance(node, ast.Attribute) and node.attr in _MCP_SENDERS:
                found.append(here)
            if (
                isinstance(node, ast.Attribute)
                and node.attr in _CONTEXT_LOG
                and _dotted(node.value).rsplit(".", 1)[-1].lower().endswith(("ctx", "context"))
            ):
                found.append(here)
        if not isinstance(node, ast.Call):
            continue
        name = _dotted(node.func)
        resolved = resolve(name)
        last = resolved.rsplit(".", 1)[-1]
        here = f"{rel}:{node.lineno} {name}("
        if last in ("ToolError", "MCPError") and rel not in _ERRORS:
            found.append(here)
        if (
            (last.endswith("Handler") or last in _LOG_SETUP)
            and rel != _LOGGING
            and last != "VersionedHandler"
        ):
            found.append(here)
        if (
            name == "getattr"
            and len(node.args) >= 2
            and isinstance(node.args[1], ast.Constant)
            and node.args[1].value in ("report_progress", "echo", "secho", "write", "writelines")
        ):
            found.append(f"{here} getattr {node.args[1].value}")
        # An exit with anything but a number prints it to stderr as it is.
        # (`SystemExit(128 + signum)` is a number; a name or a call may be text.)
        if resolved in ("sys.exit", "SystemExit", "exit", "quit") and any(
            isinstance(a, ast.Name)
            or any(
                isinstance(n, ast.JoinedStr | ast.Call)
                or (isinstance(n, ast.Constant) and isinstance(n.value, str))
                for n in ast.walk(a)
            )
            for a in node.args
        ):
            found.append(f"{here} with a message")
        if (
            last in _CLICK_ERRORS
            and resolved.startswith("click.")
            and (rel, last) not in _KNOWN_CLICK_ERRORS
        ):
            found.append(f"{here} (click prints it, not say())")
    # A project exception that click renders itself, by subclassing one of click's.
    for node in ast.walk(tree):
        if isinstance(node, ast.ClassDef):
            for base in node.bases:
                base_name = resolve(_dotted(base))
                if base_name.startswith("click.") and base_name.rsplit(".", 1)[-1] in _CLICK_ERRORS:
                    found.append(f"{rel}:{node.lineno} class {node.name}({_dotted(base)})")
    return found


def test_every_output_goes_through_its_sink() -> None:
    found: list[str] = []
    for path in sorted(SRC.rglob("*.py")):
        rel = path.relative_to(SRC).as_posix()
        found += _output_paths(rel, ast.parse(path.read_text(encoding="utf-8")))
    assert found == [], "\n".join(found)


# Each way around a sink QA's reviewer planted in a synthetic module (round 1 on #681), and
# the earlier rounds' forms: the guard must name every one.
_BYPASSES = """
import sys, warnings, traceback, os
import click
from click import echo as e
async def tool(ctx):
    await ctx.info("x")
    await ctx.log("info", "x")
    await ctx.session.send_log_message("info", "x")
    await ctx.session.send_progress_notification("t", 1)
    await ctx.elicit("x", dict)
    rp = ctx.report_progress
    await rp(1, 2, "x")
    raise MCPError(1, "x")
def cli():
    e("x")
    print("x")
    sys.stderr.writelines(["x"])
    sys.__stderr__.buffer.write(b"x")
    os.write(2, b"x")
    traceback.print_exc()
    sys.exit("x")
    raise SystemExit(f"x {1}")
    raise click.UsageError("x")
    getattr(sys.stdout, "write")("x")
    sys.exit("a" + str(1))
    exit("x")
    raise SystemExit(str(1))
async def more(self, req_ctx):
    await self.ctx.warning("x")
    await req_ctx.info("x")
    click.get_binary_stream("stdout").write(b"x")
class Refusal(click.ClickException):
    pass
"""


def test_the_guard_names_every_way_around_a_sink() -> None:
    found = _output_paths("mcp_synology/modules/fake.py", ast.parse(_BYPASSES))
    expected_lines = {6, 7, 8, 9, 10, 11, 13, 15, 16, 17, 18, 19, 20, 21, 22, 23, 24}
    expected_lines |= {25, 26, 27, 29, 30, 31, 32}
    assert {int(f.split(":")[1].split()[0]) for f in found} == expected_lines, "\n".join(found)


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
