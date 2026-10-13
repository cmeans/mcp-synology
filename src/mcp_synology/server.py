"""FastMCP server initialization, module loading, and startup."""

from __future__ import annotations

import asyncio
import atexit
import logging
import platform
import re
import signal
from pathlib import Path
from typing import TYPE_CHECKING, Any, NoReturn

import httpx
from mcp.server.fastmcp import FastMCP
from mcp.types import Icon

from mcp_synology.core.auth import AuthManager
from mcp_synology.core.client import DsmClient
from mcp_synology.core.errors import ErrorCode
from mcp_synology.core.formatting import error_response
from mcp_synology.core.state import ServerState
from mcp_synology.modules import PermissionTier, RegisterContext, filter_tools_by_permission
from mcp_synology.modules import downloadstation as _downloadstation_mod
from mcp_synology.modules import filestation as _filestation_mod
from mcp_synology.modules import system as _system_mod

if TYPE_CHECKING:
    from collections.abc import Callable
    from types import ModuleType

    from mcp_synology.core.config import AppConfig, ModuleConfig

    _ModulePlan = list[tuple[str, ModuleType, ModuleConfig, set[str]]]

logger = logging.getLogger(__name__)


def _load_instruction(name: str) -> str:
    """Load an instruction file from the instructions directory."""
    path = Path(__file__).parent / "instructions" / name
    return path.read_text(encoding="utf-8").strip()


_BASE_INSTRUCTIONS = _load_instruction("server.md")

_ICON_BASE_URL = "https://raw.githubusercontent.com/cmeans/mcp-synology/main/src/mcp_synology/icons"


def _load_icons() -> list[Icon]:
    """Return Icon objects pointing to hosted SVGs on GitHub."""
    icons: list[Icon] = []
    theme_map = {"light": "mcp-synology-logo-light.svg", "dark": "mcp-synology-logo-dark.svg"}
    for theme, filename in theme_map.items():
        icons.append(
            Icon(  # type: ignore[call-arg]
                src=f"{_ICON_BASE_URL}/{filename}",
                mimeType="image/svg+xml",
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
    "system": _system_mod,
}


def _unreachable_error(base_url: str, timeout: float, exc: httpx.TransportError) -> NoReturn:
    """Raise a structured error for a NAS that can't be reached (#138).

    httpx's connect errors often stringify to ``''``, which used to reach the
    model as "Error executing tool list_shares: " with nothing to act on.
    """
    timed_out = isinstance(exc, httpx.TimeoutException)
    what = f"timed out after {timeout:g} s" if timed_out else "could not connect"
    detail = f" ({type(exc).__name__}: {exc})" if str(exc) else f" ({type(exc).__name__})"
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


_NULL_SCHEMA = {"type": "null"}


def _compact_schema(node: Any) -> Any:
    """Return a copy of a JSON schema without what a model can't use (#163).

    Drops every ``title`` (pydantic's restatement of the parameter name) and
    collapses ``{"anyOf": [T, {"type": "null"}], "default": null}`` into ``T``:
    the parameter stays out of ``required``, so leaving it out still means "not
    set". Everything else is kept, including ``description``/``enum``/``examples``.
    FastMCP validates arguments against each function's pydantic model, not this
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


def _compact_tool_schemas(server: FastMCP) -> None:
    """Compact every registered tool's advertised input schema in place (#163).

    Uses mcp 1.x's ``FastMCP._tool_manager``; the mcp 2.x migration (#114) must keep
    this behavior (``TestServedSchemas`` checks what a real client session receives).
    """
    for tool in server._tool_manager.list_tools():
        tool.parameters = _compact_schema(tool.parameters)


_HTML_COMMENT = re.compile(r"<!--.*?-->\n?", re.DOTALL)


def _strip_html_comments(text: str) -> str:
    """Remove ``<!-- ... -->`` blocks (and the newline after each) from instruction text.

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


def create_server(config: AppConfig) -> FastMCP:
    """Create and configure the MCP server.

    This sets up the FastMCP instance and registers tools based on
    module configuration and permission tiers.
    """
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

    if config.instructions_file:
        # Full replacement — user provides their own instructions file.
        # They can copy the built-in server.md as a starting point.
        instructions_path = Path(config.instructions_file).expanduser()
        try:
            instructions = _strip_html_comments(
                instructions_path.read_text(encoding="utf-8")
            ).strip()
        except OSError as e:
            logger.error("Failed to load instructions_file '%s': %s", instructions_path, e)
            instructions = _strip_html_comments(_BASE_INSTRUCTIONS)
    elif config.custom_instructions:
        # Prepend custom instructions before base — gives them priority.
        # Strip each source on its own: an unclosed `<!--` in the custom text must not
        # pair with a `-->` in the base file and swallow the text between them.
        instructions = (
            _strip_html_comments(config.custom_instructions).strip()
            + "\n\n"
            + _strip_html_comments(_BASE_INSTRUCTIONS)
        )
    else:
        instructions = _strip_html_comments(_BASE_INSTRUCTIONS)

    instructions = instructions.format_map(template_vars)

    # Resolve each enabled module's tools first, so the instructions can say
    # which ones this connection withholds.
    plan: _ModulePlan = []
    for mod_name, mod_config in config.modules.items():
        if not mod_config.enabled:
            logger.debug("Module '%s' is disabled, skipping", mod_name)
            continue
        module = _MODULE_REGISTRY.get(mod_name)
        if module is None:
            logger.warning("Unknown module '%s' — skipping.", mod_name)
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

    if not config.instructions_file:
        # A full replacement file is the user's own; everything else gets the notice.
        instructions += _withheld_tools_notice(config, plan)

    server = FastMCP(
        server_name,
        instructions=instructions,
        icons=_load_icons(),
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
        )
        module.register(ctx)

    _compact_tool_schemas(server)
    return server
