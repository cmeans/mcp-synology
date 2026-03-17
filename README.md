# synology-mcp

MCP server for Synology NAS devices. Exposes Synology DSM API functionality as MCP tools that Claude can use.

## Features

- **File Station** — browse, search, move, copy, delete, and organize files on your NAS
- **Permission tiers** — READ (safe browsing) or WRITE (file operations), configured per module
- **2FA support** — device token flow for accounts with two-factor authentication
- **Secure credentials** — OS keyring integration (macOS Keychain, Windows Credential Manager, Linux Secret Service)

## Quick Start

### 1. Install & run setup

```bash
uvx --from git+https://github.com/cmeans/synology-mcp synology-mcp setup --config ~/.config/synology-mcp/config.yaml
```

### 2. Create a config file

```yaml
# ~/.config/synology-mcp/config.yaml
schema_version: 1

connection:
  host: 192.168.1.100

modules:
  filestation:
    enabled: true
    permission: write
```

### 3. Add to Claude Desktop

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

## Configuration

See `examples/` for sample configs:
- `config-minimal.yaml` — quick start
- `config-power-user.yaml` — HTTPS, custom settings, logging
- `config-docker.yaml` — environment-variable-driven

## Design Docs

Detailed specs live in `docs/specs/`:
- `architecture.md` — layered architecture, auth strategy, session lifecycle
- `filestation-module-spec.md` — all 12 File Station tools
- `config-schema-spec.md` — YAML config structure and validation
- `project-scaffolding-spec.md` — repo structure, CI, testing

## Debugging

Three ways to enable debug logging, from most to least convenient:

```bash
synology-mcp check -v                  # --verbose flag on setup/check commands
SYNOLOGY_LOG_LEVEL=debug synology-mcp serve  # env var, works for all commands
```

Or set it persistently in your config file:

```yaml
logging:
  level: debug
  file: ~/.local/state/synology-mcp/primary/server.log  # optional, logs to stderr by default
```

Debug output includes every DSM API request/response (passwords masked), credential resolution steps, config discovery, version negotiation, and module registration decisions. Each log line includes the full module path (e.g., `synology_mcp.core.client`) for traceability.

## Development

```bash
uv sync --extra dev                              # Install dependencies
uv run ruff check src/ tests/              # Lint
uv run ruff format --check src/ tests/     # Format check
uv run mypy src/                           # Type check
uv run pytest                              # Run tests
uv run pytest -m integration               # Integration tests (requires NAS)
```

## Acknowledgements

This project was built using a **Spec-First Coding** approach — a human-AI collaboration model where design precedes implementation and specs are the contract between the two.

Unlike vibe coding, where you describe what you want and let the AI generate code on the fly, spec-first coding treats design as a separate, deliberate phase. The four specs in `docs/specs/` were developed through extended conversation — exploring trade-offs, rejecting alternatives, and documenting decisions with rationale. Only after the specs were complete did implementation begin, with the specs serving as the source of truth across 11 build phases.

The result: every line of code traces back to a design decision that was made intentionally, not improvised.

## License

MIT

---

Copyright (c) 2026 Chris Means
