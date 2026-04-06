"""Tests for server.py — server creation and tool registration."""

from __future__ import annotations

from mcp_synology.server import _BASE_INSTRUCTIONS, create_server
from tests.conftest import make_test_config


class TestCreateServer:
    def test_server_creation(self) -> None:
        config = make_test_config()
        server = create_server(config)
        assert server is not None

    def test_server_with_read_permission(self) -> None:
        config = make_test_config(modules={"filestation": {"enabled": True, "permission": "read"}})
        server = create_server(config)
        assert server is not None

    def test_server_with_write_permission(self) -> None:
        config = make_test_config(modules={"filestation": {"enabled": True, "permission": "write"}})
        server = create_server(config)
        assert server is not None

    def test_server_with_disabled_module(self) -> None:
        config = make_test_config(modules={"filestation": {"enabled": False}})
        server = create_server(config)
        assert server is not None

    def test_server_with_unknown_module(self) -> None:
        config = make_test_config(
            modules={
                "filestation": {"enabled": True},
                "unknown_module": {"enabled": True},
            }
        )
        server = create_server(config)
        assert server is not None

    def test_server_uses_display_name_for_hostname(self) -> None:
        """Server should use config.display_name, not raw host."""
        config = make_test_config(alias="My NAS")
        assert config.display_name == "My NAS"
        server = create_server(config)
        assert server is not None

    def test_server_with_custom_settings(self) -> None:
        """Custom filestation settings are applied."""
        config = make_test_config(
            modules={
                "filestation": {
                    "enabled": True,
                    "permission": "write",
                    "settings": {
                        "file_type_indicator": "text",
                        "async_timeout": 300,
                        "search_timeout": 600,
                        "search_poll_interval": 2.0,
                        "hide_recycle_in_listings": False,
                    },
                }
            }
        )
        server = create_server(config)
        assert server is not None


class TestMcpInstructions:
    def test_instructions_mention_path_format(self) -> None:
        assert "PATH FORMAT" in _BASE_INSTRUCTIONS

    def test_instructions_mention_file_sizes(self) -> None:
        assert "FILE SIZES" in _BASE_INSTRUCTIONS

    def test_instructions_mention_recycle_bin(self) -> None:
        assert "RECYCLE BIN" in _BASE_INSTRUCTIONS

    def test_instructions_mention_list_shares_first(self) -> None:
        assert "list_shares" in _BASE_INSTRUCTIONS


class TestFileStationSettings:
    def test_default_settings(self) -> None:
        from mcp_synology.modules.filestation import FileStationSettings

        s = FileStationSettings()
        assert s.hide_recycle_in_listings is False
        assert s.file_type_indicator == "emoji"
        assert s.async_timeout == 120
        assert s.search_timeout is None
        assert s.copy_move_timeout is None
        assert s.delete_timeout is None
        assert s.dir_size_timeout is None
        assert s.search_poll_interval == 1.0

    def test_specific_timeouts_override(self) -> None:
        from mcp_synology.modules.filestation import FileStationSettings

        s = FileStationSettings(
            async_timeout=60,
            search_timeout=300,
            copy_move_timeout=180,
        )
        assert s.async_timeout == 60
        assert s.search_timeout == 300
        assert s.copy_move_timeout == 180
        assert s.delete_timeout is None  # falls back to async_timeout

    def test_search_poll_interval_bounds(self) -> None:
        import pytest

        from mcp_synology.modules.filestation import FileStationSettings

        with pytest.raises(ValueError):
            FileStationSettings(search_poll_interval=0.1)  # below minimum 0.5
        with pytest.raises(ValueError):
            FileStationSettings(search_poll_interval=20.0)  # above maximum 10.0
