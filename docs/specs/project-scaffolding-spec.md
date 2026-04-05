# Project Scaffolding — Repo Structure, Build, CI, Testing

> **Version:** 0.2 | **Updated:** 2026-03-16T23:30Z — Domain-based module split, click for CLI, spec docs in repo, example configs, py.typed marker.
> **Parent doc:** `mcp-synology-architecture.md` v0.3

## Repository Structure

```
mcp-synology/
├── src/
│   └── mcp_synology/
│       ├── __init__.py              # Package version, top-level exports
│       ├── __main__.py              # `python -m mcp_synology` entry point
│       ├── py.typed                 # PEP 561 marker — ship type information
│       ├── cli.py                   # CLI: serve, setup, check (click-based)
│       ├── server.py                # FastMCP server init, module loading, startup
│       │
│       ├── core/
│       │   ├── __init__.py
│       │   ├── client.py            # DSM API client (async httpx)
│       │   ├── auth.py              # Auth manager (session lifecycle, 2FA)
│       │   ├── config.py            # YAML config loading + Pydantic validation
│       │   ├── formatting.py        # Shared response formatters (table, kv, status, tree, error)
│       │   ├── errors.py            # Typed exception hierarchy, error code mapping
│       │   └── state.py             # State file read/write
│       │
│       └── modules/
│           ├── __init__.py          # ModuleInfo dataclass, registration helpers
│           └── filestation/
│               ├── __init__.py      # MODULE_INFO, register(), FileStationSettings
│               ├── listing.py       # list_shares, list_files, list_recycle_bin
│               ├── search.py        # search_files
│               ├── metadata.py      # get_file_info, get_dir_size
│               ├── operations.py    # create_folder, rename, copy, move, delete, restore
│               └── helpers.py       # Path normalization, size parsing, async polling
│
├── tests/
│   ├── conftest.py                  # Shared fixtures (mock client, sample configs)
│   ├── core/
│   │   ├── test_client.py           # DSM client: URL building, envelope parsing, error mapping
│   │   ├── test_auth.py             # Auth flows: simple login, 2FA, re-auth
│   │   ├── test_config.py           # Config loading, validation, env var merging, defaults
│   │   └── test_formatting.py       # Formatter output verification
│   └── modules/
│       └── filestation/
│           ├── test_listing.py      # list_shares, list_files, list_recycle_bin
│           ├── test_search.py       # search_files (async polling, exclude_pattern)
│           ├── test_metadata.py     # get_file_info, get_dir_size
│           ├── test_operations.py   # create, rename, copy, move, delete, restore
│           └── test_helpers.py      # Path normalization, size parsing
│
├── docs/
│   └── specs/
│       ├── architecture.md          # Layered architecture, auth, session lifecycle
│       ├── filestation-module.md     # File Station tool specs (12 tools)
│       └── config-schema.md         # YAML config structure, validation rules
│
├── examples/
│   ├── config-minimal.yaml          # Quick start — just host + one module
│   ├── config-power-user.yaml       # HTTPS, custom settings, logging
│   └── config-docker.yaml           # Env-var-driven, minimal file
│
├── pyproject.toml                   # Build config, dependencies, entry points
├── README.md                        # User-facing docs: install, configure, usage
├── CLAUDE.md                        # Claude Code instructions for this repo
├── LICENSE                          # Apache 2.0
├── .gitignore
└── .github/
    └── workflows/
        └── ci.yml                   # Lint, type check, test on push/PR
```

### Design Rationale

**`src/` layout:** Standard Python packaging convention. Prevents accidental imports from the working directory during development. Matches clipboard-mcp and the official MCP Python server template.

**Domain-based module split:** Tool handlers are grouped by *what they do*, not their permission tier. `listing.py` has the browsing tools, `search.py` has search, `metadata.py` has info/size queries, `operations.py` has all mutations. When you're working on search behavior, you open `search.py` — no mental mapping from "what tier is this?" to "which file?"

**Test files mirror module files:** `test_listing.py` tests `listing.py`, `test_operations.py` tests `operations.py`. Easy to find the tests for any code you're changing. Also prevents a single 800-line `test_filestation.py`.

**`helpers.py` per module:** Path normalization, human-readable size parsing, async polling wrapper — shared across tools within a module but not core infrastructure. Keeps `core/` clean of module-specific logic.

**`cli.py` separate from `server.py`:** The CLI handles argument parsing (click), config file discovery, and dispatching to subcommands. `server.py` handles MCP server initialization and module loading. Clean separation — `server.py` is testable without CLI concerns.

**`docs/specs/`:** Design specs live in the repo so they're versioned alongside the code. Claude Code references them. Future contributors can understand *why*, not just *what*.

**`examples/`:** Copy-and-edit configs. Users shouldn't have to read through an annotated reference to get started — they grab the example closest to their setup and modify it.

