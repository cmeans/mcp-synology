# Credential Storage

synology-mcp stores credentials in your OS keyring (macOS Keychain, Windows Credential Manager, Linux Secret Service / GNOME Keyring / KDE Wallet).

## Keyring Service Name

Credentials are stored under the service name:

```
synology-mcp/{instance_id}
```

Where `instance_id` is:
- The value of `instance_id` in your config file, if set explicitly (any `[a-z0-9-]` string)
- Derived from `connection.host` otherwise:
  - IP addresses: dots become hyphens (`192.168.1.100` -> `192-168-1-100`)
  - Hostnames: first component only (`nas.local` -> `nas`)

The `instance_id` is the key that separates everything — keyring entries, state files, and log identification. Choose a meaningful name if you manage multiple NAS devices.

## Stored Keys

Each service stores up to three keys:

| Key | Description |
|-----|-------------|
| `username` | DSM login username |
| `password` | DSM login password |
| `device_id` | 2FA device token (only present after 2FA bootstrap) |

## 2FA Device Tokens

For accounts with two-factor authentication enabled:

1. Run `synology-mcp setup` — it detects 2FA (DSM error 403) and prompts for your OTP code
2. On successful OTP, DSM returns a device token which is stored as `device_id` in the keyring
3. Subsequent logins include the device token, so DSM treats the server as a remembered device — no OTP required
4. If the device token expires or is revoked in DSM, run `synology-mcp setup` again to re-bootstrap

The device token is specific to the `instance_id`. Multiple NAS configs with different 2FA accounts each get their own token.

## Platform Support

| Platform | Keyring Backend | Claude Desktop | Notes |
|----------|----------------|----------------|-------|
| macOS | Keychain | Works | May prompt once for keychain access |
| Windows | Credential Manager | Works | Runs as logged-in user |
| Linux | GNOME Keyring / KWallet | Works | Uses standard D-Bus socket path |
| Docker | None | N/A | Use env vars or config file credentials |

### Linux and Claude Desktop

On Linux, keyring backends communicate via D-Bus. When Claude Desktop launches the MCP server, the subprocess may not inherit the `DBUS_SESSION_BUS_ADDRESS` environment variable. synology-mcp handles this by checking for the standard systemd socket at `/run/user/<uid>/bus` and setting the env var if the socket exists.

No special configuration is needed — keyring works from Claude Desktop on Linux with standard systemd-based desktop environments.

## Inspecting Keyring Entries

You can inspect stored credentials using your OS keyring tools or Python's `keyring` CLI:

```bash
# Check what's stored for a given instance
python -m keyring get synology-mcp/192-168-1-100 username

# Or using the keyring CLI directly
keyring get synology-mcp/nas-primary username

# Check if a device token is stored (2FA)
keyring get synology-mcp/nas-primary device_id
```

## Removing Credentials

```bash
keyring del synology-mcp/192-168-1-100 username
keyring del synology-mcp/192-168-1-100 password
keyring del synology-mcp/192-168-1-100 device_id
```

## Credential Resolution Order

When authenticating, synology-mcp checks these sources in order:

1. **Environment variables** (highest priority) — `SYNOLOGY_USERNAME`, `SYNOLOGY_PASSWORD`, `SYNOLOGY_DEVICE_ID`
2. **Config file** — `auth.username`, `auth.password` — triggers a plaintext warning
3. **OS keyring** (default) — set via `synology-mcp setup`

Explicit sources (env vars, config file) override the implicit default (keyring). This means setting `SYNOLOGY_PASSWORD=x` will always use that password, even if the keyring has a different one stored.

## Multiple NAS Configurations

Each NAS gets its own keyring entry keyed by `instance_id`. Use any meaningful name:

```yaml
# ~/.config/synology-mcp/nas-primary.yaml
instance_id: nas-primary
connection:
  host: 192.168.1.100

# ~/.config/synology-mcp/nas-backup.yaml
instance_id: nas-backup
connection:
  host: 192.168.1.200
```

Their credentials are stored independently:
- `synology-mcp/nas-primary` — username, password, device_id
- `synology-mcp/nas-backup` — username, password, device_id

Run `synology-mcp setup --list` to see all configured instances.
