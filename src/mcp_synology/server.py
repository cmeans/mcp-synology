"""MCP server initialization, module loading, and startup."""

from __future__ import annotations

import asyncio
import atexit
import base64
import logging
import platform
import re
import signal
from pathlib import Path
from typing import TYPE_CHECKING, Any, Literal, NoReturn

import httpx
from mcp.server.mcpserver import MCPServer
from mcp.server.mcpserver.exceptions import ToolError, UnexpectedToolError
from mcp.types import Icon

from mcp_synology import __version__
from mcp_synology.core.auth import AuthManager
from mcp_synology.core.client import DsmClient, dsm_call_of
from mcp_synology.core.errors import ErrorCode, SynologyError
from mcp_synology.core.formatting import error_response, synology_error_response
from mcp_synology.core.redaction import redact_urls
from mcp_synology.core.state import ServerState
from mcp_synology.modules import PermissionTier, RegisterContext, filter_tools_by_permission
from mcp_synology.modules import downloadstation as _downloadstation_mod
from mcp_synology.modules import filestation as _filestation_mod
from mcp_synology.modules import photos as _photos_mod
from mcp_synology.modules import system as _system_mod

if TYPE_CHECKING:
    from collections.abc import Callable
    from types import ModuleType

    from mcp.server.mcpserver import Context
    from mcp.types import CallToolResult, InputRequiredResult, ToolAnnotations
    from mcp.types import Tool as MCPTool

    from mcp_synology.core.config import AppConfig, ModuleConfig

    _ModulePlan = list[tuple[str, ModuleType, ModuleConfig, set[str]]]

logger = logging.getLogger(__name__)


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
        """Register every tool as unstructured (#375).

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
            raise UnexpectedToolError(f"{prefix}{text or type(cause).__name__}") from cause


# A comment that fills its whole line (allowing indentation) goes with its line break;
# anywhere else only the comment goes, so the text around it keeps its line breaks.
# The whole-line alternative must not cross a "-->": with DOTALL a lazy ".*?" would
# otherwise run on past a comment that has text after it, to a later "-->", and
# delete the instruction text between them (QA round 1, #164).
_HTML_COMMENT = re.compile(
    r"^[ \t]*<!--(?:(?!-->).)*-->[ \t]*\n|<!--.*?-->", re.DOTALL | re.MULTILINE
)


def _strip_html_comments(text: str) -> str:
    """Remove ``<!-- ... -->`` blocks from instruction text.

    A comment on a line of its own is removed with that line; elsewhere only the comment
    is removed, so it never joins the lines around it.

    Comments are notes for whoever edits the file. Stripping happens before
    ``format_map``, so a comment's ``{placeholders}`` are never filled in and sent to the
    model, and can't raise ``KeyError``. An unclosed ``<!--`` is left as written (#163).
    """
    return _HTML_COMMENT.sub("", text)


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
        self._bg_task: asyncio.Task[None] | None = None
        self._cleanup_task: asyncio.Task[None] | None = None
        # Re-auth callbacks queued by modules during sync `register()` — flushed
        # to the AuthManager once it's lazily created in `get_client`. Modules
        # use `subscribe_on_reauth` to register cache invalidators (see #37).
        self._pending_reauth_callbacks: list[Callable[[], None]] = []

    async def get_client(self) -> DsmClient:
        """Lazily initialize and return the DSM client."""
        if self._client is None:
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
                auth = AuthManager(self._config, client)
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

            # Check for updates in background — never blocks tool execution
            if self._config.check_for_updates:
                self._bg_task = asyncio.create_task(self._bg_update_check())

        if self._client is None:
            raise RuntimeError("Client initialization failed")
        return self._client

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

    def with_update_notice(self, result: str) -> str:
        """Append update notice to tool result (first call only, then clears)."""
        notice = self._update_notice or ""
        if notice:
            self._update_notice = None
        return result + notice

    def install_cleanup_handlers(self) -> None:
        """Register atexit and signal handlers for graceful session cleanup."""
        atexit.register(self._cleanup_session)

        def _signal_handler(signum: int, _frame: object) -> None:
            logger.debug("Received signal %d, cleaning up session", signum)
            self._cleanup_session()
            raise SystemExit(128 + signum)

        signal.signal(signal.SIGTERM, _signal_handler)
        signal.signal(signal.SIGINT, _signal_handler)

    def _cleanup_session(self) -> None:
        """Best-effort session logout on shutdown."""
        if self._auth is None:
            return

        async def _logout() -> None:
            try:
                if self._auth is not None:
                    await self._auth.logout()
                if self._client is not None:
                    await self._client.__aexit__(None, None, None)
            except Exception:
                pass  # Best effort — process is exiting

        try:
            loop = asyncio.get_running_loop()
            self._cleanup_task = loop.create_task(_logout())
        except RuntimeError:
            # No running loop — create one for cleanup
            asyncio.run(_logout())

    async def _bg_update_check(self) -> None:
        """Background update check — appends notice on first tool result.

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
            from mcp_synology.cli import (
                _check_for_update,
                _load_global_state,
                _save_global_state,
                _with_global_state_lock,
            )

            loop = asyncio.get_running_loop()

            # Run the blocking PyPI check in a thread, bounded so a stuck
            # executor (slow socket, slow YAML parse, hung disk) can't
            # keep this coroutine alive forever. The inner urlopen has
            # its own 5s timeout; the 10s outer bound is a safety margin
            # for everything else _check_for_update does.
            #
            # The full load → check → save sequence runs inside the executor
            # so the synchronous fcntl.flock never spans an `await`. Holding
            # a sync lock across an await would be a deadlock footgun for any
            # future async caller of `_with_global_state_lock` on this event
            # loop (e.g. #75's ServerState lifecycle).
            def _check_under_lock() -> str | None:
                with _with_global_state_lock():
                    gstate = _load_global_state()
                    result = _check_for_update(gstate)
                    _save_global_state(gstate)
                    return result

            async with asyncio.timeout(10):
                latest = await loop.run_in_executor(None, _check_under_lock)
            if latest:
                from mcp_synology import __version__

                self._update_notice = (
                    f"\n\n---\nUpdate available: mcp-synology {latest} "
                    f"(current: {__version__}). "
                    f"Run: mcp-synology --check-update"
                )
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
        lines.append(
            f"- The {mod_name} module ({count} tools) is not enabled. The user can "
            f"{fix} in the mcp-synology config."
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

    instructions = instructions.format_map(template_vars)

    if replacement is None:
        # A full replacement file is the user's own; everything else gets the notices.
        instructions += _photos_notice(plan)
        instructions += _withheld_tools_notice(config, plan)
        local_paths = _local_paths_notice(config, plan)
        if local_paths:
            local_machine = _strip_html_comments(_LOCAL_MACHINE_INSTRUCTIONS).strip()
            instructions += "\n\n" + local_machine.format_map(template_vars) + local_paths

    server = _SynologyServer(
        server_name,
        instructions=instructions,
        icons=_load_icons(),
        version=__version__,
    )

    logger.debug("Creating MCP server")

    manager = SharedClientManager(config)
    manager.install_cleanup_handlers()

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
