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

### Current Status

**21 of 47 vdsm tests pass** on a bare DSM 7.2.2 instance (wizard completed, admin + test user created). The remaining 26 tests require shared folders on a properly configured storage volume. Virtual-dsm does not auto-create a storage volume during initial setup — this requires either manual Storage Manager configuration or additional automation (tracked as a follow-up).

Tests that pass without a volume: connection (3), system info (1), resource usage (2), error handling (3), empty search cases (2), and several listing/metadata/transfer tests that validate error responses.

### Requirements

- **Linux host with KVM** — `/dev/kvm` must exist. macOS is not supported (KVM is Linux-only); use the real NAS integration tests on macOS.
- **Podman (recommended)** or native Docker Engine — Docker Desktop runs containers inside a VM that lacks `/dev/kvm` passthrough. Podman runs natively on the host with direct KVM access.
- ~2 GB disk per DSM version (golden images)

#### Podman Setup

```bash
# Enable the Podman API socket (one-time)
systemctl --user enable --now podman.socket
```

The test infrastructure auto-detects the Podman socket at `/run/user/$UID/podman/podman.sock` and prefers it over Docker Desktop. No `DOCKER_HOST` configuration needed.

### First-Time Setup

Each DSM version needs a one-time golden image creation (~5 min):

```bash
uv sync --extra dev --extra vdsm
uv run playwright install chromium
echo y | uv run python scripts/vdsm_setup.py --version 7.2.2 \
    --admin-user mcpadmin --admin-password 'McpTest123!'
```

The setup script:
1. Boots a fresh virtual-dsm container via Podman/Docker
2. Automates the DSM first-boot wizard via headless Playwright (account, updates, analytics)
3. Dismisses post-login popups (2FA, MFA promotions) by force-removing them from the DOM
4. Creates the `mcptest` test user via the Control Panel User Creation Wizard (Playwright)
5. Creates test directories and seed data via `docker exec`
6. Saves a compressed golden image to `.vdsm/golden/`

Golden images are stored in `.vdsm/golden/` (~865 MB each, gitignored).

#### Manual Storage Volume Setup (for full 47-test suite)

After the automated setup, the golden image has no DSM storage volume. To enable shared folder tests:

1. Boot the golden image: run `vdsm_setup.py` but stop it before the "Stopping container" step (or boot manually)
2. Open the DSM web UI at the container's URL
3. Open **Storage Manager** → create a **Storage Pool** (Basic/SHR) on the virtual disk → create a **Volume**
4. Open **Control Panel** → **Shared Folder** → create `testshare` and `writable`
5. Set read/write permissions for `mcptest` on both shares
6. Upload test files to `/testshare/Documents/` (report.txt, search_target.txt) and `/testshare/Media/` (sample.mkv)
7. Stop the container and re-save the golden image

This manual step is a one-time investment per DSM version. Automating Storage Manager via Playwright is a planned follow-up.

### Running

```bash
uv run pytest -m vdsm -v --log-cli-level=INFO --no-cov   # Default (7.2.2)
uv run pytest -m vdsm -v --dsm-version 7.1 --no-cov      # Specific version
uv run pytest -m vdsm -v -k "TestConnection" --no-cov     # Single test class
```

Note: `--no-cov` is recommended for vdsm runs since the coverage floor (95%) applies globally and vdsm tests alone won't meet it.

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
2. Boots the virtual-dsm container via Podman/Docker (~30s from golden image)
3. Runs the same test functions as the real NAS integration tests
4. Container is stopped and cleaned up automatically

### Known Limitations

- **No auto-volume** — virtual-dsm's QEMU disk is visible to DSM but requires manual Storage Manager configuration to create a storage pool and volume. The `DISK_SIZE` env var controls disk size but does not trigger DSM-level volume creation.
- **Undocumented APIs fail** — `SYNO.Core.User`, `SYNO.Core.Share`, and `SYNO.Core.Share.Permission` return error 105/403 on virtual-dsm even with valid admin sessions. User creation is done via Playwright web UI automation instead.
- **DSM password policy** — DSM 7.2.2 requires "Strong" passwords (blocks "Moderate"). The test user password is `Mcp#Test9!xK27zQ`.
- **Boot time** — first boot from PAT download takes ~2 min; subsequent boots from golden image take ~30s.

## Design Docs

Detailed specs live in `docs/specs/`. These were the original design documents — the code is authoritative where they diverge (e.g., DSM API version pinning, GET-only requests, and search behavior were discovered during live testing and are documented in `CLAUDE.md`).

- `architecture.md` — layered architecture, auth strategy, session lifecycle
- `filestation-module-spec.md` — all 14 File Station tools (7 READ + 7 WRITE)
- `config-schema-spec.md` — YAML config structure and validation
- `project-scaffolding-spec.md` — repo structure, CI, testing

## Architecture

See `CLAUDE.md` for detailed conventions around type safety, async patterns, error handling, DSM API client behavior, and the module registration system.
