# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project Overview

mcp-synology is an MCP server for Synology NAS devices. It exposes Synology DSM API functionality as MCP tools that Claude can use. Modular, secure (2FA-ready), permission-tiered. Python 3.11+, async throughout, Apache 2.0 licensed.

**Current status:** v0.9.x — File Station (15 tools) + System monitoring (2 tools) + Download Station (12 tools, opt-in), CLI, integration and vdsm tests.

## Architecture

Layered design: core → modules → server/CLI.

- **Core** (`src/mcp_synology/core/`): DSM API client (async httpx), auth manager (session lifecycle, 2FA, keyring), YAML+Pydantic config loader, shared response formatters, typed exception hierarchy
- **Modules** (`src/mcp_synology/modules/`): Feature-specific tool handlers. Each module declares `MODULE_INFO` with API requirements and tool metadata. File Station (15 tools: 7 READ + 8 WRITE), System (2 tools: get_system_info, get_resource_usage), Download Station (12 tools: 5 READ + 7 WRITE; opt-in)
- **Server** (`src/mcp_synology/server.py`): MCP server (`MCPServer` subclass) initialization, module loading, startup
- **CLI** (`src/mcp_synology/cli/`): click-based package with `serve`, `setup`, `check` subcommands

Modules are domain-split: `listing.py`, `search.py`, `metadata.py`, `operations.py`, `helpers.py` — grouped by what they do, not permission tier.

## Design Specs

**Read the relevant spec before implementing.** These are the source of truth for design decisions. If the code and a spec disagree, flag it — don't silently deviate. `tests/test_module_specs.py` fails when a module spec's tools, permission tiers, parameter names or LLM-facing descriptions don't match the code: update the spec in the same PR.

