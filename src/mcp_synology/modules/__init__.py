"""Module system: ModuleInfo, registration helpers, permission tiers."""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import TYPE_CHECKING, Any, TypeVar, get_args

from mcp.types import ToolAnnotations
from pydantic import BaseModel, ValidationInfo, field_validator

if TYPE_CHECKING:
    from mcp.server.mcpserver import MCPServer

    from mcp_synology.server import SharedClientManager

_Settings = TypeVar("_Settings", bound=BaseModel)


def _plain_types(annotation: Any) -> set[Any]:
    """The types in an annotation, through `| None` and the like."""
    args = get_args(annotation)
    if not args:
        return {annotation}
    return {t for arg in args for t in _plain_types(arg)}


class ModuleSettings(BaseModel):
    """Base of every module's settings: the rules that hold for any setting (#511 PR 3).

    They look at a field's own value, not inside a list or mapping: no setting today holds
    numbers in one.
    """

    @field_validator("*", mode="before")
    @classmethod
    def _same_rules_as_the_config(cls, value: object, info: ValidationInfo) -> object:
        field_name = info.field_name or ""
        types = _plain_types(cls.model_fields[field_name].annotation)
        # Pydantic reads a boolean as 1 or 0, so `task_timeout: on` loaded a 1 s timeout,
        # as `port: yes` loaded port 1.
        if isinstance(value, bool) and bool not in types and types & {int, float}:
            msg = "must be a number, not true or false (YAML reads yes/no/on/off as those)."
            raise ValueError(msg)
        # An empty path names no folder: an error, not "none", as `logging.file` (#511). Bytes
        # are YAML's `!!binary`, which pydantic would decode into a text setting (#553), and
        # `bytes.strip` leaves a no-break space, so it's judged as the text it would become.
        text = value.decode(errors="replace") if isinstance(value, bytes) else value
        if str in types and isinstance(text, str) and not text.strip():
            msg = f"is empty: give a value, or leave {field_name} out."
            raise ValueError(msg)
        return value


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
    # A display name for clients, where the name alone is ambiguous next to another
    # module's tool (Photos' photo_folder beside File Station's create_folder).
    title: str | None = None


@dataclass
class ModuleInfo:
    """Static metadata for a module."""

    name: str
    description: str
    required_apis: list[ApiRequirement] = field(default_factory=list)
    tools: list[ToolInfo] = field(default_factory=list)
    settings_schema: type[ModuleSettings] | None = None


@dataclass
class RegisterContext:
    """Context passed to each module's register() function."""

    server: MCPServer
    manager: SharedClientManager
    allowed_tools: set[str]
    display_name: str
    # Already validated (server.module_settings); None means the module's defaults.
    settings: BaseModel | None = None
    # The top-level allowed_local_paths, shared by every module (#257).
    allowed_local_paths: list[str] | None = None

    def settings_as(self, schema: type[_Settings]) -> _Settings:
        """This module's validated settings, or its defaults when none were given."""
        if self.settings is None:
            return schema()
        if not isinstance(self.settings, schema):
            msg = f"Expected {schema.__name__}, got {type(self.settings).__name__}"
            raise TypeError(msg)
        return self.settings


def default_annotations(tier: PermissionTier) -> ToolAnnotations:
    """Return default ToolAnnotations for a permission tier."""
    if tier == PermissionTier.READ:
        return ToolAnnotations(read_only_hint=True, destructive_hint=False)
    return ToolAnnotations(read_only_hint=False, destructive_hint=False)


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
