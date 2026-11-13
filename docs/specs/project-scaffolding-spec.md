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
│       ├── server.py                # MCPServer init, module loading, startup
│       │
│       ├── core/
│       │   ├── __init__.py
│       │   ├── client.py            # DSM API client (async httpx)
│       │   ├── auth.py              # Auth manager (session lifecycle, 2FA)
│       │   ├── config.py            # YAML config loading + Pydantic validation
│       │   ├── formatting.py        # Shared response formatters (table, kv, status, tree, error)
│       │   ├── errors.py            # Typed exception hierarchy, error code mapping
│       │   └── state.py             # The SYNO.API.Info cache entry
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
│       ├── filestation-module.md     # File Station tool specs (15 tools)
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
    "Programming Language :: Python :: 3.14",
    "Programming Language :: Python :: 3.15",
    "Topic :: System :: Systems Administration",
]
dependencies = [
    "mcp>=2.3,<3",            # Official MCP Python SDK 2.x (MCPServer)
    "httpx>=0.28",             # Async HTTP client for DSM API calls
    "pyyaml>=6.0.1",           # YAML config parsing (a SafeLoader only)
    "keyring>=25.0",           # OS-native credential storage
    "pydantic>=2.14.2",        # Config and settings validation
    "click>=8.2.1",            # CLI framework (serve, setup, check)
    "regex>=2026.9.29",        # Current Unicode properties for File Station's new-name rules
]

