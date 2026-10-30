"""MCP server initialization, module loading, and startup."""

from __future__ import annotations

import asyncio
import base64
import logging
import platform
import re
import signal
from contextlib import asynccontextmanager
from pathlib import Path
from typing import TYPE_CHECKING, Any, Literal, NoReturn

import anyio
import httpx
from mcp.server.mcpserver import MCPServer
from mcp.server.mcpserver.exceptions import ToolError, UnexpectedToolError
from mcp.types import CallToolResult, Icon, TextContent

from mcp_synology import __version__
from mcp_synology.core.auth import (
    AuthManager,
    LoginRefusals,
    credentials_fingerprint,
    resolve_credentials,
)
from mcp_synology.core.client import DsmClient, dsm_call_of
from mcp_synology.core.errors import ErrorCode, SynologyError
from mcp_synology.core.formatting import error_response, synology_error_response
from mcp_synology.core.redaction import redact_urls
from mcp_synology.core.state import ServerState
from mcp_synology.modules import PermissionTier, RegisterContext, filter_tools_by_permission
from mcp_synology.modules import downloadstation as _downloadstation_mod
from mcp_synology.modules import filestation as _filestation_mod
from mcp_synology.modules import photos as _photos_mod
from mcp_synology.modules import storage as _storage_mod
from mcp_synology.modules import system as _system_mod

if TYPE_CHECKING:
    from collections.abc import AsyncIterator, Callable, Mapping
    from types import ModuleType

    from mcp.server.mcpserver import Context
    from mcp.types import InputRequiredResult, ToolAnnotations
    from mcp.types import Tool as MCPTool

    from mcp_synology.core.config import AppConfig, ModuleConfig

    _ModulePlan = list[tuple[str, ModuleType, ModuleConfig, set[str]]]

logger = logging.getLogger(__name__)

# How long shutdown waits for DSM to answer the logout (#402).
LOGOUT_TIMEOUT = 5.0


def _load_instruction(name: str) -> str:
    """Load an instruction file from the instructions directory."""
    path = Path(__file__).parent / "instructions" / name
    return path.read_text(encoding="utf-8").strip()


_BASE_INSTRUCTIONS = _load_instruction("server.md")
# Sent only with the module they describe (#169): File Station's sections, and the local
# machine's details when a tool reads or writes local files. A module's WRITE-tool guidance
# is sent only when those tools are registered (#318).
_MODULE_INSTRUCTIONS = {"filestation": _load_instruction("filestation.md")}
_MODULE_WRITE_INSTRUCTIONS = {"filestation": _load_instruction("filestation-write.md")}
_LOCAL_MACHINE_INSTRUCTIONS = _load_instruction("local.md")

_ICON_BASE_URL = "https://raw.githubusercontent.com/cmeans/mcp-synology/main/src/mcp_synology/icons"


_PNG_ICON_SIZE = 128


def _load_icons() -> list[Icon]:
    """Return the server's icons: hosted SVGs, plus embedded PNGs (#229).

    The MCP spec requires clients that display icons to support PNG, but SVG only
    SHOULD be supported. The PNGs are ``data:`` URIs because the spec lets clients
    refuse icon URLs from another origin, and a stdio server has none.
    """
    icons: list[Icon] = []
    themes: tuple[Literal["light", "dark"], ...] = ("light", "dark")
    for theme in themes:
        icons.append(
            Icon(
                src=f"{_ICON_BASE_URL}/mcp-synology-logo-{theme}.svg",
                mime_type="image/svg+xml",
                sizes=["any"],  # the spec's value for scalable formats
                theme=theme,
            )
        )
    icon_dir = Path(__file__).parent / "icons"
    for theme in themes:
        png = (icon_dir / f"mcp-synology-{theme}-{_PNG_ICON_SIZE}.png").read_bytes()
        icons.append(
            Icon(
                src=f"data:image/png;base64,{base64.b64encode(png).decode('ascii')}",
                mime_type="image/png",
                sizes=[f"{_PNG_ICON_SIZE}x{_PNG_ICON_SIZE}"],
                theme=theme,
            )
        )
    return icons


def _platform_label() -> str:
    """Return a short platform label like 'Linux', 'macOS', or 'Windows'."""
    system = platform.system()
    if system == "Darwin":
        return "macOS"
    return system  # "Linux", "Windows", etc.


# Known module registry — each entry exposes MODULE_INFO and register()
_MODULE_REGISTRY: dict[str, ModuleType] = {
    "downloadstation": _downloadstation_mod,
    "filestation": _filestation_mod,
    "photos": _photos_mod,
    "storage": _storage_mod,
    "system": _system_mod,
}


