"""FastMCP server initialization, module loading, and startup."""

from __future__ import annotations

import asyncio
import atexit
import logging
import signal
from pathlib import Path
from typing import TYPE_CHECKING

from mcp.server.fastmcp import FastMCP

from synology_mcp.core.auth import AuthManager
from synology_mcp.core.client import DsmClient
from synology_mcp.core.state import ServerState
from synology_mcp.modules import PermissionTier, RegisterContext, filter_tools_by_permission
from synology_mcp.modules import filestation as _filestation_mod
from synology_mcp.modules import system as _system_mod

if TYPE_CHECKING:
    from types import ModuleType

    from synology_mcp.core.config import AppConfig

logger = logging.getLogger(__name__)


def _load_instruction(name: str) -> str:
    """Load an instruction file from the instructions directory."""
    path = Path(__file__).parent / "instructions" / name
    return path.read_text(encoding="utf-8").strip()


_BASE_INSTRUCTIONS = _load_instruction("server.md")

# Known module registry — each entry exposes MODULE_INFO and register()
_MODULE_REGISTRY: dict[str, ModuleType] = {
    "filestation": _filestation_mod,
    "system": _system_mod,
}


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
            await client.query_api_info()

            auth = AuthManager(self._config, client)
            await auth.login()

            self._client = client
            self._auth = auth

            # Check for updates in background — never blocks tool execution
            if self._config.check_for_updates:
                asyncio.create_task(self._bg_update_check())

        if self._client is None:
            raise RuntimeError("Client initialization failed")
        return self._client

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
            except Exception:  # noqa: BLE001
                pass  # Best effort — process is exiting

        try:
            loop = asyncio.get_running_loop()
            loop.create_task(_logout())
        except RuntimeError:
            # No running loop — create one for cleanup
            asyncio.run(_logout())

    async def _bg_update_check(self) -> None:
        """Background update check — appends notice on first tool result."""
        try:
            from synology_mcp.cli import (
                _check_for_update,
                _load_global_state,
                _save_global_state,
            )

            loop = asyncio.get_running_loop()
            gstate = _load_global_state()
            # Run the blocking PyPI check in a thread
            latest = await loop.run_in_executor(None, _check_for_update, gstate)
            _save_global_state(gstate)
            if latest:
                from synology_mcp import __version__

                self._update_notice = (
                    f"\n\n---\nUpdate available: synology-mcp {latest} "
                    f"(current: {__version__}). "
                    f"Run: synology-mcp --check-update"
                )
        except (OSError, ValueError, KeyError):
            pass  # Never let update check break tool functionality


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
    }
    server_name = f"synology-{config.display_name}"

    if config.instructions_file:
        # Full replacement — user provides their own instructions file.
        # They can copy the built-in server.md as a starting point.
        instructions_path = Path(config.instructions_file).expanduser()
        try:
            instructions = instructions_path.read_text(encoding="utf-8").strip()
        except OSError as e:
            logger.error("Failed to load instructions_file '%s': %s", instructions_path, e)
            instructions = _BASE_INSTRUCTIONS
    elif config.custom_instructions:
        # Prepend custom instructions before base — gives them priority.
        instructions = config.custom_instructions.strip() + "\n\n" + _BASE_INSTRUCTIONS
    else:
        instructions = _BASE_INSTRUCTIONS

    instructions = instructions.format_map(template_vars)

    server = FastMCP(
        server_name,
        instructions=instructions,
    )

    logger.debug("Creating MCP server")

    manager = SharedClientManager(config)
    manager.install_cleanup_handlers()

    # Determine which modules and tools to register
    for mod_name, mod_config in config.modules.items():
        if not mod_config.enabled:
            logger.debug("Module '%s' is disabled, skipping", mod_name)
            continue

        module = _MODULE_REGISTRY.get(mod_name)
        if module is None:
            logger.warning("Unknown module '%s' — skipping.", mod_name)
            continue

        module_info = module.MODULE_INFO
        tier = PermissionTier(mod_config.permission)
        allowed_tools = filter_tools_by_permission(module_info.tools, tier)
        logger.debug(
            "Module '%s': permission=%s, registering %d/%d tools: %s",
            mod_name,
            tier.value,
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

    return server
