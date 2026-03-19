# Development

## Setup

```bash
git clone https://github.com/cmeans/synology-mcp.git
cd synology-mcp
uv sync --extra dev                        # Install dependencies
```

## Commands

```bash
uv run ruff check src/ tests/              # Lint
uv run ruff format --check src/ tests/     # Format check
uv run ruff format src/ tests/             # Auto-format
uv run mypy src/                           # Type check (strict mode)
uv run pytest                              # Run unit tests (no NAS needed)
uv run pytest --cov=synology_mcp           # Tests with coverage
```

## Integration Tests

Integration tests run against a real Synology NAS. They verify the full stack — HTTP, auth, and all module operations.

### Setup

```bash
cp tests/integration_config.yaml.example tests/integration_config.yaml
```

Edit `integration_config.yaml` with your NAS connection details. The file is gitignored — credentials come from the OS keyring (populated by `synology-mcp setup`). For CI, use environment variables (`SYNOLOGY_HOST`, `SYNOLOGY_USERNAME`, `SYNOLOGY_PASSWORD`).

Configure `test_paths` to match folders on your NAS:

```yaml
test_paths:
  existing_share: /home           # A share that exists
  search_folder: /home/3D         # A folder with known content to search
  search_keyword: Bambu           # A keyword that matches something in search_folder
  writable_folder: /home/Test     # A folder where tests can create/copy/delete files
```

For tests requiring admin privileges (e.g., resource utilization), point `admin_config` to a config using an admin account:

```yaml
admin_config: ~/.config/synology-mcp/admin.yaml
```

### Running

```bash
uv run pytest -m integration -v --log-cli-level=INFO    # All integration tests
uv run pytest -m integration -v -k "TestSearch"          # Just search tests
uv run pytest -m integration -v -k "TestSystemInfo"      # Just system info tests
```

Integration tests are excluded from CI by default (`addopts = "-m 'not integration'"` in `pyproject.toml`).

### Known quirks

- **Search service throttling** — DSM's search service on non-indexed shares can be overwhelmed by rapid-fire requests, returning 0 results or 502 errors. Allow recovery time between search-heavy test runs.
- **Background task cleanup** — orphaned Search/DirSize tasks consume CPU indefinitely. The code uses `try/finally` to prevent this, but if tests are interrupted (Ctrl+C), tasks may linger. Check DSM Resource Monitor > Processes for `synoscgi_SYNO.FileStation.Search` entries.

## Design Docs

Detailed specs live in `docs/specs/`. These were the original design documents — the code is authoritative where they diverge (e.g., DSM API version pinning, GET-only requests, and search behavior were discovered during live testing and are documented in `CLAUDE.md`).

- `architecture.md` — layered architecture, auth strategy, session lifecycle
- `filestation-module-spec.md` — all 12 File Station tools
- `config-schema-spec.md` — YAML config structure and validation
- `project-scaffolding-spec.md` — repo structure, CI, testing

## Architecture

See `CLAUDE.md` for detailed conventions around type safety, async patterns, error handling, DSM API client behavior, and the module registration system.
