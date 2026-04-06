# Development

## Setup

```bash
git clone https://github.com/cmeans/mcp-synology.git
cd mcp-synology
uv sync --extra dev                        # Install dependencies
```

## Commands

```bash
uv run ruff check src/ tests/              # Lint
uv run ruff format --check src/ tests/     # Format check
uv run ruff format src/ tests/             # Auto-format
uv run mypy src/                           # Type check (strict mode)
uv run pytest                              # Run unit tests (no NAS needed)
uv run pytest --cov=mcp_synology           # Tests with coverage
```

## Integration Tests

Integration tests run against a real Synology NAS. They verify the full stack — HTTP, auth, and all module operations.

### Setup

```bash
cp tests/integration_config.yaml.example tests/integration_config.yaml
```

Edit `integration_config.yaml` with your NAS connection details. The file is gitignored — credentials come from the OS keyring (populated by `mcp-synology setup`). For CI, use environment variables (`SYNOLOGY_HOST`, `SYNOLOGY_USERNAME`, `SYNOLOGY_PASSWORD`).

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
admin_config: ~/.config/mcp-synology/admin.yaml
```

### Running

```bash
uv run pytest -m integration -v --log-cli-level=INFO    # All integration tests
uv run pytest -m integration -v -k "TestSearch"          # Just search tests
uv run pytest -m integration -v -k "TestSystemInfo"      # Just system info tests
```

Integration tests are excluded from CI by default (`addopts` in `pyproject.toml`).

### Known quirks

- **Search service throttling** — DSM's search service on non-indexed shares can be overwhelmed by rapid-fire requests, returning 0 results or 502 errors. Allow recovery time between search-heavy test runs.
- **Background task cleanup** — orphaned Search/DirSize tasks consume CPU indefinitely. The code uses `try/finally` to prevent this, but if tests are interrupted (Ctrl+C), tasks may linger. Check DSM Resource Monitor > Processes for `synoscgi_SYNO.FileStation.Search` entries.

## Virtual-DSM Tests

Virtual-DSM tests run the same integration test suite against a Docker container
running full Synology DSM via QEMU/KVM. This enables testing across multiple DSM
versions without a physical NAS.

### Requirements

- Linux host with KVM support (`/dev/kvm` must exist)
- Docker installed and running
- ~2 GB disk per DSM version (golden images)

macOS is not currently supported — KVM is Linux-only. Use the real NAS integration
tests on macOS.

### First-Time Setup

Each DSM version needs a one-time golden image creation (~15 min):

```bash
uv sync --extra dev --extra vdsm
python scripts/vdsm_setup.py --version 7.2.2
```

The script boots a fresh virtual-dsm container. Complete the DSM setup
wizard in your browser (set admin password, basic storage). The script then
automatically creates test users, shared folders, and seed data via the DSM API.

Golden images are stored in `.vdsm/golden/` (~2 GB each, gitignored).

### Running

```bash
uv run pytest -m vdsm -v --log-cli-level=INFO          # Default (7.2.2)
uv run pytest -m vdsm -v --dsm-version 7.1             # Specific version
uv run pytest -m vdsm -v -k "TestSearch"                # Single test class
```

### Supported DSM Versions

| Version | Build | PAT |
|---------|-------|-----|
| 7.0.1   | 42218 | VirtualDSM_42218.pat |
| 7.1     | 42661 | VirtualDSM_42661.pat |
| 7.2.1   | 69057 | VirtualDSM_69057.pat |
| 7.2.2   | 72806 | VirtualDSM_72806.pat (default) |
| 7.3.2   | 86009 | VirtualDSM_86009.pat |

### How It Works

1. Restores a golden image (pre-configured DSM) to a temp storage directory
2. Boots the virtual-dsm Docker container (~2 min)
3. Runs the same test functions as the real NAS integration tests
4. Container is stopped and cleaned up automatically

## Design Docs

Detailed specs live in `docs/specs/`. These were the original design documents — the code is authoritative where they diverge (e.g., DSM API version pinning, GET-only requests, and search behavior were discovered during live testing and are documented in `CLAUDE.md`).

- `architecture.md` — layered architecture, auth strategy, session lifecycle
- `filestation-module-spec.md` — all 14 File Station tools (7 READ + 7 WRITE)
- `config-schema-spec.md` — YAML config structure and validation
- `project-scaffolding-spec.md` — repo structure, CI, testing

## Architecture

See `CLAUDE.md` for detailed conventions around type safety, async patterns, error handling, DSM API client behavior, and the module registration system.
