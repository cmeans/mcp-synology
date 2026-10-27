# Config Schema — YAML Structure

> **Version:** 0.2 | **Updated:** 2026-03-16T22:30Z — Resolved all open items: env-var-only config (Option B), schema_version field, hybrid module settings validation.
> **Parent doc:** `mcp-synology-architecture.md` v0.3

## Overview

The config file is the single user-edited input to the mcp-synology server. It is **never modified by the server** — runtime state goes in a separate state file. The config's job is to answer three questions:

1. **Where is the NAS?** (connection details)
2. **What modules should be active, and with what permissions?** (module config)
3. **Any overrides for defaults?** (optional tuning)

---

## Minimal Config (Quick Start)

The smallest useful config file. The `mcp-synology setup` CLI fills in credentials via the keyring, so they don't appear here:

```yaml
# ~/.config/mcp-synology/config.yaml
schema_version: 1

connection:
  host: 192.168.1.100

modules:
  filestation:
    enabled: true
```

That's it. Everything else has sensible defaults:
- Port defaults to `5000` (HTTP) or `5001` (HTTPS)
- HTTPS defaults to `false` (most home LANs use HTTP internally)
- Instance ID auto-generated from hostname if not specified
- Permission tier defaults to `read`
- Credentials come from the keyring (populated by `mcp-synology setup`)

---

## Full Config (Annotated Reference)

