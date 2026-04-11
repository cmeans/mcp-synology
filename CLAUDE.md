# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project Overview

mcp-synology is an MCP server for Synology NAS devices. It exposes Synology DSM API functionality as MCP tools that Claude can use. Modular, secure (2FA-ready), permission-tiered. Python 3.11+, async throughout, Apache 2.0 licensed.

**Current status:** v0.4.x — File Station (14 tools) + System monitoring (2 tools), CLI, integration tests.

## Architecture

Layered design: core → modules → server/CLI.

- **Core** (`src/mcp_synology/core/`): DSM API client (async httpx), auth manager (session lifecycle, 2FA, keyring), YAML+Pydantic config loader, shared response formatters, typed exception hierarchy
- **Modules** (`src/mcp_synology/modules/`): Feature-specific tool handlers. Each module declares `MODULE_INFO` with API requirements and tool metadata. File Station (14 tools: 7 READ + 7 WRITE), System (2 tools: get_system_info, get_resource_usage)
- **Server** (`src/mcp_synology/server.py`): FastMCP initialization, module loading, startup
- **CLI** (`src/mcp_synology/cli/`): click-based package with `serve`, `setup`, `check` subcommands

Modules are domain-split: `listing.py`, `search.py`, `metadata.py`, `operations.py`, `helpers.py` — grouped by what they do, not permission tier.

## Design Specs

**Read the relevant spec before implementing.** These are the source of truth for design decisions. If the code and a spec disagree, flag it — don't silently deviate.

- `docs/specs/architecture.md` — layered architecture, module system, auth strategy chain, session lifecycle, credential storage
- `docs/specs/project-scaffolding-spec.md` — repo structure, pyproject.toml, CI, testing strategy
- `docs/specs/filestation-module-spec.md` — all 14 File Station tools with parameters, response shapes, error codes
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
- **MCP SDK:** `mcp.server.fastmcp.FastMCP` from the official `mcp` package — NOT the standalone `fastmcp` by PrefectHQ
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
- Three ways to enable debug: `mcp-synology check -v` (flag), `SYNOLOGY_LOG_LEVEL=debug` (env var), `logging.level: debug` (config)
- The `setup` and `check` commands accept `-v`/`--verbose`; `serve` uses config/env var (no interactive flag since it's launched by Claude Desktop)
- Logging is initialized *before* config loading so config discovery is visible at debug level

### Error Handling
- DSM API errors map to typed exceptions in `core/errors.py`
- Common error codes (100-series) handled in the core client; module-specific codes (400-series for File Station) handled in modules
- Always include actionable suggestions in error messages
- Session errors (106/107/119) trigger transparent re-auth with exactly one retry; error 105 (permission denied) is NOT a session issue — never re-auth on 105
- **Background task cleanup:** All async background tasks (Search, DirSize, CopyMove, Delete) must use `try/finally` to ensure stop/clean is called. Orphaned tasks consume CPU indefinitely on the NAS. Log warnings on cleanup failure — never silently suppress

### Auth
- Strategy chain auto-detects 2FA vs non-2FA on login attempt
- Credential lookup: keyring → env vars → config file (last resort, plaintext warning)
- DSM session name format: `MCPSynology_{instance_id}_{unique_id}`
- Lazy keepalive (re-auth on next request, no proactive pings)

### DSM API Client
- Thin wrapper — knows DSM request/response conventions, nothing about specific APIs
- **Always use GET** — never POST. DSM reports `requestFormat=JSON` on all FileStation APIs (even v2), but this is metadata not a mandate. POST causes silent failures on DSM 7.1
- Calls `SYNO.API.Info` with `query=ALL` at startup; caches API name → path/version map
- **Version pinning:** CopyMove, Delete, and Search are pinned to v2 (`negotiate_version(..., max_version=2)`) to avoid v3 JSON request format issues
- Session ID injection and comma/backslash escaping in multi-path params are transparent to modules

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
- DSM search service on non-indexed shares (`has_not_index_share: True`) can be unreliable under load — orphaned tasks or rapid-fire requests can exhaust it

## Testing

- **Mock boundary is HTTP:** `respx` intercepts httpx calls, returns canned DSM responses — not function-level mocks
- **Test files mirror source files:** `listing.py` → `test_listing.py`
- **Integration tests** marked `@pytest.mark.integration`, excluded from CI by default
  - Require `tests/integration_config.yaml` (copy from `integration_config.yaml.example`)
  - Configure NAS connection + `test_paths` (existing_share, search_folder, search_keyword, writable_folder)
  - Run: `uv run pytest -m integration -v --log-cli-level=INFO`
  - Search tests can be flaky if the NAS search service is overloaded — allow recovery time between runs

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

Use the existing section headings to categorize:
- `### Added` — new features or capabilities
- `### Changed` — behavior changes that aren't bug fixes
- `### Fixed` — bug fixes
- `### Internal` — test/CI/tooling/refactor changes with no user-visible behavior
- `### Documentation` — docs-only changes

Reference the PR number and any closed issue: `- ... (#16) — closes #14`. If no `## Unreleased` section exists (because the previous release just shipped), add one above the latest version section.

This convention was retired-and-restored on 2026-04-11 after PRs #13, #15, and #16 all merged or were under review without entries because the prior practice was "CHANGELOG only at release time." Reconstructing the changelog from `git log` at release time loses per-PR rationale.

### Bumping the version for a release
1. Update `[project].version` in `pyproject.toml` (single source of truth)
2. Run `python scripts/sync-server-json.py` to propagate the version into `server.json` (top-level + `packages[0].version`)
3. Run `uv lock` to refresh `uv.lock`
4. Rename `## Unreleased` in `CHANGELOG.md` to `## <version> (<date>)`, then add a fresh empty `## Unreleased` section above it for the next cycle
5. Commit all four files together

CI runs `python scripts/sync-server-json.py --check` (no project install needed — stdlib only) and fails any PR where `server.json` has drifted from `pyproject.toml`. Never edit `server.json`'s version fields by hand.

The `publish.yml` `github-release` job's awk extractor matches `## <version>( |\()` — i.e., it requires a space or `(` after the version. An `## Unreleased` section without trailing `(` is therefore harmless during a tag-push release: the awk pattern walks past it and lands on the version section below.
