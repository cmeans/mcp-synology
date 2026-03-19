# synology-mcp

MCP server for Synology NAS devices. Exposes Synology DSM API functionality as MCP tools that Claude can use.

## Supported Modules

### File Station

Browse, search, move, copy, delete, and organize files on your NAS. 12 tools across two permission tiers:

- **READ** — list_shares, list_files, list_recycle_bin, search_files, get_file_info, get_dir_size
- **WRITE** — create_folder, rename, copy_files, move_files, delete_files, restore_from_recycle_bin

### System

Monitor NAS health and resource utilization. 2 read-only tools:

- **get_system_info** — model, firmware version, RAM, temperature, uptime (works for all users)
- **get_resource_usage** — live CPU load, memory usage, disk I/O, network throughput (requires admin account)

## Features

- **Interactive setup** — guided configuration that creates your config, stores credentials, handles 2FA, and emits a Claude Desktop snippet
- **Permission tiers** — READ or WRITE per module, enforced at tool registration
- **2FA support** — auto-detected; device token bootstrap with automatic silent re-auth
- **Secure credentials** — OS keyring integration that works transparently on macOS, Windows, and Linux (including from Claude Desktop). See [docs/credentials.md](docs/credentials.md).
- **Multi-NAS** — manage multiple NAS devices with separate configs, credentials, and state

## Quick Start

### 1. Install

```bash
uv tool install synology-mcp
```

Installs the `synology-mcp` command globally from [PyPI](https://pypi.org/project/synology-mcp/). Requires [uv](https://docs.astral.sh/uv/).

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

The config file name (e.g., `nas.yaml`) also serves as a natural identifier for the connection — you can name it to match your NAS (e.g., `home-nas.yaml`, `office-nas.yaml`).

On Linux, the server auto-detects the D-Bus session socket for keyring access. If auto-detection fails, add `"env": {"DBUS_SESSION_BUS_ADDRESS": "unix:path=/run/user/<uid>/bus"}` to the Claude Desktop config. The setup command includes this in the generated snippet.

### 4. Verify

```bash
synology-mcp check                    # Validates credentials work
synology-mcp setup --list             # Shows all configured NAS instances
```

### Alternative: run without global install

If you prefer not to install globally, `uvx` downloads and runs the latest version on each invocation:

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

You can also use `uvx` for CLI commands:

```bash
uvx synology-mcp setup
uvx synology-mcp check
```

### Alternative: env-var-only mode

No config file needed if `SYNOLOGY_HOST` is set. This is useful for Docker or CI environments:

```json
{
  "mcpServers": {
    "synology": {
      "command": "synology-mcp",
      "args": ["serve"],
      "env": {
        "SYNOLOGY_HOST": "192.168.1.100",
        "SYNOLOGY_USERNAME": "your_user",
        "SYNOLOGY_PASSWORD": "your_password"
      }
    }
  }
}
```

Or from the CLI:

```bash
SYNOLOGY_HOST=192.168.1.100 synology-mcp check
```

## 2FA Support

synology-mcp fully supports DSM accounts with two-factor authentication. It's auto-detected — you don't need to configure anything special:

1. **Bootstrap** — `synology-mcp setup` detects 2FA, prompts for your OTP code, and stores a device token in the keyring
2. **Silent re-auth** — subsequent logins use the device token automatically (no OTP prompts)
3. **Per-instance** — each NAS config gets its own device token, so mixed 2FA/non-2FA setups work fine

Device tokens persist until you explicitly revoke them in DSM (Personal > Security > Sign-in Activity). They do not expire on their own. If a token is revoked, run `synology-mcp setup` again to re-bootstrap.

## Keyring & Credentials

Credentials are stored in the OS keyring and accessed transparently:

| Platform | Backend | Notes |
|----------|---------|-------|
| macOS | Keychain | Just works |
| Windows | Credential Manager | Just works |
| Linux | GNOME Keyring / KWallet | Auto-detects D-Bus session, works from Claude Desktop |

Credential resolution order: **env vars > config file > keyring**. Explicit sources override the implicit default.

For environments without a keyring (Docker, CI), use environment variables or inline credentials in the config file.

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

To disable update notifications, add to your config (top level):

```yaml
# ~/.config/synology-mcp/config.yaml
check_for_updates: false
```

## Configuration

Interactive setup creates a config file for you. For manual configuration or advanced options, see `examples/`:
- `config-minimal.yaml` — simplest possible config
- `config-power-user.yaml` — HTTPS, custom timeouts, logging, instructions
- `config-docker.yaml` — environment-variable-driven

### Multi-NAS

Each NAS gets its own config file, credentials, and Claude Desktop entry. The config file name serves as a natural identifier (e.g., `home-nas.yaml`, `media-server.yaml`).

Set `alias` to give Claude a display name for the connection:

```yaml
# ~/.config/synology-mcp/home-nas.yaml
alias: HomeNAS
```

The alias appears in the MCP server name (e.g., `synology-HomeNAS`) so Claude knows which NAS it's talking to.

### Custom Instructions

Custom instructions let you shape how Claude interacts with your NAS tools. This is useful when:

- **Multiple NAS connections** — tell Claude which connection to prefer for different tasks ("use this for media, use admin for cross-user operations")
- **Safety guardrails** — add rules like "always confirm before deleting" or "never touch /Backups"
- **Context** — explain what's on the NAS ("this is a media server, /video has our library sorted by genre")

**Add context** — `custom_instructions` is prepended to the built-in prompt (higher priority):

```yaml
# ~/.config/synology-mcp/config.yaml
custom_instructions: |
  This is the admin NAS with elevated privileges.
  Prefer this connection for file operations requiring cross-user access.
  Never delete files from /Backups without explicit confirmation.
```

**Full control** — `instructions_file` replaces the built-in prompt entirely. Copy the [built-in server.md](src/synology_mcp/instructions/server.md) as a starting point:

```yaml
# ~/.config/synology-mcp/config.yaml
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
# ~/.config/synology-mcp/config.yaml
logging:
  level: debug
  file: ~/.local/state/synology-mcp/nas/server.log  # optional, logs to stderr by default
```

Debug output includes every DSM API request/response (passwords masked), credential resolution steps, config discovery, version negotiation, and module registration decisions.

## Contributing

See [DEVELOPMENT.md](DEVELOPMENT.md) for build commands, testing, integration test setup, and design docs.

## Acknowledgements

This project was built using a **Spec-First Coding** approach — a human-AI collaboration model where design precedes implementation and specs are the contract between the two.

Unlike vibe coding, where you describe what you want and let the AI generate code on the fly, spec-first coding treats design as a separate, deliberate phase. The four specs in `docs/specs/` were developed through extended conversation — exploring trade-offs, rejecting alternatives, and documenting decisions with rationale. Implementation then used the specs as the source of truth across 11 build phases.

Live testing against real hardware revealed behaviors the specs couldn't anticipate (DSM API quirks, search service throttling, version format incompatibilities). These discoveries are documented in `CLAUDE.md` and the code, which is authoritative where specs diverge.

## License

[MIT](LICENSE)

---

Copyright (c) 2026 Chris Means