```yaml
# mcp-synology configuration
# Docs: https://github.com/cmeans/mcp-synology#configuration

# ─── Schema Version ──────────────────────────────────────────────
# Required. Identifies the config format version so the server can
# detect incompatible configs from older/newer versions and provide
# clear upgrade instructions rather than cryptic validation errors.
schema_version: 1

# ─── Instance Identity ───────────────────────────────────────────
# Unique identifier for this server instance. Used for:
#   - Keyring namespacing (mcp-synology/{instance_id})
#   - DSM session naming (MCPSynology_{instance_id}_{uuid})
#   - State file location (~/.local/state/mcp-synology/{instance_id}/)
#   - Claude Desktop server name differentiation (multi-NAS setups)
#
# Default: derived from connection.host (sanitized to alphanumeric + hyphens)
# Override when: you have multiple configs pointing at the same host (e.g.,
# different accounts), or you want a human-friendly name.
instance_id: primary

# ─── NAS Connection ──────────────────────────────────────────────
connection:
  # Required. IP address or hostname of the Synology NAS.
  host: 192.168.1.100

  # Port for DSM web interface.
  # Default: 5001 if https is true, 5000 if https is false.
  port: 5000

  # Use HTTPS for API connections.
  # Default: false
  # Set to true if you've configured a certificate on your NAS,
  # or if connecting over an untrusted network.
  https: false

  # Verify SSL certificate when using HTTPS.
  # Default: true
  # Set to false for self-signed certificates (common on home NAS).
  # ⚠ Only disable on trusted networks.
  verify_ssl: true

  # Connection timeout in seconds for individual API requests.
  # Default: 30
  timeout: 30

# ─── Authentication ──────────────────────────────────────────────
# Each value (username, password, device_id) is resolved separately;
# the first source that has it wins:
#   1. Environment variables: SYNOLOGY_USERNAME, SYNOLOGY_PASSWORD,
#      SYNOLOGY_DEVICE_ID (explicit override)
#   2. Values in this section
#   3. OS keyring (the default, populated by `mcp-synology setup`)
#
# Recommended: run `mcp-synology setup`, which stores the password and
# any 2FA device token in the keyring. A username here is fine: it
# isn't a secret.
auth:
  # username: my_mcp_user
  # ⚠ INSECURE: a password or device_id here is stored in plaintext on
  # disk (the server warns about a password at startup). Use the keyring
  # or env vars instead.
  # password: my_password
  # device_id: AbCdEf123456    # From 2FA bootstrap

# ─── Modules ─────────────────────────────────────────────────────
# Each key is a module name. Modules not listed here are not loaded.
# Only enabled modules register tools with the LLM.
modules:
  filestation:
    # Whether to load this module. Default: true (if listed).
    # Set to false to temporarily disable without removing the config block.
    enabled: true

    # Permission tier for this module. Controls which tools are registered.
    #   read  — list, search, get info, browse recycle bin (default)
    #   write — all read tools + move, copy, delete, create, rename, restore
    #   admin — all write tools + system-level operations (future)
    # Default: read
    permission: write

    # Module-specific settings (optional, all have defaults)
    settings:
      # Hide #recycle folders from list_files output.
      # Default: false
      hide_recycle_in_listings: false

      # Output style for file type indicators.
      #   emoji — 📁 📄 🎬 (default)
      #   text  — [DIR] [FILE] [VIDEO]
      # Default: emoji
      file_type_indicator: emoji

      # Time limit for search_files and get_dir_size, in seconds.
      # Delete, copy, move and restore don't use it: they wait delete_timeout /
      # copy_move_timeout (default 30, capped at 600) and then hand off (#245).
      # Default: 120
      async_timeout: 120

      # Seconds search_files waits between checks on DSM's search while it runs.
      # Accepted: 0.5-10.0. Default: 1.0
      search_poll_interval: 1.0

      # Local folders upload_file may read from and download_file may write to,
      # including anything below them. Paths are compared after resolving ~, ..
      # and symlinks. "/" allows everything on macOS and Linux; on Windows list
      # each drive ("C:\\", "D:\\"). default_download_dir is always allowed too.
      # Entries must be absolute or start with ~.
      # Added to the top-level allowed_local_paths when that's set (#257).
      # Default, with no top-level list: the user's Downloads folder (XDG
      # user-dirs on Linux, ~/Downloads on macOS, %USERPROFILE%\Downloads on
      # Windows)
      # allowed_local_paths:
      #   - ~/Downloads
      #   - ~/Media

  # downloadstation:
  #   enabled: true
  #   permission: write
  #   settings:
  #     # Folders create_download may read .torrent/.nzb files from, added to the
  #     # top-level allowed_local_paths (same default rule as File Station's).
  #     allowed_local_paths:
  #       - ~/Torrents

  # photos:
  #   enabled: true
  #   permission: write
  #   settings:
  #     # Folders upload_photos reads from and download_photos writes to, added to
  #     # the top-level allowed_local_paths (same default rule as File Station's).
  #     allowed_local_paths:
  #       - ~/Pictures
  #     task_timeout: 30  # seconds before a copy, move or delete hands off, or an upload stops

  # Example: future module (not yet implemented)
  # docker:
  #   enabled: true
  #   permission: read
  #   settings:
  #     # Module-specific settings would go here

  # Example: another future module
  # system_info:
  #   enabled: true
  #   permission: read

# ─── Local folders ───────────────────────────────────────────────
# Optional. Local folders every module's local-path tools may use (#257):
# upload_file/download_file, and create_download's torrent files. A module's
# own allowed_local_paths (in its settings) adds to this list. Unset: each
# module uses its own list, or the Downloads folder.
# allowed_local_paths:
#   - ~/Downloads

# ─── Logging ─────────────────────────────────────────────────────
# Optional. Controls server-side logging behavior.
logging:
  # Log level: debug, info, warning, error
  # Default: info
  level: info

  # Log file path. If not set, logs go to stderr only.
  # Useful for debugging when the server is launched by Claude Desktop
  # (where stderr may not be visible).
  # Default: null (stderr only)
  # file: ~/.local/state/mcp-synology/primary/server.log
```

---

## Schema Definition

### Top-Level Keys

