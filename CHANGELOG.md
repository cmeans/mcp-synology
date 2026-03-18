# Changelog

## 0.2.0 (2026-03-18)

Quality and correctness fixes from critical code review and live testing.

### Bug Fixes

- **Update check no longer blocks first tool call** — PyPI check runs in background thread via asyncio, tool response returns immediately
- **Deduplicated login flows** — extracted shared `_attempt_login()`, eliminating ~100 lines of duplicate 2FA handling code
- **Instance ID accepts uppercase** — `MyNAS` silently becomes `mynas` instead of erroring about invalid characters
- **Search timeout accurate** — uses `time.monotonic()` instead of counting sleep intervals, which excluded request duration
- **Search pattern fix** — `*.mkv` correctly uses DSM extension filter instead of broken pattern parameter
- **Pagination correct with hidden #recycle** — changed default to show `#recycle` (avoids offset math bugs); users can still hide via config
- **Auth error 402 correctly identified** — Auth-specific error code map prevents FileStation "System too busy" misidentification
- **Session parameter removed from login** — was causing 402 errors on some DSM configurations
- **D-Bus socket not found now logged** — was silently failing; helps diagnose keyring issues on Linux
- **Directory detection improved** — better heuristic in copy/move/delete output formatting

### Features

- **MCP tool annotations** — all 12 tools annotated with readOnlyHint, destructiveHint, idempotentHint
- **Version management** — `--check-update`, `--auto-upgrade enable|disable`, `--revert [VERSION]`
- **In-session update notices** — first tool response in Claude Desktop shows notice if newer version on PyPI
- **`check_for_updates` config flag** — set to false to silence update notices
- **Instruction files** — MCP server instructions moved to external `.md` file for easier maintenance
- **Publish workflow** — GitHub Actions publishes to PyPI on tag push, TestPyPI on manual dispatch
- **Auth error codes 400-410** — full Auth API error map with actionable messages
- **File Station error codes 900, 1100, 1101** — filesystem permission denied, unsupported target

### Documentation

- README rewritten with `uv tool install` Quick Start (not git clone)
- Updates section documenting version management
- Credentials doc expanded with 2FA device tokens, platform table, Linux D-Bus

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
