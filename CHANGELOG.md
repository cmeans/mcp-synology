# Changelog

## 0.4.1 (2026-04-07)

### Fixes

- **Claude Desktop config** — setup snippet now uses `uvx mcp-synology` instead of bare command, which failed with ENOENT on systems where `~/.local/bin` isn't in Claude Desktop's PATH
- **Migration script** — now auto-updates `claude_desktop_config.json` (detects and rewrites old synology-mcp entries), creates `.json.bak` backup before writing, preserves extra args, handles `--config=value` equals syntax
- **README** — added migration section near top for users upgrading from synology-mcp, standardized all examples on `uvx`

### Added

- **MCP registry files** — `glama.json` for Glama.ai directory, `server.json` for official MCP Registry
- **PyPI ownership verification** — `mcp-name` comment in README for official registry validation
- **GitHub issue templates** — bug report, feature request, platform test report

## 0.4.0 (2026-04-05)

### Breaking Changes

- **Package renamed** — `synology-mcp` → `mcp-synology` (distribution, CLI command, config paths, state paths, keyring service)
- **Python import renamed** — `synology_mcp` → `mcp_synology`
- **Config directory** — `~/.config/synology-mcp/` → `~/.config/mcp-synology/`
- **State directory** — `~/.local/state/synology-mcp/` → `~/.local/state/mcp-synology/`
- **Keyring service** — `synology-mcp/{instance_id}` → `mcp-synology/{instance_id}` (re-run `mcp-synology setup`)
- **DSM session/device name** — `SynologyMCP` → `MCPSynology`
- **License** — MIT → Apache 2.0

### Features

- **File transfer tools** — 2 new tools for uploading and downloading files:
  - `upload_file` — upload local files to NAS with overwrite control, custom filenames, and progress reporting (WRITE tier)
  - `download_file` — download NAS files to local disk with pre-flight disk space check, streaming writes, partial file cleanup on failure, and progress reporting (READ tier)
  - Large file warnings when transfers exceed 1 GB
- **Project icons** — light/dark SVGs, PNGs (16–256px), and favicon.ico exposed via MCP `icons` parameter
- **TestPyPI workflow** — dedicated `test-publish.yml` for manual dispatch; `publish.yml` simplified to tag-only PyPI publishing
- **Virtual DSM test framework** — container-based integration testing with golden image save/restore, Playwright-based DSM wizard automation, and Podman/Docker auto-detection (`tests/vdsm/`)

### Migration

A migration script handles config, state, and keyring automatically:

```bash
uv tool install mcp-synology
python scripts/migrate-from-synology-mcp.py          # dry run — preview changes
python scripts/migrate-from-synology-mcp.py --apply  # apply changes
```

Then update Claude Desktop config: change `"command"` from `"synology-mcp"` to `"mcp-synology"`.

## 0.3.1 (2026-03-18)

### Features

- **System monitoring module** — 2 new read-only tools:
  - `get_system_info` — model, firmware, CPU specs, RAM, temperature, uptime (works for all users via `SYNO.DSM.Info`, supplemented by `SYNO.Core.System` for admin users)
  - `get_resource_usage` — live CPU load, memory usage, disk I/O per drive, network throughput per interface (requires admin account via `SYNO.Core.System.Utilization`)

### Bug Fixes

- **Orphaned background tasks** — Search, DirSize, CopyMove, and Delete operations now use `try/finally` to ensure tasks are always stopped. Previously, errors during polling could skip cleanup, leaving `synoscgi` processes consuming CPU indefinitely on the NAS
- **Cleanup failures logged** — replaced silent `contextlib.suppress` with warning-level log messages
- **Always use GET** — removed POST logic entirely. DSM 7.1 reports `requestFormat=JSON` on all FileStation APIs even at v2, causing silent failures with POST

### Code Quality

- Removed unused `noqa` directives
- `datetime.now(tz=UTC)` instead of naive `datetime.now()`
- `Self` return type for `__aenter__`
- `int | float` simplified to `float` in type hints
- `list.extend` with generators instead of append loops
- Store `asyncio.create_task` references to prevent GC
- Move inline imports (`asyncio`, `time`) to module top level
- Initialize task attributes in `__init__`

### Documentation

- README restructured: modules listed separately from features, env-var mode shows Claude Desktop config, 2FA token expiry clarified, config hierarchy shown in YAML examples, custom instructions use cases expanded
- DEVELOPMENT.md extracted from README: build commands, integration test setup, design docs
- CLAUDE.md updated: v0.3.x status, GET-only rule, version pinning, search gotchas, background task cleanup pattern
- Integration tests expanded to 37 (system info, resource usage with admin fixture, utilization under load)