| Key | Type | Required | Default | Description |
|-----|------|----------|---------|-------------|
| `schema_version` | `int` | **Yes** | — | Config format version. Current: `1`. |
| `instance_id` | `str` | No | Derived from `connection.host` | Unique identifier for this instance. Alphanumeric + hyphens only. |
| `alias` | `str` | No | — | Human-friendly name for this NAS (e.g., "HomeNAS"). Used as `display_name` in tool output and server identity. |
| `check_for_updates` | `bool` | No | `true` | Check PyPI for newer versions on first tool call. |
| `custom_instructions` | `str` | No | — | Additional instructions prepended to the built-in server prompt (higher priority). Supports template variables: `{display_name}`, `{instance_id}`, `{host}`, `{port}`, `{home_dir}`, `{platform}`; only these are replaced, and any other brace (a JSON example, an unknown `{name}`) is left as written. Before 0.10.0 such a brace made `serve` fail with `KeyError`. Useful for guiding Claude on when to use this connection vs others. HTML comments (`<!-- ... -->`) are removed before the text is sent, so they can hold notes for whoever edits the file. |
| `instructions_file` | `str` | No | — | Path to a custom `.md` file that **replaces** the built-in server instructions entirely. Start from the built-in files in `src/mcp_synology/instructions/` (see **Built-in instructions** below). Same template variables are supported, with other braces left as written. Takes precedence over `custom_instructions`. Unlike the built-in or prepended instructions, a replacement file doesn't get the generated PERMISSIONS section (below). HTML comments (`<!-- ... -->`) are removed before the text is sent, so they can hold notes for whoever edits the file. |
| `connection` | `object` | No | — | NAS connection details. Required unless `SYNOLOGY_HOST` env var is set. |
| `auth` | `object` | No | `{}` | Credentials that override the keyring (env vars override these). A username here is fine; a password or device_id is stored in plaintext. |
| `modules` | `object` | **Yes** | — | Module enable/disable and configuration. |
| `logging` | `object` | No | See defaults | Logging configuration. |
| `allowed_local_paths` | `list[str]` | No | — | Local folders that every module's local-path tools may use (#257): File Station's `upload_file`/`download_file`, Download Station's `create_download` torrent files. A module's own `allowed_local_paths` adds to this list. While it's unset, each module keeps the rule from before #257: its own list replaces the default, the user's Downloads folder. When it's set, the Downloads default is not added implicitly. Same entry rules and checks as File Station's setting below (absolute or `~`; empty or relative is a config error). |

**Built-in instructions (#169).** The server sends only the enabled modules' guidance, in this order: `custom_instructions` if set; `instructions/server.md` (server identity, size units and how times are shown, on every connection); each enabled module's own file (`filestation.md` when File Station is enabled, at any permission, then `filestation-write.md` when its WRITE tools are registered); the Synology Photos section when Photos is enabled (its paragraph on deletes and handed-off tasks only when the Photos WRITE tools are registered); the PERMISSIONS section below; and, when a tool that reads or writes local files is registered, `local.md` (LOCAL MACHINE: the local OS and home folder) followed by the LOCAL PATHS section naming the allowed local folders. A module's WRITE-tool guidance is sent only when those tools are registered (`permission: write` or `admin`); at `permission: read` only the PERMISSIONS section names them, and LOCAL PATHS names only the registered tools (#318). HTML comments are removed from each file separately. `instructions_file` replaces all of it; if that file can't be read, the error is logged and the server sends the built-in instructions as if it weren't set.

**Generated PERMISSIONS section.** Unless `instructions_file` replaces them, the server instructions include a PERMISSIONS section when the connection withholds tools. It lists each enabled module's tools above its `permission` tier (e.g. File Station's 8 WRITE tools at `permission: read`) and each registered module that isn't enabled (absent from `modules:`, or listed with `enabled: false`), with the config change that enables it (`permission: write`, adding the module, or `enabled: true`), and tells the model to say such a tool exists but isn't enabled. Registration is unaffected.

### `connection` Object

| Key | Type | Required | Default | Description |
|-----|------|----------|---------|-------------|
| `host` | `str` | Conditional | — | IP address or hostname. Required unless `SYNOLOGY_HOST` env var is set. |
| `port` | `int` | No | `5001` if `https`, else `5000` | DSM web interface port. |
| `https` | `bool` | No | `false` | Use HTTPS. |
| `verify_ssl` | `bool` | No | `true` | Verify SSL certificate (only relevant if `https: true`). |
| `timeout` | `int` | No | `30` | Connection timeout in seconds. |

### `auth` Object

