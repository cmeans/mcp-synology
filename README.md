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

Tested on DSM 7.1 to 7.4.1, on virtual DSMs in CI and on a real NAS by AI clients driving the tools: see [Testing](#testing).

Once it's connected, ask in plain words, for example:

- "Which shared folders can I see, and how much space is free?"
- "Find every .mkv under /video and move the ones from 2023 into /video/Archive."
- "Make an album of last July's beach photos and give me a share link."
- "Add this magnet link to Download Station, into /video/TV."
- "Is any disk failing, and how full is each volume?" (Storage needs an administrator account)

**Security at a glance:**

- Each module is READ-only unless its config says `permission: write` (or `admin`, the same); the tools that delete or replace are marked so your client can ask before running them (where the client passes those marks on).
- Local files: uploads and downloads use only the folders you list (your Downloads folder by default).
- `setup` uses HTTPS unless you decline, and stores the password in the OS keyring, tied to the NAS's host and port.
- Text from the NAS is marked as data, not instructions: [docs/prompt-injection.md](docs/prompt-injection.md).
- Reporting a vulnerability: [SECURITY.md](SECURITY.md).

<!-- mcp-name: io.github.cmeans/mcp-synology -->

## Supported modules

Each module covers one DSM package. A module loads only when it's listed under `modules:` in the config, and its `permission` decides whether you get its READ tools only or its WRITE tools too.

| Module | Config key | READ tools | WRITE tools | DSM account | DSM versions |
|---|---|---|---|---|---|
| [System](#system) | `system` | 2 · Partial | — | Any; `get_resource_usage` and some details need an administrator | 7.1 – 7.4.1 |
| [File Station](#file-station) | `filestation` | 7 · Most | 8 · Partial | Any | 7.1 – 7.4.1 |
| [Synology Photos](#synology-photos) | `photos` | 4 · Most | 15 · Most | Any, with the Photos privilege | 7.1 – 7.4.1 (favorites and concepts: 7.2.2+) |
| [Download Station](#download-station) | `downloadstation` | 5 · Most | 7 · Partial | Any, with the Download Station privilege (other accounts see only their own tasks) | 7.1 – 7.4.1 |
| [Storage](#storage) | `storage` | 11 · Most | — | Admin only | 7.1 – 7.4.1 |

What's not covered, in short (each module's section has the full list):

- **System:** process list, system health, logs, connected users. No WRITE tools (no reboot or shutdown).
- **File Station:** compress and extract, share links, favorites, thumbnails, MD5 checksums, permissions and ownership changes, mounting remote folders.
- **Synology Photos:** timeline and similar-photo views, video streaming, Shared Space folder permissions, face-level People edits, guest uploads to photo requests, changing Photos' own settings.
- **Download Station:** BT search, RSS feeds, choosing files inside a torrent, editing a torrent's trackers, settings other than speed limits and the default destination.
- **Storage:** no WRITE tools (no data scrubbing, S.M.A.R.T. tests, hot spares, storage settings, TRIM or snapshot changes); no iSCSI LUNs and SAN Manager, Hyper Backup or Task Scheduler. Never anything that wipes disks or changes a pool's layout.

## Quick start

### 1. Run setup

```bash
uvx mcp-synology setup
```

Requires [uv](https://docs.astral.sh/uv/). `uvx` downloads the latest version and caches it — no separate install step needed. It checks PyPI again once its cached copy of the index is more than 10 minutes old, so a restart of your MCP client usually picks up a new release; see [Updates](#updates).

The rest of this README writes commands as `mcp-synology …`. If you run it through `uvx` (no install), type `uvx mcp-synology …` instead.

Setup will prompt for your NAS host, credentials, and preferences. If your account has 2FA enabled, it will prompt for an OTP code and store a device token for automatic future logins. It writes a config with the **File Station** module only; add other modules afterward (see each module below).

At the end, it prints the `check` command for the config it wrote, and a JSON snippet for your MCP client.

### 2. Add it to your MCP client

**Claude Desktop:** open **Settings → Developer → Edit Config**, which opens `claude_desktop_config.json`, add the snippet `setup` printed, and restart the app. It looks like this, with the full path to `uvx` and to your config, and the server named `synology-` plus the connection's alias if you set one, otherwise its instance ID:

```json
{
  "mcpServers": {
    "synology-nas": {
      "command": "/Users/you/.local/bin/uvx",
      "args": ["mcp-synology", "serve", "--config", "~/.config/mcp-synology/nas.yaml"]
    }
  }
}
```

The config file is named after the connection's instance ID, which `setup` takes from the host (`home-nas.local` gives `home-nas.yaml`).

**Claude Code:**

```bash
claude mcp add synology -- uvx mcp-synology serve --config ~/.config/mcp-synology/<name>.yaml
```

**Other clients:** any client that starts a local (stdio) server: the command is `uvx` (its full path, which `which uvx` prints, for an app started outside a shell), with the arguments `mcp-synology serve --config <your config file>`.

**Keep the server's name short.** Claude's API refuses a tool whose full name is over 64 characters, and the client builds that name from the server's name and the tool's: `mcp__<server>__<tool>` in Claude Code, and `mcp__remote-devices__<server>__<tool>` when the Claude app reaches a local server on your computer. The longest tool name is `restore_from_recycle_bin` (24 characters), so the server's name can be at most 17 characters in the Claude app (`synology-` plus an alias of up to 8: `synology-HomeNAS` fits) and 33 in Claude Code. With a longer one, every message fails with an API error saying a string "should have at most 64 characters".

On Linux, the server auto-detects the D-Bus session socket for keyring access. If auto-detection fails, add `"env": {"DBUS_SESSION_BUS_ADDRESS": "unix:path=/run/user/<uid>/bus"}` to the Claude Desktop config. The setup command includes this in the generated snippet.

### 3. Verify

```bash
uvx mcp-synology check --config ~/.config/mcp-synology/<name>.yaml   # checks the config and logs in; setup prints this line (without uvx)
uvx mcp-synology setup --list                                        # lists the NAS configs you've set up
```

Then restart your client and ask it: "What shared folders are on my NAS?"

### Adding a module

1. **Install the package on the NAS** (Synology Photos or Download Station) from Package Center, if it isn't installed already. Without it the module's tools still appear, but every call fails with [`api_not_found`](docs/error-codes.md#api_not_found).
2. **Edit your config file**: the one `setup` wrote (the file your client's `--config` names), or the per-NAS file such as `home-nas.yaml` if you use [Multi-NAS](#multi-nas). Add the module **inside the existing `modules:` section**; don't add a second `modules:` key. Each module below has a block to copy.
3. **Check the config:** `mcp-synology check --config <your config file>` runs the same validation as `serve`, so it lists the YAML, setting and module-setting mistakes it finds and names the file. It also names misspelled settings and modules, and confirms the login still works.
4. **Restart your MCP client** (for example, Claude Desktop). The server reads its config only at startup.

### Alternative: global install

If you prefer a persistent install (avoids download on each invocation). Requires [uv](https://docs.astral.sh/uv/getting-started/installation/) — install with `curl -LsSf https://astral.sh/uv/install.sh | sh` (macOS/Linux) or see the [installation guide](https://docs.astral.sh/uv/getting-started/installation/) for other options.

```bash
uv tool install mcp-synology
mcp-synology setup
mcp-synology check --config <your config file>
```

### Alternative: env-var-only mode

Without a config file, set `SYNOLOGY_HOST` and the server runs on environment variables alone, which suits Docker and CI. One catch: if the folder the server starts in holds a `mcp-synology.yaml`, the server stops and asks you to name it or move it (so give an MCP client a fixed working folder if it has that setting).

```json
{
  "mcpServers": {
    "synology": {
      "command": "uvx",
      "args": ["mcp-synology", "serve"],
      "env": {
        "SYNOLOGY_HOST": "192.168.1.100",
        "SYNOLOGY_HTTPS": "true",
        "SYNOLOGY_USERNAME": "your_user",
        "SYNOLOGY_PASSWORD": "your_password"
      }
    }
  }
}
```

Or from the CLI:

```bash
SYNOLOGY_HOST=192.168.1.100 SYNOLOGY_HTTPS=true uvx mcp-synology check
```

`SYNOLOGY_HTTPS=true` (port 5001 by default) keeps the password off the network in cleartext; leave it out only on a network you trust. DSM's default certificate is self-signed, which env-only mode can't accept (`verify_ssl` has no variable), so use a config file or a certificate the machine trusts.

Without a config file, mcp-synology always runs **File Station, read-only**. No environment variable enables another module or the write tools; for those, use a config file (the same variables then override its values, and overriding its host, port, HTTPS or instance ID prints a warning on stderr naming the variable, at any log level). The variables:

| Variable | Sets | Default |
|---|---|---|
| `SYNOLOGY_HOST` | `connection.host` (needed for env-only mode) | — |
| `SYNOLOGY_PORT` | `connection.port` | 5000, or 5001 with HTTPS |
| `SYNOLOGY_HTTPS` | `connection.https` (`true`/`false`, `1`/`0`, `yes`/`no` or `on`/`off`, any case) | `false` |
| `SYNOLOGY_USERNAME` | `auth.username` | the keyring |
| `SYNOLOGY_PASSWORD` | `auth.password` | the keyring |
| `SYNOLOGY_DEVICE_ID` | `auth.device_id` (2FA device token) | the keyring |
| `SYNOLOGY_INSTANCE_ID` | `instance_id` (names the keyring entry and state folder) | derived from the host |
| `SYNOLOGY_LOG_LEVEL` | `logging.level` (`debug`, `info`, `warning`, `error`) | `info` |
| `SYNOLOGY_UPDATE_NOTICES` | `update_notices` (`all`, `security`, `off`) | `all` |

Each value is read as its config field's type, and choices in any case (`SYNOLOGY_LOG_LEVEL=DEBUG`); `SYNOLOGY_UPDATE_NOTICES` also takes `no`/`yes` as the config file does. A value of the wrong type is an error that names the variable, and the server doesn't start: `SYNOLOGY_HTTPS=sure` never means off. An empty or blank variable counts as unset.

Credentials come from environment variables first, then the config file, then the keyring.

**Which config is used:**

- `--config <file>`, else `MCP_SYNOLOGY_CONFIG=<file>`, else `~/.config/mcp-synology/config.yaml`; with none of these, environment variables alone. The log says which, at INFO.
- **No fallback:** a file named by `--config` or `MCP_SYNOLOGY_CONFIG` that doesn't exist, or an empty value, is an error; mcp-synology doesn't fall back to the default location or to env-var-only mode.
- **A config in the current folder** (`./mcp-synology.yaml`) is loaded only when you name it (`--config ./mcp-synology.yaml`, or its full path in an MCP client's config), because it chooses the host your stored password goes to: a folder you merely opened could otherwise send it elsewhere. Name it only if you trust it. With a home config, an unnamed one is ignored; without one, `check` and `serve` stop and say so, even with `SYNOLOGY_HOST` set.

## Modules

Every module is configured the same way, under `modules:`:

- **`enabled`**: `true` (the default) or `false`. Listing a module is enough to load it: for every module, a name with nothing under it (`filestation:`) is the same as `filestation: {}`, enabled at `read` with no settings, and an empty `settings:` is the same as no settings. `enabled: false` keeps its block in the file without loading it.
- **`permission`**: `read` (the default) registers the READ tools only; `write` adds the WRITE tools. `admin` is also accepted and is the same as `write`. It doesn't refer to the DSM account: an administrator account doesn't need it.
- **`settings`**: the module's own options, listed in its section. A setting the module doesn't know is ignored with a warning that names the valid ones, so a config written for a newer version still loads. Any other key next to these three is an error, so a typo such as `permision` stops the server instead of being ignored.

### System

Monitor NAS health and resource usage.

- **READ:** `get_system_info`, `get_resource_usage`
- **WRITE:** none

```yaml
modules:
  system:
    enabled: true   # no write tools, so no permission is needed
```

**Settings:** none.

**Tools.** `get_system_info` gives every account the model, serial number, DSM version, RAM, uptime, the NAS's local time and code page, and the temperature where the NAS has a sensor. An administrator account also gets the CPU, time zone and current UTC offset, NTP server, firmware date, DSM's update-ready and crash flags, eSATA port, attached USB, SATA and PCIe devices, and redundant power on models that report it; any other account gets a line naming what needs an administrator. `get_resource_usage` shows live CPU load, memory usage, disk I/O and network throughput, and needs an administrator account.

**Accounts.** Any account can use `get_system_info`; an administrator account sees more (above). `get_resource_usage` needs an administrator account.

**DSM versions:** 7.1 – 7.4.1. The incompatible-NIC flag (administrators only) appears from DSM 7.2.2.

**Not covered:** process list, system health, logs, connected users; no reboot or shutdown.

Spec: [`docs/specs/system-module-spec.md`](docs/specs/system-module-spec.md).

### File Station

Browse, search, transfer and manage the files in the NAS's shared folders.

- **READ:** `list_shares`, `list_files`, `list_recycle_bin`, `search_files`, `get_file_info`, `get_dir_size`, `download_file`
- **WRITE:** `create_folder`, `rename`, `copy_files`, `move_files`, `delete_files`, `restore_from_recycle_bin`, `upload_file`, `file_task`

```yaml
modules:
  filestation:
    enabled: true
    permission: write             # or read
    settings:                     # all optional; the values shown are the defaults
      hide_recycle_in_listings: false
      file_type_indicator: emoji  # or text
      async_timeout: 120          # search_files and get_dir_size
      # search_timeout: 120       # defaults to async_timeout
      # dir_size_timeout: 120     # defaults to async_timeout
      delete_timeout: 30          # how long delete_files waits before handing off
      copy_move_timeout: 30       # copy_files, move_files, restore_from_recycle_bin
      upload_timeout: 300
      download_timeout: 300
      search_poll_interval: 1.0
      # allowed_local_paths:      # default: your Downloads folder
      #   - ~/Downloads
      # default_download_dir: ~/Downloads
      # default_upload_dir: /home/uploads
```

**Settings:**

| Setting | Default | Accepted | What it does |
|---|---|---|---|
| `hide_recycle_in_listings` | `false` | `true`, `false` | Hide `#recycle` folders from `list_files`. |
| `file_type_indicator` | `emoji` | `emoji`, `text` | Markers for files and folders in listings. |
| `async_timeout` | `120` | 10–3600 s | Time limit for `search_files` and `get_dir_size`. It doesn't apply to delete, copy, move or restore. |
| `search_timeout` | `async_timeout` | 10–3600 s | Time limit for `search_files` only. The NAS search is stopped when it runs out. |
| `dir_size_timeout` | `async_timeout` | 10–3600 s | Time limit for `get_dir_size` only. |
| `delete_timeout` | `30` | 10–3600 s | How long `delete_files` waits before handing off (capped at 600). |
| `copy_move_timeout` | `30` | 10–3600 s | How long `copy_files`, `move_files` and `restore_from_recycle_bin` wait before handing off (capped at 600). |
| `upload_timeout` | `300` | 10–3600 s | Time limit for `upload_file`. |
| `download_timeout` | `300` | 10–3600 s | Time limit for `download_file`. |
| `search_poll_interval` | `1.0` | 0.5–10.0 s | How often `search_files` checks on DSM's search while it runs. |
| `allowed_local_paths` | your Downloads folder | local folders | Local folders `upload_file` and `download_file` may use, including subfolders. See [Local paths](#local-paths). |
| `default_download_dir` | none | a local folder | Where `download_file` saves when no `dest_folder` is given. It's always allowed, with its subfolders. A relative path is from the config file's folder, so `.` would allow that folder, config file included. |
| `default_upload_dir` | none | a NAS folder | Where `upload_file` uploads when no `dest_folder` is given. |

**Accounts.** Any account. The tools see the shared folders that account can see, and can change only what its DSM permissions allow.

**DSM versions:** 7.1 – 7.4.1, the same tools on each.

**Not covered:** compress and extract, share links, favorites, thumbnails, MD5 checksums, permissions and ownership changes, mounting remote folders.

<details>
<summary><b>Good to know:</b> long operations, client timeouts, folder downloads, shared folders</summary>

- **Long operations hand off.** For delete, copy, move and restore, the timeout is **how long a call waits before handing off**, not a limit on the operation. An operation that finishes within the wait returns its result as usual. One that's still running is left running on the NAS, and the reply gives a handle, such as `delete:FileStation_…`, to use with `file_task`: check its progress or final result, or stop it. `file_task` with no handle lists the operations still running. The wait is capped at 600 s whatever the setting, because an MCP client that gives up on a call cancels it, and a canceled call stops the operation.
- **Client timeouts.** The MCP client has its own time limits. Claude Code aborts a tool call that sends no result and no progress notification for an idle window: 30 minutes by default for a stdio server like this one (5 minutes for HTTP servers). Raise it with the `CLAUDE_CODE_MCP_TOOL_IDLE_TIMEOUT` environment variable (milliseconds; `0` turns the check off), or for this server only with a `timeout` field (milliseconds) in its MCP config entry, which also sets a hard limit per call. See [Claude Code's MCP docs](https://code.claude.com/docs/en/mcp). Delete, copy, move and restore send progress notifications while they wait, so they don't hit the idle limit. `get_dir_size` and `search_files` don't send progress, so one that runs longer than the idle window can be aborted by the client; raise the client's limit, or narrow the path.
- **Folder downloads.** `download_file` of a folder saves DSM's ZIP of it as `<folder>.zip` (or under `filename`, when given). A whole shared folder can't be downloaded this way (DSM doesn't send one), so it's refused.
- **Shared folders, recycle bins and users' home folders aren't deleted, moved or renamed whole:** a shared folder (`/video`), a whole recycle bin (`/video/#recycle`) or a user's home folder (`/homes/<user>`). DSM would remove the shared folder itself, empty the bin for good (other users' items included) or break that user's home. Work with the items inside, or use DSM's Control Panel; `copy_files` of a shared folder is fine. Every tool also refuses a path with a `.`, `..` or empty part, or a NUL character. The [File Station spec](docs/specs/filestation-module-spec.md) has the details per tool and DSM version.
- **Paths are taken exactly.** Whitespace inside a path is part of a name, as it is on DSM: `/video/report.pdf ` (with a trailing space) is a different file from `/video/report.pdf`, and a stray space gets "not found", never another item. A name the tools create (`rename`, `upload_file`, `create_folder`) can't start or end with whitespace or hold a control character (a tab inside is fine) or an invisible character that changes how it reads, and `#recycle` can't be created, copied or moved into a shared folder's or a user's home folder's top level, where it would be taken for the recycle bin.
- **Names with a line break or other control character.** Every reply shows text from the NAS with such characters escaped (`report\nnote.txt`), so a name can't start a line of its own in a reply, which a model could read as the server's. An argument holding one is refused, so a file or folder named that way (made on the NAS by other means) is listed but can't be acted on through File Station's tools until it's renamed in DSM. Photos finds albums, people and tags by name as shown. A tab is fine.
- **File names and case.** Whether `X.TXT` and `x.txt` are two files depends on the share's file system: Btrfs keeps them apart, and a USB or network share may not. So with `overwrite=true`, `copy_files`, `move_files` and `restore_from_recycle_bin` refuse two sources going to one folder whose names match ignoring case, as one could replace the other. `overwrite=false` still does the call: anything whose name is already there is skipped and listed, except a folder landing on a file, which fails.

</details>

Spec: [`docs/specs/filestation-module-spec.md`](docs/specs/filestation-module-spec.md).

### Synology Photos

Browse, search, organize and share a Synology Photos library, in your personal space and in Shared Space. Needs the **Synology Photos** package (Photo Station, its DSM 6 predecessor, isn't supported).

- **READ:** `list_photos`, `list_photo_collections`, `get_photo`, `download_photos`
- **WRITE:** `upload_photos`, `photo_album`, `photo_share_link`, `photo_folder`, `copy_photos`, `move_photos`, `photo_task`, `tag_photos`, `edit_photo`, `edit_person`, `photo_request`, `merge_people`, `delete_photos`, `delete_photo_folders`, `delete_photo_albums`

```yaml
modules:
  photos:
    enabled: true
    permission: write       # or read
    settings:               # all optional; the values shown are the defaults
      task_timeout: 30      # seconds a copy, move or delete waits before handing off
      # allowed_local_paths:  # default: your Downloads folder
      #   - ~/Downloads
```

**Settings:**

| Setting | Default | Accepted | What it does |
|---|---|---|---|
| `task_timeout` | `30` | 1–600 s | How long `copy_photos`, `move_photos`, `delete_photos` and `delete_photo_folders` wait before handing off with a `photos:<id>` handle for `photo_task`. Also when `upload_photos` stops starting new files (at most 45 s, below the about 60 s that claude.ai waits for a tool call through the desktop app; it lists the ones not tried, for another call). |
| `allowed_local_paths` | your Downloads folder | local folders | Local folders `upload_photos` reads from and `download_photos` writes to. See [Local paths](#local-paths). |

**Accounts.** The account needs the Synology Photos application privilege (**Control Panel → Application Privileges → Synology Photos**), and the personal space needs the user home service (**Control Panel → User & Group → Advanced → User Home**). Shared Space must be enabled in Synology Photos' settings, with access granted to the account. The tools report each of these by name when it's missing.

**DSM versions:** DSM 7.1 (Synology Photos 1.3.4) and DSM 7.2.2, 7.3.2 and 7.4.1 (Synology Photos 1.9.1). **Favorites and concepts** (things and scenes Photos recognized) need Photos 1.9.1; on 1.3.4 they're refused with `not_supported`.

**Not covered:** timeline and similar-photo views, video streaming, Shared Space folder permissions, face-level People edits, guest uploads to photo requests, changing Photos' own settings.

<details>
<summary><b>Good to know:</b> edits, tags, deletes</summary>

- **Edits change the original files.** Photos writes ratings, descriptions, tags, taken-time changes and rotations into the files themselves, so their dates and sizes change; backup and sync tools see new versions.
- **Tags are given by name.** A tag exists only while some item has it, so removing it from every item deletes it.
- **Albums, people and folders are given by ID or by name or path, in separate parameters** (`album_id` or `album_name`, `folder_id` or `folder_path`, and so on), so a name or path made of digits is never taken for an ID, nor an ID for a name.
- **Deletes** go to the recycle bin of the share that holds the space, when it has one; Photos has no trash of its own. A large copy, move or delete hands off with a `photos:<id>` handle that `photo_task` follows up on.

</details>

Spec: [`docs/specs/photos-module-spec.md`](docs/specs/photos-module-spec.md), with what each tool sends and the DSM behavior behind it.

### Download Station

Manage the Download Station queue and settings.

- **READ:** `list_downloads`, `get_download_info`, `get_download_stats`, `get_download_config`, `get_schedule`
- **WRITE:** `create_download`, `delete_download`, `pause_download`, `resume_download`, `edit_download`, `set_download_config`, `set_schedule`

```yaml
modules:
  downloadstation:
    enabled: true
    permission: write       # or read
    settings:               # optional
      allowed_local_paths:  # folders create_download may read .torrent/.nzb files from
        - ~/Downloads       # the default; list it to keep it when adding others
        - ~/Torrents
```

**Settings:**

| Setting | Default | Accepted | What it does |
|---|---|---|---|
| `allowed_local_paths` | your Downloads folder | local folders | Local folders `create_download`'s `torrent_file_path` may read `.torrent` and `.nzb` files from. Magnet links and URLs aren't affected. See [Local paths](#local-paths). |

**Accounts.** An administrator (a Download Station manager) can use every tool. Any other account needs the Download Station application privilege (**Control Panel → User & Group → [account] → Edit → Applications**), and **must set a default destination once** before Download Station will start its tasks: `set_download_config(default_destination="<share>")` (a WRITE tool, so `permission: write`), or in Download Station's own settings in DSM. More below.

**DSM versions:** DSM 7.1 (Download Station 3.9-4627) and DSM 7.2.2, 7.3.2 and 7.4.1 (Download Station 4.1-5012), the same tools on each.

**Not covered:** BT search, RSS feeds, choosing files inside a torrent, editing a torrent's trackers, settings other than the speed limits and the default destination (BT, FTP/HTTP, NZB, auto-extract).

<details>
<summary><b>Good to know:</b> accounts other than administrators, known limits</summary>

**Accounts other than administrators.** All of these are enforced or reported by the tools:

- **First-time setup.** Download Station doesn't start the tasks of an account other than an administrator until that user has a default destination; until then they wait forever. A user who has never opened Download Station has none. Set it once with `set_download_config(default_destination="<share>")`, which DSM applies to that user only. Until then, `create_download` with an explicit `destination` still creates the task but warns that it won't start. Without a `destination`, it refuses and says what to set. That refusal applies to any account, administrator included, whenever there's no default destination to fall back on.
- **Own tasks only.** `list_downloads` and the other task tools see only the user's own tasks.
- **Settings.** `get_download_config` shows the user's own view, not the NAS-wide settings. `set_download_config` can change only the user's own `default_destination`; the speed limits are NAS-wide and are refused, because DSM accepts and then silently ignores them from any other account.
- **Schedule.** The weekly plan and limited-speed rates in `get_schedule` / `set_schedule` need an administrator. Any other account can still turn the schedule on or off, and that switch is NAS-wide.

**Known limits:**

- A `destination` must be an existing folder: Download Station doesn't create missing folders. Create it first with File Station's `create_folder` (`/video/TV/New show`; the `filestation` module needs `permission: write`) or in DSM's own File Station, then pass it share-relative (`video/TV/New show`). An error that says the destination doesn't exist names this fix.
- `create_download` returns no task IDs for URLs that use `username` / `password`.
- `edit_download` changes only the destination.
- A destination is taken exactly: `video/TV/New show ` (with a trailing space) is a different folder from `video/TV/New show`.
- `delete_download(force_complete=true)` doesn't reliably keep partial data: DSM discarded unfinished HTTP downloads anyway. DSM also refuses it for finished tasks, and the tool reports those as not removed.

</details>

Spec: [`docs/specs/downloadstation-module-spec.md`](docs/specs/downloadstation-module-spec.md).

### Storage

See the health of the NAS's storage: storage pools and RAID, volumes and free space, disks with S.M.A.R.T., SSD caches, external drives and share snapshots.

- **READ:** `get_storage_overview`, `list_storage_pools`, `list_volumes`, `get_volume`, `list_disks`, `get_disk`, `list_ssd_caches`, `get_storage_schedules`, `get_storage_settings`, `list_external_storage`, `list_snapshots`
- **WRITE:** none

```yaml
modules:
  storage:
    enabled: true   # no write tools, so no permission is needed
```

**Settings:** none.

**Tools.** Start with `get_storage_overview`: DSM's own status summary, every pool, volume, disk and SSD cache in one line each, anything that needs attention (a degraded pool, a failing disk, a nearly full volume) and the storage tasks running or scheduled next. The others give one area in full, with every field DSM sends: `get_disk` adds every S.M.A.R.T. attribute, test results and history and a 12-month health trend; `get_volume` adds space by category, TRIM, deduplication and the attached SSD cache.

**Accounts.** An administrator account only: Storage Manager answers no other account, so with any other account every tool returns `permission_denied`. `list_snapshots` also needs the Snapshot Replication package.

**DSM versions:** 7.1 – 7.4.1, the same tools on each. A volume's encryption and lock state, a snapshot's immutable flag and the drive database version appear from DSM 7.2.2. A section a NAS doesn't support (deduplication on some models, for example) is left out with a note.

**Not covered:** no WRITE tools: no data scrubbing, S.M.A.R.T. tests, hot spares, storage settings, TRIM or snapshot management. iSCSI LUNs and SAN Manager, Hyper Backup and Task Scheduler aren't covered. Anything that wipes disks or changes a pool's layout (creating, deleting, expanding or repairing pools and volumes, secure erase) is left out on purpose.

Spec: [`docs/specs/storage-module-spec.md`](docs/specs/storage-module-spec.md).

## Features

- **Interactive setup**: guided configuration that creates your config, stores credentials, handles 2FA, and prints the JSON for your MCP client
- **Permission tiers**: READ or WRITE per module, enforced at tool registration. Tiers control what the server can do on the NAS. Tools that write on this machine (`download_file`, `download_photos`) are annotated as destructive so your client can confirm them, and so is every WRITE tool except `create_folder`, `rename`, `upload_photos`, `create_download`, `pause_download` and `resume_download`. A read-only connection tells the model which tools it withholds and how to enable them, so the model doesn't claim the server can't do it
- **Long operations keep running**: a large delete, copy, move or recycle-bin restore reports progress while the call waits, and if it's still running when the wait ends (30 s by default) the NAS keeps working on it: the reply gives a handle for `file_task`, which checks on it, lists running operations or stops one. See [File Station](#file-station). Photos' `copy_photos`, `move_photos`, `delete_photos` and `delete_photo_folders` also hand off, after `task_timeout` (default 30 s, up to 600), with a `photos:<id>` handle for `photo_task`
- **Local path confinement**: tools that read or write files on this machine use only the folders you list, by default your Downloads folder. See [Local paths](#local-paths)
- **Text from the NAS is data**: see [below](#text-from-the-nas-is-data)
- **2FA support**: auto-detected; device token bootstrap with automatic silent re-auth
- **Secure credentials**: OS keyring integration that works transparently on macOS, Windows, and Linux (including from Claude Desktop). See [docs/credentials.md](docs/credentials.md).
- **Multi-NAS**: manage multiple NAS devices with separate configs, credentials, and state

## Text from the NAS is data

File names, folder and album names, descriptions and a download's title may have been written by someone else, and they reach the model in tool replies. mcp-synology can't tell an instruction from a name, so it marks all such text as data:

- every reply starts with a line saying the text in it comes from the NAS or local files and isn't instructions, and the server instructions tell the model not to follow instructions found in it and to tell you about them;
- control, line-break and bidirectional characters in that text are escaped, so a name can't write a line that looks like the server's;
- a tool call is refused when it carries such characters (except a Photos description and Download Station's URIs, which are escaped wherever they're shown), an argument the tool doesn't take, or text the MCP SDK would turn into something else. The MCP layer's own type checks never quote the value sent, and an unknown argument's name is never repeated, except a renamed parameter's old name, from the server's own table; a tool's own refusal may quote what it refused, after the character check, so it can't start a line of its own.

Wording isn't filtered: whether a model acts on planted text is up to the model, and each release candidate's live test plants some in names and descriptions to check. Nor does the server see inside the files it downloads: a document's text, or an image with written instructions or a QR code, reaches the model through the client's own file reading, unlabeled; a download that saved a file says in its reply that the contents are data, not instructions. [docs/prompt-injection.md](docs/prompt-injection.md) has each guard, why it exists and its tests.

## Configuration

Interactive setup creates a config file for you. For manual configuration or advanced options, see `examples/`:
- `config-minimal.yaml` — simplest possible config
- `config-power-user.yaml` — every module, HTTPS, custom timeouts, logging, instructions
- `config-docker.yaml` — environment-variable-driven

The full reference is [`docs/specs/config-schema-spec.md`](docs/specs/config-schema-spec.md). After editing a config, run `mcp-synology check --config <file>`: it reports YAML and validation mistakes, names misspelled settings and modules, then tries to log in. Module settings are in each module's section under [Modules](#modules).

A config with every module, READ and WRITE (the [module sections](#modules) list each one's settings):

```yaml
# ~/.config/mcp-synology/config.yaml
schema_version: 1
connection:
  host: 192.168.1.100
  https: true
  verify_ssl: false        # DSM's default self-signed certificate
auth:
  username: your_user      # the password stays in the keyring (setup puts it there)
modules:
  system:
    enabled: true
  filestation:
    enabled: true
    permission: write
  photos:
    enabled: true
    permission: write
  downloadstation:
    enabled: true
    permission: write
  storage:
    enabled: true          # READ tools only; needs a DSM administrator account
```

### Connection and HTTPS

```yaml
connection:
  host: 192.168.1.100    # or a hostname; bare: no https://, user name or :port ("[fd00::1]" for IPv6, quoted, with instance_id set)
  https: true            # default false: credentials then cross the network in cleartext
  port: 5001             # default 5000, or 5001 with https: true
  verify_ssl: false      # default true; false for DSM's default self-signed certificate
  timeout: 30            # seconds per request, 1 to 600 (default 30)
```

Use `https: true` unless the NAS is on a network you trust: with plain HTTP, the login and every request travel unencrypted, and the server logs a warning saying so. `setup` writes `https: true` unless you decline. [docs/https.md](docs/https.md) covers DSM's self-signed certificate and how to get a trusted one. If DSM uses its own self-signed certificate, set `verify_ssl: false` (the server warns that verification is off), or install a certificate the machine trusts and keep it `true`. A misspelled key in `connection`, `auth` or `logging` (`htps`, `verfy_ssl`) stops the server with an error naming it, rather than being ignored and leaving HTTPS off. When the key may be part of a password (any unknown key under `auth`, or one with no value), the error names the real key it's close to instead, or lists the valid keys.

### Local paths

`upload_file`, `download_file`, `upload_photos`, `download_photos` and `create_download`'s torrent files only use local folders listed in `allowed_local_paths`, by default your Downloads folder:

```yaml
allowed_local_paths:   # top level: for every module
  - ~/Downloads
  - ~/Pictures/nas
```

- Set it at the top level for every module, or in a module's settings (File Station, Synology Photos and Download Station each have their own list).
- A module's own list adds to the top-level list when that's set. Without a top-level list, it **replaces** the Downloads default, so include `~/Downloads` if you still want it.
- Symlinks and `..` are resolved first, so they can't lead outside. `create_download` reads only `.torrent` and `.nzb` files.
- `["/"]` allows everything (macOS and Linux).

### Multi-NAS

Each NAS gets its own config file, credentials, and Claude Desktop entry. Run `setup` once per NAS: it names each file after the instance ID it takes from the host (`home-nas.local` gives `home-nas.yaml`), and asks for one when the host gives none.

Set `alias` to give Claude a display name for the connection:

```yaml
# ~/.config/mcp-synology/home-nas.yaml
alias: HomeNAS
```

The alias appears in the MCP server name (e.g., `synology-HomeNAS`) so Claude knows which NAS it's talking to.

### Custom instructions

Custom instructions let you shape how Claude interacts with your NAS tools. This is useful when:

- **Multiple NAS connections**: tell Claude which connection to prefer for different tasks ("use this for media, use admin for cross-user operations")
- **Safety guardrails**: add rules like "always confirm before deleting" or "never touch /Backups"
- **Context**: explain what's on the NAS ("this is a media server, /video has our library sorted by genre")

**Add context:** `custom_instructions` is prepended to the built-in prompt (higher priority):

```yaml
# ~/.config/mcp-synology/config.yaml
custom_instructions: |
  This is the admin NAS with elevated privileges.
  Prefer this connection for file operations requiring cross-user access.
  Never delete files from /Backups without explicit confirmation.
```

**Full control:** `instructions_file` replaces the built-in prompt entirely. Start from the [built-in files](src/mcp_synology/instructions/): `server.md` is sent on every connection, `filestation.md` only when File Station is enabled, `filestation-write.md` after it only when File Station's WRITE tools are enabled (`permission: write` or `admin`), and `local.md` before the generated LOCAL PATHS section. The Synology Photos section isn't a file: if you use Photos, copy it from the instructions a Photos connection sends. A replacement file is used as written, apart from HTML comments, so it doesn't get the generated PERMISSIONS section, which tells the model which tools this connection withholds and how to enable them (`custom_instructions` keeps it):

```yaml
# ~/.config/mcp-synology/config.yaml
instructions_file: ~/.config/mcp-synology/my-instructions.md  # a relative path is from this file's folder
```

Both support template variables: `{display_name}`, `{instance_id}`, `{host}`, `{port}`, `{home_dir}` (the local home folder) and `{platform}` (`Linux`, `macOS` or `Windows`). Any other brace is left as written, so JSON examples are fine (`{{` and `}}` are not escapes).

HTML comments (`<!-- ... -->`) are removed from both before the text is sent, so they can hold notes for whoever edits the file. A comment on a line of its own is removed with that line.

## 2FA support

mcp-synology fully supports DSM accounts with two-factor authentication. It's auto-detected: you don't need to configure anything special:

1. **Bootstrap**: `mcp-synology setup` (with `--config <your config file>` if you already have one) detects 2FA, prompts for your OTP code, and stores a device token in the keyring
2. **Silent re-auth**: subsequent logins use the device token automatically (no OTP prompts)
3. **Per-instance**: each NAS config gets its own device token, so mixed 2FA/non-2FA setups work fine

Device tokens have no fixed lifetime: they last until the trusted device is removed in DSM (Personal > Security > Sign-in Activity) or DSM otherwise stops accepting it. If DSM rejects the stored token, the error says so; run `mcp-synology setup --config <your config file>` again to sign in with a new 2FA code (if `device_id` is set in the environment, `SYNOLOGY_DEVICE_ID`, or the config file, it overrides the keyring: remove it there first, then restart the server after `setup`).

## Keyring and credentials

Credentials are stored in the OS keyring and accessed transparently:

| Platform | Backend | Notes |
|----------|---------|-------|
| macOS | Keychain | Just works |
| Windows | Credential Manager | Just works |
| Linux | GNOME Keyring / KWallet | Auto-detects D-Bus session, works from Claude Desktop |

Credential resolution order: **env vars > config file > keyring**. Explicit sources override the implicit default.

A keyring entry is tied to the host and port it was set up for. If a config points at another host or port, the server refuses before sending any credential and says what to do. Two NAS whose names start the same (`nas.home`, `nas.office`) need their own `instance_id`.

For environments without a keyring (Docker, CI), use environment variables or inline credentials in the config file. `setup` still writes the config there, and lists the variables to set. For a 2FA account, get the device token from a machine with a keyring: see [docs/credentials.md](docs/credentials.md#2fa-with-no-keyring).

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

To manage updates from the CLI (`--auto-upgrade` and `--revert` work for a `uv tool` or `pipx` install; `uvx` picks up new releases itself, as Quick start says):

```bash
mcp-synology --check-update                 # Check for a newer version
mcp-synology --auto-upgrade enable          # Auto-upgrade on each interactive run
mcp-synology --revert                       # Roll back to the previous version
mcp-synology --revert 0.10.0                # Roll back to a specific version
```

## Testing

### Tested DSM versions

| DSM | Where | What runs |
|---|---|---|
| 7.1.1-42962 Update 9 | Virtual DSM, in CI | Every module's integration tests, on every change to `main` and every pull request that changes code |
| 7.2.2-72806 Update 10 | Virtual DSM, in CI | The same |
| 7.3.2-86009 Update 5 | Virtual DSM, in CI | The same |
| 7.4.1-90080 Update 1 | Virtual DSM, in CI | The same |
| 7.1.1-42962 Update 9 | A real NAS | Each release candidate's live test: AI clients driving the tools, as administrator, regular, read-only and restricted users ([how](docs/live-testing.md)) |

A virtual DSM ([virtual-dsm](https://github.com/vdsm/virtual-dsm)) has the DSM packages the modules use (Synology Photos, Download Station, Snapshot Replication), but no physical disks or storage pools. The Storage module's disk and pool replies were written from captures of the real NAS.

### Tested clients

Each release candidate is live-tested by these clients driving the tools on the real NAS ([how](docs/live-testing.md)):

| Client | Model | Tested | How it runs the server | Notes |
|---|---|---|---|---|
| **Claude Code** | Claude Opus 5.5 | 0.10.0, 0.11.0 | Starts it itself (stdio) | Gets the server's instructions; no short reply limit (it aborts a call only after 30 minutes idle by default), so long operations and hand-offs are tested here |
| **claude.ai** in the Claude desktop app | Claude Opus 5.5 | 0.10.0, 0.11.0 | The app starts it locally; claude.ai reaches it through the app's device bridge | Doesn't pass on the server's instructions, tool titles or annotations, so the tools are written to work from their descriptions and replies alone. Replies are cut off after about 60 s |
| **Meta Muse** (Power Plan) | Muse Wonder | 0.11.0 | Its own script around `mcp-synology serve`, on its VM | Keep one long-lived session: [docs/muse.md](docs/muse.md) has the instructions to paste |
| **Meta Muse** (free plan) | Muse Spark | 0.10.0 | The same | By default a fresh server process per call (slow: every call logs in); the same instructions fix that |

A client can change the outcome on its own: the same model does less well through a client that drops the server's instructions or annotations.

### Tested models

Every model that has run the tools here, with what it was tested on and how it did. These are results, not a ranking: a live round's steps differ by client, and one run per task can come out differently on a rerun.

| Model | Client | Tested on | Results | Fit for |
|---|---|---|---|---|
| **Claude Opus 5.5** | Claude Code, claude.ai | Every live round of 0.10.0 and 0.11.0; the 12-task comparison (0.11.0) | 0.11.0's last round: claude.ai 94 passed, 0 failed, 0 surprises; Claude Code 48 passed, 0 failed, 3 surprises (all the server's, open for a later release: a first login that found no stored password, a delete reply that didn't mention a stopped copy still listed in that folder, and a disk-test line that named disks already done). Comparison (0.11.0): 12 of 12 | Everything, writes and deletes included |
| **Claude Sonnet 5.5** | Claude Code | The 12-task comparison (0.10.0, 0.11.0) | 0.10.0: 12 of 12. 0.11.0: 12 of 12 | Everything, writes and deletes included |
| **Claude Haiku 5.5** | Claude Code | The 12-task comparison (0.10.0, 0.11.0) | 0.10.0: 11 of 12. 0.11.0: 9 of 11 (a twelfth task depended on one it missed, so it isn't scored). Both runs missed the same task: it didn't match a download by its source URL, so it deleted nothing. In 0.11.0 it also refused a copy, reading the server's connection-name line as forbidding a request that didn't name the NAS | Reads; check its writes, and confirm deletes yourself |
| **Muse Wonder** | Meta Muse (Power Plan) | 0.11.0's live rounds | Last round: 64 passed, 0 failed, 0 surprises (Muse itself graded one correct refusal as a surprise, a password-only share link and then an expiry on a private one: it grades refusals literally). In the earlier rounds its misses found two server bugs (Photos IDs and names), fixed in 0.11.0 | Reads and writes; it follows most hints in replies, not all |
| **Muse Spark** | Meta Muse (free plan) | 0.10.0's live rounds | No fails in the rounds recorded | Reads and writes |

The comparison holds the client fixed: headless Claude Code (server instructions and full tool schemas delivered, no other tools), the same 12 tasks across System, File Station, Synology Photos and Download Station, on a virtual DSM 7.2.2 restored fresh for each model, as a user who isn't an administrator. The tasks, how each was graded and the per-task results are in [Comparing models](docs/live-testing.md#comparing-models).

## Troubleshooting

An error's `help_url` points to its section in [docs/error-codes.md](docs/error-codes.md). For behavior that looks wrong but isn't an error (a download sitting in `waiting`, a Photos date that reads back old, a delete that hands off with a handle), see [docs/troubleshooting.md](docs/troubleshooting.md), organized by module.

## Debugging

Two ways to enable debug logging:

```bash
mcp-synology check --verbose --config <your config file>   # --verbose flag on setup/check
SYNOLOGY_LOG_LEVEL=debug mcp-synology serve --config <your config file>   # env var, works for all commands
```

Or set it persistently in your config file:

```yaml
# ~/.config/mcp-synology/config.yaml
logging:
  level: debug
  file: ~/.local/state/mcp-synology/nas/server.log  # optional, logs to stderr by default; a relative path is from this file's folder
```

Debug output includes every DSM API request/response (passwords masked), credential resolution steps, config discovery, version negotiation, and module registration decisions.

## Migrating from synology-mcp

The package was called `synology-mcp` up to v0.3.x. If you still have that install, a migration script moves its config and state directories, keyring credentials and Claude Desktop entry to the new names:

```bash
curl -O https://raw.githubusercontent.com/cmeans/mcp-synology/main/scripts/migrate-from-synology-mcp.py
python migrate-from-synology-mcp.py          # dry run — preview changes
python migrate-from-synology-mcp.py --apply  # apply changes
```

The moved keyring entries don't say which host they're for. Unless the config is named by its full path, a login is refused until the host is recorded; see [Credentials Are Tied to a Host](docs/credentials.md#credentials-are-tied-to-a-host).

## Contributing

See [DEVELOPMENT.md](DEVELOPMENT.md) for build commands, testing, integration test setup, and design docs.

## Acknowledgements

This project was built using a **Spec-First Coding** approach — a human-AI collaboration model where design precedes implementation and specs are the contract between the two.

Unlike vibe coding, where you describe what you want and let the AI generate code on the fly, spec-first coding treats design as a separate, deliberate phase. The four original specs in `docs/specs/` were developed through extended conversation — exploring trade-offs, rejecting alternatives, and documenting decisions with rationale. Implementation then used the specs as the source of truth across 11 build phases.

Live testing against real hardware revealed behaviors the specs couldn't anticipate (DSM API quirks, such as JSON-parsed parameter values that once looked like search-service throttling, and version format incompatibilities). These discoveries are documented in the module specs and the code, which is authoritative where specs diverge.

## License

[Apache 2.0](LICENSE)

---

<picture>
  <source media="(prefers-color-scheme: dark)" srcset="https://raw.githubusercontent.com/cmeans/mcp-synology/main/src/mcp_synology/icons/mcp-synology-logo-dark.svg" width="24">
  <source media="(prefers-color-scheme: light)" srcset="https://raw.githubusercontent.com/cmeans/mcp-synology/main/src/mcp_synology/icons/mcp-synology-logo-light.svg" width="24">
  <img src="https://raw.githubusercontent.com/cmeans/mcp-synology/main/src/mcp_synology/icons/mcp-synology-logo-light.svg" alt="" width="24" align="top">
</picture> © 2026 Chris Means