- `docs/specs/architecture.md` — layered architecture, module system, auth strategy chain, session lifecycle, credential storage
- `docs/specs/project-scaffolding-spec.md` — repo structure, pyproject.toml, CI, testing strategy
- `docs/specs/filestation-module-spec.md` — all 15 File Station tools with parameters, response shapes, error codes
- `docs/specs/system-module-spec.md` — the 2 System tools
- `docs/specs/downloadstation-module-spec.md` — all 12 Download Station tools, the verified DSM behavior behind them (v1 vs DS2, non-manager semantics, paging and totals, secrets in URLs)
- `docs/specs/photos-module-spec.md` — the planned Synology Photos module (19 tools, #258), written before the code from live captures on DSM 7.1 (Photos 1.3.4), 7.2.2 (Photos 1.9.1) and a real NAS: personal vs Shared Space, per-call transport choices, edits rewriting originals, writes that report success for unknown IDs, the per-version support matrix
- `docs/specs/config-schema-spec.md` — YAML config structure, validation rules, env var overrides, state file

## Build & Development Commands

```bash
uv sync --extra dev                              # Install all dependencies
uv run ruff check src/ tests/              # Lint
uv run ruff format --check src/ tests/     # Format check
uv run ruff format src/ tests/             # Auto-format
uv run mypy src/                           # Type check (strict mode)
uv run pytest                              # Run unit + module tests
uv run pytest tests/modules/filestation/test_listing.py  # Single test file
uv run pytest -k "test_list_shares"        # Single test by name
uv run pytest -m integration               # Integration tests (requires real NAS)
uv run pytest --cov=mcp_synology           # Tests with coverage
```

## Key Conventions

### Stack & Dependencies
- **MCP SDK:** `mcp.server.mcpserver.MCPServer` from the official `mcp` package (2.x; 1.x called it `mcp.server.fastmcp.FastMCP`) — NOT the standalone `fastmcp` by PrefectHQ. `server.py`'s `_SynologyServer` overrides only the documented `list_tools()` (schema compaction, #163) and `call_tool()` (keeps the message of a non-`ToolError` exception, which 2.x would hide, #114). Don't reach into private SDK attributes
- **HTTP:** `httpx` (async only) for all NAS communication, `respx` for mocking in tests
- **Config:** `pyyaml` — always `yaml.safe_load()`, never `yaml.load()`
- **Credentials:** `keyring` (OS-native backends)
- **Validation:** `pydantic` for config and module settings schemas
- **CLI:** `click` for subcommands (serve, setup, check)

### Type Safety
- Type hints on all functions, parameters, and return values — `mypy --strict` must pass
- Use `dataclass` for internal data structures, `pydantic.BaseModel` for validated external input (config, API responses)
- Ruff: line length 100, rules: E/F/W/I/N/UP/B/SIM/TCH

### Async
- All DSM API calls and tool handlers are async
- Use `asyncio.Lock` for session re-auth coordination (not threading locks)

### Formatting
- All tool output goes through shared formatters in `core/formatting.py` (`format_table`, `format_key_value`, `format_status`, `format_tree`, `format_error`) — never format strings inline in tool handlers
- Output is plain text optimized for LLM consumption, not JSON

### Logging
- Every module uses `logging.getLogger(__name__)` — log output includes the full module path for traceability
- **DEBUG**: detailed operational trace — every DSM request/response (passwords masked), credential resolution steps, config discovery, version negotiation, API cache contents, session lifecycle, module registration
- **INFO**: significant lifecycle events only — successful auth, re-auth, security config notes
- **WARNING/ERROR**: configuration issues, failures
- **Enabling debug logging** depends on the subcommand:
  - For `serve` (launched by Claude Desktop, no interactive flag available): set `SYNOLOGY_LOG_LEVEL=debug` env var OR `logging.level: debug` in the instance config
  - For `setup` / `check` (interactive CLI): pass `-v` / `--verbose`, or use either of the above
- Logging is initialized *before* config loading so config discovery is visible at debug level

### Error Handling
- DSM API errors map to typed exceptions in `core/errors.py`
- Common error codes (100-series) handled in the core client; module-specific codes (400-series for File Station) handled in modules
- Always include actionable suggestions in error messages
- Session errors (106/107/119) trigger transparent re-auth with exactly one retry; error 105 (permission denied) is NOT a session issue — never re-auth on 105
- **Background task cleanup:** All async background tasks (Search, DirSize, CopyMove, Delete) must use `try/finally` to ensure stop/clean is called. Orphaned tasks consume CPU indefinitely on the NAS. Log warnings on cleanup failure — never silently suppress. One exception (#162): a Delete task that reports `finished` is already released by DSM — `stop` then always answers 401 on DSM 7.1 and 7.2.2 — so it's stopped only when it didn't finish (a DSM error while polling, cancellation). A second exception (#245): a Delete or CopyMove task (delete, copy, move, restore) still running when the call's wait runs out (`delete_timeout` / `copy_move_timeout`, default 30 s, capped at 600 s) is intentionally **left running** on the NAS and handed off with a handle (`delete:<taskid>`, …) that `file_task` follows up on or stops. So is one whose status request gets no answer or an unreadable one during the wait (a transport-level error: any httpx timeout, refused or dropped connection; or any other unexpected exception, e.g. a 200 text/html page from a captive portal or proxy login; maintainer decision 2026-10-17), since the task is most likely still running and losing its handle would leave it unreported. The reply names only the exception type, never its text. Only cancellation and a DSM error code (a `SynologyError`, or an error the task itself reports, unless it already finished) still stop it. Search and DirSize have no exception: always stopped and cleaned. CopyMove `stop` answers success for any task ID (finished, unknown or another user's), so `file_task` reads the task's status before stopping. Stop requests get a 90 s read timeout (`tasks.STOP_TIMEOUT`; connect, write and pool keep the client default): DSM 7.1 sometimes answers a Delete `stop` only after ~60 s, though the stop takes effect at once

### Auth
- Strategy chain auto-detects 2FA vs non-2FA on login attempt
- Credential lookup: env vars → config file → keyring, each value resolved separately (explicit sources override the keyring default). Only a password in the config file gets the plaintext warning; a config username with the password in the keyring is the recommended setup (#167)
- DSM session name format: `MCPSynology_{instance_id}_{unique_id}`
- Lazy keepalive (re-auth on next request, no proactive pings)

### DSM API Client
- Thin wrapper — knows DSM request/response conventions, nothing about specific APIs
- **Transport is chosen per call** (maintainer, 2026-10-17); each module spec records its choices. **File Station and Download Station use GET**, except the scoped exceptions below. The old note that "POST causes silent failures on DSM 7.1" is retired (#263): the whole File Station vdsm suite passes with every request sent as a form POST on DSM 7.1 and 7.2.2. What does fail is Download Station's v1 CGIs (`DownloadStation/*.cgi`), which **ignore `_sid` in the query string of a POST** and answer 105 (which never re-auths, so it reads as a permissions problem); with `_sid` in the body or as the `id` cookie the same POST works, and the client sends the cookie (#176, below). `tests/vdsm/test_vdsm_transport.py` pins both. Moving calls is #264. DSM reports `requestFormat=JSON` on all FileStation APIs (even v2), which means DSM **parses each parameter value as JSON first** (#121)
  - **JSON-quote free-text values** with `filestation.helpers.dsm_str()`: task IDs (Search, DirSize, via `task_request()`), `name` (CreateFolder, Rename), `pattern` (List, Search) and `extension` (Search). Unquoted, a value that is valid JSON is read as that type: a Search/DirSize task ID like `17918443788CE649B9` matches no task (Search `list` answers a bare `{"finished": true}`, DirSize `status` 599, `stop`/`clean` clean nothing), a folder named `2024` or `true` gets error 400, and a `list` pattern of `2024` crashes the request (HTTP 502). Synology's API guide quotes every such value
  - Paths don't need it (they start with `/`, so never valid JSON); booleans and numbers are meant as JSON scalars
  - **One scoped exception (#123):** Download Station URI `Task.create` (DS2 `SYNO.DownloadStation2.Task` v2, with v1 `SYNO.DownloadStation.Task` as fallback) is sent as a form POST via `DsmClient.request_form_post()`. A GET URL past ~8 KB (a magnet with many trackers, or a URI list) gets HTTP 414 from DSM, and GET would put source-URL credentials in access logs. Its #123 merge gate is the full Download Station vdsm suite passing on DSM 7.1 (where the POST failures were seen) and 7.2.2. Do not route anything else through `request_form_post()`. Multipart uploads (`upload()`, `create_download_task_with_file()`) were already POST
  - **Second scoped exception (#235):** `SYNO.API.Auth` `login` is sent as a form POST via `request(..., post_form=True)` (all paths: `AuthManager.login()`, and `setup`'s login and OTP login), so `passwd`, `otp_code` and `device_id` never appear in a URL, where DSM's or a reverse proxy's access logs would record them. The session cookie (below), `request()`'s error handling and re-auth retry are unchanged. Verified on DSM 7.1 and 7.2.2 (admin and non-admin login, wrong password, device-token parameters); its merge gate added a maintainer 2FA check on a real NAS. `logout` and every other File Station, Download Station and auth call stay GET; outside the Photos module (below), don't pass `post_form=True` anywhere else
  - **Photos module (#258): transport chosen per call.** Maintainer decision 2026-10-17: GET is no longer a blanket rule; a new module picks GET or POST per call on its merits and its spec records the choice and why. In Photos, every JSON call is a form POST (free text and share passwords stay out of access logs; no URL-length limit), with `api`/`method`/`version` in the body (an empty body with them only in the query string fails with 101) and every value JSON-encoded; uploads are multipart through `DsmClient.upload_multipart()`; thumbnails and originals stream over GET through `DsmClient.download_binary()` (File Station's `upload()` and `download()` are wrappers around these two). Photos error codes live in `core/photos_errors.py` (dispatched for any `SYNO.Foto*` API); DSM's 103 and features an older Photos lacks are `not_supported`. See `docs/specs/photos-module-spec.md`. File Station and Download Station keep their current transports
- **Download Station v1 vs DS2:** the v1 `SYNO.DownloadStation.*` APIs return task `status` as a string; the weekly schedule plan lives only in DS2 `SYNO.DownloadStation2.Settings.Scheduler` (JSON-format: string values are JSON-encoded; no server-side validation); global config writes use `Info.setserverconfig` (there is no `setconfig`). Build test fixtures from a real capture (`tests/modules/downloadstation/fixtures/`) and route mocks by `api`/`method`/`version` (`tests/modules/downloadstation/dsm.py`) — the invented shapes behind #123 passed a catch-all mock
- Calls `SYNO.API.Info` with `query=ALL` at startup; caches API name → path/version map
- **Version pinning:** CopyMove, Delete, Search, Upload, and `List getinfo` are pinned to v2 to avoid v3 JSON request format issues. CopyMove/Delete/Search use `negotiate_version(..., max_version=2)` from their handlers; Upload's pin lives in `core/client.py::upload_file` (`min(info.max_version, 2)` default) because Upload is a special-case multipart POST not routed through `request()`; `List getinfo` is pinned in `modules/filestation/metadata.py` (added by #77 to dodge the v3 multipath quirk that surfaced as #68). Download Station `Task.edit` is pinned to exactly v2 (`negotiate_version(..., min_version=2, max_version=2)` — edit does not exist at v1) and DS2 `Task.create` to v2. Removing any of these pins has caused silent failures on DSM 7.x. The pins predate #121, which found that DSM parses JSON-format parameter values as JSON (see the GET bullet above), so they may share its cause; #157 tracks retesting them
- **The session goes as DSM's `id` cookie, never as `_sid` (#176).** DSM parses `_sid` as JSON too (#121): some session IDs (on vdsm, about 1 in 75 on DSM 7.2.2 and 1 in 170 on 7.1) begin with a shape that parser misreads (`-8bV…`, `6E5h…`, `02-P…`), and such a session matches nothing as `_sid`, so every call, the re-auth retry included, got 119 until a later login drew another shape. That was the "flaky 119 right after login" on vdsm; real NAS users would be affected the same way (inferred, not reported). JSON-quoting `_sid` fixes the JSON-format APIs but breaks Download Station's v1 CGIs (105), and `logout` with a quoted `_sid` answers success but leaves the session open. The cookie works for every API (#263's whole-suite runs), and `logout` with it ends the session. `DsmClient._session_headers()` builds the `Cookie` header for every call (GET, form POST, multipart, streamed download), keeping any other cookie in the jar beside it; `tests/vdsm/test_vdsm_session.py` pins it
- Session cookie and comma/backslash escaping in multi-path params are transparent to modules

### Config
- Config is read-only from the server's perspective — never write to it
- Runtime state goes in `~/.local/state/mcp-synology/{instance_id}/state.yaml`
- Strict validation at top level (unknown keys = error), lenient within module settings (unknown keys = warning)
- Two-phase loading: parse YAML → merge env var overrides → apply defaults → validate with Pydantic

### Path Handling (File Station)
- Accept paths with or without leading `/`; normalize internally
- Always return fully-qualified paths: `/shared_folder/...`
- Validate first path component against cached share list

### Search (File Station)
- Always pass `filetype=all` — DSM defaults to `"file"` if omitted, excluding directories from results
- Auto-wrap bare keywords with wildcards: `"Bambu"` → `"*Bambu*"` so substring matching works
- Pure extension patterns like `"*.mkv"` use DSM's native `extension` filter
- A Search `list` reply without `total`/`files` (bare `{"finished": true}`) is DSM's answer for an unknown task, so it's reported as a lost task, never as 0 results. The "search is unreliable under load / needs recovery delays" lore was this unquoted-task-ID bug (#121): with the ID quoted, back-to-back searches all succeed

## Testing

- **Mock boundary is HTTP:** `respx` intercepts httpx calls, returns canned DSM responses — not function-level mocks
- **Test files mirror source files:** `listing.py` → `test_listing.py`
- **Integration tests** marked `@pytest.mark.integration`, excluded from CI by default
  - Require `tests/integration_config.yaml` (copy from `integration_config.yaml.example`)
  - Configure NAS connection + `test_paths` (existing_share, search_folder, search_keyword, writable_folder)
  - Run: `uv run pytest -m integration -v --log-cli-level=INFO`
  - Search tests don't need recovery delays: the old "flaky under load" behavior was the unquoted task ID (#121), and `test_search_back_to_back_all_find` runs 10 searches with no delay
- **vdsm Download Station coverage**: the bake installs Download Station with `synopkg` over SSH (`tests/vdsm/setup_dsm.py:_install_download_station_via_ssh`), creates the test user with `synouser`, and grants that user the DS app privilege via `SYNO.Core.AppPriv.Rule.set`. It fails unless the test user's own session can `Task.list`. Since bake v5 it also installs Synology Photos with its dependencies (`PHOTOS_PACKAGES`, per DSM version), turns on the user home service (Photos' personal space, with its recycle bin), grants `mcptest` the Photos privilege and makes it a Shared Space manager, and fails unless `mcptest` reaches both spaces' root folders and `mcpfresh` gets 160 (no Photos privilege; #258). Photos registers its APIs a while after boot, so a Photos vdsm test depends on the `photos_apis` fixture (waits once per session via `photos_ready`, then refreshes the clients' API caches). **Bump `tests/vdsm/config.py:BAKE_VERSION` on every bake change.** The bake records it in `meta.json`, and the vdsm conftest fails fast on an older local image (`.vdsm/golden/dsm-<version>.tar.gz`): delete the image and its `.meta.json`, then rebuild with `python scripts/vdsm_setup.py --version <version>`. CI's cache key hashes the bake files, so CI rebuilds on its own. CI runs the full vdsm suite on both DSM versions as a matrix (checks `vdsm integration tests (7.2.2)` and `vdsm integration tests (7.1)`, #212); locally, run `tests/vdsm/test_vdsm_downloadstation.py` on both `--dsm-version 7.2.2` and `--dsm-version 7.1`.
- **vdsm transport runs (#263)**: `--dsm-transport post|post_sid_body|cookie_get|cookie_post` resends every request of the vdsm test clients another way (`tests/vdsm/transport.py`, an httpx transport swapped in under the real client), so the whole suite's state checks test that transport; the default `get` changes nothing. Each mode moves the session wherever the client put it: `post` and `post_sid_body` send it as `_sid`, so they meet #176's misread session IDs (a 119 and a re-auth, which fails tests that assert none); `cookie_get` is today's client. Before moving any call off GET (#264), run the vdsm suite on both versions with the matching mode.
- **vdsm personas (#124)**: on vdsm, `nas_client` is the **non-admin** test user (`mcptest`) and `admin_client` is admin. A test that needs admin uses `admin_client` explicitly. A persona-dependent test reads `nas_persona` (`is_admin` is known on vdsm; on a real NAS it comes from `is_admin` in `integration_config.yaml`, and the test may skip only when it's unknown), and asserts "105 never re-auths" with `reauth_spy`. DS behavior to remember: DS never starts a non-manager's tasks until that user has set their own `default_destination` (DSM applies it per user). The bake sets it for `mcptest`; `mcpfresh` is the deliberately unconfigured DS user.

## Common Tasks

### Adding a new tool to an existing module
1. Add handler in the appropriate domain file (`listing.py`, `search.py`, `metadata.py`, or `operations.py`)
2. Add `ToolInfo` entry to `MODULE_INFO.tools` in the module's `__init__.py`
3. Register in `register()`, gated by permission tier
4. Use shared formatters for output
5. Add tests with mocked DSM responses in the matching test file

### Adding a new module
1. Create `modules/{name}/` package with `__init__.py`, domain files
2. Define `MODULE_INFO` with `ApiRequirement` list, `ToolInfo` list, and optional Pydantic `settings_schema`
3. Implement `register(ctx: RegisterContext)` — see `modules/system/` for a minimal example
4. Import and add to `_MODULE_REGISTRY` in `server.py`
5. Add module name to example configs and integration test config

### Adding a new DSM error code
1. Common codes (100-series): add to `core/errors.py` error code map
2. Module-specific codes: add to the module's error handling
3. Always include: code, human-readable message, actionable suggestion

### Adding a CHANGELOG entry on every PR
Every PR — features, fixes, infra, tests, docs — adds an entry to `CHANGELOG.md` under the `## Unreleased` section at the top of the file. Do not defer CHANGELOG updates until release prep.

`CHANGELOG.md` follows **strict [Keep a Changelog](https://keepachangelog.com/) categories** — only three section headings are valid:
- `### Added` — anything new: features, capabilities, tests, docs, dev tooling
- `### Changed` — behavior or API changes that aren't bug fixes
- `### Fixed` — bug fixes

Do not invent new categories like `### Internal`, `### Documentation`, or `### Code Quality`. The 0.5.0 entry deliberately transitioned away from the older Conventional Commits-style taxonomy; new PRs must respect that. Test coverage and doc additions both go under `### Added` (precedent: 0.5.0 puts unit test coverage and `docs/error-codes.md` under Added).

Reference the PR number and any closed issue: `- ... (#16) — closes #14`. If no `## Unreleased` section exists (because the previous release just shipped), add one above the latest version section.

This convention was retired-and-restored on 2026-04-11 after PRs #13, #15, and #16 all merged or were under review without entries because the prior practice was "CHANGELOG only at release time." Reconstructing the changelog from `git log` at release time loses per-PR rationale.

### Bumping the version for a release
1. Update `[project].version` in `pyproject.toml` (single source of truth)
2. Run `python scripts/sync-server-json.py` to propagate the version into `server.json` (top-level + `packages[0].version`)
3. Run `uv lock` to refresh `uv.lock`
4. Rename `## Unreleased` in `CHANGELOG.md` to `## <version> (<date>)`, then add a fresh empty `## Unreleased` section above it for the next cycle
5. **Minor or major bump only:** update the supported-versions line and table in `SECURITY.md` (e.g. "the 0.6.x line", `0.6.x ✅` / `< 0.6 ❌`) and the **Current status** line at the top of this file
6. Commit these files together

CI runs `python scripts/sync-server-json.py --check` (no project install needed — stdlib only) and fails any PR where `server.json` has drifted from `pyproject.toml`. Never edit `server.json`'s version fields by hand.

The `publish.yml` `github-release` job's awk extractor matches `## <version>( |\()` — i.e., it requires a space or `(` after the version. An `## Unreleased` section without trailing `(` is therefore harmless during a tag-push release: the awk pattern walks past it and lands on the version section below.
