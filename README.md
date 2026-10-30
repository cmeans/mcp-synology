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

Tested against DSM 7.1.1-42962 Update 9, 7.2.2-72806 Update 10, 7.3.2-86009 Update 5 and 7.4.1-90080 Update 1: CI runs the integration tests for every module on a virtual DSM of each. Release candidates are also live-tested on a real NAS (DSM 7.1.1-42962 Update 9) by AI clients driving the tools: for 0.10.0, Claude Code, claude.ai through the desktop app, and Meta Muse, as admin, regular, read-only and restricted users. See [docs/live-testing.md](docs/live-testing.md).

<!-- mcp-name: io.github.cmeans/mcp-synology -->

## Supported modules

Each module covers one DSM package. A module loads only when it's listed under `modules:` in the config, and its `permission` decides whether you get its READ tools only or its WRITE tools too.

| Module | Config key | READ tools | WRITE tools | DSM account | DSM versions |
|---|---|---|---|---|---|
| [System](#system) | `system` | 2 · 🟡 Partial | — | Any; `get_resource_usage` and some details need an admin | 7.1 – 7.4.1 |
| [File Station](#file-station) | `filestation` | 7 · 🟢 Most | 8 · 🟡 Partial | Any | 7.1 – 7.4.1 |
| [Synology Photos](#synology-photos) | `photos` | 4 · 🟢 Most | 15 · 🟢 Most | Any, with the Photos privilege | 7.1 – 7.4.1 (favorites and concepts: 7.2.2+) |
| [Download Station](#download-station) | `downloadstation` | 5 · 🟢 Most | 7 · 🟡 Partial | Any, with the Download Station privilege (non-admins see only their own tasks) | 7.1 – 7.4.1 |
| [Storage](#storage) | `storage` | 11 · 🟢 Most | — | Admin only | 7.1 – 7.4.1 |

**Coverage**, per tier, of what the package itself can do: ✅ **Full**: everything; 🟢 **Most**: the main features, with gaps listed under the module; 🟡 **Partial**: the core features, with notable gaps; — no tools in that tier.

What's not covered yet, in short:

- **System:** process list, system health, logs, connected users. No WRITE tools (no reboot or shutdown).
- **File Station:** compress and extract, share links, favorites, thumbnails, MD5 checksums, permissions and ownership changes, mounting remote folders.
- **Synology Photos:** timeline and similar-photo views, video streaming, Shared Space folder permissions, face-level People edits, guest uploads to photo requests, Photos' own settings.
- **Download Station:** BT search, RSS feeds, choosing files inside a torrent, editing a torrent's trackers, settings other than speed limits and the default destination.
- **Storage:** no WRITE tools yet (phase 2: scrubbing, S.M.A.R.T. tests, hot spares, settings, snapshots); no iSCSI LUNs and SAN Manager, Hyper Backup or Task Scheduler. Never anything that wipes disks or changes a pool's layout.

### Tested clients

Each release candidate is live-tested by these clients driving the tools on a real NAS ([how](docs/live-testing.md)):

| Client | Model | How it runs the server | Notes |
|---|---|---|---|
| **Claude Code** | Claude Opus 5.5 | Starts it itself (stdio) | Gets the server's instructions; no short reply limit (it aborts a call only after 30 minutes idle by default), so long operations and hand-offs are tested here |
| **claude.ai** in the Claude desktop app | Claude Opus 5.5 | The app starts it locally; claude.ai reaches it through the app's device bridge | Doesn't pass on the server's instructions, tool titles or annotations, so the tools are written to work from their descriptions and replies alone. Replies are cut off after about 60 s |
| **Meta Muse** (free plan) | Muse Spark | Its own script around `mcp-synology serve`, on its VM | By default a fresh server process per call (slow: every call logs in). Ask it to keep one long-lived session instead: [docs/muse.md](docs/muse.md) has the instructions to paste |

A client can change the outcome on its own: the same model does less well through a client that drops the server's instructions or annotations.

### Tested models

**For best results, use Claude Sonnet 5.5 or better.** The live tests above ran on Claude Opus 5.5 in both Claude clients. To compare models, we held the client fixed: headless Claude Code (server instructions and full tool schemas delivered, no other tools), the same 12 tasks across System, File Station, Synology Photos and Download Station (Storage came later), on a DSM 7.2.2 test NAS restored fresh for each model, as a non-admin user (2026-10-28).

| Model | Tasks completed |
|---|---|
| Claude Sonnet 5.5 | 12 of 12 |
| Claude Haiku 5.5 | 11 of 12 |

Both handled Photos uploads, albums and tags, recycle-bin checks, Download Station tasks and honest answers about what isn't supported. The task Haiku missed needed an extra lookup it didn't think to make (matching a download by its source URL); Sonnet made it. Haiku is fine for reads; for writes, and deletes above all, use Sonnet or better. The tasks, how each was graded and the per-task results are in [Comparing models](docs/live-testing.md#comparing-models).

## Quick Start

### 1. Run setup

```bash
uvx mcp-synology setup
```

Requires [uv](https://docs.astral.sh/uv/). `uvx` downloads the latest version and caches it — no separate install step needed. It checks PyPI again once its cached copy of the index is more than 10 minutes old, so a restart of your MCP client usually picks up a new release; see [Updates](#updates).

Setup will prompt for your NAS host, credentials, and preferences. If your account has 2FA enabled, it will prompt for an OTP code and store a device token for automatic future logins. It writes a config with the **File Station** module only; add other modules afterward (see each module below).

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

### Adding a module

1. **Install the package on the NAS** (Synology Photos or Download Station) from Package Center, if it isn't installed already. Without it the module's tools still appear, but every call fails with [`api_not_found`](docs/error-codes.md#api_not_found).
2. **Edit your config file**: the one `setup` wrote (the file your client's `--config` names), or the per-NAS file such as `home-nas.yaml` if you use [Multi-NAS](#multi-nas). Add the module **inside the existing `modules:` section**; don't add a second `modules:` key. Each module below has a block to copy.
3. **Check the config:** `mcp-synology check --config <your config file>` reports YAML or validation mistakes, names misspelled settings and modules, and confirms the login still works.
4. **Restart your MCP client** (for example, Claude Desktop). The server reads its config only at startup.

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

## Modules

Every module is configured the same way, under `modules:`:

- **`enabled`**: `true` (the default) or `false`. Listing a module is enough to load it; `enabled: false` keeps its block in the file without loading it.
- **`permission`**: `read` (the default) registers the READ tools only; `write` adds the WRITE tools. `admin` is also accepted and is the same as `write` today. It doesn't refer to the DSM account: an admin account doesn't need it.
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

**Tools.** `get_system_info` gives every account the model, serial number, DSM version, RAM, uptime, the NAS's local time and code page, and the temperature where the NAS has a sensor. An admin account also gets the CPU, time zone and current UTC offset, NTP server, firmware date, DSM's update-ready and crash flags, eSATA port, attached USB, SATA and PCIe devices, and redundant power on models that report it; any other account gets a line naming what needs an administrator. `get_resource_usage` shows live CPU load, memory usage, disk I/O and network throughput, and needs an admin account.

**Accounts.** Any account can use `get_system_info`; an admin account sees more (above). `get_resource_usage` needs an admin account.

**DSM versions:** 7.1 – 7.4.1. The incompatible-NIC flag (admin only) appears from DSM 7.2.2.

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
| `allowed_local_paths` | your Downloads folder | local folders | Local folders `upload_file` and `download_file` may use, including subfolders. See [Local path confinement](#features). |
| `default_download_dir` | none | a local folder | Where `download_file` saves when no `dest_folder` is given. It's always allowed. |
| `default_upload_dir` | none | a NAS folder | Where `upload_file` uploads when no `dest_folder` is given. |

**Accounts.** Any account. The tools see the shared folders that account can see, and can change only what its DSM permissions allow.

**DSM versions:** 7.1 – 7.4.1, the same tools on each.

**Not covered:** compress and extract, share links, favorites, thumbnails, MD5 checksums, permissions and ownership changes, mounting remote folders.

<details>
<summary><b>Good to know:</b> long operations, client timeouts, folder downloads</summary>

- **Long operations hand off.** For delete, copy, move and restore, the timeout is **how long a call waits before handing off**, not a limit on the operation. An operation that finishes within the wait returns its result as usual. One that's still running is left running on the NAS, and the reply gives a handle, such as `delete:FileStation_…`, to use with `file_task`: check its progress or final result, or stop it. `file_task` with no handle lists the operations still running. The wait is capped at 600 s whatever the setting, because an MCP client that gives up on a call cancels it, and a canceled call stops the operation.
- **Client timeouts.** The MCP client has its own time limits. Claude Code aborts a tool call that sends no result and no progress notification for an idle window: 30 minutes by default for a stdio server like this one (5 minutes for HTTP servers). Raise it with the `CLAUDE_CODE_MCP_TOOL_IDLE_TIMEOUT` environment variable (milliseconds; `0` turns the check off), or for this server only with a `timeout` field (milliseconds) in its MCP config entry, which also sets a hard limit per call. See [Claude Code's MCP docs](https://code.claude.com/docs/en/mcp). Delete, copy, move and restore send progress notifications while they wait, so they don't hit the idle limit. `get_dir_size` and `search_files` don't send progress yet (#252), so one that runs longer than the idle window can be aborted by the client; raise the client's limit, or narrow the path.
- **Folder downloads.** `download_file` of a folder saves DSM's ZIP of it as `<folder>.zip` (or under `filename`, when given). A whole shared folder can't be downloaded this way (DSM doesn't send one), so it's refused.

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
| `task_timeout` | `30` | 1–600 s | How long `copy_photos`, `move_photos`, `delete_photos` and `delete_photo_folders` wait before handing off with a `photos:<id>` handle for `photo_task`. Also when `upload_photos` stops starting new files (at most 45 s, below Claude Desktop's tool-call timeout of about 60 s; it lists the ones not tried, for another call). |
| `allowed_local_paths` | your Downloads folder | local folders | Local folders `upload_photos` reads from and `download_photos` writes to. See [Local path confinement](#features). |

**Accounts.** The account needs the Synology Photos application privilege (**Control Panel → Application Privileges → Synology Photos**), and the personal space needs the user home service (**Control Panel → User & Group → Advanced → User Home**). Shared Space must be enabled in Synology Photos' settings, with access granted to the account. The tools report each of these by name when it's missing.

**DSM versions:** DSM 7.1 (Synology Photos 1.3.4) and DSM 7.2.2, 7.3.2 and 7.4.1 (Synology Photos 1.9.1). **Favorites and concepts** (things and scenes Photos recognized) need Photos 1.9.1; on 1.3.4 they're refused with `not_supported`.

**Not covered:** timeline and similar-photo views, video streaming, Shared Space folder permissions, face-level People edits, guest uploads to photo requests, changing Photos' own settings.

<details>
<summary><b>Good to know:</b> edits, tags, deletes</summary>

- **Edits change the original files.** Photos writes ratings, descriptions, tags, taken-time changes and rotations into the files themselves, so their dates and sizes change; backup and sync tools see new versions.
- **Tags are given by name.** A tag exists only while some item has it, so removing it from every item deletes it.
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
| `allowed_local_paths` | your Downloads folder | local folders | Local folders `create_download`'s `torrent_file_path` may read `.torrent` and `.nzb` files from. Magnet links and URLs aren't affected. See [Local path confinement](#features). |

**Accounts.** An admin (Download Station manager) can use every tool. A non-admin account needs the Download Station application privilege (**Control Panel → User & Group → [account] → Edit → Applications**), and **must set a default destination once** before Download Station will start its tasks: `set_download_config(default_destination="<share>")`. More below.

**DSM versions:** DSM 7.1 (Download Station 3.9-4627) and DSM 7.2.2, 7.3.2 and 7.4.1 (Download Station 4.1-5012), the same tools on each.

**Not covered:** BT search, RSS feeds, choosing files inside a torrent, editing a torrent's trackers, settings other than the speed limits and the default destination (BT, FTP/HTTP, NZB, auto-extract).

<details>
<summary><b>Good to know:</b> non-admin accounts, known limits</summary>

**Non-admin accounts.** All of these are enforced or reported by the tools:

- **First-time setup.** Download Station doesn't start a non-admin's tasks until that user has a default destination; until then they wait forever. A user who has never opened Download Station has none. Set it once with `set_download_config(default_destination="<share>")`, which DSM applies to that user only. Until then, `create_download` with an explicit `destination` still creates the task but warns that it won't start. Without a `destination`, it refuses and says what to set. That refusal applies to any account, admin included, whenever there's no default destination to fall back on.
- **Own tasks only.** `list_downloads` and the other task tools see only the user's own tasks.
- **Settings.** `get_download_config` shows the user's own view, not the NAS-wide settings. `set_download_config` can change only the user's own `default_destination`; the speed limits are NAS-wide and are refused, because DSM accepts and then silently ignores them from a non-admin.
- **Schedule.** The weekly plan and limited-speed rates in `get_schedule` / `set_schedule` need an admin. A non-admin can still turn the schedule on or off, and that switch is NAS-wide.

**Known limits:**

- A `destination` must be an existing folder: Download Station doesn't create missing folders. Create it first with File Station's `create_folder` (`/video/TV/New show`; the `filestation` module needs `permission: write`) or in DSM's own File Station, then pass it share-relative (`video/TV/New show`). An error that says the destination doesn't exist names this fix (#296).
- `create_download` returns no task IDs for URLs that use `username` / `password`.
- `edit_download` changes only the destination.
- `delete_download(force_complete=true)` doesn't reliably keep partial data: DSM discarded unfinished HTTP downloads anyway. DSM also refuses it for finished tasks, and the tool reports those as not removed.

</details>

Spec: [`docs/specs/downloadstation-module-spec.md`](docs/specs/downloadstation-module-spec.md).

### Storage

See the health of the NAS's storage: storage pools and RAID, volumes and free space, disks with S.M.A.R.T., SSD caches, external drives and share snapshots.

- **READ:** `get_storage_overview`, `list_storage_pools`, `list_volumes`, `get_volume`, `list_disks`, `get_disk`, `list_ssd_caches`, `get_storage_schedules`, `get_storage_settings`, `list_external_storage`, `list_snapshots`
- **WRITE:** none yet (phase 2)

```yaml
modules:
  storage:
    enabled: true   # no write tools yet, so no permission is needed
```

**Settings:** none.

**Tools.** Start with `get_storage_overview`: DSM's own status summary, every pool, volume, disk and SSD cache in one line each, anything that needs attention (a degraded pool, a failing disk, a nearly full volume) and the storage tasks running or scheduled next. The others give one area in full, with every field DSM sends: `get_disk` adds every S.M.A.R.T. attribute, test results and history and a 12-month health trend; `get_volume` adds space by category, TRIM, deduplication and the attached SSD cache.

**Accounts.** An administrator account only: Storage Manager answers no other account, so with any other account every tool returns `permission_denied`. `list_snapshots` also needs the Snapshot Replication package.

**DSM versions:** 7.1 – 7.4.1, the same tools on each. A volume's encryption and lock state, a snapshot's immutable flag and the drive database version appear from DSM 7.2.2. A section a NAS doesn't support (deduplication on some models, for example) is left out with a note.

**Not covered:** no WRITE tools yet: phase 2 plans data scrubbing, S.M.A.R.T. tests, hot spares, storage settings, TRIM and snapshot management. iSCSI LUNs and SAN Manager, Hyper Backup and Task Scheduler aren't covered. Anything that wipes disks or changes a pool's layout (creating, deleting, expanding or repairing pools and volumes, secure erase) is left out on purpose.

Spec: [`docs/specs/storage-module-spec.md`](docs/specs/storage-module-spec.md).

## Features

- **Interactive setup** — guided configuration that creates your config, stores credentials, handles 2FA, and emits a Claude Desktop snippet
- **Permission tiers** — READ or WRITE per module, enforced at tool registration. Tiers control what the server can do on the NAS. Tools that write on this machine (`download_file`, `download_photos`) are annotated as destructive so your client can confirm them, and so is every WRITE tool except `create_folder`, `rename`, `upload_photos`, `create_download`, `pause_download` and `resume_download`. A read-only connection tells the model which tools it withholds and how to enable them, so the model doesn't claim the server can't do it
- **Long operations keep running** — a large delete, copy, move or recycle-bin restore reports progress while the call waits, and if it's still running when the wait ends (30 s by default) the NAS keeps working on it: the reply gives a handle for `file_task`, which checks on it, lists running operations or stops one. See [File Station](#file-station). Photos' `copy_photos`, `move_photos`, `delete_photos` and `delete_photo_folders` also hand off, after `task_timeout` (default 30 s, up to 600), with a `photos:<id>` handle for `photo_task`
- **Local path confinement** — `upload_file`, `download_file`, `upload_photos`, `download_photos` and `create_download`'s torrent files only use local folders listed in `allowed_local_paths`, by default your Downloads folder. Set it at the top level of the config for every module, or per module in that module's settings (File Station, Synology Photos and Download Station each have their own list). A module's own list adds to the top-level list when that's set. Without a top-level list, it **replaces** the Downloads default, so include `~/Downloads` if you still want it. `create_download` only reads `.torrent` and `.nzb` files. Symlinks and `..` are resolved first, so they can't lead outside. Set it to `["/"]` to allow everything (macOS/Linux)
- **2FA support** — auto-detected; device token bootstrap with automatic silent re-auth
- **Secure credentials** — OS keyring integration that works transparently on macOS, Windows, and Linux (including from Claude Desktop). See [docs/credentials.md](docs/credentials.md).
- **Multi-NAS** — manage multiple NAS devices with separate configs, credentials, and state

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
```

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

## 2FA Support

mcp-synology fully supports DSM accounts with two-factor authentication. It's auto-detected — you don't need to configure anything special:

1. **Bootstrap** — `mcp-synology setup` detects 2FA, prompts for your OTP code, and stores a device token in the keyring
2. **Silent re-auth** — subsequent logins use the device token automatically (no OTP prompts)
3. **Per-instance** — each NAS config gets its own device token, so mixed 2FA/non-2FA setups work fine

Device tokens have no fixed lifetime: they last until the trusted device is removed in DSM (Personal > Security > Sign-in Activity) or DSM otherwise stops accepting it. If DSM rejects the stored token, the error says so; run `mcp-synology setup` again to sign in with a new 2FA code (if `device_id` is set in the environment, `SYNOLOGY_DEVICE_ID`, or the config file, it overrides the keyring: remove it there first, then restart the server after `setup`).

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

## Migrating from synology-mcp

The package was called `synology-mcp` up to v0.3.x. If you still have that install, a migration script moves its config and state directories, keyring credentials and Claude Desktop entry to the new names:

```bash
curl -O https://raw.githubusercontent.com/cmeans/mcp-synology/main/scripts/migrate-from-synology-mcp.py
python migrate-from-synology-mcp.py          # dry run — preview changes
python migrate-from-synology-mcp.py --apply  # apply changes
```

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