| Key | Type | Required | Default | Description |
|-----|------|----------|---------|-------------|
| `username` | `str` | No | — | ⚠ Plaintext. DSM account username. |
| `password` | `str` | No | — | ⚠ Plaintext. DSM account password. |
| `device_id` | `str` | No | — | ⚠ Plaintext. 2FA device token from bootstrap. |

### `modules` Object

Each key is a module name (e.g., `filestation`, `docker`, `system_info`). The value is a module config object:

| Key | Type | Required | Default | Description |
|-----|------|----------|---------|-------------|
| `enabled` | `bool` | No | `true` | Whether to load the module. |
| `permission` | `str` | No | `"read"` | Permission tier: `read`, `write`, or `admin`. |
| `settings` | `object` | No | `{}` | Module-specific settings. Schema depends on the module. |

### `modules.filestation.settings` Object

| Key | Type | Required | Default | Description |
|-----|------|----------|---------|-------------|
| `hide_recycle_in_listings` | `bool` | No | `false` | Hide `#recycle` folders from `list_files` output. |
| `file_type_indicator` | `str` | No | `"emoji"` | File type display: `emoji` or `text`. |
| `async_timeout` | `int` | No | `120` | Default timeout (seconds, 10–3600) for File Station's Search and DirSize background tasks. Wall-clock time, including slow status requests (#237). It doesn't apply to delete, copy, move or restore (#245). |
| `search_timeout`, `dir_size_timeout` | `int` | No | `async_timeout` | Per-operation overrides (seconds, 10–3600). The task is stopped when it runs out. |
| `delete_timeout`, `copy_move_timeout` | `int` | No | `30` | How long `delete_files` (`delete_timeout`) and `copy_files`, `move_files` and `restore_from_recycle_bin` (`copy_move_timeout`) wait before handing a still-running task off (seconds, 10–3600 accepted). The wait is capped at **600 s** whatever the setting, below known client timeouts (a client's timeout arrives as a cancel, which stops the task). A handed-off task keeps running on the NAS and the reply gives a handle for `file_task` (#245). |
| `upload_timeout`, `download_timeout` | `int` | No | `300` | Transfer timeouts (seconds, 10–3600). |
| `search_poll_interval` | `float` | No | `1.0` | Seconds `search_files` waits between checks on DSM's search while it runs (0.5–10.0). |
| `allowed_local_paths` | `list[str]` | No | the user's Downloads folder (unless the top-level `allowed_local_paths` is set) | Local folders `upload_file` and `download_file` may use, including subfolders, added to the top-level `allowed_local_paths` when that's set (#257). Compared after resolving `~`, `..` and symlinks; `/` allows everything on macOS/Linux (on Windows, list each drive). `default_download_dir` is always allowed. Entries must be absolute or start with `~` (a relative entry would depend on the server's working directory); an empty list, a relative entry, or one that can't be resolved (a NUL byte, a symlink loop) is a config error. A path outside is refused with `permission_denied` before any NAS request; a path that can't be resolved is `invalid_parameter`. The refusal shows where a path resolves to only when the path itself is inside an allowed folder (a symlink there leading out), so it never reveals what exists outside. |

### `modules.downloadstation.settings` Object

| Key | Type | Required | Default | Description |
|-----|------|----------|---------|-------------|
| `allowed_local_paths` | `list[str]` | No | the user's Downloads folder (unless the top-level `allowed_local_paths` is set) | Local folders `create_download` may read a `torrent_file_path` from, added to the top-level list when that's set (#257). Same entry rules and checks as File Station's. Only `.torrent` and `.nzb` files are read. |

### `modules.photos.settings` Object

| Key | Type | Required | Default | Description |
|-----|------|----------|---------|-------------|
| `allowed_local_paths` | `list[str]` | No | the user's Downloads folder (unless the top-level `allowed_local_paths` is set) | Local folders `upload_photos` may read from and `download_photos` may write to, added to the top-level list when that's set (#257, #258). Same entry rules and checks as File Station's. |
| `task_timeout` | `int` | No | `30` | Seconds `copy_photos`, `move_photos`, `delete_photos` and `delete_photo_folders` wait before handing a still-running task off with a `photos:<id>` handle for `photo_task` (1–600). `upload_photos` starts no new file after it, or after 45 s if it's longer, so a large value for long copies doesn't make uploads outlast a client's tool-call timeout (about 60 s on Claude Desktop), and lists the files not tried (#416). |

### `logging` Object

| Key | Type | Required | Default | Description |
|-----|------|----------|---------|-------------|
| `level` | `str` | No | `"info"` | Log level: `debug`, `info`, `warning`, `error`. |
| `file` | `str` | No | `null` | Log file path. If not set, logs to stderr only. |

**Logging level guidance:**
- `debug` — detailed operational trace for troubleshooting: every DSM API request/response (passwords masked), credential resolution steps, config discovery, version negotiation, API cache contents, session lifecycle, module registration. Use this when something isn't working.
- `info` — significant lifecycle events: successful auth, re-auth, config notes (e.g. a module disabled).
- `warning` — configuration issues (plain HTTP, SSL verification off, unknown modules, deprecated settings).
- `error` — failures that prevent operation.

All modules use `logging.getLogger(__name__)` so log output includes the full module path (e.g., `mcp_synology.core.client`), making it easy to trace messages to source.

---

## Validation Rules

At startup, the config is validated with Pydantic. Validation failures produce clear, actionable error messages.

### Hard Errors (server won't start)

- `schema_version` is missing or not an integer
- `schema_version` is higher than what this server version supports (with upgrade guidance)
- `connection.host` is missing AND `SYNOLOGY_HOST` env var is not set
- `connection.port` is not a valid port number (1–65535)
- `modules` is missing or empty, or no listed module is enabled (every module `enabled: false`). Either way the server would have no tools. The error names the listed modules and shows the fix (`filestation: {enabled: true}`); `check` and `setup` report it like any other config error (#168)
- `instance_id` contains characters other than `a-z`, `0-9`, `-` (after lowercasing)
- Unknown top-level keys (catch typos early — strict schema)
- `permission` value is not one of `read`, `write`, `admin`
- `logging.level` value is not one of `debug`, `info`, `warning`, `error`
- Module settings fail Pydantic validation (if the module provides a `settings_schema`)

### Warnings (server starts, but user is alerted)

- `auth.password` is present in the config file → warn about a plaintext password, recommend keyring. A password from `SYNOLOGY_PASSWORD` gets an env-var variant of the warning instead. `auth.username` alone isn't a secret and gets no warning: a config username with the password in the keyring is the recommended setup (#167)
- `connection.https` is `false` → warning that credentials are sent in cleartext (acceptable on a trusted LAN, risky otherwise), linking `docs/https.md` (#374; it was info-level). `mcp-synology setup` writes `https: true` unless the user declines
- `connection.verify_ssl` is `false` → warn that SSL verification is disabled
- A module is listed but `enabled: false` → info-level note (helps users remember they disabled something)
- Unknown keys within `modules.*.settings`, and unknown module names → warn but don't fail (forward-compatible for module updates). The warning names the key and the valid ones; `serve` logs it and `check` prints it (`server.config_notices`, #368). Before #368 nothing warned: an `allowed_local_paths` typo silently left the Downloads default in force
- Unknown keys directly under `modules.<name>` (anything but `enabled`, `permission`, `settings`) → **error** (#368): there's no forward-compatibility case, and a dropped `permision: write` silently left the module read-only

### Soft Defaults

- If `instance_id` is not set, derive it from `connection.host`:
  - IP addresses: replace dots with hyphens (`192.168.1.100` → `192-168-1-100`)
  - Hostnames: use the first component (`nas.local` → `nas`)
- If `connection.port` is not set, infer from `connection.https`
- If a module block has no `permission` key, default to `read`
- If a module block has no `enabled` key, default to `true` (listing it implies you want it)

---

## Environment Variable Overrides

Certain config values can be overridden by environment variables. This supports Docker deployments and CI pipelines where config files may not be practical.

| Env Var | Overrides | Notes |
|---------|-----------|-------|
| `SYNOLOGY_HOST` | `connection.host` | — |
| `SYNOLOGY_PORT` | `connection.port` | — |
| `SYNOLOGY_HTTPS` | `connection.https` | `"true"` or `"1"` for true |
| `SYNOLOGY_USERNAME` | `auth.username` | Part of credential hierarchy |
| `SYNOLOGY_PASSWORD` | `auth.password` | Part of credential hierarchy |
| `SYNOLOGY_DEVICE_ID` | `auth.device_id` | Part of credential hierarchy |
| `SYNOLOGY_INSTANCE_ID` | `instance_id` | — |
| `SYNOLOGY_LOG_LEVEL` | `logging.level` | — |

**Precedence:** Environment variables override config file values. For credentials the keyring comes last: env vars, then the config file, then the keyring (`core/auth.py`), each value resolved separately. Explicit sources override the keyring default.

---

## Config File Discovery

The server finds its config file in this order:

1. **Explicit `--config` flag:** `mcp-synology serve --config /path/to/config.yaml`
2. **Environment variable:** `MCP_SYNOLOGY_CONFIG=/path/to/config.yaml`
3. **Default locations** (first found wins):
   - `~/.config/mcp-synology/config.yaml`
   - `./mcp-synology.yaml` (current directory)

For multi-NAS setups, users must use the `--config` flag to specify which config to load:

```yaml
# Claude Desktop config (claude_desktop_config.json)
{
  "mcpServers": {
    "synology-primary": {
      "command": "uvx",
      "args": ["mcp-synology", "serve", "--config", "~/.config/mcp-synology/primary.yaml"]
    },
    "synology-backup": {
      "command": "uvx",
      "args": ["mcp-synology", "serve", "--config", "~/.config/mcp-synology/backup.yaml"]
    }
  }
}
```

---

## State File

Separate from the config. Server-managed, never user-edited.

**Location:** `~/.local/state/mcp-synology/{instance_id}/state.yaml`

**Contents:**

```yaml
# Auto-generated by mcp-synology. Do not edit.
api_info_cache:
  SYNO.API.Auth:
    path: entry.cgi
    min_version: 1
    max_version: 7
  SYNO.FileStation.List:
    path: entry.cgi
    min_version: 1
    max_version: 2
    request_format: JSON
  # ... (full SYNO.API.Info response, cached)

negotiated_versions:
  SYNO.API.Auth: 6
  SYNO.FileStation.List: 2
  SYNO.FileStation.Search: 2
  # ... (highest compatible version per API)

recycle_bin_status:
  video: true
  docker: false
  music: true
  photo: true

last_connected: "2026-03-16T22:00:00Z"
dsm_version: "7.2.1-69057"
hostname: "MyNAS"
```

**Refresh policy:**
- `api_info_cache`: refreshed on every server startup (fast — single API call)
- `negotiated_versions`: recomputed from cache on startup
- `recycle_bin_status`: refreshed on startup
- `last_connected`, `dsm_version`, `hostname`: updated on successful auth

---

## Example Configs

### Home User — Single NAS, File Management Only

```yaml
schema_version: 1

connection:
  host: 192.168.1.100

modules:
  filestation:
    enabled: true
    permission: write
```

### Power User — Multiple Modules, Custom Settings

```yaml
schema_version: 1
instance_id: nas-primary
alias: HomeNAS
custom_instructions: |
  This is the primary NAS for media and backups.
  Use this connection for all file operations unless the user
  specifies a different NAS by name.

connection:
  host: nas.local
  port: 5001
  https: true
  verify_ssl: false  # Self-signed cert

modules:
  filestation:
    enabled: true
    permission: write
    settings:
      file_type_indicator: text
      async_timeout: 180

  # docker:
  #   enabled: true
  #   permission: read

logging:
  level: debug
  file: ~/.local/state/mcp-synology/nas-primary/server.log
```

### Docker Deployment — All Config via Environment

```yaml
# Docker: host, credentials, and other connection details come from
# environment variables. Only module config lives in the file.
schema_version: 1

# connection block can be omitted entirely — SYNOLOGY_HOST env var
# provides the host, port defaults from SYNOLOGY_HTTPS, etc.

modules:
  filestation:
    enabled: true
    permission: write
```

With environment variables:
```
SYNOLOGY_HOST=192.168.1.100
SYNOLOGY_USERNAME=mcp-service
SYNOLOGY_PASSWORD=secret
SYNOLOGY_DEVICE_ID=AbCdEf123456
```

---

## Design Decisions

### 1. Strict vs Lenient Schema

**Decision: Strict at top level, lenient within module settings.**

Unknown top-level keys are errors (catches typos like `conection` or `moduels`), and so are unknown keys directly under `modules.<name>` (`permision`). Unknown keys within `modules.*.settings`, and unknown module names, are warnings (allows modules to evolve their settings without the core schema needing updates).

### 2. Config File is Read-Only from Server Perspective

**Decision: The server never writes to the config file.**

All server-managed state goes in the state file. This means `mcp-synology setup` writes credentials to the keyring, not to the config file. The config file remains exactly as the user authored it.

### 3. YAML Variable Interpolation

**Decision: No variable interpolation in YAML. Use env var overrides instead.**

Supporting `${VAR}` syntax in YAML adds complexity (custom loader, escaping edge cases, error messages about unresolved variables). The env var override mechanism achieves the same goal more simply: env vars take precedence over config values for all supported keys.

### 4. Two-Phase Config Validation (env var fallback for connection.host) ✓

**Decision: Option B — `connection.host` (and the entire `connection` block) can be omitted if the corresponding env vars are set.**

Config loading follows this sequence:
1. Load raw YAML (structural parse only, no field-level validation)
2. Merge environment variable overrides into the loaded config
3. Apply defaults (port from https, instance_id from host, etc.)
4. Validate the merged config with Pydantic (required fields, types, ranges)

This means Docker deployments can use a minimal config file that contains only `schema_version` and `modules`, with all connection details from environment variables. The trade-off is slightly more complex config loader code, but this is a one-time implementation cost for a significantly better Docker experience.

### 5. Schema Version Field ✓

**Decision: Yes — `schema_version` is a required integer field.**

The server checks `schema_version` before attempting to parse the rest of the config. This enables:
- Clear error messages when a user's config is from an older/newer schema version
- Automated migration guidance ("your config is schema_version 1, this version of mcp-synology expects schema_version 2 — here's what changed")
- The ability to make breaking config changes without silent failures

Current schema version: `1`. The version increments only on breaking changes to the config structure, not on additive changes (new optional keys are backward-compatible).

### 6. Hybrid Module Settings Validation ✓

**Decision: Modules may optionally declare a Pydantic settings model. If they do, the core validates against it. If not, settings are passed through as an untyped dict.**

The module interface for this:

```python
@dataclass
class ModuleInfo:
    name: str
    description: str
    required_apis: list[ApiRequirement]
    tools: list[ToolInfo]
    settings_schema: type[BaseModel] | None = None  # Optional Pydantic model
```

If `settings_schema` is provided:
- The core validates `modules.*.settings` against it at startup
- Invalid settings produce clear error messages with field names and expected types
- The module receives a validated, typed settings object

If `settings_schema` is `None`:
- The core passes `modules.*.settings` as a `dict[str, Any]`
- The module is responsible for its own validation (or lack thereof)
- Any key there produces a warning ("<module> has no settings"), but not an error

This gives mature modules (like File Station) full type safety while letting experimental or third-party modules iterate quickly without fighting the schema.

Example for File Station:

```python
class FileStationSettings(BaseModel):
    hide_recycle_in_listings: bool = False
    file_type_indicator: Literal["emoji", "text"] = "emoji"
    async_timeout: int = Field(default=120, ge=10, le=3600)

MODULE_INFO = ModuleInfo(
    name="filestation",
    ...,
    settings_schema=FileStationSettings,
)
```

---

## Resolved — No Remaining Open Items

All config schema design decisions have been made. The spec is ready for implementation.
