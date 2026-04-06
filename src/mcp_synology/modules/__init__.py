"""Module system: ModuleInfo, registration helpers, permission tiers."""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import TYPE_CHECKING, Any

from mcp.types import ToolAnnotations

if TYPE_CHECKING:
    from mcp.server.fastmcp import FastMCP
    from pydantic import BaseModel

    from mcp_synology.server import SharedClientManager


class PermissionTier(Enum):
    """Permission tiers for tool access control.

    READ < WRITE < ADMIN — each tier includes all tools from lower tiers.
    """

    READ = "read"
    WRITE = "write"
    ADMIN = "admin"

    def __ge__(self, other: object) -> bool:
        if not isinstance(other, PermissionTier):
            return NotImplemented
        return _TIER_ORDER[self] >= _TIER_ORDER[other]

    def __gt__(self, other: object) -> bool:
        if not isinstance(other, PermissionTier):
            return NotImplemented
        return _TIER_ORDER[self] > _TIER_ORDER[other]

    def __le__(self, other: object) -> bool:
        if not isinstance(other, PermissionTier):
            return NotImplemented
        return _TIER_ORDER[self] <= _TIER_ORDER[other]

    def __lt__(self, other: object) -> bool:
        if not isinstance(other, PermissionTier):
            return NotImplemented
        return _TIER_ORDER[self] < _TIER_ORDER[other]


_TIER_ORDER: dict[PermissionTier, int] = {
    PermissionTier.READ: 0,
    PermissionTier.WRITE: 1,
    PermissionTier.ADMIN: 2,
}


@dataclass
class ApiRequirement:
    """Declares that a module requires a specific DSM API."""

    api_name: str
    min_version: int = 1
    max_version: int | None = None
    optional: bool = False


@dataclass
class ToolInfo:
    """Metadata for a single MCP tool."""

    name: str
    description: str
    permission_tier: PermissionTier = PermissionTier.READ
    annotations: ToolAnnotations | None = None


@dataclass
class ModuleInfo:
    """Static metadata for a module."""

    name: str
    description: str
    required_apis: list[ApiRequirement] = field(default_factory=list)
    tools: list[ToolInfo] = field(default_factory=list)
    settings_schema: type[BaseModel] | None = None


@dataclass
class RegisterContext:
    """Context passed to each module's register() function."""

    server: FastMCP
    manager: SharedClientManager
    allowed_tools: set[str]
    settings_dict: dict[str, Any]
    display_name: str


def default_annotations(tier: PermissionTier) -> ToolAnnotations:
    """Return default ToolAnnotations for a permission tier."""
    if tier == PermissionTier.READ:
        return ToolAnnotations(readOnlyHint=True, destructiveHint=False)
    return ToolAnnotations(readOnlyHint=False, destructiveHint=False)


class VersionedHandler:
    """Decorator-based dispatch for handling different API versions.

    Usage:
        handler = VersionedHandler()

        @handler.version(1, 1)
        async def handle_v1(**kwargs):
            ...

        @handler.version(2, 3)
        async def handle_v2_v3(**kwargs):
            ...

        # Dispatches to best match:
        result = await handler(negotiated_version=2, **kwargs)
    """

    def __init__(self) -> None:
        self._handlers: list[tuple[int, int, Any]] = []

    def version(self, min_ver: int, max_ver: int | None = None) -> Any:
        """Register a handler for a version range."""
        effective_max = max_ver if max_ver is not None else min_ver

        def decorator(func: Any) -> Any:
            self._handlers.append((min_ver, effective_max, func))
            return func

        return decorator

    async def __call__(self, negotiated_version: int, **kwargs: Any) -> Any:
        """Dispatch to the best matching handler for the negotiated version."""
        best_handler = None
        best_max = -1

        for min_ver, max_ver, handler in self._handlers:
            if min_ver <= negotiated_version <= max_ver and max_ver > best_max:
                best_max = max_ver
                best_handler = handler

        if best_handler is None:
            msg = f"No handler registered for version {negotiated_version}"
            raise ValueError(msg)

        return await best_handler(**kwargs)


def filter_tools_by_permission(
    tools: list[ToolInfo],
    tier: PermissionTier,
) -> set[str]:
    """Return tool names that are allowed at the given permission tier."""
    return {t.name for t in tools if t.permission_tier <= tier}


def validate_api_requirements(
    requirements: list[ApiRequirement],
    api_cache: dict[str, Any],
) -> list[str]:
    """Validate that all required APIs are available.

    Returns a list of warning/error messages for missing or incompatible APIs.
    """
    issues: list[str] = []

    for req in requirements:
        if req.api_name not in api_cache:
            if req.optional:
                issues.append(
                    f"Optional API '{req.api_name}' not available — some features may be limited."
                )
            else:
                issues.append(f"Required API '{req.api_name}' not available on this NAS.")
            continue

        info = api_cache[req.api_name]
        nas_max = info.max_version if hasattr(info, "max_version") else info.get("maxVersion", 1)
        nas_min = info.min_version if hasattr(info, "min_version") else info.get("minVersion", 1)

        if req.min_version > nas_max:
            issues.append(
                f"API '{req.api_name}' requires v{req.min_version}+, "
                f"but NAS only supports v{nas_min}-v{nas_max}."
            )

    return issues
