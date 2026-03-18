# synology-mcp

MCP server for Synology NAS devices. Exposes Synology DSM API functionality as MCP tools that Claude can use.

## Features

- **File Station** — browse, search, move, copy, delete, and organize files on your NAS (12 tools)
- **Interactive setup** — guided configuration that creates your config, stores credentials, handles 2FA, and emits a Claude Desktop snippet
- **Permission tiers** — READ (safe browsing) or WRITE (file operations), configured per module
- **2FA support** — auto-detected; device token bootstrap with automatic silent re-auth
- **Secure credentials** — OS keyring integration that works transparently on macOS, Windows, and Linux (including from Claude Desktop). See [docs/credentials.md](docs/credentials.md).
- **Multi-NAS** — manage multiple NAS devices with separate configs, credentials, and state

## Quick Start

### 1. Install

```bash
uv tool install git+https://github.com/cmeans/synology-mcp
```

This installs the `synology-mcp` command globally. Requires [uv](https://docs.astral.sh/uv/).

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

No `env` block needed — keyring credentials are accessed automatically on all platforms.

### 4. Verify

```bash
synology-mcp check                    # Validates credentials work
synology-mcp setup --list             # Shows all configured NAS instances
```

### Alternative: run without global install

If you prefer not to install globally, `uvx` downloads and runs the latest version on each invocation:

```bash
uvx --from git+https://github.com/cmeans/synology-mcp synology-mcp setup
uvx --from git+https://github.com/cmeans/synology-mcp synology-mcp check
```

The trade-off: the Claude Desktop config must use the full `uvx` invocation:

```json
{
  "mcpServers": {
    "synology": {
      "command": "uvx",
      "args": ["--from", "git+https://github.com/cmeans/synology-mcp", "synology-mcp", "serve", "--config", "~/.config/synology-mcp/config.yaml"]
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
- `config-power-user.yaml` — HTTPS, custom timeouts, logging
- `config-docker.yaml` — environment-variable-driven

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
uv run pytest                              # Run tests
uv run pytest -m integration               # Integration tests (requires NAS)
```

## Design Docs

Detailed specs live in `docs/specs/`:
- `architecture.md` — layered architecture, auth strategy, session lifecycle
- `filestation-module-spec.md` — all 12 File Station tools
- `config-schema-spec.md` — YAML config structure and validation
- `project-scaffolding-spec.md` — repo structure, CI, testing

## Acknowledgements

This project was built using a **Spec-First Coding** approach — a human-AI collaboration model where design precedes implementation and specs are the contract between the two.

Unlike vibe coding, where you describe what you want and let the AI generate code on the fly, spec-first coding treats design as a separate, deliberate phase. The four specs in `docs/specs/` were developed through extended conversation — exploring trade-offs, rejecting alternatives, and documenting decisions with rationale. Only after the specs were complete did implementation begin, with the specs serving as the source of truth across 11 build phases.

The result: every line of code traces back to a design decision that was made intentionally, not improvised.

## License

MIT

---

Copyright (c) 2026 Chris Means