def _unreachable_error(base_url: str, timeout: float, exc: httpx.TransportError) -> NoReturn:
    """Raise a structured error for a first connection (API info + login) that failed (#138).

    httpx's connect errors often stringify to ``''``, which used to reach the
    model as "Error executing tool list_shares: " with nothing to act on.
    Only a failure to connect is "can't reach" (#254): a NAS that accepted the connection
    and then didn't answer in time, or dropped it, is there but busy (e.g. a large
    delete), and a pool timeout never sent anything. Any URL in the text is redacted.
    """
    text = redact_urls(str(exc))
    detail = f" ({type(exc).__name__}: {text})" if text else f" ({type(exc).__name__})"
    busy = (
        "Try again shortly. If the NAS is often this slow, raise connection.timeout "
        f"in the config (now {timeout:g} s). "
        "`mcp-synology check --config <file>` tests the connection and login."
    )
    if isinstance(exc, httpx.ReadTimeout | httpx.WriteTimeout):
        error_response(
            ErrorCode.TIMEOUT,
            f"The NAS at {base_url} accepted the connection but didn't answer within "
            f"{timeout:g} s{detail}. It may be busy (heavy load, or a large delete or copy).",
            retryable=True,
            suggestion=busy,
        )
    if isinstance(exc, httpx.ReadError | httpx.WriteError | httpx.RemoteProtocolError):
        error_response(
            ErrorCode.UNAVAILABLE,
            f"The NAS at {base_url} accepted the connection but dropped it{detail}. It may "
            "be busy or restarting.",
            retryable=True,
            suggestion=busy,
        )
    if isinstance(exc, httpx.PoolTimeout):
        error_response(
            ErrorCode.TIMEOUT,
            f"No connection to the NAS at {base_url} was free within {timeout:g} s{detail}, "
            "so the request wasn't sent.",
            retryable=True,
            suggestion="Try again shortly.",
        )
    timed_out = isinstance(exc, httpx.TimeoutException)
    what = f"timed out after {timeout:g} s" if timed_out else "could not connect"
    error_response(
        ErrorCode.TIMEOUT if timed_out else ErrorCode.UNAVAILABLE,
        f"Can't reach the NAS at {base_url}: {what}{detail}.",
        retryable=True,
        suggestion=(
            "Check that the NAS is powered on and reachable from this machine (LAN or VPN), "
            "and that connection host, port and https in the config are right. "
            "`mcp-synology check --config <file>` tests the connection and login."
        ),
    )


def _transport_error_response(exc: httpx.TransportError) -> NoReturn:
    """Raise a structured error for a network failure after the client connected (#237)."""
    timed_out = isinstance(exc, httpx.TimeoutException)
    text = redact_urls(str(exc))
    detail = f"{type(exc).__name__}: {text}" if text else type(exc).__name__
    call = dsm_call_of(exc)
    if timed_out:
        what = f"DSM didn't answer {call} in time" if call else "DSM didn't answer in time"
    else:
        what = (
            f"The connection to DSM failed during {call}"
            if call
            else "The connection to DSM failed"
        )
    error_response(
        ErrorCode.TIMEOUT if timed_out else ErrorCode.UNAVAILABLE,
        f"{what} ({detail}).",
        retryable=True,
        suggestion=(
            "DSM may be busy (e.g. with a very large delete or copy). Try again shortly; if "
            "this was a change, check whether it applied first. connection.timeout in the "
            "config gives DSM longer to answer."
        ),
    )


_NULL_SCHEMA = {"type": "null"}


def _compact_schema(node: Any) -> Any:
    """Return a copy of a JSON schema without what a model can't use (#163).

    Drops every ``title`` (pydantic's restatement of the parameter name) and
    collapses ``{"anyOf": [T, {"type": "null"}], "default": null}`` into ``T``:
    the parameter stays out of ``required``, so leaving it out still means "not
    set". Everything else is kept, including ``description``/``enum``/``examples``.
    MCPServer validates arguments against each function's pydantic model, not this
    advertised schema, so what the server accepts doesn't change.
    """
    if isinstance(node, list):
        return [_compact_schema(item) for item in node]
    if not isinstance(node, dict):
        return node
    out = {key: _compact_schema(value) for key, value in node.items() if key != "title"}
    any_of = out.get("anyOf")
    if (
        isinstance(any_of, list)
        and len(any_of) == 2
        and _NULL_SCHEMA in any_of
        and "default" in out
        and out["default"] is None
    ):
        branches = [branch for branch in any_of if branch != _NULL_SCHEMA]
        if len(branches) == 1 and isinstance(branches[0], dict):
            rest = {k: v for k, v in out.items() if k not in ("anyOf", "default")}
            return {**branches[0], **rest}
    return out