[project.optional-dependencies]
dev = [
    "pytest>=8.0",
    "pytest-asyncio>=0.24",
    "pytest-cov>=5.0",
    "pytest-timeout>=2.3",
    "pytest-xdist>=3.6",
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
asyncio_default_fixture_loop_scope = "function"
markers = ["integration: requires a real Synology NAS (not run in CI)"]
addopts = "-m 'not integration'"
```

### Dependency Notes

- **`mcp>=2.3,<3`**: The official Python MCP SDK. Floor is 2.3.0, the release the 2.x migration (#114) was tested against; 1.x (`mcp.server.fastmcp`) is no longer supported. Provides `mcp.server.mcpserver.MCPServer`, `Context`, type definitions, and stdio transport. 2.x brings its own HTTP client (`httpx2`) and `opentelemetry-api` (a no-op unless an OpenTelemetry SDK is configured); the DSM client keeps using `httpx`. We do NOT use the standalone `fastmcp` package by PrefectHQ.
- **`httpx>=0.28`**: Async HTTP client. Used by the DSM API client for all NAS communication. Cleaner API than `aiohttp`, better testing story via `respx`. Floor: below 0.28, a space in a query is `%20` rather than `+`, which `test_entry_cgi_keeps_httpx_encoding` pins.
- **`keyring>=25.0`**: OS-native credential storage. Auto-detects macOS Keychain, Windows Credential Manager, or Linux Secret Service.
- **`pydantic>=2.14.2`**: Config validation, module settings schemas, type coercion. Floor: `mcp` needs 2.12.0 or later, and 2.14.2 has the fixes for pydantic's advisories of 2026-10-29 that reach those versions (GHSA-3r8g-cqwr-jr29, GHSA-qpmf-fgh3-ff25, GHSA-955g-x97g-8mcp, GHSA-gj4f-r7mf-jjxh).
- **`pyyaml>=6.0.1`**: Config file parsing. Floor: 6.0 has no wheels past Python 3.11 and its source fails to build on 3.12 and later. Always a SafeLoader: `yaml.safe_load()`, or `core/config.py` `parse_config_text` for config files (a SafeLoader subclass that refuses repeated keys, #464) — never `yaml.load()` with any other loader.
- **`click>=8.2.1`**: CLI framework. Three subcommands: `serve` (MCP server mode), `setup` (interactive credential bootstrap), `check` (validate credentials). Click handles prompts, password masking, and colored output for the interactive `setup` flow — things that would be tedious with argparse. Floor: the tests read `CliRunner`'s separate stderr, new in 8.2, and interactive `setup` tests hang at 8.2.0.
- **`regex>=2026.9.29`**: Unicode properties the standard library lacks or has only in an old version: `Default_Ignorable_Code_Point`, joining type, combining class, Indic syllabic category, emoji properties and scripts, for File Station's rules on the names a tool creates (#515). Python 3.11's `unicodedata` is Unicode 14. Which names are refused follows the Unicode data of the installed `regex` (from 2024.11.6 to 2026.9.29, `Extended_Pictographic` lost 707 characters such as ★), so the floor is the version the tests run against, and `test_helpers.py` pins a case that changed. The repeated-mark check's NFD takes combining classes from `regex` too, and decompositions from `unicodedata` up to Python 3.11's Unicode (14.0; they never change once a character is assigned) and from the generated `core/decompositions.py` for every character assigned since, so its verdict is the same on every Python. The rules live in `core/display.py` (moved from File Station's helpers in #697, so `core` can use them). Raise the floor and the data together: regenerate `core/standardized_variants.py` (`scripts/gen_standardized_variants.py`, from StandardizedVariants.txt and emoji-variation-sequences.txt) and `core/decompositions.py` (`scripts/gen_decompositions.py`, from UnicodeData.txt and DerivedAge.txt) from the Unicode version of the new floor's data. `regex` doesn't report that version: its release notes name it, and the header of `standardized_variants.py` records the one in use (18.0.0). It ships wheels for every platform. Its stubs, `types-regex`, are a dev dependency.
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
    """Validate the config, as serve does, then check the stored credentials can log in."""
    # Load config → load credentials → attempt login → report
    ...
```

**Why click over argparse:**
- `click.prompt()` with `hide_input=True` for password entry — the `setup` command needs this
- Colored output for status messages during setup, through `cli/output.py` `say()` (redacted, control characters shown visibly; #514)
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
      - run: uv sync --locked --extra dev
      - run: uv run ruff check src/ tests/
      - run: uv run ruff format --check src/ tests/

  typecheck:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v4
      - uses: astral-sh/setup-uv@v4
      - run: uv sync --locked --extra dev
      - run: uv run mypy src/

  test:
    runs-on: ubuntu-latest
    strategy:
      matrix:
        python-version: ["3.11", "3.12", "3.13", "3.14", "3.15"]
    steps:
      - uses: actions/checkout@v4
      - uses: astral-sh/setup-uv@v4
        with:
          python-version: ${{ matrix.python-version }}
      - run: uv sync --locked --extra dev
      - run: uv run pytest -n auto --cov=mcp_synology --cov-report=xml
      - uses: codecov/codecov-action@v4
        if: matrix.python-version == '3.12'
        with:
          files: coverage.xml
```

**CI Notes:**
- Parallel jobs, among them lint, typecheck, test and floor. Fast feedback — failures are independent.
- Test matrix: Python 3.11 to 3.15.
- `uv` for CI via `astral-sh/setup-uv` — faster than pip.
- `UV_LOCKED: "1"` at the top of every workflow that runs uv, so `uv sync` and `uv run` refuse a `uv.lock` that doesn't match `pyproject.toml`. `tests/test_ci_workflows.py` checks it. Dependabot's Python updates use its `uv` ecosystem, which updates the lock.
- `deps-newest.yml` is the one exception (#115): weekly, by hand, and on PRs touching `pyproject.toml`, `uv.lock` or the workflow, it runs the unit tests on the newest versions those allow, on the newest Python CI tests, since a user's resolver takes them and the lock doesn't. Not required. A failure on main opens or updates one issue, and a pass closes it, from a job with no checkout that never runs on a PR. Known limit: the lock resolves all extras together, so a cap in the `dev` or `vdsm` extra can hold a runtime dependency back from what a user gets.
- The `floor` job in `ci.yml` (#115), on every PR and push: on the oldest and newest Python CI tests, it installs each runtime dependency at exactly its `pyproject.toml` floor (`scripts/runtime_floors.py`; dev tools stay at the lock) and runs the unit tests. What usually breaks a floor is our own code outgrowing it, so unlike `deps-newest.yml` it has no path filter. It also fails when a floor can't install on either Python, as `pyyaml>=6.0` couldn't on the newest. Not required. Known limit: a floor with no wheel for a Python builds from source with the newest build tools (pyyaml 6.0.1 on 3.15), so a build-tool release can fail the job with no change in this repo (#571). Not covered: vdsm at the floors, the Pythons in between, other operating systems (#184), transitive dependencies (they stay at the lock unless a floor forces them), and dev tools' own floors.
- Coverage collected on 3.12, uploaded to Codecov. Not a hard gate initially — just visibility.
- `Ready for QA` needs the CI and vdsm workflows passed job by job, and CodeQL's check run when the commit has one (#578; `.github/scripts/pr-checks-state.sh`). When CI or vdsm finishes, or `Dev Active` comes off, a failure gives `CI Failed` and removes `Ready for QA`, even one left beside a QA label; a PR with only QA labels doesn't change. A commit with no CodeQL check (seen on a fork PR) isn't held for one; a check that hasn't posted yet and a renamed check or app read the same way, so it isn't a merge gate. CodeQL is GitHub's default setup, not a workflow here, so nothing fires when it finishes: `pr-labels-recheck.yml` reads it when it next runs, while the PR is at `Awaiting CI` or `CI Failed`, where it settles the labels, or at `Ready for QA`, a fork's too, where it only comments, once per commit and failed workflow or check name, saying how to see who applied the label and that a CodeQL alert needs fixing, not an empty commit (inferred: an empty commit and a dismissed alert are untested). The label stays, since someone may have applied it by hand and a bot shouldn't undo that (maintainer, 2026-11-06), except on a PR that also has `Awaiting CI` or `CI Failed`: that's settled as those, so a failure takes `Ready for QA` back. On a branch of this repository `on-label` clears such a stale label first; on a fork its token is read-only, so a hand label beside one goes until a push (maintainer, 2026-11-06: rare and forks only, so stated rather than coded). A `CI Failed` that comes from CodeQL alone, with no workflow job failed, gets a comment saying so, once per commit (`pr-labels-settle.sh`), also when `CI Failed` was already there from a job since re-run green; the "who applied it" command is paginated, as a busy PR's latest label event is past the first page; and a branch that's a draft, like a fork, gets no label by itself, and the missing-results comment's marker names what's missing, so a lagging job's comment doesn't silence a stuck CodeQL's. The cron asks for every 15 minutes, but GitHub has run it hours apart, so QA may have started by then, and a PR that no longer has `Ready for QA` is off its list (#584). A push judges the new commit instead.
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
- Credential resolution hierarchy (env → config → keyring)
- Strategy chain: no 2FA, 2FA with device token, 2FA without device token (error)
- Session re-auth on error 106/107/119
- Re-auth lock prevents concurrent re-auth
- Error 105 (permission denied) is NOT retried

**Config tests (`test_config.py`):**
- Minimal config loading
- Full config with all optional fields
- Env var override merging (two-phase validation)
- Default derivation (port from https, instance_id from host)
- Strict validation at the top level, in `connection`/`auth`/`logging` and directly under `modules.<name>` (unknown keys → error)
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
- Human-readable size parsing: `"500MiB"`, `"2GiB"`, `"1.5TiB"` and the KB/MB/GB/TB spellings (same binary units), case insensitivity, decimals, error cases
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
- **Server** (`server.py`): `MCPServer` initialization, module loading, startup sequence
- **CLI** (`cli.py`): click-based CLI with serve/setup/check subcommands

## Architecture & Design Docs
Design decisions and tool specifications live in `docs/specs/`:
- `architecture.md` — layered architecture, auth strategy chain, session lifecycle
- `filestation-module.md` — all 15 File Station tools with parameters and response shapes
- `config-schema.md` — YAML config structure, validation rules, env var overrides

**Read the relevant spec before implementing.** These docs record design decisions
and their rationale — don't reinvent or contradict them without discussion.

## Key Conventions

### SDK & Dependencies
- `mcp.server.mcpserver` from the official `mcp` package — NOT the standalone `fastmcp` by PrefectHQ
- `httpx` for all HTTP (async), `respx` for mocking in tests
- `click` for CLI, `pydantic` for validation, `keyring` for credentials
- A SafeLoader ONLY (`yaml.safe_load()`, or `parse_config_text` for config files) — never `yaml.load()` with any other loader

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
- `uv run pytest -n auto` — unit + module tests in parallel (mocked HTTP, fast)
- `uv run pytest -m integration` — real NAS tests (requires `tests/integration_config.yaml`); never with `-n`, since they run in order
- Mock at the HTTP layer with `respx`, not at the function/method level
- Test files mirror source files: `listing.py` → `test_listing.py`

## Common Tasks
- Add a new File Station tool: add handler in the appropriate domain file, add to `MODULE_INFO.tools`, add tests
- Add a new module: create `modules/<name>/` with `__init__.py` (MODULE_INFO + register), add to config schema
- Run all checks: `uv run ruff check src/ tests/ && uv run mypy src/ && uv run pytest -n auto`
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

### Automated release pipeline

Tagging `v*` triggers `.github/workflows/publish.yml`. Its `gate` job runs first and nothing else starts unless it passes (`scripts/check_release.py`, #457): the tag must be `v` plus `pyproject.toml`'s version, `server.json` must match, `CHANGELOG.md` must have notes for that version, no entries in any section above them and none under an Unreleased heading anywhere; `changelog.d/` must hold no CHANGELOG fragments (#731: the release PR assembles them into the version's notes), and the tagged commit must be on `main` or, for a patch, the version's backport branch, `release/<major>.<minor>.x` (as v0.5.3 was), cut at an earlier final tag of that line and containing that line's earlier releases; a release PR's own branch, `release/0.11.0`, doesn't count, nor does any backport branch for a `.0`. A backport branch gets no CI, review or `main` ruleset: only these checks, the workflow's `build` and `validate-server-json` jobs, the `release-tags` ruleset and the `pypi` approval apply. It can't tell a later commit that added no CHANGELOG entry from the release commit, and "notes" is a heuristic: it catches leftovers, not poor notes. It covers commits that contain the job: a tag on an older commit runs that commit's workflow. Then:

1. `build` — runs the unit test suite and builds sdist + wheel. `validate-server-json` runs beside it, and `publish-pypi` needs both.
2. `publish-pypi` — uploads the built distributions to PyPI via Trusted Publishing (OIDC, no long-lived token).
3. `publish-registry` — installs [`mcp-publisher`](https://github.com/modelcontextprotocol/registry), authenticates via GitHub OIDC (`id-token: write`), and publishes `server.json` to `registry.modelcontextprotocol.io`. Idempotent on re-run: the registry's "version already exists" error is caught and downgraded to a workflow warning.
4. `github-release` — extracts the release notes for the tag from `CHANGELOG.md` (via `awk` on the `## <version>` heading) and either creates a new GitHub Release or updates the existing one (`gh release create` / `gh release edit`).

`server.json` is validated on every PR by the `validate-server-json` job in `ci.yml`, which runs `mcp-publisher validate` against the current schema. The `version-sync` job in the same workflow enforces that `server.json`'s version fields stay aligned with `[project].version` in `pyproject.toml` via `scripts/sync-server-json.py --check`.

### Future: Docker

```dockerfile
FROM python:3.12-slim
RUN pip install mcp-synology
ENTRYPOINT ["mcp-synology", "serve", "--config", "/config/config.yaml"]
```

---

## Open Items

None — all scaffolding decisions are resolved. Ready for implementation.