**`py.typed`:** PEP 561 marker. Since we run mypy strict, downstream consumers who depend on this package (unlikely but possible) get type checking too. Signals quality.

---

## pyproject.toml

```toml
[build-system]
requires = ["hatchling"]
build-backend = "hatchling.build"

[project]
name = "mcp-synology"
version = "0.1.0"
description = "MCP server for Synology NAS — manage files, containers, and more via Claude"
readme = "README.md"
license = "Apache-2.0"
requires-python = ">=3.11"
authors = [
    { name = "Chris Means", email = "..." },
]
keywords = ["mcp", "synology", "nas", "file-station", "claude"]
classifiers = [
    "Development Status :: 3 - Alpha",
    "Intended Audience :: Developers",
    "Intended Audience :: System Administrators",
    "License :: OSI Approved :: Apache Software License",
    "Programming Language :: Python :: 3",
    "Programming Language :: Python :: 3.11",
    "Programming Language :: Python :: 3.12",
    "Programming Language :: Python :: 3.13",
    "Topic :: System :: Systems Administration",
]
dependencies = [
    "mcp>=1.0",               # Official MCP Python SDK (includes FastMCP)
    "httpx>=0.27",             # Async HTTP client for DSM API calls
    "pyyaml>=6.0",             # YAML config parsing (safe_load only)
    "keyring>=25.0",           # OS-native credential storage
    "pydantic>=2.0",           # Config and settings validation
    "click>=8.0",              # CLI framework (serve, setup, check)
]

[project.optional-dependencies]
dev = [
    "pytest>=8.0",
    "pytest-asyncio>=0.24",
    "pytest-cov>=5.0",
    "ruff>=0.5",               # Linting + formatting
    "mypy>=1.10",              # Type checking
    "respx>=0.22",             # httpx mock library for testing
]

[project.scripts]
mcp-synology = "mcp_synology.cli:main"

[project.urls]
Homepage = "https://github.com/cmeans/mcp-synology"
Repository = "https://github.com/cmeans/mcp-synology"
Issues = "https://github.com/cmeans/mcp-synology/issues"
Documentation = "https://github.com/cmeans/mcp-synology/tree/main/docs"

[tool.hatch.build.targets.wheel]
packages = ["src/mcp_synology"]

[tool.ruff]
target-version = "py311"
line-length = 100

[tool.ruff.lint]
select = ["E", "F", "W", "I", "N", "UP", "B", "SIM", "TCH"]
# E/F/W: pyflakes + pycodestyle basics
# I: isort (import sorting)
# N: pep8-naming
# UP: pyupgrade (modern Python syntax)
# B: flake8-bugbear (common pitfalls)
# SIM: flake8-simplify
# TCH: type-checking imports

[tool.mypy]
python_version = "3.11"
strict = true
warn_return_any = true
warn_unused_configs = true
disallow_untyped_defs = true

[tool.pytest.ini_options]
testpaths = ["tests"]
asyncio_mode = "auto"
markers = ["integration: requires a real Synology NAS (not run in CI)"]
addopts = "-m 'not integration'"
```

### Dependency Notes

- **`mcp>=1.0`**: The official Python MCP SDK. Provides `mcp.server.fastmcp.FastMCP`, `Context`, type definitions, and stdio transport. We do NOT use the standalone `fastmcp` package by PrefectHQ.
- **`httpx>=0.27`**: Async HTTP client. Used by the DSM API client for all NAS communication. Cleaner API than `aiohttp`, better testing story via `respx`.
- **`keyring>=25.0`**: OS-native credential storage. Auto-detects macOS Keychain, Windows Credential Manager, or Linux Secret Service.
- **`pydantic>=2.0`**: Config validation, module settings schemas, type coercion.
- **`pyyaml>=6.0`**: Config file parsing. Always `yaml.safe_load()` — never `yaml.load()`.
- **`click>=8.0`**: CLI framework. Three subcommands: `serve` (MCP server mode), `setup` (interactive credential bootstrap), `check` (validate credentials). Click handles prompts, password masking, and colored output for the interactive `setup` flow — things that would be tedious with argparse.
- **`respx>=0.22`** (dev): httpx-native mock library. Much cleaner than `unittest.mock.patch` for HTTP testing.

### Python Version

**Minimum: 3.11.** Gives us `ExceptionGroup`, `TaskGroup`, modern union type hints (`X | Y`), and `tomllib` in stdlib. Python 3.10 reaches EOL October 2026 — no reason to support it.

---

## CLI Design (click)

