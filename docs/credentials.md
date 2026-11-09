# Credential Storage

mcp-synology stores credentials in your OS keyring (macOS Keychain, Windows Credential Manager, Linux Secret Service / GNOME Keyring / KDE Wallet).

## Keyring Service Name

Credentials are stored under the service name:

```
mcp-synology/{instance_id}
```

Where `instance_id` is:
- The value of `instance_id` in your config file, if set explicitly (up to 63 characters of `[a-z0-9-]`)
- Derived from `connection.host` otherwise:
  - IP addresses: dots become hyphens (`192.168.1.100` -> `192-168-1-100`)
  - Hostnames: first component only (`nas.local` -> `nas`)

The `instance_id` is the key that separates everything — keyring entries, session names, and log identification. Choose a meaningful name if you manage multiple NAS devices.

## Stored Keys

Each service stores up to four keys:

| Key | Description |
|-----|-------------|
| `username` | DSM login username |
| `password` | DSM login password |
| `device_id` | 2FA device token (only present after 2FA bootstrap) |
| `host` | The host and port these are for, as `host:port` (not a secret) |

## Credentials Are Tied to a Host

The entry is looked up by `instance_id`, and a derived one is only the host's first part, so `nas.home` and `nas.office` would share an entry. So `setup` records the host and port beside the password, and any login using a username or password from the keyring compares them with `connection.host` and `connection.port`. The port is the one in use, so leaving it to the default compares equal with writing the default out. Two NAS behind one name on different ports need their own `instance_id`, and moving one NAS to a new port, or to HTTPS with the default port, means running `setup --config <your config file>` again and replacing the entry, which removes any 2FA device token. On a mismatch, no credential is sent, and the error says what to do. When it refuses, `mcp-synology check` also says which host the entry was set up for. There's no setting to skip this. It doesn't cover a proxy set in the environment, which the request goes through to the right host (#558).

- **Entries from an older version** have no recorded host. It's recorded when DSM accepts the entry's password on a login with the config named by `--config` and its full path, with none of `SYNOLOGY_HOST`, `SYNOLOGY_PORT` and `SYNOLOGY_INSTANCE_ID` set. Otherwise, including from the default location, `MCP_SYNOLOGY_CONFIG`, a relative path or a literal `~/...` in a client's args, the login is refused. **If you know these are that NAS's own credentials,** record it once by running `mcp-synology check --config <full path to your config file>` at a terminal, with no `SYNOLOGY_PASSWORD` set and no `auth.password` in the file: it says when it's recorded, and keeps any 2FA device token. With a config on plain HTTP, set `SYNOLOGY_HTTPS` as your client's `env` block does, or the password goes in cleartext. It asks first, default No, and with no one at a terminal to answer it sends nothing: the stored password goes to the host in that file. Then keep your client's config as it was. **Otherwise,** give this NAS its own `instance_id` in the config, then run `setup --config <your config file>`, which asks for the password and leaves the entry alone, as it may be another NAS's.
- **An entry recorded for another host:** `check` names that host. If it's this NAS's old address, run `setup --config <your config file>` and replace the entry. If it's another NAS, give this one its own `instance_id`, then run `setup`, which leaves that entry alone. Plain `setup` on a shared entry would replace the other NAS's credentials.
- **`SYNOLOGY_INSTANCE_ID` in a client's `env` block** overrides the config's `instance_id`, so a new ID goes there, and `setup` runs with that same new value.
- **Variables in a client's `env` block** aren't in your terminal. If it sets `SYNOLOGY_HOST`, `SYNOLOGY_INSTANCE_ID`, `SYNOLOGY_PORT` or `SYNOLOGY_HTTPS`, run `setup` with the same value, or it stores for the file's host or instance, or can't connect, and the server is refused again. An entry with a password and no username also needs `SYNOLOGY_USERNAME` at the terminal. Run `check` with the same `SYNOLOGY_PORT` and `SYNOLOGY_HTTPS` too: with `SYNOLOGY_HTTPS` left out, it could send the stored password over plain HTTP.
- **A username in the config or in `SYNOLOGY_USERNAME`** is the one the server logs in as, ahead of the keyring's. `setup` offers it as the default and refuses any other answer, before anything is sent or stored: otherwise it would store another account's password for that username, and the server's first login would fail. To use another account, change that setting first.
- **`setup` asks first** before replacing an entry recorded for another host or port, or with no host or port recorded. Declining changes nothing, and so does a keyring that can't be read at that point. Replacing it means any other config using that entry is refused until it's set up again, and the entry's 2FA device token is removed.
- **`setup` writes the entry only after DSM accepts the login.** A mistyped password, a wrong code or an unreachable NAS leaves the entry as it was, and `setup` exits 1. Each failed login counts toward DSM's Auto Block, so verify the password or code before retrying. If the login works but the keyring refuses a write, `setup` restores the entry where it can and exits 0 with the variables to set when a fresh read shows nothing changed or the entry couldn't be read before writing, or 1 otherwise, or when a new 2FA token couldn't be stored. A new 2FA device token replaces the stored one. With no new token, the stored one stays for the same username and goes for another.
- **After changing the config or an `env` block,** restart the MCP client, as the server reads them once. Recording the host or storing credentials needs no restart.
- **A NAS reached by two addresses** (LAN and DDNS) uses a second config with its own `instance_id`.
- **Credentials from environment variables or the config file** don't use the keyring's username or password, so they aren't refused. A device token in the keyring is sent beside them only if the entry is recorded for this host; otherwise it's left out, with a warning, and a 2FA account gets DSM's own error. Such a login never records a host, since it uses none of the entry's password. The warning says what to do. A username from the keyring with a password from elsewhere is refused like a mismatch, and doesn't record a host either.