class _SynologyServer(MCPServer):
    """MCPServer with compacted tool schemas, text-only results and error details kept.

    mcp 2.x documents ``list_tools()`` and ``call_tool()`` as what the protocol handlers
    call, and ``add_tool()`` as what the ``tool()`` decorator calls, so these overrides
    take effect without touching SDK internals.
    """

    # Set by create_server: the update notice goes on the session's first tool result,
    # whichever module the tool belongs to (#314).
    update_notices: SharedClientManager | None = None

    def add_tool(
        self,
        fn: Callable[..., Any],
        name: str | None = None,
        title: str | None = None,
        description: str | None = None,
        annotations: ToolAnnotations | None = None,
        icons: list[Icon] | None = None,
        meta: dict[str, Any] | None = None,
        structured_output: bool | None = None,
    ) -> None:
        """Register every tool as unstructured (#377).

        Every tool returns text written for the model. Left to auto-detect, the SDK
        treats ``-> str`` as structured: it gives each tool an ``outputSchema`` of
        ``{"result": string}`` (about 7,000 characters across the tool list) and sends
        each reply twice, as ``content`` and again as ``structuredContent``. Only an
        explicit ``structured_output=True`` keeps it.
        """
        super().add_tool(
            fn,
            name=name,
            title=title,
            description=description,
            annotations=annotations,
            icons=icons,
            meta=meta,
            structured_output=structured_output is True,
        )

    async def list_tools(self) -> list[MCPTool]:
        """Serve each tool's input schema compacted (#163)."""
        return [
            tool.model_copy(update={"input_schema": _compact_schema(tool.input_schema)})
            for tool in await super().list_tools()
        ]

    async def call_tool(
        self, name: str, arguments: dict[str, Any], context: Context[Any, Any] | None = None
    ) -> CallToolResult | InputRequiredResult:
        """Run the tool (``_call_tool``), then add a pending update notice to its result.

        The notice goes on the first result after the background check finds a version
        to announce, for any module's tool and for an error too (#314). Before, only File
        Station's tools carried it. It's taken only for a result that can carry it (not an
        input request), and taking it can't fail the tool.
        """
        try:
            result = await self._call_tool(name, arguments, context)
        except ToolError as exc:
            notice = await self._take_update_notice()
            if notice:
                raise type(exc)(f"{exc}{notice}") from exc.__cause__
            raise
        if not isinstance(result, CallToolResult):
            return result
        notice = await self._take_update_notice()
        if notice:
            texts = [i for i, c in enumerate(result.content) if isinstance(c, TextContent)]
            if texts:
                content = list(result.content)
                last = content[texts[-1]]
                assert isinstance(last, TextContent)
                content[texts[-1]] = last.model_copy(update={"text": last.text + notice})
                result = result.model_copy(update={"content": content})
            else:
                result = result.model_copy(
                    update={"content": [*result.content, TextContent(type="text", text=notice)]}
                )
        return result

    async def _take_update_notice(self) -> str:
        if self.update_notices is None:
            return ""
        try:
            return await self.update_notices.take_update_notice()
        except Exception:  # noqa: BLE001 - a notice must never fail a tool that worked
            logger.debug("Couldn't deliver the update notice", exc_info=True)
            return ""

    async def _call_tool(
        self, name: str, arguments: dict[str, Any], context: Context[Any, Any] | None = None
    ) -> CallToolResult | InputRequiredResult:
        """Keep the message of an exception that isn't a ``ToolError``.

        mcp 2.x sends the model only "Error executing tool <name>" for one, where 1.x
        added the message. Login failures reach here as ``AuthenticationError`` (from
        ``SharedClientManager.get_client()``), so without this the model loses hints
        like "run mcp-synology setup". A ``SynologyError`` becomes the usual structured
        error; anything else keeps 1.x's text, with any URL in it redacted (#232).

        Both get the "Error executing tool <name>: " prefix the SDK puts on a
        ``ToolError`` raised inside a handler, so every error reads the same. A
        ``SynologyError`` stays a plain ``ToolError`` (logged at INFO, no traceback);
        anything else stays an ``UnexpectedToolError``, which the SDK logs at ERROR
        with the traceback.

        A network error talking to DSM (httpx ``TransportError``, e.g. a ``ReadTimeout``
        while DSM is busy) is anticipated too: a structured ``timeout`` or
        ``unavailable`` error. httpx timeouts stringify to ``''``, which reached the
        model as "Error executing tool delete_files: " (#237).
        """
        prefix = f"Error executing tool {name}: "
        try:
            return await super().call_tool(name, arguments, context)
        except UnexpectedToolError as exc:
            cause = exc.__cause__
            if isinstance(cause, SynologyError):
                try:
                    synology_error_response(name, cause)
                except ToolError as structured:
                    raise ToolError(f"{prefix}{structured}") from cause
            # Backstop for #232: a crash naming a DSM URL mustn't hand the model its
            # passwd or _sid. Known DSM HTTP failures never carry the URL (DsmHttpError).
            if isinstance(cause, httpx.TransportError):
                try:
                    _transport_error_response(cause)
                except ToolError as structured:
                    raise ToolError(f"{prefix}{structured}") from cause
            # Never an empty message: httpx-style errors stringify to '' (#237).
            text = redact_urls(str(cause)) if cause is not None else ""
            # Deliberately against UnexpectedToolError's docstring ("the SDK raises this
            # itself... you never raise it"): re-raising it with the message restored keeps
            # the SDK's ERROR-level log with the traceback, which a plain ToolError (logged
            # at INFO) would lose (#114).
            raise UnexpectedToolError(f"{prefix}{text or type(cause).__name__}") from cause