## 0.3.0 (2026-03-18)

Major refactor: CLI split, module registration system, DSM API fixes, integration tests.

### Breaking Changes

- **CLI is now a package** — `src/mcp_synology/cli.py` split into `cli/` package with 6 submodules (main, setup, check, version, logging_). Backward-compatible re-exports via `cli/__init__.py`

### Bug Fixes

- **Always use GET for DSM API calls** — DSM 7.1 reports `requestFormat=JSON` on all FileStation APIs even at v2, causing silent failures with POST. All requests now use GET exclusively
- **Pin CopyMove, Delete, Search to v2** — v3 JSON request format incompatible with our comma-separated path encoding
- **Search finds directories** — always send `filetype=all` (DSM defaults to `"file"`, excluding directories from results)
- **Search wildcard wrapping** — bare keywords auto-wrapped with `*...*` (e.g., `"Bambu"` → `"*Bambu*"`) so substring matching works
- **Search poll retry** — don't trust `finished=True` with 0 results until 3+ polls, preventing false positives on non-indexed shares
- **Orphaned background task cleanup** — all async tasks (Search, DirSize, CopyMove, Delete) now use `try/finally` to ensure stop/clean is called. Previously, errors during polling would skip cleanup, leaving orphaned `synoscgi` processes consuming CPU indefinitely
- **Cleanup failures logged** — replaced silent `contextlib.suppress` with warning-level log messages on stop/clean failure
- **Copy/move error detection** — check `error` field in status response, not just `finished` flag. Added error codes 1000-1002 for copy/move failures
- **Error 600 mapped** — search folder access denied now returns actionable message

### Features

- **Generic module registration** — `RegisterContext` + `SharedClientManager` pattern replaces 400-line monolithic `_register_filestation()`. New modules just define `register(ctx)` functions
- **MCP tool annotations** — `readOnlyHint`, `destructiveHint`, `idempotentHint` from mcp.types, with `default_annotations()` helper
- **Multi-NAS server identity** — server name includes `display_name` (e.g., `synology-nas01`). Template variables `{display_name}`, `{instance_id}`, `{host}`, `{port}` in instruction files
- **Custom instructions** — `custom_instructions` config field (prepended to built-in instructions) and `instructions_file` (full replacement) for non-clone installs
- **Integration test suite** — 32 tests against real NAS: connection, listing, search, metadata, copy/move/rename/delete lifecycle, recycle bin, error handling
- **Configurable test paths** — `test_paths` in `integration_config.yaml` for NAS-specific folders

### Documentation

- CLAUDE.md updated: v0.3.0 status, GET-only rule, version pinning, search gotchas, background task cleanup pattern, integration test setup
- README: multi-NAS setup with aliases, custom instructions, Linux DBUS note
- Config spec: `alias`, `custom_instructions`, `instructions_file` fields
- Power-user example: alias and instruction configuration

## 0.2.2 (2026-03-17)

Code quality fixes from second external review.

### Bug Fixes

- **No more `assert` in production** — replaced 8 bare asserts in server.py and cli.py with explicit `if`/`raise RuntimeError` checks that survive `python -O`
- **Renamed builtin-shadowing exceptions** — `PermissionError` → `SynologyPermissionError`, `FileExistsError` → `SynologyFileExistsError` to prevent confusion with Python builtins
- **Removed fragile is-directory heuristic** — copy/move/delete output no longer guesses file vs folder icons; plain names until type is known
- **Session cleanup on shutdown** — `atexit` handler and SIGTERM/SIGINT signal handlers call `AuthManager.logout()` to free DSM sessions
- **Search truncation notice** — when results exceed limit, output now shows "(showing 500 of 1,234 — increase limit to see more)"
- **Removed dead `poll_async_task` helper** — unused generic polling function removed from helpers.py

### Documentation

- README install updated to `uv tool install mcp-synology` (PyPI) instead of git URL

## 0.2.1 (2026-03-18)

### Bug Fixes

- **Sort by modified date** — map common field names (modified, date, created) to DSM API fields (mtime, crtime, etc.)
- **Narrow exception handling** — replaced all broad `except Exception` with specific types across cli.py
- **Typed lazy state** — server init state is now a dataclass instead of untyped dict
- **Publish workflow runs tests** — broken code can no longer publish to PyPI
- **Async fixture type hint** — proper `AsyncGenerator` annotation
- **Docs accuracy** — D-Bus wording, README install section title

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
- **Interactive setup** — `mcp-synology setup` creates config, stores credentials, handles 2FA, emits Claude Desktop snippet
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