## 2FA Device Tokens

For accounts with two-factor authentication enabled:

1. Run `mcp-synology setup` (with `--config <your config file>` if you already have one) — it detects 2FA (DSM error 403) and prompts for your OTP code
2. On successful OTP, DSM returns a device token which is stored as `device_id` in the keyring
3. Subsequent logins include the device token, so DSM treats the server as a remembered device — no OTP required
4. Device tokens have no fixed lifetime. If DSM stops accepting the stored token (for example, the trusted device was removed in **Personal → Security → Sign-in Activity**), the error says DSM rejected it; run `mcp-synology setup --config <your config file>` again to sign in with a new 2FA code

The device token is specific to the `instance_id`. Multiple NAS configs with different 2FA accounts each get their own token.

### 2FA With No Keyring

On a host with no keyring, such as Docker, `setup` writes the config and lists the variables to set, but doesn't log in, so it can't ask for a code. It never prints a device token. To use a 2FA account there:

1. Copy the config file to a machine with a keyring, and run `mcp-synology setup --config <your config file>` there, so it has the same `instance_id`. With no config file, set the same `SYNOLOGY_HOST`, `SYNOLOGY_HTTPS` and `SYNOLOGY_PORT` there and run `mcp-synology setup`.
2. Read the token there, in your own terminal rather than through a chat: `uvx keyring get mcp-synology/<instance_id> device_id`.
3. Set it in `SYNOLOGY_DEVICE_ID` on the host with no keyring, beside `SYNOLOGY_USERNAME` and `SYNOLOGY_PASSWORD`.

## Platform Support

| Platform | Keyring Backend | Claude Desktop | Notes |
|----------|----------------|----------------|-------|
| macOS | Keychain | Works | May prompt once for keychain access |
| Windows | Credential Manager | Works | Runs as logged-in user |
| Linux | GNOME Keyring / KWallet | Works | Uses standard D-Bus socket path |
| Docker | None | N/A | Use env vars or config file credentials; for 2FA, see [above](#2fa-with-no-keyring) |

### Linux and Claude Desktop

On Linux, keyring backends communicate via D-Bus. When Claude Desktop launches the MCP server, the subprocess may not inherit the `DBUS_SESSION_BUS_ADDRESS` environment variable. mcp-synology handles this by checking for the standard systemd socket at `/run/user/<uid>/bus` and setting the env var if the socket exists.

No special configuration is needed — keyring works from Claude Desktop on Linux with standard systemd-based desktop environments.

## Inspecting Keyring Entries

You can inspect stored credentials using your OS keyring tools or Python's `keyring` CLI:

```bash
# Check what's stored for a given instance
uvx keyring get mcp-synology/192-168-1-100 username

# Check if a device token is stored (2FA)
uvx keyring get mcp-synology/nas-primary device_id
```

## Removing Credentials

```bash
uvx keyring del mcp-synology/192-168-1-100 username
uvx keyring del mcp-synology/192-168-1-100 password
uvx keyring del mcp-synology/192-168-1-100 device_id
uvx keyring del mcp-synology/192-168-1-100 host
```

## Credential Resolution Order

When authenticating, mcp-synology checks these sources in order:

1. **Environment variables** (highest priority) — `SYNOLOGY_USERNAME`, `SYNOLOGY_PASSWORD`, `SYNOLOGY_DEVICE_ID`
2. **Config file** — `auth.username`, `auth.password` — a password here triggers a plaintext warning at startup. A username alone doesn't: a config username with the password in the keyring is the recommended setup
3. **OS keyring** (default) — set via `mcp-synology setup`

Explicit sources (env vars, config file) override the implicit default (keyring). This means setting `SYNOLOGY_PASSWORD=x` will always use that password, even if the keyring has a different one stored.

## Multiple NAS Configurations

Each NAS gets its own keyring entry keyed by `instance_id`. Use any meaningful name:

```yaml
# ~/.config/mcp-synology/nas-primary.yaml
instance_id: nas-primary
connection:
  host: 192.168.1.100

# ~/.config/mcp-synology/nas-backup.yaml
instance_id: nas-backup
connection:
  host: 192.168.1.200
```

Their credentials are stored independently:
- `mcp-synology/nas-primary` — username, password, device_id, host
- `mcp-synology/nas-backup` — username, password, device_id, host

Run `mcp-synology setup --list` to see all configured instances.