# A comment that fills its whole line (allowing indentation) goes with its line break;
# anywhere else only the comment goes, so the text around it keeps its line breaks.
# The whole-line alternative must not cross a "-->": with DOTALL a lazy ".*?" would
# otherwise run on past a comment that has text after it, to a later "-->", and
# delete the instruction text between them (QA round 1, #164).
_HTML_COMMENT = re.compile(
    r"^[ \t]*<!--(?:(?!-->).)*-->[ \t]*\n|<!--.*?-->", re.DOTALL | re.MULTILINE
)


_TEMPLATE_VAR = re.compile(r"\{([a-z_]+)\}")


def _fill_template(text: str, template_vars: Mapping[str, str | None]) -> str:
    """Replace the documented ``{name}`` template variables and nothing else.

    ``str.format_map`` read every brace as a field, so a custom instruction such as
    ``Use JSON like {"a": 1}`` raised ``KeyError`` and ``serve`` failed to start. Any
    other brace, including an unknown ``{name}``, is left as written.
    """

    def fill(m: re.Match[str]) -> str:
        name = m.group(1)
        # str() as format_map did: a value typed Optional (instance_id) reads "None", not "".
        return str(template_vars[name]) if name in template_vars else m.group(0)

    return _TEMPLATE_VAR.sub(fill, text)


def _strip_html_comments(text: str) -> str:
    """Remove ``<!-- ... -->`` blocks from instruction text.

    A comment on a line of its own is removed with that line; elsewhere only the comment
    is removed, so it never joins the lines around it.

    Comments are notes for whoever edits the file. Stripping happens before the template
    variables are filled in (``_fill_template``), so a comment's ``{placeholders}`` are
    never filled in and sent to the model. An unclosed ``<!--`` is left as written (#163).
    """
    return _HTML_COMMENT.sub("", text)


def _startup_update_notice(config: AppConfig) -> tuple[str, str] | None:
    """A security release the last check found, for the server instructions (#314).

    Read from the global state's cache (no network call, so startup isn't delayed). Only
    a security release goes in the instructions: an MCP client keeps a server running
    across chats, so the instructions reach every chat until the client restarts, which
    is right for a security release (announced every session anyway) and too often for
    an ordinary one (once a week, through a tool result). Returns ``(version, notice)``,
    or None when there's nothing to add or ``update_notices`` is ``off``.
    """
    if not config.check_for_updates or config.update_notices == "off":
        return None
    from mcp_synology.cli import _load_global_state
    from mcp_synology.cli.version import _cached_update, update_notice

    state = _load_global_state()
    latest = _cached_update(state)
    if latest is None or not state.get("latest_is_security"):
        return None
    if state.get("latest_known_version") != latest:
        return None
    return latest, update_notice(latest, security=True)


