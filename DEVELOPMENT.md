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
uv run pytest -n auto                      # Run unit tests in parallel (no NAS needed)
uv run pytest -n auto --cov=mcp_synology   # Tests with coverage
```

### Measuring the tool payload

`scripts/measure_tools.py` reports what the server sends a model before any conversation (tool schemas, descriptions and instructions), for an offline all-modules config that can't reach a NAS (#163):

```bash
uv run python scripts/measure_tools.py                    # full report to .cache/measure/
uv run python scripts/measure_tools.py --no-external      # size and tokens only
uv run python scripts/measure_tools.py --compare OLD.json # difference from an earlier report
```

- **Contents:** size in characters, mcpscore and `tdqs lint` (pinned versions run via `uvx`; no tool is ever called). With `ANTHROPIC_API_KEY` set, also exact Claude token counts and, with `--tdqs-score`, `tdqs score` graded by Claude. `--ollama URL --ollama-model M` adds Ollama prompt tokens. See the script's `--help`.
- **CI:** the non-blocking `tool-quality` job runs it on every PR, writes the report to the job summary and uploads it as the `tool-quality` artifact.
- **Budget test:** `tests/core/test_server.py::TestPayloadBudget::test_payload_within_budget` fails when the served payload grows past `PAYLOAD_BUDGET_CHARS`. If the growth is intended, raise the budget in the same PR and say why.

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

- **JSON-parsed parameter values** — DSM's JSON-format File Station APIs parse each value as JSON first, so free-text values (task IDs, names, patterns) are sent JSON-quoted (`filestation.helpers.dsm_str()`). Before #121's fix, unquoted task IDs made most searches look like "0 results" and the search service look overloaded; back-to-back searches need no delay.
- **Background task cleanup** — orphaned Search/DirSize tasks consume CPU indefinitely. The code uses `try/finally` to prevent this (with the task ID quoted; unquoted, `stop`/`clean` silently did nothing until #121), but if tests are interrupted (Ctrl+C), tasks may linger. Check DSM Resource Monitor > Processes for `synoscgi_SYNO.FileStation.Search` entries.

## Virtual-DSM Tests

Virtual-DSM tests run the same integration test suite against a Docker container
running full Synology DSM via QEMU/KVM. This enables testing across multiple DSM
versions without a physical NAS.

### Current Status

The bake sets up everything the suite needs (users, packages, shared folders and seed data on the auto-created `/volume1`), and CI runs the full vdsm suite on DSM 7.1, 7.2.2, 7.3.2 and 7.4.1. No manual setup is needed.

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

Each DSM version needs a one-time golden image creation:

```bash
uv sync --extra dev --extra vdsm
uv run playwright install chromium
echo y | uv run python scripts/vdsm_setup.py --version 7.2.2 \
    --admin-user mcpadmin --admin-password 'McpTest123!'
```

The setup script:
1. Boots a fresh virtual-dsm container (the pinned `VDSM_IMAGE`) via Podman/Docker
2. Automates the DSM first-boot wizard via headless Playwright (account, updates, analytics)
3. Sets the timezone and enables SSH via the DSM API, then over SSH installs the DSM update and reboots, creates the `mcptest` and `mcpfresh` users (`synouser`), installs Download Station, Synology Photos and Snapshot Replication (`synopkg`), and creates the shared folders and seed data (`synoshare`) (`tests/vdsm/setup_dsm.py:setup_dsm_for_testing`)
4. Configures Download Station and Photos via the DSM API, and checks each test user's access
5. Saves a compressed golden image to `.vdsm/golden/`, with its `meta.json`

Golden images are stored in `.vdsm/golden/` (about 1.2 GB each, gitignored).

### Running

```bash
uv run pytest tests/vdsm -m vdsm -v --log-cli-level=INFO --no-cov   # Default (7.2.2)
uv run pytest tests/vdsm -m vdsm -v --dsm-version 7.1 --no-cov      # Specific version
uv run pytest tests/vdsm -m vdsm -v -k "TestConnection" --no-cov     # Single test class
```

Pass `tests/vdsm` (or a file in it): pytest registers `--dsm-version` from `tests/vdsm/conftest.py` only when that directory is on the command line, so from the repo root without it, `--dsm-version` fails with "unrecognized arguments".

Note: `--no-cov` is recommended for vdsm runs since the coverage floor (95%) applies globally and vdsm tests alone won't meet it.

### Supported DSM Versions

| Version (`--version`) | DSM | Build | Update | PAT |
|---------|-----|-------|--------|-----|
| 7.0.1   | 7.0.1 | 42218 | — | VirtualDSM_42218.pat |
| 7.1     | 7.1.1 | 42962 | 9 | VirtualDSM_42962.pat |
| 7.2.1   | 7.2.1 | 69057 | — | VirtualDSM_69057.pat |
| 7.2.2   | 7.2.2 | 72806 | 10 | VirtualDSM_72806.pat (default) |
| 7.3.2   | 7.3.2 | 86009 | 5 | VirtualDSM_86009.pat |
| 7.4.1   | 7.4.1 | 90080 | 1 | VirtualDSM_90080.pat |

CI runs 7.1, 7.2.2, 7.3.2 and 7.4.1 (#307); 7.0.1 and 7.2.1 are defined but not baked or tested. The `7.1` image runs 7.1.1 since bake v9 (7.1-42661 before): 7.1-42661's updates stop at Update 4, and Synology's later 7.1 updates are for 7.1.1.

Since bake v9, the bake installs the update in the Update column (`DsmVersionInfo.update` in `tests/vdsm/config.py`) right after enabling SSH, before the packages: the guest downloads Synology's update pack, `synoupgrade --check-pat` and `--patch` install it, and the bake reboots and checks `smallfixnumber`. `meta.json` records the build, the update and the version string DSM reports (`firmware_ver`), and `test_vdsm_bake.py` checks the restored image still reports it. When Synology ships a newer update, change the number and bump `BAKE_VERSION`. The virtual-dsm container image is pinned too (`VDSM_IMAGE`); move the pin in a bake change, not by pulling `latest`.

### How It Works

1. Restores a golden image (pre-configured DSM) to a temp storage directory
2. Boots the virtual-dsm container via Podman/Docker
3. Runs the same test functions as the real NAS integration tests
4. Container is stopped and cleaned up automatically

### Known Limitations

- **Users and shares over SSH** — the write methods of `SYNO.Core.User`, `SYNO.Core.Share` and `SYNO.Core.Share.Permission` (creating users and shares, setting permissions) answered 105/403 on virtual-dsm even with an admin session, so the bake creates users and shared folders with `synouser` and `synoshare` in the guest over SSH.
- **DSM password policy** — DSM 7.2.2 requires "Strong" passwords (blocks "Moderate"). The test user password is `Mcp#Test9!xK27zQ`.

## Design Docs

Detailed specs live in `docs/specs/`. These were the original design documents — the code is authoritative where they diverge (e.g., DSM API version pinning, the transport each call uses, and search behavior were discovered during live testing and are documented in `CLAUDE.md`).

- `architecture.md` — layered architecture, auth strategy, session lifecycle
- `filestation-module-spec.md` — all 15 File Station tools (7 READ + 8 WRITE)
- `config-schema-spec.md` — YAML config structure and validation
- `project-scaffolding-spec.md` — repo structure, CI, testing

## Architecture

See `CLAUDE.md` for detailed conventions around type safety, async patterns, error handling, DSM API client behavior, and the module registration system.