```python
# cli.py sketch

import click

@click.group()
@click.version_option()
def main():
    """mcp-synology — MCP server for Synology NAS."""
    pass

@main.command()
@click.option("--config", type=click.Path(exists=True), help="Path to config file")
def serve(config: str | None):
    """Start the MCP server (launched by Claude Desktop)."""
    # Load config → init server → run stdio transport
    ...

@main.command()
@click.option("--config", type=click.Path(exists=True), help="Path to config file")
def setup(config: str | None):
    """Interactive credential setup and 2FA bootstrap."""
    # Load config for host/port → prompt username/password →
    # attempt login → handle 2FA → store in keyring
    username = click.prompt("DSM username")
    password = click.prompt("DSM password", hide_input=True)
    ...

@main.command()
@click.option("--config", type=click.Path(exists=True), help="Path to config file")
def check(config: str | None):
    """Validate stored credentials can authenticate."""
    # Load config → load credentials → attempt login → report
    ...
```

**Why click over argparse:**
- `click.prompt()` with `hide_input=True` for password entry — the `setup` command needs this
- Colored output (`click.style()`, `click.echo()`) for status messages during setup
- Clean subcommand dispatch without manual `argparse` subparser boilerplate
- Decorator-based API is more readable and easier for Claude Code to generate correctly

---

## CI — GitHub Actions

```yaml
# .github/workflows/ci.yml
name: CI

on:
  push:
    branches: [main]
  pull_request:
    branches: [main]

jobs:
  lint:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v4
      - uses: astral-sh/setup-uv@v4
      - run: uv sync --dev
      - run: uv run ruff check src/ tests/
      - run: uv run ruff format --check src/ tests/

  typecheck:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v4
      - uses: astral-sh/setup-uv@v4
      - run: uv sync --dev
      - run: uv run mypy src/

  test:
    runs-on: ubuntu-latest
    strategy:
      matrix:
        python-version: ["3.11", "3.12", "3.13"]
    steps:
      - uses: actions/checkout@v4
      - uses: astral-sh/setup-uv@v4
        with:
          python-version: ${{ matrix.python-version }}
      - run: uv sync --dev
      - run: uv run pytest --cov=mcp_synology --cov-report=xml
      - uses: codecov/codecov-action@v4
        if: matrix.python-version == '3.12'
        with:
          files: coverage.xml
```

**CI Notes:**
- Three parallel jobs: lint, typecheck, test. Fast feedback — failures are independent.
- Test matrix: Python 3.11, 3.12, 3.13.
- `uv` for CI via `astral-sh/setup-uv` — faster than pip.
- Coverage collected on 3.12, uploaded to Codecov. Not a hard gate initially — just visibility.
- Integration tests excluded by default (`addopts = "-m 'not integration'"` in pyproject.toml).

---

## Testing Strategy

### Test Categories

#### 1. Unit Tests (fast, no I/O, run in CI)

**Core client tests (`test_client.py`):**
- URL building from API name + version + method
- Session ID injection into requests
- Response envelope parsing (`{success, data, error}`)
- Error code → exception mapping (common codes + File Station codes)
- API info cache parsing
- Version negotiation (pick highest compatible version)

**Auth tests (`test_auth.py`):**
- Credential resolution hierarchy (keyring → env → config)
- Strategy chain: no 2FA, 2FA with device token, 2FA without device token (error)
- Session re-auth on error 106/107/119
- Re-auth lock prevents concurrent re-auth
- Error 105 (permission denied) is NOT retried

**Config tests (`test_config.py`):**
- Minimal config loading
- Full config with all optional fields
- Env var override merging (two-phase validation)
- Default derivation (port from https, instance_id from host)
- Strict top-level validation (unknown keys → error)
- Lenient module settings (unknown keys → warning)
- Schema version checking
- Pydantic validation for module settings schemas

**Formatter tests (`test_formatting.py`):**
- `format_table`: column alignment, title, empty state
- `format_key_value`: key-value pairs, title
- `format_status`: success/failure messages
- `format_tree`: nested directory rendering
- `format_error`: error + suggestion formatting

#### 2. Module Tests (mock HTTP via respx, run in CI)

**File Station tests** — one test file per source file:

`test_listing.py`:
- `list_shares`: parse share list, recycle bin column, footer guidance text
- `list_files`: pagination, pattern filtering, `#recycle` hiding
- `list_recycle_bin`: wrapper correctness, sort by mtime desc

`test_search.py`:
- Async polling mock (start → list → clean cycle)
- `exclude_pattern` client-side filtering
- Timeout behavior (partial results)
- Zero results (suggestion message)

`test_metadata.py`:
- `get_file_info`: single vs multiple paths, partial failure handling
- `get_dir_size`: async polling mock, progress reporting

`test_operations.py`:
- `move_files` / `copy_files`: success, conflict (414), progress reporting
- `delete_files`: recycle bin status adaptation in response
- `restore_from_recycle_bin`: path inference (strip `#recycle`)
- `create_folder`: idempotent behavior, force_parent
- `rename`: bare name validation