class SharedClientManager:
    """Manages the shared DSM client lifecycle across all modules.

    Handles lazy initialization, authentication, session cleanup,
    and update notice delivery.
    """

    def __init__(self, config: AppConfig) -> None:
        self._config = config
        self._client: DsmClient | None = None
        self._auth: AuthManager | None = None
        self._server_state: ServerState = ServerState()
        self._update_notice: str | None = None
        self._pending_version: str | None = None
        # The version this session already announced (in the instructions or a result),
        # so the background check doesn't announce it twice.
        self._announced_version: str | None = None
        self._bg_task: asyncio.Task[None] | None = None
        # Re-auth callbacks queued by modules during sync `register()` — flushed
        # to the AuthManager once it's lazily created in `get_client`. Modules
        # use `subscribe_on_reauth` to register cache invalidators (see #37).
        self._pending_reauth_callbacks: list[Callable[[], None]] = []
        # Parallel first tool calls build one client and log in once (#359).
        self._init_lock = asyncio.Lock()
        # Kept across failed first logins, so refused credentials aren't sent again (#447).
        self._login_refusals = LoginRefusals()

    async def get_client(self) -> DsmClient:
        """Lazily initialize and return the DSM client."""
        if self._client is None:
            async with self._init_lock:
                if self._client is None:
                    await self._init_client()
        if self._client is None:
            raise RuntimeError("Client initialization failed")
        return self._client

    async def _init_client(self) -> None:
        """Build the client, read the API list and log in; on failure leave nothing open."""
        if self._login_refusals.any:
            # Answer credentials DSM already refused without contacting DSM at all (#447).
            self._login_refusals.check(credentials_fingerprint(*resolve_credentials(self._config)))
        conn = self._config.connection
        if conn is None:
            raise RuntimeError("Server started without connection config")
        protocol = "https" if conn.https else "http"
        base_url = f"{protocol}://{conn.host}:{conn.port}"
        client = DsmClient(
            base_url=base_url,
            verify_ssl=conn.verify_ssl,
            timeout=conn.timeout,
        )
        await client.__aenter__()
        try:
            await client.query_api_info()
            auth = AuthManager(self._config, client, self._login_refusals)
            await auth.login()
        except BaseException as e:
            # Don't leak the half-open client; the next call starts fresh.
            await client.__aexit__(None, None, None)
            if isinstance(e, httpx.TransportError):
                _unreachable_error(base_url, conn.timeout, e)
            raise

        # Flush any reauth callbacks that modules queued during sync
        # `register()` (before the AuthManager existed).
        for cb in self._pending_reauth_callbacks:
            auth.add_on_reauth_callback(cb)
        self._pending_reauth_callbacks.clear()

        self._client = client
        self._auth = auth

    def subscribe_on_reauth(self, cb: Callable[[], None]) -> None:
        """Register an on-reauth callback at any point in the lifecycle.

        Safe to call from sync `register()` before the AuthManager exists —
        the callback is queued and forwarded once the manager comes up.
        Subsequent calls (e.g. after `get_client`) attach directly.
        """
        if self._auth is not None:
            self._auth.add_on_reauth_callback(cb)
        else:
            self._pending_reauth_callbacks.append(cb)

    def start_update_check(self) -> None:
        """Start the background update check (from the server's lifespan, #314).

        It used to start on the first DSM login, so a session's first tool result could
        never carry the notice, and a session that never called a tool never checked.
        """
        if self._config.check_for_updates and self._bg_task is None:
            self._bg_task = asyncio.create_task(self._bg_update_check())

    async def take_update_notice(self) -> str:
        """The pending update notice, once, as a footer (#314).

        Every connection on an install checks on its own; the announcement is claimed
        under the global-state lock here, at delivery, so only the first connection to
        deliver an ordinary release says it, once a week (a security release is always
        claimed). A connection that loses the claim says nothing.
        """
        from mcp_synology.cli.version import claim_announcement

        notice, self._update_notice = self._update_notice, None
        version = self._pending_version
        if not notice or not version:
            return ""
        try:
            claimed = await asyncio.to_thread(claim_announcement, version)
        except BaseException:
            # Not delivered: keep it for the next result. (A call canceled mid-claim may
            # still have recorded it; that costs one weekly slot, not a repeat.)
            self._update_notice = notice
            raise
        self._announced_version = version
        return f"\n\n---\n{notice}" if claimed else ""

    def install_signal_handlers(self) -> None:
        """Turn SIGTERM and SIGINT into SystemExit, so the event loop unwinds through the
        server's lifespan, whose teardown logs out of DSM (``aclose``)."""

        def _signal_handler(signum: int, _frame: object) -> None:
            logger.debug("Received signal %d, shutting down", signum)
            raise SystemExit(128 + signum)

        signal.signal(signal.SIGTERM, _signal_handler)
        signal.signal(signal.SIGINT, _signal_handler)

    async def aclose(self) -> None:
        """Log out of DSM and close the HTTP client, from the running event loop (#402).

        Called from the server's lifespan teardown. It used to run at atexit, where
        asyncio can no longer resolve a host ("cannot schedule new futures after
        interpreter shutdown"), so the logout never completed and each exit left its DSM
        session open until DSM's own timeout. Shielded so a cancelled run still logs out,
        and bounded by ``LOGOUT_TIMEOUT`` so a NAS that doesn't answer can't hold up exit.
        """
        auth, client = self._auth, self._client
        self._auth = self._client = None
        if self._bg_task is not None and not self._bg_task.done():
            self._bg_task.cancel()
        with anyio.CancelScope(shield=True):
            if auth is not None:
                with anyio.move_on_after(LOGOUT_TIMEOUT) as scope:
                    try:
                        await auth.logout(raise_errors=True)
                    except Exception as e:  # noqa: BLE001 - logged; exit must go on
                        logger.warning(
                            "Couldn't log out of DSM on shutdown (%s: %s); the session "
                            "stays open until DSM's session timeout.",
                            type(e).__name__,
                            e,
                        )
                if scope.cancelled_caught:
                    logger.warning(
                        "DSM didn't answer the logout within %g s on shutdown; the session "
                        "stays open until DSM's session timeout.",
                        LOGOUT_TIMEOUT,
                    )
            if client is not None:
                await client.__aexit__(None, None, None)

    async def _bg_update_check(self) -> None:
        """Background update check: log a newer version, and queue the notice (#314).

        A newer version is logged at WARNING (the client's MCP server log). The notice
        is queued for the next tool result when this connection's ``update_notices``
        wants it (``all``, or ``security`` for a security release), the instructions
        don't already carry it, and the once-a-week cooldown allows it; a security
        release is announced every session. ``take_update_notice`` claims it at
        delivery, so connections sharing an install don't each say it.

        Closes #39. Two pre-fix gaps:

        1. The `(OSError, ValueError, KeyError)` handler used a bare `pass`,
           so a failed PyPI check (DNS down, malformed version string,
           state-file KeyError) left no breadcrumb anywhere — even with
           `SYNOLOGY_LOG_LEVEL=debug`. Now logs at DEBUG with `exc_info=True`
           so the actual exception type, message, and traceback land in
           verbose output.

        2. The `run_in_executor()` call was unbounded. The inner
           `urlopen(timeout=5)` covered the socket, but a thread stuck after
           `urlopen` returned (pathological YAML parsing, slow disk) would
           keep this coroutine alive forever and delay session shutdown.
           Now wrapped in `asyncio.timeout(10)`; on timeout we log at DEBUG
           and exit cleanly so the tool flow is never blocked.
        """
        try:
            from mcp_synology.cli.version import (
                _should_announce,
                refresh_update_check,
                update_notice,
            )

            loop = asyncio.get_running_loop()

            # Run the blocking check in a thread, bounded so a stuck executor (slow
            # socket, slow YAML parse, hung disk) can't keep this coroutine alive
            # forever. PyPI (5 s) and GitHub (3 s) have their own timeouts; 10 s is the
            # margin for the rest. The synchronous fcntl.flock stays inside the thread,
            # never spanning an `await`, and the network calls run outside it.
            def _check() -> tuple[str | None, bool, bool]:
                result, sec, gstate = refresh_update_check()
                return result, sec, bool(result) and _should_announce(gstate, str(result))

            async with asyncio.timeout(10):
                latest, security, announce = await loop.run_in_executor(None, _check)
            if latest:
                logger.warning(
                    "mcp-synology %s is available%s (running %s).",
                    latest,
                    ", a security release" if security else "",
                    __version__,
                )
                wanted = self._config.update_notices == "all" or (
                    security and self._config.update_notices == "security"
                )
                if wanted and latest != self._announced_version and announce:
                    self._update_notice = update_notice(latest, security=security)
                    self._pending_version = latest
        except TimeoutError:
            # asyncio.timeout raises builtins.TimeoutError on 3.11+. Treat
            # as a non-event for the user-facing flow — they just don't
            # get an "update available" notice this session.
            logger.debug("Update check timed out after 10s; skipping notice", exc_info=True)
        except (OSError, ValueError, KeyError):
            # Closes #39: surfaces the actual exception under
            # `SYNOLOGY_LOG_LEVEL=debug`. Update check is best-effort, so
            # we still don't propagate — the tool flow runs fine without
            # an update notice.
            logger.debug("Update check failed", exc_info=True)


