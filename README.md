<p align="center">
  <picture>
    <source media="(prefers-color-scheme: dark)" srcset="https://raw.githubusercontent.com/cmeans/mcp-synology/main/src/mcp_synology/icons/mcp-synology-logo-dark.svg" width="128">
    <source media="(prefers-color-scheme: light)" srcset="https://raw.githubusercontent.com/cmeans/mcp-synology/main/src/mcp_synology/icons/mcp-synology-logo-light.svg" width="128">
    <img src="https://raw.githubusercontent.com/cmeans/mcp-synology/main/src/mcp_synology/icons/mcp-synology-logo-light.svg" alt="mcp-synology logo" width="128">
  </picture>
</p>

# mcp-synology

[![PyPI version](https://img.shields.io/pypi/v/mcp-synology)](https://pypi.org/project/mcp-synology/)
[![Python versions](https://img.shields.io/pypi/pyversions/mcp-synology)](https://pypi.org/project/mcp-synology/)
[![License](https://img.shields.io/pypi/l/mcp-synology)](https://github.com/cmeans/mcp-synology/blob/main/LICENSE)
[![Tests](https://img.shields.io/github/actions/workflow/status/cmeans/mcp-synology/ci.yml?label=tests)](https://github.com/cmeans/mcp-synology/actions/workflows/ci.yml)
[![Coverage](https://codecov.io/gh/cmeans/mcp-synology/graph/badge.svg)](https://codecov.io/gh/cmeans/mcp-synology)
[![Downloads](https://img.shields.io/endpoint?url=https%3A%2F%2Fpypi-badges.intfar.com%2Fmcp-synology%2Fdownloads-30d-non-ci.json)](https://github.com/cmeans/pypi-winnow-downloads)
[![Glama](https://glama.ai/mcp/servers/cmeans/mcp-synology/badges/score.svg)](https://glama.ai/mcp/servers/cmeans/mcp-synology)

[![pip downloads](https://img.shields.io/endpoint?url=https%3A%2F%2Fpypi-badges.intfar.com%2Fmcp-synology%2Finstaller-pip-30d-non-ci.json)](https://github.com/cmeans/pypi-winnow-downloads)
[![pipenv downloads](https://img.shields.io/endpoint?url=https%3A%2F%2Fpypi-badges.intfar.com%2Fmcp-synology%2Finstaller-pipenv-30d-non-ci.json)](https://github.com/cmeans/pypi-winnow-downloads)
[![pipx downloads](https://img.shields.io/endpoint?url=https%3A%2F%2Fpypi-badges.intfar.com%2Fmcp-synology%2Finstaller-pipx-30d-non-ci.json)](https://github.com/cmeans/pypi-winnow-downloads)
[![uv downloads](https://img.shields.io/endpoint?url=https%3A%2F%2Fpypi-badges.intfar.com%2Fmcp-synology%2Finstaller-uv-30d-non-ci.json)](https://github.com/cmeans/pypi-winnow-downloads)
[![poetry downloads](https://img.shields.io/endpoint?url=https%3A%2F%2Fpypi-badges.intfar.com%2Fmcp-synology%2Finstaller-poetry-30d-non-ci.json)](https://github.com/cmeans/pypi-winnow-downloads)
[![pdm downloads](https://img.shields.io/endpoint?url=https%3A%2F%2Fpypi-badges.intfar.com%2Fmcp-synology%2Finstaller-pdm-30d-non-ci.json)](https://github.com/cmeans/pypi-winnow-downloads)

[![linux downloads](https://img.shields.io/endpoint?url=https%3A%2F%2Fpypi-badges.intfar.com%2Fmcp-synology%2Fos-linux-30d-non-ci.json)](https://github.com/cmeans/pypi-winnow-downloads)
[![macos downloads](https://img.shields.io/endpoint?url=https%3A%2F%2Fpypi-badges.intfar.com%2Fmcp-synology%2Fos-macos-30d-non-ci.json)](https://github.com/cmeans/pypi-winnow-downloads)
[![windows downloads](https://img.shields.io/endpoint?url=https%3A%2F%2Fpypi-badges.intfar.com%2Fmcp-synology%2Fos-windows-30d-non-ci.json)](https://github.com/cmeans/pypi-winnow-downloads)

MCP server for Synology NAS devices. Exposes Synology DSM API functionality as MCP tools that Claude can use.

Tested against DSM 7.1, 7.2.2, 7.3.2 and 7.4.1: CI runs the integration tests for every module on a virtual DSM of each version. Release candidates are also live-tested on a real NAS (DSM 7.1.1) by AI clients driving the tools: for 0.10.0, Claude Code, claude.ai through the desktop app, and an agent on a remote machine, as admin, regular, read-only and restricted users. See [docs/live-testing.md](docs/live-testing.md).

<!-- mcp-name: io.github.cmeans/mcp-synology -->

## Migrating from synology-mcp

If you're upgrading from `synology-mcp` (v0.3.x or earlier), the package has been renamed. A migration script handles config, state, keyring entries, and Claude Desktop config automatically:

```bash
# Download and run the migration script
curl -O https://raw.githubusercontent.com/cmeans/mcp-synology/main/scripts/migrate-from-synology-mcp.py
python migrate-from-synology-mcp.py          # dry run — preview changes
python migrate-from-synology-mcp.py --apply  # apply changes
```

The script migrates:
- Config directory (`~/.config/synology-mcp/` → `~/.config/mcp-synology/`)
- State directory (`~/.local/state/synology-mcp/` → `~/.local/state/mcp-synology/`)
- Keyring credentials
- Claude Desktop `claude_desktop_config.json` (updates command and paths)

See [CHANGELOG.md](CHANGELOG.md) for full details on breaking changes.

## Supported Modules

### File Station

Browse, search, transfer, and manage files on your NAS. 15 tools across two permission tiers:

- **READ** — list_shares, list_files, list_recycle_bin, search_files, get_file_info, get_dir_size, download_file
- **WRITE** — create_folder, rename, copy_files, move_files, delete_files, restore_from_recycle_bin, upload_file, file_task

`download_file` of a folder saves DSM's ZIP of it as `<folder>.zip` (or under `filename`, when given). A whole shared folder can't be downloaded this way (DSM doesn't send one), so it's refused.

### System

Monitor NAS health and resource utilization. 2 read-only tools:

- **get_system_info** — everything DSM reports. Every account gets the model, serial number, DSM version, RAM, uptime, the NAS's local time and code page, and the temperature where the NAS has a sensor. An admin account also gets the CPU, time zone and current UTC offset, NTP server, firmware date, DSM's update-ready and crash flags, eSATA port, and attached USB, SATA and PCIe devices, plus the network-card flag on DSM 7.2.2 and later and redundant power on models that report it; any other account gets a line naming what needs an administrator
- **get_resource_usage** — live CPU load, memory usage, disk I/O, network throughput (requires admin account)

`mcp-synology setup` writes only the File Station module. To add System, put it inside the existing `modules:` section (it has no write tools, so no `permission` is needed):

```yaml
modules:
  system:
    enabled: true
```

### Download Station

Manage the Download Station queue and settings. 12 tools across two permission tiers:

- **READ** — list_downloads, get_download_info, get_download_stats, get_download_config, get_schedule
- **WRITE** — create_download, delete_download, pause_download, resume_download, edit_download, set_download_config, set_schedule

Download Station is opt-in: `mcp-synology setup` only configures File Station. To turn it on:

1. **Install Download Station on the NAS** from Package Center, if it isn't installed already. Without the package the tools still appear, but every call fails with [`api_not_found`](docs/error-codes.md#api_not_found).
2. **Edit your config file.** That's `~/.config/mcp-synology/config.yaml`, or the per-NAS file such as `home-nas.yaml` if you use [Multi-NAS](#multi-nas). Add `downloadstation` **inside the existing `modules:` section**; don't add a second `modules:` key. Use `permission: read` for the read tools only, or `permission: write` for all 12:

   ```yaml
   modules:
     filestation:          # already there
       enabled: true
       permission: write
     downloadstation:      # add this
       enabled: true
       permission: write   # or read
   ```

3. **Check the config:** `mcp-synology check --config <your config file>` reports YAML or validation mistakes and confirms the login still works.
4. **Restart your MCP client** (for example, Claude Desktop). The server reads its config only at startup. The Download Station tools (`list_downloads` and the rest) then appear alongside the File Station ones. Ask the assistant to "list my downloads" to try it.

If the account isn't an admin, read **Accounts** below first: a non-admin needs a one-time setup step before Download Station will run its tasks.

**Accounts.** An admin (Download Station manager) can use every tool. A non-admin account needs the Download Station application privilege (**Control Panel → User & Group → [account] → Edit → Applications**). It also has these limits, all enforced or reported by the tools:

- **First-time setup.** Download Station doesn't start a non-admin's tasks until that user has a default destination; until then they wait forever. A user who has never opened Download Station has none. Set it once with `set_download_config(default_destination="<share>")`, which DSM applies to that user only. Until then, `create_download` with an explicit `destination` still creates the task but warns that it won't start. Without a `destination`, it refuses and says what to set. That refusal applies to any account, admin included, whenever there's no default destination to fall back on.
- **Own tasks only.** `list_downloads` and the other task tools see only the user's own tasks.
- **Settings.** `get_download_config` shows the user's own view, not the NAS-wide settings. `set_download_config` can change only the user's own `default_destination`; the speed limits are NAS-wide and are refused, because DSM accepts and then silently ignores them from a non-admin.
- **Schedule.** The weekly plan and limited-speed rates in `get_schedule` / `set_schedule` need an admin. A non-admin can still turn the schedule on or off, and that switch is NAS-wide.

**Torrent files.** `create_download` with `torrent_file_path` uploads a local `.torrent` or `.nzb` file, and only from the allowed local folders: by default your Downloads folder. Anything else is refused before the file is read, including a link that points to another kind of file. To use another folder, list it in `allowed_local_paths`, either at the top level of the config (every module) or under this module's settings. A module's own list adds to the top-level list when that's set. Without a top-level list, it **replaces** the Downloads default, so include `~/Downloads` if you still want it.

```yaml
modules:
  downloadstation:
    enabled: true
    permission: write
    settings:
      allowed_local_paths:
        - ~/Downloads   # keep the default folder too
        - ~/Torrents
```

See **Local path confinement** under [Features](#features). Magnet links and URLs (`uri`) aren't affected.

**Known limits:**

- A `destination` must be an existing folder: Download Station doesn't create missing folders. Create it first with File Station's `create_folder` (`/video/TV/New show`; the `filestation` module needs `enabled: true` and `permission: write`) or in DSM's own File Station, then pass it share-relative (`video/TV/New show`). An error that says the destination doesn't exist names this fix (#296).
- `create_download` returns no task IDs for URLs that use `username` / `password`.
- `edit_download` changes only the destination.
- There is no way to pick individual files inside a torrent.
- BT search and RSS feeds are not supported yet.
- `delete_download(force_complete=true)` doesn't reliably keep partial data: DSM discarded unfinished HTTP downloads anyway. DSM also refuses it for finished tasks, and the tool reports those as not removed.

Tested against DSM 7.1 (Download Station 3.9-4627) and DSM 7.2.2, 7.3.2 and 7.4.1 (Download Station 4.1-5012).

### Synology Photos

Browse, search, organize and share a Synology Photos library, in your personal space and in Shared Space. 19 tools across two permission tiers:

- **READ** — list_photos, list_photo_collections, get_photo, download_photos
- **WRITE** — upload_photos, photo_album, photo_share_link, photo_folder, copy_photos, move_photos, photo_task, tag_photos, edit_photo, edit_person, photo_request, merge_people, delete_photos, delete_photo_folders, delete_photo_albums

Photos is opt-in, like Download Station: install **Synology Photos** from Package Center, then add `photos` inside the existing `modules:` section, check the config with `mcp-synology check`, and restart your MCP client.

```yaml
modules:
  photos:
    enabled: true
    permission: write   # or read
```

**Accounts.** The account needs the Synology Photos application privilege (**Control Panel → Application Privileges → Synology Photos**), and the personal space needs the user home service (**Control Panel → User & Group → Advanced → User Home**). Shared Space must be enabled in Synology Photos' settings, with access granted to the account. The tools report each of these by name when it's missing.

**Good to know:**

- **Edits change the original files.** Photos writes ratings, descriptions, tags, taken-time changes and rotations into the files themselves, so their dates and sizes change; backup and sync tools see new versions.
- **Tags are given by name.** A tag exists only while some item has it, so removing it from every item deletes it.
- **Deletes** go to the recycle bin of the share that holds the space, when it has one; Photos has no trash of its own. A large copy, move or delete hands off with a `photos:<id>` handle that `photo_task` follows up on.
- **Favorites and concepts** (things and scenes Photos recognized) need Synology Photos 1.9.1 (DSM 7.2); on 1.3.4 they're refused with `not_supported`.
- **Local files.** `upload_photos` reads and `download_photos` writes only inside the allowed local folders (by default your Downloads folder); see the Download Station torrent-file notes above for `allowed_local_paths`. `task_timeout` (seconds, default 30, from 1 to 600; under `photos:` → `settings:`) sets how long a copy, move or delete waits before handing off, and when `upload_photos` stops starting new files (at most 45 s, below Claude Desktop's tool-call timeout of about 60 s; it lists the ones not tried, for another call).
- **Not covered yet:** Shared Space folder sharing, face-level People edits, guest uploads to photo requests, and changing Photos' own settings.

Tested against DSM 7.1 (Synology Photos 1.3.4), DSM 7.2.2, 7.3.2 and 7.4.1 (Synology Photos 1.9.1). See [`docs/specs/photos-module-spec.md`](docs/specs/photos-module-spec.md) for what each tool sends and the DSM behavior behind it.

## Features

- **Interactive setup** — guided configuration that creates your config, stores credentials, handles 2FA, and emits a Claude Desktop snippet
- **Permission tiers** — READ or WRITE per module, enforced at tool registration. Tiers control what the server can do on the NAS. Tools that write on this machine (`download_file`, `download_photos`) are annotated as destructive so your client can confirm them, and so is every WRITE tool except `create_folder`, `rename`, `upload_photos`, `create_download`, `pause_download` and `resume_download`. A read-only connection tells the model which tools it withholds and how to enable them, so the model doesn't claim the server can't do it
- **Long operations keep running** — a large delete, copy, move or recycle-bin restore reports progress while the call waits, and if it's still running when the wait ends (30 s by default) the NAS keeps working on it: the reply gives a handle for `file_task`, which checks on it, lists running operations or stops one. See [File Station settings](#file-station-settings). Photos' `copy_photos`, `move_photos`, `delete_photos` and `delete_photo_folders` also hand off, after `task_timeout` (default 30 s, up to 600), with a `photos:<id>` handle for `photo_task`
- **Local path confinement** — `upload_file`, `download_file`, `upload_photos`, `download_photos` and `create_download`'s torrent files only use local folders listed in `allowed_local_paths`, by default your Downloads folder. Set it at the top level of the config for every module, or per module in that module's settings (File Station, Download Station and Photos each have their own list). A module's own list adds to the top-level list when that's set. Without a top-level list, it **replaces** the Downloads default, so include `~/Downloads` if you still want it. `create_download` only reads `.torrent` and `.nzb` files. Symlinks and `..` are resolved first, so they can't lead outside. Set it to `["/"]` to allow everything (macOS/Linux)
- **2FA support** — auto-detected; device token bootstrap with automatic silent re-auth
- **Secure credentials** — OS keyring integration that works transparently on macOS, Windows, and Linux (including from Claude Desktop). See [docs/credentials.md](docs/credentials.md).
- **Multi-NAS** — manage multiple NAS devices with separate configs, credentials, and state

## Quick Start

### 1. Run setup

```bash
uvx mcp-synology setup
```

Requires [uv](https://docs.astral.sh/uv/). `uvx` downloads the latest version and caches it — no separate install step needed. It checks PyPI again once its cached copy of the index is more than 10 minutes old, so a restart of your MCP client usually picks up a new release; see [Updates](#updates).

Setup will prompt for your NAS host, credentials, and preferences. If your account has 2FA enabled, it will prompt for an OTP code and store a device token for automatic future logins.

At the end, it prints a Claude Desktop JSON snippet ready to copy-paste.

### 2. Add to Claude Desktop

Copy the snippet from setup into your `claude_desktop_config.json` and restart Claude Desktop. It will look something like:

```json
{
  "mcpServers": {
    "synology-nas": {
      "command": "uvx",
      "args": ["mcp-synology", "serve", "--config", "~/.config/mcp-synology/nas.yaml"]
    }
  }
}
```

The config file name (e.g., `nas.yaml`) also serves as a natural identifier for the connection — you can name it to match your NAS (e.g., `home-nas.yaml`, `office-nas.yaml`).

On Linux, the server auto-detects the D-Bus session socket for keyring access. If auto-detection fails, add `"env": {"DBUS_SESSION_BUS_ADDRESS": "unix:path=/run/user/<uid>/bus"}` to the Claude Desktop config. The setup command includes this in the generated snippet.

### 3. Verify

```bash
uvx mcp-synology check                # Validates credentials work
uvx mcp-synology setup --list         # Shows all configured NAS instances
```

### Alternative: global install

If you prefer a persistent install (avoids download on each invocation). Requires [uv](https://docs.astral.sh/uv/getting-started/installation/) — install with `curl -LsSf https://astral.sh/uv/install.sh | sh` (macOS/Linux) or see the [installation guide](https://docs.astral.sh/uv/getting-started/installation/) for other options.

```bash
uv tool install mcp-synology
mcp-synology setup
mcp-synology check
```

### Alternative: env-var-only mode

No config file needed if `SYNOLOGY_HOST` is set. This is useful for Docker or CI environments:

```json
{
  "mcpServers": {
    "synology": {
      "command": "uvx",
      "args": ["mcp-synology", "serve"],
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
SYNOLOGY_HOST=192.168.1.100 uvx mcp-synology check
```

Without a config file, mcp-synology always runs **File Station, read-only**. No environment variable enables another module or the write tools; for those, use a config file (the same variables then override its values). The variables:

| Variable | Sets | Default |
|---|---|---|
| `SYNOLOGY_HOST` | `connection.host` (needed for env-only mode) | — |
| `SYNOLOGY_PORT` | `connection.port` | 5000, or 5001 with HTTPS |
| `SYNOLOGY_HTTPS` | `connection.https` (`true`, `1` or `yes` for on; anything else is off) | `false` |
| `SYNOLOGY_USERNAME` | `auth.username` | the keyring |
| `SYNOLOGY_PASSWORD` | `auth.password` | the keyring |
| `SYNOLOGY_DEVICE_ID` | `auth.device_id` (2FA device token) | the keyring |
| `SYNOLOGY_INSTANCE_ID` | `instance_id` (names the keyring entry and state folder) | derived from the host |
| `SYNOLOGY_LOG_LEVEL` | `logging.level` (`debug`, `info`, `warning`, `error`) | `info` |
| `SYNOLOGY_UPDATE_NOTICES` | `update_notices` (`all`, `security`, `off`) | `all` |

## 2FA Support

mcp-synology fully supports DSM accounts with two-factor authentication. It's auto-detected — you don't need to configure anything special:

1. **Bootstrap** — `mcp-synology setup` detects 2FA, prompts for your OTP code, and stores a device token in the keyring
2. **Silent re-auth** — subsequent logins use the device token automatically (no OTP prompts)
3. **Per-instance** — each NAS config gets its own device token, so mixed 2FA/non-2FA setups work fine

Device tokens have no fixed lifetime: they last until the trusted device is removed in DSM (Personal > Security > Sign-in Activity) or DSM otherwise stops accepting it. If DSM rejects the stored token, the error says so; run `mcp-synology setup` again to sign in with a new 2FA code.

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

mcp-synology checks PyPI for a newer version once a day (hourly while PyPI can't be reached), in the background when the server starts, and tells your MCP client's model, which passes it on:

- **On the first tool result** after the check finds one, whichever module the tool belongs to. An ordinary release is announced **once a week** per install: connections that share an install (one copy serving several NASes) say it once between them.
- **A security release** is announced in every session: in the server's instructions when the previous check already knew of it (so the model knows from the first message), otherwise on the first tool result.
- **Outside the chat:** the client's MCP server log gets a WARNING, and `mcp-synology check` prints it.

The notice names the new version, the command that upgrades your install (`uv tool upgrade mcp-synology`, `pipx upgrade mcp-synology`, a restart for `uvx`, with `uv cache clean mcp-synology` if it still runs the old version, or `pip install -U`), and the release notes.

Each config (connection) chooses what it hears, since configs may run different installs and versions:

```yaml
update_notices: all        # all (default) | security | off
```

`security` keeps only security releases; `off` stops every notice in the chat (the log line and `check` still report it). `check_for_updates: false` turns off the check altogether.

To hear about releases without running the server, watch the repository on GitHub (**Watch → Custom → Releases**), or follow [PyPI](https://pypi.org/project/mcp-synology/) or the [MCP Registry](https://registry.modelcontextprotocol.io/v0/servers?search=io.github.cmeans/mcp-synology).

To manage updates from the CLI:

```bash
mcp-synology --check-update                 # Check for a newer version
mcp-synology --auto-upgrade enable           # Auto-upgrade on each interactive run
mcp-synology --revert                        # Roll back to previous version
mcp-synology --revert 0.1.0                  # Roll back to a specific version
```

## Configuration

Interactive setup creates a config file for you. For manual configuration or advanced options, see `examples/`:
- `config-minimal.yaml` — simplest possible config
- `config-power-user.yaml` — HTTPS, custom timeouts, logging, instructions
- `config-docker.yaml` — environment-variable-driven

The full reference is [`docs/specs/config-schema-spec.md`](docs/specs/config-schema-spec.md). After editing a config, run `mcp-synology check --config <file>`: it reports YAML and validation mistakes, names misspelled settings and modules, then tries to log in.

### Modules and permissions

Each module is enabled under `modules:`, with three possible keys: `enabled`, `permission` and `settings`. Any other key there is an error, so a typo such as `permision` stops the server instead of being ignored.

- **`permission`** is `read` (the default, when it's left out) or `write`. `read` registers the module's READ tools only; `write` adds its WRITE tools. `admin` is also accepted and is the same as `write` today. It doesn't refer to the DSM account: an admin account doesn't need it.
- **`settings`** holds the module's own options (File Station's are [below](#file-station-settings)). A setting the module doesn't know is ignored with a warning that names the valid ones, so a config written for a newer version still loads.
- A module that isn't listed isn't loaded. Download Station and Synology Photos are opt-in (see their sections above); System has no write tools.

### Connection and HTTPS

```yaml
connection:
  host: 192.168.1.100    # or a hostname
  https: true            # default false: credentials then cross the network in cleartext
  port: 5001             # default 5000, or 5001 with https: true
  verify_ssl: false      # default true; false for DSM's default self-signed certificate
  timeout: 30            # seconds per request, 1 to 600 (default 30)
```

Use `https: true` unless the NAS is on a network you trust: with plain HTTP, the login and every request travel unencrypted, and the server logs a warning saying so. `setup` writes `https: true` unless you decline. [docs/https.md](docs/https.md) covers DSM's self-signed certificate and how to get a trusted one. If DSM uses its own self-signed certificate, set `verify_ssl: false` (the server warns that verification is off), or install a certificate the machine trusts and keep it `true`.

### Multi-NAS

Each NAS gets its own config file, credentials, and Claude Desktop entry. The config file name serves as a natural identifier (e.g., `home-nas.yaml`, `media-server.yaml`).

Set `alias` to give Claude a display name for the connection:

```yaml
# ~/.config/mcp-synology/home-nas.yaml
alias: HomeNAS
```

The alias appears in the MCP server name (e.g., `synology-HomeNAS`) so Claude knows which NAS it's talking to.

### File Station settings

File Station settings go under `settings:` in the `filestation` module. All are optional:

```yaml
modules:
  filestation:
    enabled: true
    permission: write
    settings:
      delete_timeout: 120        # wait up to 2 minutes before handing a delete off
      copy_move_timeout: 300     # copy, move and restore: 5 minutes
      async_timeout: 180         # search_files and get_dir_size
      allowed_local_paths:
        - ~/Downloads
        - ~/Media
      default_download_dir: ~/Downloads
```

Timeouts are in seconds (10–3600 accepted) and count wall-clock time:

| Setting | Default | What it does |
|---------|---------|--------------|
| `delete_timeout` | `30` | How long `delete_files` waits before handing off. |
| `copy_move_timeout` | `30` | How long `copy_files`, `move_files` and `restore_from_recycle_bin` wait before handing off. |
| `async_timeout` | `120` | Time limit for `search_files` and `get_dir_size`. It doesn't apply to delete, copy, move or restore. |
| `search_timeout`, `dir_size_timeout` | `async_timeout` | Per-tool time limits for `search_files` and `get_dir_size`. The NAS task is stopped when the limit runs out. |
| `upload_timeout`, `download_timeout` | `300` | Time limits for `upload_file` and `download_file`. |

For delete, copy, move and restore, the timeout is **how long a call waits before handing off**, not a limit on the operation. An operation that finishes within the wait returns its result as usual. One that's still running is left running on the NAS, and the reply gives a handle, such as `delete:FileStation_…`, to use with `file_task`: check its progress or final result, or stop it. `file_task` with no handle lists the operations still running. The wait is capped at 600 s whatever the setting, because an MCP client that gives up on a call cancels it, and a canceled call stops the operation.

**Client timeouts.** The MCP client has its own time limits. Claude Code aborts a tool call that sends no result and no progress notification for an idle window: 30 minutes by default for a stdio server like this one (5 minutes for HTTP servers). Raise it with the `CLAUDE_CODE_MCP_TOOL_IDLE_TIMEOUT` environment variable (milliseconds; `0` turns the check off), or for this server only with a `timeout` field (milliseconds) in its MCP config entry, which also sets a hard limit per call. See [Claude Code's MCP docs](https://code.claude.com/docs/en/mcp). Delete, copy, move and restore send progress notifications while they wait, so they don't hit the idle limit. `get_dir_size` and `search_files` don't send progress yet (#252), so one that runs longer than the idle window can be aborted by the client; raise the client's limit, or narrow the path.

Other settings:

- `allowed_local_paths` — local folders `upload_file` and `download_file` may use, including subfolders (default: your Downloads folder). A top-level `allowed_local_paths` applies to every module, and this list adds to it. Without a top-level list, this list replaces the Downloads default. See **Local path confinement** under [Features](#features).
- `default_download_dir` — local folder `download_file` uses when no `dest_folder` is given. It's always allowed.
- `default_upload_dir` — NAS folder `upload_file` uses when no `dest_folder` is given.
- `hide_recycle_in_listings` — hide `#recycle` folders from `list_files` (default: `false`).
- `file_type_indicator` — `emoji` (default) or `text` markers in listings.
- `search_poll_interval` — seconds `search_files` waits between checks on DSM's search while it runs (default `1.0`, `0.5`–`10.0` accepted).

See [docs/specs/config-schema-spec.md](docs/specs/config-schema-spec.md) for the full schema, and [examples/config-power-user.yaml](examples/config-power-user.yaml) for a complete config.

### Custom Instructions

Custom instructions let you shape how Claude interacts with your NAS tools. This is useful when:

- **Multiple NAS connections** — tell Claude which connection to prefer for different tasks ("use this for media, use admin for cross-user operations")
- **Safety guardrails** — add rules like "always confirm before deleting" or "never touch /Backups"
- **Context** — explain what's on the NAS ("this is a media server, /video has our library sorted by genre")

**Add context** — `custom_instructions` is prepended to the built-in prompt (higher priority):

```yaml
# ~/.config/mcp-synology/config.yaml
custom_instructions: |
  This is the admin NAS with elevated privileges.
  Prefer this connection for file operations requiring cross-user access.
  Never delete files from /Backups without explicit confirmation.
```

**Full control** — `instructions_file` replaces the built-in prompt entirely. Start from the [built-in files](src/mcp_synology/instructions/): `server.md` is sent on every connection, `filestation.md` only when File Station is enabled, `filestation-write.md` after it only when File Station's WRITE tools are enabled (`permission: write` or `admin`), and `local.md` before the generated LOCAL PATHS section. The Synology Photos section is generated in `server.py` (`_PHOTOS_NOTICE`, plus `_PHOTOS_WRITE_NOTICE` when the Photos WRITE tools are enabled); copy it from a Photos connection's instructions if you use Photos. A replacement file is used as written, apart from HTML comments, so it doesn't get the generated PERMISSIONS section, which tells the model which tools this connection withholds and how to enable them (`custom_instructions` keeps it):

```yaml
# ~/.config/mcp-synology/config.yaml
instructions_file: ~/.config/mcp-synology/my-instructions.md
```

Both support template variables: `{display_name}`, `{instance_id}`, `{host}`, `{port}`, `{home_dir}` (the local home folder) and `{platform}` (`Linux`, `macOS` or `Windows`). Any other brace is left as written, so JSON examples are fine (`{{` and `}}` are not escapes).

HTML comments (`<!-- ... -->`) are removed from both before the text is sent, so they can hold notes for whoever edits the file. A comment on a line of its own is removed with that line.

## Troubleshooting

An error's `help_url` points to its section in [docs/error-codes.md](docs/error-codes.md). For behavior that looks wrong but isn't an error (a download sitting in `waiting`, a Photos date that reads back old, a delete that hands off with a handle), see [docs/troubleshooting.md](docs/troubleshooting.md), organized by module.

## Debugging

Two ways to enable debug logging:

```bash
mcp-synology check --verbose                          # --verbose flag on setup/check
SYNOLOGY_LOG_LEVEL=debug mcp-synology serve           # env var, works for all commands
```

Or set it persistently in your config file:

```yaml
# ~/.config/mcp-synology/config.yaml
logging:
  level: debug
  file: ~/.local/state/mcp-synology/nas/server.log  # optional, logs to stderr by default
```

Debug output includes every DSM API request/response (passwords masked), credential resolution steps, config discovery, version negotiation, and module registration decisions.

## Contributing

See [DEVELOPMENT.md](DEVELOPMENT.md) for build commands, testing, integration test setup, and design docs.

## Acknowledgements

This project was built using a **Spec-First Coding** approach — a human-AI collaboration model where design precedes implementation and specs are the contract between the two.

Unlike vibe coding, where you describe what you want and let the AI generate code on the fly, spec-first coding treats design as a separate, deliberate phase. The four original specs in `docs/specs/` were developed through extended conversation — exploring trade-offs, rejecting alternatives, and documenting decisions with rationale. Implementation then used the specs as the source of truth across 11 build phases.

Live testing against real hardware revealed behaviors the specs couldn't anticipate (DSM API quirks, such as JSON-parsed parameter values that once looked like search-service throttling, and version format incompatibilities). These discoveries are documented in `CLAUDE.md` and the code, which is authoritative where specs diverge.

## License

[Apache 2.0](LICENSE)

---

<picture>
  <source media="(prefers-color-scheme: dark)" srcset="https://raw.githubusercontent.com/cmeans/mcp-synology/main/src/mcp_synology/icons/mcp-synology-logo-dark.svg" width="24">
  <source media="(prefers-color-scheme: light)" srcset="https://raw.githubusercontent.com/cmeans/mcp-synology/main/src/mcp_synology/icons/mcp-synology-logo-light.svg" width="24">
  <img src="https://raw.githubusercontent.com/cmeans/mcp-synology/main/src/mcp_synology/icons/mcp-synology-logo-light.svg" alt="" width="24" align="top">
</picture> © 2026 Chris Means
