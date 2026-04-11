"""Tests for modules/__init__.py — module system, permission tiers, versioned handlers."""

from __future__ import annotations

import pytest

from mcp_synology.core.state import ApiInfoEntry
from mcp_synology.modules import (
    ApiRequirement,
    ModuleInfo,
    PermissionTier,
    ToolInfo,
    VersionedHandler,
    filter_tools_by_permission,
    validate_api_requirements,
)


class TestPermissionTier:
    def test_ordering(self) -> None:
        assert PermissionTier.READ < PermissionTier.WRITE
        assert PermissionTier.WRITE < PermissionTier.ADMIN
        assert PermissionTier.ADMIN > PermissionTier.READ

    def test_equality(self) -> None:
        assert PermissionTier.READ <= PermissionTier.READ
        assert PermissionTier.READ >= PermissionTier.READ

    def test_values(self) -> None:
        assert PermissionTier.READ.value == "read"
        assert PermissionTier.WRITE.value == "write"
        assert PermissionTier.ADMIN.value == "admin"

    def test_comparison_with_non_tier_returns_notimplemented(self) -> None:
        """The reflected-comparison contract: comparing PermissionTier to a
        non-PermissionTier value returns NotImplemented from each operator
        so Python can fall back to the other operand's reflected method.

        This walks the `if not isinstance(other, PermissionTier): return
        NotImplemented` defensive branches in modules/__init__.py:30/35/40/45.
        """
        tier = PermissionTier.READ
        assert tier.__ge__("not a tier") is NotImplemented
        assert tier.__gt__(42) is NotImplemented
        assert tier.__le__(None) is NotImplemented
        assert tier.__lt__(object()) is NotImplemented


class TestFilterToolsByPermission:
    def _sample_tools(self) -> list[ToolInfo]:
        return [
            ToolInfo("list_shares", "List shares", PermissionTier.READ),
            ToolInfo("list_files", "List files", PermissionTier.READ),
            ToolInfo("move_files", "Move files", PermissionTier.WRITE),
            ToolInfo("delete_files", "Delete files", PermissionTier.WRITE),
            ToolInfo("admin_op", "Admin operation", PermissionTier.ADMIN),
        ]

    def test_read_tier(self) -> None:
        tools = self._sample_tools()
        allowed = filter_tools_by_permission(tools, PermissionTier.READ)
        assert allowed == {"list_shares", "list_files"}

    def test_write_tier(self) -> None:
        tools = self._sample_tools()
        allowed = filter_tools_by_permission(tools, PermissionTier.WRITE)
        assert allowed == {"list_shares", "list_files", "move_files", "delete_files"}

    def test_admin_tier(self) -> None:
        tools = self._sample_tools()
        allowed = filter_tools_by_permission(tools, PermissionTier.ADMIN)
        assert len(allowed) == 5


class TestValidateApiRequirements:
    def _sample_cache(self) -> dict[str, ApiInfoEntry]:
        return {
            "SYNO.FileStation.List": ApiInfoEntry(path="entry.cgi", min_version=1, max_version=2),
            "SYNO.FileStation.Search": ApiInfoEntry(path="entry.cgi", min_version=1, max_version=2),
        }

    def test_all_requirements_met(self) -> None:
        reqs = [
            ApiRequirement("SYNO.FileStation.List", min_version=1),
            ApiRequirement("SYNO.FileStation.Search", min_version=1),
        ]
        issues = validate_api_requirements(reqs, self._sample_cache())
        assert issues == []

    def test_missing_required_api(self) -> None:
        reqs = [ApiRequirement("SYNO.NonExistent", min_version=1)]
        issues = validate_api_requirements(reqs, self._sample_cache())
        assert len(issues) == 1
        assert "not available" in issues[0]

    def test_missing_optional_api(self) -> None:
        reqs = [ApiRequirement("SYNO.NonExistent", min_version=1, optional=True)]
        issues = validate_api_requirements(reqs, self._sample_cache())
        assert len(issues) == 1
        assert "Optional" in issues[0]

    def test_version_too_high(self) -> None:
        reqs = [ApiRequirement("SYNO.FileStation.List", min_version=5)]
        issues = validate_api_requirements(reqs, self._sample_cache())
        assert len(issues) == 1
        assert "v5" in issues[0]


class TestVersionedHandler:
    async def test_single_version(self) -> None:
        handler = VersionedHandler()

        @handler.version(1, 2)
        async def handle_v1(**kwargs: object) -> str:
            return "v1-2"

        result = await handler(negotiated_version=1)
        assert result == "v1-2"

    async def test_dispatch_to_best_match(self) -> None:
        handler = VersionedHandler()

        @handler.version(1, 1)
        async def handle_v1(**kwargs: object) -> str:
            return "v1"

        @handler.version(2, 3)
        async def handle_v2_v3(**kwargs: object) -> str:
            return "v2-3"

        assert await handler(negotiated_version=1) == "v1"
        assert await handler(negotiated_version=2) == "v2-3"
        assert await handler(negotiated_version=3) == "v2-3"

    async def test_no_matching_handler(self) -> None:
        handler = VersionedHandler()

        @handler.version(1, 2)
        async def handle_v1(**kwargs: object) -> str:
            return "v1"

        with pytest.raises(ValueError, match="No handler"):
            await handler(negotiated_version=5)

    async def test_passes_kwargs(self) -> None:
        handler = VersionedHandler()

        @handler.version(1)
        async def handle_v1(path: str = "") -> str:
            return f"got:{path}"

        result = await handler(negotiated_version=1, path="/video")
        assert result == "got:/video"


class TestModuleInfo:
    def test_module_info_creation(self) -> None:
        info = ModuleInfo(
            name="filestation",
            description="File Station module",
            required_apis=[ApiRequirement("SYNO.FileStation.List")],
            tools=[ToolInfo("list_shares", "List shares")],
        )
        assert info.name == "filestation"
        assert len(info.tools) == 1
        assert info.settings_schema is None
