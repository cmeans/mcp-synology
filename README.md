# synology-mcp

MCP server for Synology NAS devices. Exposes Synology DSM API functionality as MCP tools that Claude can use.

## Features

- **File Station** — browse, search, move, copy, delete, and organize files on your NAS (12 tools)
- **System monitoring** — NAS model, firmware, temperature, uptime, and live CPU/memory/disk/network utilization (2 tools; resource usage requires admin)
- **Interactive setup** — guided configuration that creates your config, stores credentials, handles 2FA, and emits a Claude Desktop snippet
- **Permission tiers** — READ (safe browsing) or WRITE (file operations), configured per module
- **2FA support** — auto-detected; device token bootstrap with automatic silent re-auth
- **Secure credentials** — OS keyring integration that works transparently on macOS, Windows, and Linux (including from Claude Desktop). See [docs/credentials.md](docs/credentials.md).
- **Multi-NAS** — manage multiple NAS devices with separate configs, credentials, and state

## Quick Start

### 1. Install

```bash
uv tool install synology-mcp
```

This installs the `synology-mcp` command globally from [PyPI](https://pypi.org/project/synology-mcp/). Requires [uv](https://docs.astral.sh/uv/).

### 2. Run setup

```bash
synology-mcp setup
```

Setup will prompt for your NAS host, credentials, and preferences. If your account has 2FA enabled, it will prompt for an OTP code and store a device token for automatic future logins.

At the end, it prints a Claude Desktop JSON snippet ready to copy-paste.

### 3. Add to Claude Desktop

Copy the snippet from setup into your `claude_desktop_config.json` and restart Claude Desktop. It will look something like:

```json
{
  "mcpServers": {
    "synology-nas": {
      "command": "synology-mcp",
      "args": ["serve", "--config", "~/.config/synology-mcp/nas.yaml"]
    }
  }
}
```

On Linux, the server auto-detects the D-Bus session socket for keyring access. If auto-detection fails, add `"env": {"DBUS_SESSION_BUS_ADDRESS": "unix:path=/run/user/<uid>/bus"}` to the Claude Desktop config. The setup command includes this in the generated snippet.

### 4. Verify

```bash
synology-mcp check                    # Validates credentials work
synology-mcp setup --list             # Shows all configured NAS instances
```

### Alternative: run without global install

If you prefer not to install globally, `uvx` downloads and runs the latest version on each invocation:

```bash
uvx synology-mcp setup
uvx synology-mcp check
```

The trade-off: the Claude Desktop config must use `uvx`:

```json
{
  "mcpServers": {
    "synology": {
      "command": "uvx",
      "args": ["synology-mcp", "serve", "--config", "~/.config/synology-mcp/config.yaml"]
    }
  }
}
```

### Alternative: env-var-only mode

No config file needed if `SYNOLOGY_HOST` is set:

```bash
SYNOLOGY_HOST=192.168.1.100 synology-mcp check
```

## 2FA Support

synology-mcp fully supports DSM accounts with two-factor authentication. It's auto-detected — you don't need to configure anything special:

1. **Bootstrap** — `synology-mcp setup` detects 2FA, prompts for your OTP code, and stores a device token in the keyring
2. **Silent re-auth** — subsequent logins use the device token automatically (no OTP prompts)
3. **Per-instance** — each NAS config gets its own device token, so mixed 2FA/non-2FA setups work fine

If a device token expires or is revoked, run `synology-mcp setup` again to re-bootstrap.

## Keyring & Credentials

Credentials are stored in the OS keyring and accessed transparently:

| Platform | Backend | Notes |
|----------|---------|-------|
| macOS | Keychain | Just works |
| Windows | Credential Manager | Just works |
| Linux | GNOME Keyring / KWallet | Auto-detects D-Bus session, works from Claude Desktop |
| Docker | N/A | Use env vars or config file credentials |

Credential resolution order: **env vars > config file > keyring**. Explicit sources override the implicit default.

See [docs/credentials.md](docs/credentials.md) for keyring service names, multi-NAS setup, and how to inspect/remove stored credentials.

## Updates

synology-mcp checks for updates and notifies you in your Claude Desktop conversation — the first tool response in each session will include a notice if a newer version is available on PyPI.

To manage updates from the CLI:

```bash
synology-mcp --check-update                 # Check for a newer version
synology-mcp --auto-upgrade enable           # Auto-upgrade on each interactive run
synology-mcp --revert                        # Roll back to previous version
synology-mcp --revert 0.1.0                  # Roll back to a specific version
```

To disable update notifications in Claude Desktop, add to your config:

```yaml
check_for_updates: false
```

## Configuration

Interactive setup creates a config file for you. For manual configuration or advanced options, see `examples/`:
- `config-minimal.yaml` — simplest possible config
- `config-power-user.yaml` — HTTPS, custom timeouts, logging, instructions
- `config-docker.yaml` — environment-variable-driven

### Multi-NAS

Each NAS gets its own config file, credentials, and Claude Desktop entry. Set `alias` to give Claude a name to distinguish them:

```yaml
alias: HomeNAS
```

### Custom Instructions

You can customize the prompt that guides Claude's behavior with your NAS tools.

**Add context** — `custom_instructions` is prepended to the built-in prompt (higher priority):

```yaml
custom_instructions: |
  This is the admin NAS with elevated privileges.
  Prefer this connection for file operations requiring cross-user access.
```

**Full control** — `instructions_file` replaces the built-in prompt entirely. Copy the [built-in server.md](src/synology_mcp/instructions/server.md) as a starting point:

```yaml
instructions_file: ~/.config/synology-mcp/my-instructions.md
```

Both support template variables: `{display_name}`, `{instance_id}`, `{host}`, `{port}`.

## Debugging

Two ways to enable debug logging:

```bash
synology-mcp check --verbose                          # --verbose flag on setup/check
SYNOLOGY_LOG_LEVEL=debug synology-mcp serve           # env var, works for all commands
```

Or set it persistently in your config file:

```yaml
logging:
  level: debug
  file: ~/.local/state/synology-mcp/nas/server.log  # optional, logs to stderr by default
```

Debug output includes every DSM API request/response (passwords masked), credential resolution steps, config discovery, version negotiation, and module registration decisions.

## Development

```bash
git clone https://github.com/cmeans/synology-mcp.git
cd synology-mcp
uv sync --extra dev                        # Install dependencies
uv run ruff check src/ tests/              # Lint
uv run ruff format --check src/ tests/     # Format check
uv run mypy src/                           # Type check
uv run pytest                              # Run unit tests (243 tests, no NAS needed)
```

### Integration tests

Integration tests run against a real Synology NAS. They verify the full stack — HTTP, auth, and all File Station operations.

```bash
cp tests/integration_config.yaml.example tests/integration_config.yaml
# Edit integration_config.yaml with your NAS connection details and test paths
uv run pytest -m integration -v --log-cli-level=INFO
```

32 tests covering: connection, listing, search, metadata, copy/move/rename/delete lifecycle, recycle bin, and error handling. See the example config for required fields.

## Design Docs

Detailed specs live in `docs/specs/`. These were the original design documents — the code is authoritative where they diverge (e.g., DSM API version pinning, GET-only requests, and search behavior were discovered during live testing and are documented in `CLAUDE.md`).

- `architecture.md` — layered architecture, auth strategy, session lifecycle
- `filestation-module-spec.md` — all 12 File Station tools
- `config-schema-spec.md` — YAML config structure and validation
- `project-scaffolding-spec.md` — repo structure, CI, testing

## Acknowledgements

This project was built using a **Spec-First Coding** approach — a human-AI collaboration model where design precedes implementation and specs are the contract between the two.

Unlike vibe coding, where you describe what you want and let the AI generate code on the fly, spec-first coding treats design as a separate, deliberate phase. The four specs in `docs/specs/` were developed through extended conversation — exploring trade-offs, rejecting alternatives, and documenting decisions with rationale. Implementation then used the specs as the source of truth across 11 build phases.

Live testing against real hardware revealed behaviors the specs couldn't anticipate (DSM API quirks, search service throttling, version format incompatibilities). These discoveries are documented in `CLAUDE.md` and the code, which is authoritative where specs diverge.

## License

MIT

---

Copyright (c) 2026 Chris Means