def _withheld_tools_notice(config: AppConfig, plan: _ModulePlan) -> str:
    """Say which tools this connection doesn't register, and how to enable them.

    A read-only connection hides its WRITE tools, so a model (or a directory's
    scorer) couldn't tell they exist and might say the server can't move or
    delete files. Registration itself is unchanged.
    """
    lines: list[str] = []
    for mod_name, module, mod_config, allowed in plan:
        withheld = [t.name for t in module.MODULE_INFO.tools if t.name not in allowed]
        if withheld:
            lines.append(
                f"- {mod_name} is {mod_config.permission}-only here. Withheld: "
                f"{', '.join(withheld)}. To enable them, the user sets "
                f"`permission: write` for {mod_name} in the mcp-synology config."
            )
    enabled = {name for name, _, _, _ in plan}
    for mod_name in sorted(set(_MODULE_REGISTRY) - enabled):
        count = len(_MODULE_REGISTRY[mod_name].MODULE_INFO.tools)
        if mod_name in config.modules:
            # Listed but `enabled: false`: adding it again wouldn't help.
            fix = f"set `enabled: true` for `{mod_name}`"
        else:
            fix = f"add `{mod_name}` under `modules:`"
        needs = " (it needs a DSM administrator account)" if mod_name == "storage" else ""
        lines.append(
            f"- The {mod_name} module ({count} tools) is not enabled. The user can "
            f"{fix} in the mcp-synology config{needs}."
        )
    if not lines:
        return ""
    return (
        "\n\nPERMISSIONS:\n"
        "Some mcp-synology tools aren't available on this connection. If the user "
        "asks for one, say it exists but isn't enabled, and how to enable it; don't "
        "say the server can't do it.\n" + "\n".join(lines)
    )