`test_helpers.py`:
- Path normalization: leading `/`, shared folder validation
- Human-readable size parsing: `"500MB"`, `"2GB"`, `"1.5TB"`, case insensitivity, decimals, error cases
- Async polling wrapper: timeout, progress callback

#### 3. Integration Tests (real NAS, local only)

Marked with `@pytest.mark.integration`, excluded from CI.

Run manually: `uv run pytest -m integration`

Requires `tests/integration_config.yaml` (git-ignored) pointing at a dev NAS.

### Mock Strategy

The mock boundary is the HTTP layer. `respx` intercepts httpx requests and returns canned DSM API responses. This tests the full path from tool handler through the DSM client without needing a NAS.

```python
# Example: mocking a list_share response
import respx
from httpx import Response

async def test_list_shares(mock_client):
    respx.get("http://nas:5000/webapi/entry.cgi").respond(json={
        "success": True,
        "data": {
            "shares": [
                {"name": "video", "path": "/video", "isdir": True},
                {"name": "music", "path": "/music", "isdir": True},
            ],
            "total": 2,
            "offset": 0,
        }
    })
    result = await list_shares_handler(mock_client)
    assert "video" in result
    assert "music" in result
```

---

## CLAUDE.md

Detailed instructions for Claude Code when working on this repo:

```markdown
# CLAUDE.md — mcp-synology

## Project Overview
MCP server for Synology NAS devices. Layered architecture:
- **Core** (`core/`): DSM API client, auth manager, config loader, formatters, errors
- **Modules** (`modules/`): Feature-specific tool handlers (File Station first)
- **Server** (`server.py`): FastMCP initialization, module loading, startup sequence
- **CLI** (`cli.py`): click-based CLI with serve/setup/check subcommands

## Architecture & Design Docs
Design decisions and tool specifications live in `docs/specs/`:
- `architecture.md` — layered architecture, auth strategy chain, session lifecycle
- `filestation-module.md` — all 12 File Station tools with parameters and response shapes
- `config-schema.md` — YAML config structure, validation rules, env var overrides

**Read the relevant spec before implementing.** These docs record design decisions
and their rationale — don't reinvent or contradict them without discussion.

## Key Conventions

### SDK & Dependencies
- `mcp.server.fastmcp` from the official `mcp` package — NOT the standalone `fastmcp` by PrefectHQ
- `httpx` for all HTTP (async), `respx` for mocking in tests
- `click` for CLI, `pydantic` for validation, `keyring` for credentials
- `yaml.safe_load()` ONLY — never `yaml.load()`

### Code Style
- Python 3.11+, async throughout
- Type hints on all functions and variables; `mypy --strict` must pass
- `ruff` for linting and formatting (line length 100)
- Imports sorted by ruff's isort rules

### Response Formatting
- ALL tool output goes through formatters in `core/formatting.py`
- Never format strings inline in tool handlers
- Formatters produce plain text optimized for LLM consumption

### Module Organization
- Tool handlers grouped by domain: `listing.py`, `search.py`, `metadata.py`, `operations.py`
- Module-specific helpers (path normalization, size parsing, async polling) in `helpers.py`
- Each module declares `MODULE_INFO` with `ModuleInfo` dataclass including API requirements and tool metadata

### Error Handling
- DSM API errors → typed exceptions via `core/errors.py`
- Tool handlers catch exceptions and return `format_error()` output
- Session errors (106/107/119) trigger transparent re-auth — modules never see them

## Testing
- `uv run pytest` — unit + module tests (mocked HTTP, fast)
- `uv run pytest -m integration` — real NAS tests (requires `tests/integration_config.yaml`)
- Mock at the HTTP layer with `respx`, not at the function/method level
- Test files mirror source files: `listing.py` → `test_listing.py`

## Common Tasks
- Add a new File Station tool: add handler in the appropriate domain file, add to `MODULE_INFO.tools`, add tests
- Add a new module: create `modules/<name>/` with `__init__.py` (MODULE_INFO + register), add to config schema
- Run all checks: `uv run ruff check src/ tests/ && uv run mypy src/ && uv run pytest`
```

---

## Release & Distribution

### Primary: uvx from GitHub

```bash
# Users install and run:
uvx --from git+https://github.com/cmeans/mcp-synology mcp-synology serve

# Claude Desktop config:
{
  "mcpServers": {
    "synology": {
      "command": "uvx",
      "args": ["--from", "git+https://github.com/cmeans/mcp-synology", "mcp-synology", "serve"]
    }
  }
}
```

### Future: PyPI

Once stable enough for versioned releases: `uvx mcp-synology serve`

### Future: Docker

```dockerfile
FROM python:3.12-slim
RUN pip install mcp-synology
ENTRYPOINT ["mcp-synology", "serve", "--config", "/config/config.yaml"]
```

---

## Open Items

None — all scaffolding decisions are resolved. Ready for implementation.
