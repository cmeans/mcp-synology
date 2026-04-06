"""Tests for core/state.py — state file read/write."""

from __future__ import annotations

from unittest.mock import patch

from mcp_synology.core.state import ApiInfoEntry, ServerState, load_state, save_state


class TestServerState:
    def test_default_state(self) -> None:
        state = ServerState()
        assert state.api_info_cache == {}
        assert state.negotiated_versions == {}
        assert state.last_connected is None

    def test_load_nonexistent_returns_default(self, tmp_path: object) -> None:
        state = load_state("nonexistent-instance")
        assert isinstance(state, ServerState)
        assert state.api_info_cache == {}

    def test_save_and_load_roundtrip(self, tmp_path: object) -> None:
        instance_id = "test-roundtrip"
        state = ServerState(
            api_info_cache={
                "SYNO.API.Auth": ApiInfoEntry(path="entry.cgi", min_version=1, max_version=7),
            },
            negotiated_versions={"SYNO.API.Auth": 6},
            last_connected="2026-03-16T22:00:00Z",
            dsm_version="7.2.1-69057",
            hostname="MyNAS",
        )

        # Use tmp_path for home directory to avoid polluting real filesystem
        with patch("mcp_synology.core.state.Path.home", return_value=tmp_path):
            save_state(instance_id, state)
            loaded = load_state(instance_id)

        assert loaded.negotiated_versions == {"SYNO.API.Auth": 6}
        assert loaded.last_connected == "2026-03-16T22:00:00Z"
        assert loaded.dsm_version == "7.2.1-69057"
        assert "SYNO.API.Auth" in loaded.api_info_cache
        assert loaded.api_info_cache["SYNO.API.Auth"].max_version == 7
