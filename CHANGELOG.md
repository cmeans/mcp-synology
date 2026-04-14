# Changelog

## Unreleased

### Added

- **vdsm CI workflow** (#24) — adds `.github/workflows/vdsm.yml` that runs the 47 vdsm integration tests on every PR using GitHub Actions' `ubuntu-24.04` runner with `/dev/kvm` access. Golden image is cached via `actions/cache@v4`, keyed on DSM version + hash of the setup scripts (`scripts/vdsm_setup.py`, `tests/vdsm/setup_dsm.py`, `tests/vdsm/config.py`, `tests/vdsm/golden_image.py`, `tests/vdsm/container.py`). Cache miss path invokes `scripts/vdsm_setup.py --yes` to build a fresh golden image. The new workflow is independent from `ci.yml` so a vdsm flake never blocks unit-test merges, and starts with `continue-on-error: true` until it has a track record of stability. Also adds a `--yes/-y` flag to `vdsm_setup.py` for non-interactive CI use.

### Fixed

- **Malformed YAML config now produces a clean error message** (#26) — `serve`, `check`, and `setup` commands previously dumped a raw `yaml.ScannerError`/`yaml.ParserError` traceback when a user had a typo in their config. They now catch `yaml.YAMLError` alongside `FileNotFoundError`/`ValueError` and emit the standard red `Error: ...` line before exiting 1. Three new regression tests cover the malformed-YAML path for each command.
- **Remove `synoshare --setopt` recycle bin enablement** (#24) — reverts the recycle bin enablement added in #23. DSM 7.2.2's `synoshare` CLI has no `--setopt` subcommand, so the call always fails (only hidden locally because the pre-existing golden image was built before the change). The revert is safe because `list_recycle_bin` in production code already handles a disabled recycle bin gracefully (returns a friendly "not enabled" message), which is exactly the path `test_02_list_recycle_bin` exercises. `TestRecycleBin` docstring updated to reflect that the test is tolerant of both states.
- **vdsm 47/47: fix all 5 remaining virtual-dsm test failures** (#23) — fixes the 5 vdsm-specific test failures identified in the #22 handoff. Production code improvements: `get_dir_size` now handles DSM error 599 (task completed before status poll) gracefully instead of crashing, returning a best-effort result; `list_recycle_bin` catches error 408 on the `#recycle` path and returns a friendly "recycle bin not enabled" message instead of raising. Test fixes: `test_get_system_info` makes Temperature assertion conditional on non-virtual hardware; `test_search_keyword_finds_directory` creates a "Bambu Studio" directory via the API (DSM search matches names, not content) and searches from the share root with retries; `test_utilization_before_and_during_load` tolerates DirSize failure since it's only a load generator. Setup fix: `setup_dsm.py` enables recycle bin via `synoshare --setopt` after share creation. New unit test for the error 599 path.

- **vdsm full automation: SSH + synoshare for shared folders** (#22) — completes the vdsm golden image setup by SSH-ing into the DSM guest VM (not the QEMU host container) to run `/usr/syno/sbin/synoshare --add` for proper DSM shared folder registration. Exposes SSH port 22, enables SSH via `SYNO.Core.Terminal` API, creates test data without sudo. Fixes FileStation API error 119 by adding `session` parameter to login. 42/47 vdsm tests pass; 5 remaining failures are virtual-dsm behavioral differences (no temp sensor, background task timing, search indexing, recycle bin config).
- **vdsm test infrastructure fixes** (#21) — fixes conftest `instance_id` validation (dots → hyphens), adds admin credentials from golden image metadata, rewrites `setup_dsm.py` with Playwright-based user creation (ExtJS-compatible `type()` input, DOM-based popup removal, wizard step navigation), adds `container_id` property, switches to stronger test password for DSM password policy. Podman KVM passthrough works; 21/47 vdsm tests pass on bare DSM without storage volume.
- **GitHub Sponsors funding configuration** (#20) — adds `.github/FUNDING.yml` to enable the Sponsor button on the repository
- **Test coverage Phase 3 + Phase 4 of #14** (#19) — closes #14. Total coverage 93% → 96%, with Phase 4's `--cov-fail-under=95` guardrail enforced in `pyproject.toml` so future regressions fail CI. Three more files at 100% (`server.py` 57% → 99% — one defensive `if self._client is None` branch unreachable; `core/auth.py` 90% → 100%; `modules/__init__.py` 96% → 100%; `core/formatting.py` 97% → 99%). Test count 457 → 487 (+30 cases). New `TestSharedClientManagerLifecycle` (15 cases) directly tests the lazy `get_client` init, `with_update_notice` clearing logic, signal handler installation including SIGTERM closure invocation, `_cleanup_session` with both running-loop and no-loop paths, and `_bg_update_check` with newer-version, no-update, and error-swallowing scenarios. New `TestPlatformLabel`, `TestCreateServerInstructionPaths` cover the `_platform_label` Darwin/Linux/Windows branches and the `instructions_file` / `custom_instructions` template paths. New `TestDbusSocketMissing`, `TestLoginErrorPaths`, `TestLogout` close the remaining gaps in `core/auth.py` (D-Bus socket-not-found branch, non-2FA SynologyError propagation, "no sid" AuthenticationError, and the three logout paths). No production code touched.
- **Test coverage Phase 2 of #14** (#17) — total coverage 85% → 93%. `cli/version.py` 27% → 100% and `cli/setup.py` 63% → 100%, the two largest gaps remaining after Phase 1. Test count 392 → 457 (+65 cases) across two new test files: `tests/core/test_cli_version.py` (40 cases covering `_get_current_version`/`_get_latest_pypi_version`/`_version_tuple`/`_detect_installer`/`_load_global_state`/`_save_global_state`/`_check_for_update`/`_do_auto_upgrade`/`_do_revert`, with `urlopen` and `subprocess.run` mocked at the boundary), and `tests/core/test_cli_setup.py` (25 cases covering the async helpers `_attempt_login`/`_connect_and_login`/`_setup_login` including the 2FA bootstrap path with device-token storage, plus `_setup_credential_flow` error paths, the `setup` command's discovered-config valid-and-invalid branches, the `_setup_interactive` validation-failure exit, and the `_emit_claude_desktop_snippet` Linux DBUS fallback). No production code touched.
- **Test coverage Phase 1 of #14** (#16) — total coverage 81% → 85%. Five files brought to 100%: `cli/check.py` (51%), `cli/main.py` (56%), `cli/logging_.py` (78%), `modules/system/__init__.py` (23%), `modules/filestation/__init__.py` (70%). Test count 336 → 392 (+56 cases). New test classes in `tests/core/test_cli.py` cover the `_check_login` async path, every top-level option in the `main` group (`--check-update`, `--auto-upgrade`, `--revert`, version-change tracking, auto-upgrade trigger), and the early/configured logging setup. Two new test files (`tests/modules/{system,filestation}/test_register.py`) exercise module registration closure bodies via `server._tool_manager._tools[name].fn` extraction with sentinel `AsyncMock` return values, walking the tool body lines that the prior `assert server is not None` style left uncovered. No production code touched.
- **`CLAUDE.md` documents the per-PR CHANGELOG convention** (#16) — adds an "Adding a CHANGELOG entry on every PR" section under "Common Tasks" specifying that every PR updates `## Unreleased` in `CHANGELOG.md` using strict Keep a Changelog categories (`### Added`, `### Changed`, `### Fixed`). Updates the "Bumping the version for a release" steps to rename `## Unreleased` to `## <version> (<date>)` and add a fresh empty `## Unreleased` section, plus notes that the `publish.yml` `github-release` awk extractor (`## <version>( |\()`) walks past `## Unreleased` harmlessly during tag-push releases.

### Changed

- **`pyproject.toml` is now the single source of truth for the project version** (#15) — closes #11. Adds `scripts/sync-server-json.py` (stdlib only, uses `tomllib`) which propagates `[project].version` from `pyproject.toml` into `server.json`'s two version fields (top-level and `packages[0].version`). New `version-sync` CI job runs the script with `--check` and fails any PR where `server.json` has drifted from `pyproject.toml`. CI's `lint` and `typecheck` jobs were extended to cover `scripts/` (a pre-existing gap, dormant until this PR introduced the first `.py` file in that directory). Release flow documented in `CLAUDE.md`: bump `pyproject.toml`, run the sync script, run `uv lock`, update `CHANGELOG.md`, commit. Never edit `server.json`'s version fields by hand.

### Fixed

- **`publish.yml` `github-release` job is now idempotent** (#13) — closes #12. The release-creation step previously failed with HTTP 422 if a Release for the tag already existed (e.g., hand-crafted ahead of the workflow run). It now reads notes from `CHANGELOG.md` via `awk` extraction (skipping the `## <version>` heading, capturing up to the next `## `) and uses `gh release view` → `gh release edit` if a Release exists, `gh release create` otherwise. Falls back to `--generate-notes` with a `::warning::` annotation if `CHANGELOG.md` has no matching entry. Hardened against shell injection by passing values via `env:` instead of `${{ }}` interpolation.

## 0.5.0 (2026-04-10)

### Changed

- **Error responses are now structured JSON envelopes with `isError=true`** (#9)
  - Tool errors previously returned human-readable strings like `[!] List files failed: ...`. They now raise `ToolError` with a JSON envelope:
    ```json
    {
      "status": "error",
      "error": {
        "code": "not_found",
        "message": "List files failed (DSM error 408): No such file or directory",
        "retryable": false,
        "suggestion": "Use list_files or search_files to find the correct path.",
        "help_url": "https://github.com/cmeans/mcp-synology/blob/main/docs/error-codes.md#not_found"
      }
    }
    ```
  - The MCP SDK wraps this in a `CallToolResult` with `isError=true`, which is the correct protocol signal for tool failures. Clients that only display text content see the JSON directly; clients that key off `isError` now get proper failure signaling.
  - All 13 possible `code` values are documented in [`docs/error-codes.md`](docs/error-codes.md), with per-code sections covering symptoms, causes, retryability, and concrete fixes.
  - This is a client-visible behavior change. Any client that was pattern-matching the old `[!] ... failed:` text format will need to update — parse the JSON envelope instead, or key off `isError` at the MCP protocol level.

### Added

- **`ErrorCode(StrEnum)` in `core/errors.py`** — single source of truth for every code the server can emit. `error_response(code: ErrorCode)` is typed so call-site typos become mypy errors rather than silent envelopes with missing `help_url`.
- **`docs/error-codes.md`** — 12-section reference covering every surfaceable `ErrorCode` member. Each section has root causes, fix steps with specific DSM control-panel paths, and explicit retryability statements. `session_expired` is intentionally omitted (auto-retried by the core client; never surfaced to users).
- **Multi-invariant drift test** (`tests/core/test_help_urls.py`) — enforces that `ErrorCode` ↔ `HELP_URLS` registry ↔ `docs/error-codes.md` anchors stay in sync in all directions. Adding a new code without its doc section, or renaming a section without updating the registry, fails CI.
- **`errno.ENOSPC` detection** in `download_file` OSError fallback — replaces locale-dependent substring matching on error text, so local disk-full is correctly reported as `disk_full`/`retryable=True` regardless of OS language or DSM version.
- **Unit test coverage** for `modules/system/info.py` and `modules/system/utilization.py` — both modules previously had no unit tests (13% coverage), now at 99–100%.

### Fixed

- **`unavailable` `retryable` semantic is now consistent across modules** — `system/utilization.py` previously reported `retryable=False` while `system/info.py` reported `retryable=True` for the same condition ("API responded but returned no data"). Both now use `retryable=True` with an inline comment explaining the transient-condition rationale.
- **`download_file` disk-full is now reported with the same code in both detection paths** — the pre-flight branch (via `shutil.disk_usage`) and the OSError fallback previously disagreed: pre-flight emitted `disk_full`/retryable=True, fallback emitted `filesystem_error`/retryable=False despite a "Free space on the local disk" suggestion. Both now emit `disk_full`/retryable=True when disk-full is the actual cause.
- **`error_response()` is safe against non-JSON-serializable `value` arguments** — `json.dumps(..., default=str)` prevents a future caller passing `bytes` or a custom object from crashing the error handler mid-envelope.

## 0.4.1 (2026-04-07)

### Fixed

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