def _local_paths_notice(config: AppConfig, plan: _ModulePlan) -> str:
    """Name the local folders each module's tools may use (allowed_local_paths, #257)."""
    from mcp_synology.core.local_paths import allowed_local_roots
    from mcp_synology.modules.downloadstation import DownloadStationSettings
    from mcp_synology.modules.filestation import FileStationSettings
    from mcp_synology.modules.photos import PhotosSettings

    def folders(roots: list[Path]) -> str:
        return ", ".join(str(r) for r in roots)

    def only_use(tools: tuple[str, ...], allowed: set[str]) -> str:
        # Name only the registered tools: no upload tool at permission: read (#318).
        names = [t for t in tools if t in allowed]
        return " and ".join(names) + (" only use" if len(names) > 1 else " only uses")

    lines: list[str] = []
    for mod_name, _module, mod_config, allowed in plan:
        if mod_name == "filestation" and {"upload_file", "download_file"} & set(allowed):
            fs = FileStationSettings(**mod_config.settings)
            default_dir = fs.default_download_dir
            roots = allowed_local_roots(
                fs.allowed_local_paths,
                str(Path(default_dir).expanduser()) if default_dir else None,
                shared_paths=config.allowed_local_paths,
                module="filestation",
            )
            lines.append(
                f"{only_use(('upload_file', 'download_file'), allowed)} local paths inside "
                f"these folders (or below them): {folders(roots)}."
            )
        elif mod_name == "downloadstation" and "create_download" in allowed:
            ds = DownloadStationSettings(**mod_config.settings)
            roots = allowed_local_roots(
                ds.allowed_local_paths,
                shared_paths=config.allowed_local_paths,
                module="downloadstation",
            )
            lines.append(
                "create_download's torrent_file_path only reads .torrent and .nzb files "
                f"inside these folders (or below them): {folders(roots)}."
            )
        elif mod_name == "photos" and {"upload_photos", "download_photos"} & set(allowed):
            ph = PhotosSettings(**mod_config.settings)
            roots = allowed_local_roots(
                ph.allowed_local_paths,
                shared_paths=config.allowed_local_paths,
                module="photos",
            )
            lines.append(
                f"{only_use(('upload_photos', 'download_photos'), allowed)} local paths inside "
                f"these folders (or below them): {folders(roots)}."
            )
    if not lines:
        return ""
    return (
        "\n\nLOCAL PATHS:\n"
        + "\n".join(lines)
        + "\nAnything else is refused; the user can add folders with allowed_local_paths, "
        "at the top level of the config for every module or in a module's settings "
        "(without a top-level list, a module's list replaces the Downloads default)."
    )


_PHOTOS_NOTICE = """

SYNOLOGY PHOTOS:
Photos keeps its own library: the personal space (space=personal, the default) and Shared
Space (space=shared). Photos tools take the ids that list_photos and
list_photo_collections return; for files outside Photos, File Station's tools take paths,
when that module is enabled. Taken times are the photo's own local clock time (the
camera's, or the NAS's for a file without EXIF): don't convert them."""

# Only when the Photos WRITE tools are registered (#318).
_PHOTOS_WRITE_NOTICE = """
delete_photos, delete_photo_folders, delete_photo_albums and merge_people can't be undone
from Photos: name what will go and confirm with the user first. A long copy, move or delete
hands off with a photos:<id> handle; follow up with photo_task."""


_STORAGE_NOTICE = """

STORAGE:
The storage tools read Storage Manager, which answers only a DSM administrator account; with
any other account each one returns permission_denied. For any question about disks, RAID or
free space, start with get_storage_overview."""


def _storage_notice(plan: _ModulePlan) -> str:
    """The storage module's administrator requirement, when it's enabled (#49)."""
    return _STORAGE_NOTICE if any(name == "storage" for name, *_ in plan) else ""


def _writes_registered(module: ModuleType, allowed: set[str]) -> bool:
    """Whether this connection registers any of the module's WRITE tools (#318)."""
    return any(
        t.name in allowed and t.permission_tier is PermissionTier.WRITE
        for t in module.MODULE_INFO.tools
    )


def _photos_notice(plan: _ModulePlan) -> str:
    """How to use the Photos tools, when the module is enabled (#258).

    The paragraph about deletes and handed-off tasks only when the WRITE tools are (#318).
    """
    for name, module, _, allowed in plan:
        if name == "photos":
            writes = _writes_registered(module, allowed)
            return _PHOTOS_NOTICE + (_PHOTOS_WRITE_NOTICE if writes else "")
    return ""


def config_notices(config: AppConfig) -> list[str]:
    """Warnings for config keys this version doesn't know: misspelled or newer.

    An unknown module name, or an unknown key in a module's `settings`, would otherwise
    be ignored without a word (an `allowed_local_paths` typo left the Downloads default in
    force). They're warnings, not errors, so a config written for a newer version still
    starts; `serve` logs them and `check` prints them. Unknown keys directly under a
    module are errors (`ModuleConfig`, extra="forbid").
    """
    notices = []
    known = ", ".join(sorted(_MODULE_REGISTRY))
    for mod_name, mod_config in config.modules.items():
        module = _MODULE_REGISTRY.get(mod_name)
        if module is None:
            notices.append(f"Unknown module '{mod_name}' in modules: skipped. Known: {known}.")
            continue
        schema = module.MODULE_INFO.settings_schema
        valid = sorted(schema.model_fields) if schema else []
        for key in sorted(set(mod_config.settings) - set(valid)):
            where = f"modules.{mod_name}.settings"
            notices.append(
                f"Unknown setting '{key}' in {where}: ignored. Valid: {', '.join(valid)}."
                if valid
                else f"Unknown setting '{key}' in {where}: ignored ({mod_name} has no settings)."
            )
    return notices


