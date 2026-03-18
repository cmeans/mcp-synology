# Changelog

## 0.1.0 (2026-03-17)

Initial release.

### Features

- **File Station module** — 12 tools for managing files on Synology NAS:
  - READ: list_shares, list_files, list_recycle_bin, search_files, get_file_info, get_dir_size
  - WRITE: create_folder, rename, copy_files, move_files, delete_files, restore_from_recycle_bin
- **Interactive setup** — `synology-mcp setup` creates config, stores credentials, handles 2FA, emits Claude Desktop snippet
- **2FA support** — auto-detected device token bootstrap with silent re-authentication
- **Secure credentials** — OS keyring integration (macOS Keychain, Windows Credential Manager, Linux GNOME Keyring / KWallet)
- **Linux D-Bus auto-detection** — keyring works from Claude Desktop without manual env var configuration
- **Multi-NAS** — separate configs, credentials, and state per instance via `instance_id`
- **Env-var-only mode** — `SYNOLOGY_HOST` without a config file synthesizes a default config
- **Permission tiers** — READ or WRITE per module, enforced at tool registration
- **MCP tool annotations** — readOnlyHint, destructiveHint, idempotentHint on all tools
- **Version management** — `--check-update`, `--auto-upgrade`, `--revert` CLI flags
- **In-session update notices** — first tool response includes update notice if newer version on PyPI
- **Configurable timeouts** — per-operation overrides for search, copy/move, delete, dir size
- **Debug logging** — passwords masked, only relevant APIs logged, `--verbose` flag

### Configuration

- `check_for_updates` — disable PyPI update checks (default: true)
- `alias` — friendly display name for the NAS instance
- `instance_id` — arbitrary identifier that keys credentials, state, and config files
- Per-operation timeouts: `search_timeout`, `copy_move_timeout`, `delete_timeout`, `dir_size_timeout`, `search_poll_interval`

### Tested against

- Synology DS1618+ running DSM 7.1.1-42962 Update 6
- All 12 File Station tools verified via Claude Desktop
- 2FA login with device token re-authentication
- 243 automated tests, 84% code coverage
