# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project Overview

synology-mcp is an MCP server for Synology NAS devices. It exposes Synology DSM API functionality as MCP tools that Claude can use. Modular, secure (2FA-ready), permission-tiered. Python 3.11+, async throughout, MIT licensed.

**Current status:** Pre-implementation. Design specs are complete in `docs/specs/`. No source code yet.

## Architecture

Layered design: core → modules → server/CLI.

- **Core** (`src/synology_mcp/core/`): DSM API client (async httpx), auth manager (session lifecycle, 2FA, keyring), YAML+Pydantic config loader, shared response formatters, typed exception hierarchy
- **Modules** (`src/synology_mcp/modules/`): Feature-specific tool handlers. Each module declares `MODULE_INFO` with API requirements and tool metadata. File Station is the first module (12 tools: 6 READ + 6 WRITE)
- **Server** (`src/synology_mcp/server.py`): FastMCP initialization, module loading, startup
- **CLI** (`src/synology_mcp/cli.py`): click-based with `serve`, `setup`, `check` subcommands

Modules are domain-split: `listing.py`, `search.py`, `metadata.py`, `operations.py`, `helpers.py` — grouped by what they do, not permission tier.

## Design Specs

**Read the relevant spec before implementing.** These are the source of truth for design decisions. If the code and a spec disagree, flag it — don't silently deviate.

- `docs/specs/architecture.md` — layered architecture, module system, auth strategy chain, session lifecycle, credential storage
- `docs/specs/project-scaffolding-spec.md` — repo structure, pyproject.toml, CI, testing strategy
- `docs/specs/filestation-module-spec.md` — all 12 File Station tools with parameters, response shapes, error codes
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
uv run pytest --cov=synology_mcp           # Tests with coverage
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
- Three ways to enable debug: `synology-mcp check -v` (flag), `SYNOLOGY_LOG_LEVEL=debug` (env var), `logging.level: debug` (config)
- The `setup` and `check` commands accept `-v`/`--verbose`; `serve` uses config/env var (no interactive flag since it's launched by Claude Desktop)
- Logging is initialized *before* config loading so config discovery is visible at debug level

### Error Handling
- DSM API errors map to typed exceptions in `core/errors.py`
- Common error codes (100-series) handled in the core client; module-specific codes (400-series for File Station) handled in modules
- Always include actionable suggestions in error messages
- Session errors (106/107/119) trigger transparent re-auth with exactly one retry; error 105 (permission denied) is NOT a session issue — never re-auth on 105

### Auth
- Strategy chain auto-detects 2FA vs non-2FA on login attempt
- Credential lookup: keyring → env vars → config file (last resort, plaintext warning)
- DSM session name format: `SynologyMCP_{instance_id}_{unique_id}`
- Lazy keepalive (re-auth on next request, no proactive pings)

### DSM API Client
- Thin wrapper — knows DSM request/response conventions, nothing about specific APIs
- Calls `SYNO.API.Info` with `query=ALL` at startup; caches API name → path/version map
- Auto-negotiates API versions (highest supported by NAS)
- Session ID injection and comma/backslash escaping in multi-path params are transparent to modules

### Config
- Config is read-only from the server's perspective — never write to it
- Runtime state goes in `~/.local/state/synology-mcp/{instance_id}/state.yaml`
- Strict validation at top level (unknown keys = error), lenient within module settings (unknown keys = warning)
- Two-phase loading: parse YAML → merge env var overrides → apply defaults → validate with Pydantic

### Path Handling (File Station)
- Accept paths with or without leading `/`; normalize internally
- Always return fully-qualified paths: `/shared_folder/...`
- Validate first path component against cached share list

## Testing

- **Mock boundary is HTTP:** `respx` intercepts httpx calls, returns canned DSM responses — not function-level mocks
- **Test files mirror source files:** `listing.py` → `test_listing.py`
- **Integration tests** marked `@pytest.mark.integration`, excluded from CI by default

## Common Tasks

### Adding a new tool to an existing module
1. Add handler in the appropriate domain file (`listing.py`, `search.py`, `metadata.py`, or `operations.py`)
2. Add `ToolInfo` entry to `MODULE_INFO.tools` in the module's `__init__.py`
3. Register in `register()`, gated by permission tier
4. Use shared formatters for output
5. Add tests with mocked DSM responses in the matching test file

### Adding a new module
1. Create `modules/{name}/` package with `__init__.py`, domain files, helpers
2. Define `MODULE_INFO` with `ApiRequirement` list, `ToolInfo` list, and optional Pydantic `settings_schema`
3. Implement `register(server, client, allowed_tools)`
4. Add module name to config schema and example configs

### Adding a new DSM error code
1. Common codes (100-series): add to `core/errors.py` error code map
2. Module-specific codes: add to the module's error handling
3. Always include: code, human-readable message, actionable suggestion