def create_server(config: AppConfig) -> MCPServer:
    """Create and configure the MCP server.

    This sets up the MCP server instance and registers tools based on
    module configuration and permission tiers.
    """
    for notice in config_notices(config):
        logger.warning("%s", notice)
    # Populate template variables in the instructions so Claude can
    # distinguish between multiple NAS connections.
    conn = config.connection
    template_vars = {
        "display_name": config.display_name,
        "instance_id": config.instance_id,
        "host": conn.host if conn else "unknown",
        "port": str(conn.port) if conn else "5000",
        "home_dir": str(Path.home()),
        "platform": _platform_label(),
    }
    server_name = f"synology-{config.display_name}"

    # Resolve each enabled module's tools first, so the instructions can include only the
    # enabled modules' sections and say which tools this connection withholds.
    plan: _ModulePlan = []
    for mod_name, mod_config in config.modules.items():
        if not mod_config.enabled:
            logger.debug("Module '%s' is disabled, skipping", mod_name)
            continue
        module = _MODULE_REGISTRY.get(mod_name)
        if module is None:  # named by config_notices above
            continue
        tier = PermissionTier(mod_config.permission)
        plan.append(
            (
                mod_name,
                module,
                mod_config,
                filter_tools_by_permission(module.MODULE_INFO.tools, tier),
            )
        )

    # The built-in instructions: the core file, then each enabled module's own section, and
    # its WRITE-tool guidance when those tools are registered (#318).
    # HTML comments are stripped per file, so an unclosed one can't swallow the next file.
    texts = [_BASE_INSTRUCTIONS]
    for name, module, _, allowed in plan:
        if name in _MODULE_INSTRUCTIONS:
            texts.append(_MODULE_INSTRUCTIONS[name])
        if name in _MODULE_WRITE_INSTRUCTIONS and _writes_registered(module, allowed):
            texts.append(_MODULE_WRITE_INSTRUCTIONS[name])
    builtin = "\n\n".join(_strip_html_comments(text).strip() for text in texts)

    replacement: str | None = None
    if config.instructions_file:
        # Full replacement — user provides their own instructions file.
        # They can start from the built-in server.md, filestation.md, filestation-write.md
        # and local.md.
        instructions_path = Path(config.instructions_file).expanduser()
        try:
            replacement = _strip_html_comments(
                instructions_path.read_text(encoding="utf-8")
            ).strip()
        except OSError as e:
            # Fall back to the full built-in instructions, generated sections included.
            logger.error("Failed to load instructions_file '%s': %s", instructions_path, e)

    if replacement is not None:
        instructions = replacement
    elif config.custom_instructions:
        # Prepend custom instructions before the built-in text — gives them priority.
        # Strip each source on its own: an unclosed `<!--` in the custom text must not
        # pair with a `-->` in a built-in file and swallow the text between them.
        instructions = _strip_html_comments(config.custom_instructions).strip() + "\n\n" + builtin
    else:
        instructions = builtin

    instructions = _fill_template(instructions, template_vars)

    if replacement is None:
        # A full replacement file is the user's own; everything else gets the notices.
        instructions += _photos_notice(plan)
        instructions += _storage_notice(plan)
        instructions += _withheld_tools_notice(config, plan)
        local_paths = _local_paths_notice(config, plan)
        if local_paths:
            local_machine = _strip_html_comments(_LOCAL_MACHINE_INSTRUCTIONS).strip()
            instructions += "\n\n" + _fill_template(local_machine, template_vars) + local_paths

    manager = SharedClientManager(config)
    manager.install_signal_handlers()

    # A security release the last check found goes in the instructions too, so the model
    # knows from the first message (#314). Added to a replacement file as well: it isn't
    # documentation the user wrote, and it's the only channel a no-tool chat has.
    startup_update = _startup_update_notice(config)
    if startup_update:
        version, notice = startup_update
        instructions += f"\n\nUPDATE AVAILABLE:\n{notice}"
        manager._announced_version = version

    @asynccontextmanager
    async def _lifespan(_server: MCPServer[Any]) -> AsyncIterator[None]:
        manager.start_update_check()
        try:
            yield None
        finally:
            await manager.aclose()

    server = _SynologyServer(
        server_name,
        instructions=instructions,
        icons=_load_icons(),
        version=__version__,
        lifespan=_lifespan,
    )
    server.update_notices = manager

    logger.debug("Creating MCP server")

    for mod_name, module, mod_config, allowed_tools in plan:
        module_info = module.MODULE_INFO
        logger.debug(
            "Module '%s': permission=%s, registering %d/%d tools: %s",
            mod_name,
            mod_config.permission,
            len(allowed_tools),
            len(module_info.tools),
            sorted(allowed_tools),
        )

        ctx = RegisterContext(
            server=server,
            manager=manager,
            allowed_tools=allowed_tools,
            settings_dict=mod_config.settings,
            display_name=config.display_name,
            allowed_local_paths=config.allowed_local_paths,
        )
        module.register(ctx)

    return server
