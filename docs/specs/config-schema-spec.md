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
| `instance_id` | `str` | No | Derived from `connection.host` | Unique identifier for this instance. Alphanumeric + hyphens only, at most 63 characters. |
| `alias` | `str` | No | — | Human-friendly name for this NAS (e.g., "HomeNAS"). Used as `display_name` in tool output and server identity. |
| `check_for_updates` | `bool` | No | `true` | Check PyPI for a newer version, in the background when the server starts (at most once a day, or hourly while PyPI doesn't answer; the result is cached for every install on the machine). `false` turns off the check and every notice for this config. |
| `update_notices` | `str` | No | `all` | Which update notices reach the model for this config (#314): `all` (an ordinary release once a week, on the first tool result, recorded per install so connections sharing an install say it once; a security release in every session, in the server instructions when the previous check already knew of it, else on the first tool result), `security` (only security releases) or `off` (none; YAML's unquoted `off`, read as false, works too). The WARNING log line and `mcp-synology check` report a newer version either way. Per config, since configs may run different installs. |
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
| `host` | `str` | Conditional | — | IP address or hostname, bare: no scheme, user name, port or path, and an IPv6 address in square brackets, as in a URL, and quoted (`"[fd00::1]"`: unquoted, YAML reads it as a list, which gets its own error saying to quote it). Required unless `SYNOLOGY_HOST` env var is set. See **Validation Rules**. |
| `port` | `int` | No | `5001` if `https`, else `5000` | DSM web interface port, 1–65535. |
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
- `error` — failures that prevent operation, plus the security warnings, which show at any level (#533).

All modules use `logging.getLogger(__name__)` so log output includes the full module path (e.g., `mcp_synology.core.client`), making it easy to trace messages to source.

---

## Validation Rules

At startup, the config is validated with Pydantic. Validation failures produce clear, actionable error messages.

### Hard Errors (server won't start)

- `schema_version` is missing or not an integer
- `schema_version` is higher than what this server version supports (with upgrade guidance)
- The file repeats a key in one mapping (#464): `https: true` then `https: false`, two `password:` lines, or a second, empty `connection:` (which dropped the first one's `https: true`). YAML would keep the last without a word. The error gives both lines (both columns when they're on one line, `{k: 1, k: 2}`; just the line where the `{…}` or section starts when a key came by alias, whose marks aren't its own) and names the key only when it's one the config defines, a module's name, or a key in a module's `settings` (`core/config.py` `_ConfigLoader`, used by `parse_config_text`); any other key may be a password's fragment. A `<<: *anchor` merge's keys may still be overridden, as YAML intends, but two `<<` keys in one mapping are a repeat too (the later one silently won): the error says to merge both in one, later one first, `<<: [*later, *earlier]`, since the first in a list wins, so the result doesn't change. The shared `global.yaml`, which the program writes, is read without these key checks
- A key with no value inside `{…}`, written without a colon right after it (`{username: me, password: Spring,device_id}`; a colon inside a comment doesn't count): YAML splits a value at a comma, so the fragment became a key set to null and the password was cut short with no error (#464). It's refused with its line and the quoting hint, never named, and the advice that a colon needs a space after it (`{host:nas}` is one colon-less key to YAML). Directly under `modules`, its last advice is to give the module a colon (`{filestation: , photos: }`), since leaving it out would drop the module. `{device_id: }`, with the colon, is still a key set to null. Valid YAML that ends a value early isn't refused: `{password: Spring,}` and `password: Spring #1` both load `Spring`, so a password holding `,` or ` #` needs quotes. Both checks read each mapping as written, before anything is built: SafeLoader flattens `<<` merges in place, so a check while building would take an override for a repeat
- `connection.host` is missing AND `SYNOLOGY_HOST` env var is not set
- `connection.host` isn't a bare host name or IP address (#511 PR 2; #476, #471, #504): empty or blank, or the key left with no value; something YAML read as a number, date or true/false (the error says to put it in quotes, without showing it); a scheme (`https://`); a user name (`admin:…@`); a port (`nas:5001`); a path; an IPv6 address without square brackets; square brackets around anything but an IPv6 address; a space or an invisible character (it's taken as given, so ` nas` is refused); a character a host name can't have (``\ ? # % " ' < > ^ ` { | } , ; = & + $ ! * ( ) ~``); an empty part (`.nas`, `nas..local`); a last part that's an ASCII number (or `0x` hex; a digit outside ASCII is an ordinary name character, so `nas.१२३` goes out as `xn--`) when the whole isn't a four-part IPv4 address (`192.168.1`, `127.1`, `2130706433`, `0x7f.0.0.1`, `nas.42`: as in a URL, such a host is an IPv4 address, and the resolver expands it, so `192.168.1` went to 192.168.0.1, typically the router, with the login and its password; judged on the host as httpx uses it, so the ideographic and full-width dots IDNA maps to `.`, as in `192。168。1`, count too); a dot after an IPv4 address (`192.168.1.100.`, which resolves nowhere and derived the instance ID `192`); a part longer than 63 characters as sent, which for a name outside ASCII is its `xn--` form; an IPv6 zone with characters outside RFC 6874's unreserved set (letters, digits, `.`, `_`, `~`, `-`); or anything else the URL library refuses as a host (`httpx.URL`: `999.1.1.1`, `01.2.3.4`, a name IDNA can't encode, a malformed `xn--` first label, whose idna error used to crash `setup`'s host question and print text decoded from the host). The URL is built as `http(s)://{host}:{port}`, so a port or an address httpx refuses ended in a traceback in `check` and `setup`; a scheme, path, space or empty part made a URL that failed to connect; and a user name put a password written into the host into the server instructions, tool errors and logs. The error says each problem with its fix and never shows the host; a bracketed IPv6 address inside a URL (`https://[fd00::1]:5001`) gets only what surrounds it, and the fixes name the source's own setting (`set https: true` / `set connection.port` / `auth` in the file, `SYNOLOGY_HTTPS` / `SYNOLOGY_PORT` / `SYNOLOGY_USERNAME` and `SYNOLOGY_PASSWORD` for `SYNOLOGY_HOST`, the questions in interactive `setup`, which asks for the port since #511 PR 2). A trailing dot after a name (`nas.local.`), names outside ASCII and an IPv6 zone (`[fe80::1%eth0]`) are accepted. Write a zone as is: RFC 6874's URL form, `%25eth0`, is read as the zone `25eth0`, so it's accepted but fails to connect
- `auth.username`, `auth.password` or `auth.device_id` is something YAML read as a number, date or true/false (`password: 123456`): the error says to put it in quotes, without showing it
- `connection.port` is not a valid port number (1–65535)
- `connection.port`, `connection.timeout` or `schema_version` is true or false: pydantic reads a boolean as 1 or 0, so `port: yes` loaded as port 1 and `timeout: on` as one second (#511 PR 2)
- `modules` is missing or empty, or no listed module is enabled (every module `enabled: false`). Either way the server would have no tools. The error names the listed modules and shows the fix (`filestation: {enabled: true}`); `check` and `setup` report it like any other config error (#168)
- `instance_id` is empty or blank (#485: an empty one, set as `""` or derived from a host like `.nas`, which the host rule now refuses first, gave every such instance one keyring entry, `mcp-synology/default`, and session name prefix), is longer than 63 characters, the limit for a part of a host (a config file and a keyring entry are named after it), contains characters other than `a-z`, `0-9`, `-` (after lowercasing), or is something YAML read as a number, date or true/false (the error says to put it in quotes). The key left with no value is "not set". The error doesn't quote it. A derived one that fails (an IPv6 host, an underscore, a letter outside a-z) gets "set instance_id, or the SYNOLOGY_INSTANCE_ID environment variable"
- `logging.file` or `instructions_file` is an empty or blank string (`""`): it names no file, so it's an error, not "none" (the same class as `--config ""`); leave the key out instead. The key left blank (`file:` with nothing after it), which YAML reads as null, is "not set", as an empty section is (Soft Defaults)
- Unknown top-level keys, and unknown keys in `connection`, `auth`, `logging` or directly under `modules.<name>` (catch typos early — strict schema, #386)
- `permission` value is not one of `read`, `write`, `admin`
- `logging.level` value is not one of `debug`, `info`, `warning`, `error`
- An enabled module's settings fail Pydantic validation (if the module provides a `settings_schema`; #498). Errors give the place (`modules.photos.settings.task_timeout`), for every module at once. A disabled or unknown module isn't loaded, so its settings aren't read. Every schema extends `ModuleSettings`, which also refuses true or false for a number (`task_timeout: on` loaded a 1 s timeout) and an empty or blank text setting (`default_download_dir: ""` meant none)
- The config file can't be read: a folder, a device (`/dev/zero` would read until memory ran out), or no permission (#483). `logging.file` can't be opened (its folder is created first; a folder at that path, a socket, or no permission, is the error, #498)

`serve`, `check` and `setup` all run this validation (`server.validate_config`, #511 PR 3), so `check` refuses what `serve` would. `check` and `setup` make `logging.file`'s folder and file too, since it's the same pass; a relative `logging.file` depends on the working folder, so the error shows it made absolute. Records logged while the config loads reach the file only once the whole config has passed, and not under `check -v` or `setup -v`. An error lists the problems of one stage together, and fixing them shows the next stage's: repeated and colon-less keys in file order (the first 50, then a count), then the fields, then the model-level rules, then module settings and the log file. It ends with `Config file: '<path>'`, at any log level, except an `EnvVarError`, which names its variable and has nothing to do with the file. `allowed_local_paths` and `default_download_dir` must resolve (not a NUL; not a symlink loop, on Python 3.11 and 3.12, where it would crash the server), and so must the Downloads default each module falls back to; none of them can resolve to a path with a control character, since the instructions print it. Those two, `logging.file` and `instructions_file` can't hold a control character, a line or paragraph separator or a lone surrogate as written, and `default_upload_dir` can't hold one either. `custom_instructions` can't hold a lone surrogate. `logging.file` can't be a named pipe, other than the server's own stderr (`/dev/stderr`, when that's a pipe, has a reader by definition: there's no way to test another pipe for one, since opening it to look gives its reader end-of-file), the config file or the instructions file, followed through links. A socket can't be opened by name, and an MCP client on Node gives its server's stderr as one (seen on Node 22), so `/dev/stderr` is refused there, with the reason; leaving `logging.file` out logs to stderr anyway; every command appends to it, so the next run would read log lines. `logging.file` is checked as the log handler will open it, with `..` collapsed without following links, so `link/../pipe` can't pass as another file. A config that fails validation can still create the log's folder, which is accepted. `alias` can't be only whitespace or hold control characters, line or paragraph separators or a lone surrogate; joiners are fine, and so are direction marks and an alias of only invisible characters, which can't forge a line (#538). A named pipe as the config file itself isn't refused, since `--config <(generate)` is one, so a pipe nothing writes to waits. An `instructions_file` that can't be used (missing, not UTF-8, not a regular file) falls back to the built-in text: `check` warns, and `serve` logs an ERROR, since the model then gets other instructions than the config names. A derived `instance_id` that fails names `SYNOLOGY_HOST` when that's where the host came from.

### Warnings (server starts, but user is alerted)

- `auth.password` is present in the config file → warn about a plaintext password, recommend keyring. A password from `SYNOLOGY_PASSWORD` gets an env-var variant of the warning instead. `auth.username` alone isn't a secret and gets no warning: a config username with the password in the keyring is the recommended setup (#167)
- `connection.https` is `false` → warning that credentials are sent in cleartext (acceptable on a trusted LAN, risky otherwise), linking `docs/https.md` (#374; it was info-level). `mcp-synology setup` writes `https: true` unless the user declines
- `connection.verify_ssl` is `false` → warn that SSL verification is disabled
- The password, cleartext and SSL-verification warnings above are shown at any log level (#533): logged as WARNING through the handlers in place while the config loads (stderr, and replayed into `logging.file` once it's open, #511 PR 3) even when `SYNOLOGY_LOG_LEVEL=error` would filter them (`core/config.py` `_warn_always`). At `error`, `SYNOLOGY_HTTPS=off` used to turn HTTPS off with no output at all
- A `SYNOLOGY_*` variable changes a value the config file sets for `connection.host`, `connection.port`, `connection.https` or `instance_id` (including the `instance_id` the file's host derives when the file sets none) → a WARNING at any log level, naming the variable and the setting, never the value: `SYNOLOGY_HOST overrides connection.host from the config file. Unset SYNOLOGY_HOST if that isn't intended.` (#533), or, for a derived ID, `SYNOLOGY_INSTANCE_ID overrides the instance_id the config file's host gives. …`. These say which NAS the credentials go to, how, and which keyring entry holds them; it used to be DEBUG only, so `SYNOLOGY_HOST` sent the file's keyring credentials to another host without a word. A variable equal to the file's value in a spelling the config treats as the same (a boolean or port as pydantic reads it, `https: 'on'`, `port: "5001"`; an instance ID in any case; a host name as httpx sends it, in any case, and an international name as its `xn--` form; a bracketed IPv6 address in any case, unless it has a zone, which names an interface and is compared as written), a setting the file doesn't set, and env-only mode get no warning. A variable must itself be a bare host (Hard Errors, above, never a comparison), so anything else in a host is on the file's side, and differs: `nas` over `admin@nas` or `nas:5001`, and `[::1]` over `::1` or `[::1]/dsm`, all warn, and the variable's host replaces the one the file's rule would refuse. Two spellings of one address the comparison doesn't recognize warn too, `[::1]` over `[0:0:0:0:0:0:0:1]`: a false alarm is the safe side
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
- An empty section (`auth:`, `logging:`, `modules:`, a module's entry, its `settings:`; YAML reads each as null) is the same as an empty mapping, whatever the environment sets (#492, #475): `auth:` used to load only when `SYNOLOGY_PASSWORD` or another `auth` variable filled it, so a config working under an MCP client failed under `mcp-synology check`. So `filestation:` with nothing under it is the module with its defaults (enabled, read), and an empty `modules:` gets the "at least one module" error. An empty `connection:` stays "no connection" and gets the host error unless `SYNOLOGY_HOST` fills it

---

## Environment Variable Overrides

Certain config values can be overridden by environment variables. This supports Docker deployments and CI pipelines where config files may not be practical.

| Env Var | Overrides | Notes |
|---------|-----------|-------|
| `SYNOLOGY_HOST` | `connection.host` | — |
| `SYNOLOGY_PORT` | `connection.port` | — |
| `SYNOLOGY_HTTPS` | `connection.https` | `true`/`false`, `1`/`0`, `yes`/`no`, `on`/`off`, any case |
| `SYNOLOGY_USERNAME` | `auth.username` | Part of credential hierarchy |
| `SYNOLOGY_PASSWORD` | `auth.password` | Part of credential hierarchy |
| `SYNOLOGY_DEVICE_ID` | `auth.device_id` | Part of credential hierarchy |
| `SYNOLOGY_INSTANCE_ID` | `instance_id` | — |
| `SYNOLOGY_LOG_LEVEL` | `logging.level` | `debug`, `info`, `warning` or `error`, any case |
| `SYNOLOGY_UPDATE_NOTICES` | `update_notices` | `all`, `security` or `off`, any case |

**Parsing (#511, #501):** each value is validated by pydantic with its field's own type (`core/config.py` `parse_env_value`), so a variable takes what its field's type takes: booleans only as the documented words (`true`/`false`, `1`/`0`, `yes`/`no`, `on`/`off`, any case; not pydantic's one-letter `t`/`f`/`y`/`n`, so `SYNOLOGY_HTTPS=n` can't turn HTTPS off), whole numbers, and choices lower-cased first (the config file's choices are lower-case only; `update_notices` also takes YAML's booleans, as the file does, `no` meaning off). Typed values are stripped of surrounding spaces; text values (host, credentials, `instance_id`) are taken as given, as in the file. A value of the wrong type raises `EnvVarError` naming the variable and what it takes, never the value (it could be a password pasted into the wrong variable), and the server doesn't start. A value that breaks its field's own rule is an `EnvVarError` naming the variable too, never quoting the value (#511 PR 2): `SYNOLOGY_PORT` outside 1–65535 (`must be a whole number from 1 to 65535`), a `SYNOLOGY_HOST` that isn't a bare host name or IP address (the same advice as the file's, with `set SYNOLOGY_PORT` for a port), and a `SYNOLOGY_INSTANCE_ID` with characters other than letters, digits and hyphens (a padded one too, since text is taken as given). An empty or blank `SYNOLOGY_*` variable counts as unset, as the credential variables already did. `MCP_SYNOLOGY_CONFIG` is the exception: set but blank, it's an error (Config File Discovery, below). `SYNOLOGY_HTTPS` used to be true for `true`, `1` or `yes` and false for anything else, so `on` or a typo turned HTTPS off even over `https: true`.

**Precedence:** Environment variables override config file values; overriding the file's host, port, HTTPS or instance ID logs a WARNING (Warnings, above; #533). For credentials the keyring comes last: env vars, then the config file, then the keyring (`core/auth.py`), each value resolved separately. Explicit sources override the keyring default.

---

## Config File Discovery

The server finds its config file in this order:

1. **Explicit `--config` flag:** `mcp-synology serve --config /path/to/config.yaml`. The file must exist, and an empty value (`--config ""`) is an error, not "no flag" (maintainer, 2026-11-02)
2. **Environment variable:** `MCP_SYNOLOGY_CONFIG=/path/to/config.yaml`. The file must exist: a missing one is an error, never env-var-only mode (#499), which would run read-only over plain HTTP instead of the config it names. Set but empty or blank, it's an error too, not "unset" (maintainer, 2026-11-02): an unfilled `""` in an MCP client's env block would otherwise drop the config it was meant to name without a word
3. **Default location:** `~/.config/mcp-synology/config.yaml`
4. **Env-var-only mode:** no config file found by 1–3 and `SYNOLOGY_HOST` set

"Found" means the file is there, or can't be told not to be: a default location that can't be checked is an error, not a reason to go on to env-var-only mode (#483). So `~/.config` without permission stops the server even with `SYNOLOGY_HOST` set, and the error suggests `--config`.

**A config in the working folder is never loaded unless it's named (#552; maintainer, 2026-11-05).** `./mcp-synology.yaml` used to be a default location, so a folder someone merely cloned or opened could supply the whole config. With `instance_id` and a host of its own, it made the server resolve the password stored for that instance, for a login to that host; with `permission`, `allowed_local_paths`, `instructions_file` or `logging.file`, it could widen access, send a local file to the model, or append to a shell's startup file. Tools that hold credentials generally read a named path, or ask first. `--config ./mcp-synology.yaml` is that consent. If the file is there at step 3's end, a warning at any log level says it isn't loaded and how to use it, since that may be why a config seems ignored; a link into a locked folder or a folder that can't be searched says nothing, since nothing can be said about the file. A config named by `--config` or `MCP_SYNOLOGY_CONFIG` keeps its full reach, whatever folder it's in: those settings are the user's own choice.

The source used is logged at INFO (while the config loads, and replayed into `logging.file` if the level allows, #511 PR 3), with the path made absolute and quoted, as in the not-found errors, since an MCP client starts the server in a working folder the user rarely knows, and a newline in a folder's name would otherwise forge a log line (`Config: '<path>' (from --config)`, `… (from MCP_SYNOLOGY_CONFIG)`, `Config: '<path>'`, or `Config: environment variables only …`; the host isn't logged at INFO, since it can hold credentials; DEBUG's `Config loaded:` line has it). The not-found and empty errors say only to fix the path: "leave it out to use the defaults" would lead, with `SYNOLOGY_HOST` set and no default file, to env-var-only mode, the downgrade they refuse (QA round 2 on #529).

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

**Decision: Strict everywhere except within module settings.**

Unknown top-level keys are errors (catches typos like `conection` or `moduels`), and so are unknown keys in `connection`, `auth` and `logging` (`htps`, `verfy_ssl`, `pasword`, `levle`; #386) and directly under `modules.<name>` (`permision`). A dropped `htps: true` left `https` off, so the server logged in, password included, over plain HTTP while the user believed HTTPS was on. A config error doesn't show a value that could be a credential. A validation error shows the rejected value only when it's plain text, a number or true/false outside `auth`, and not an unknown key's (`pasword:` holds the password wherever it's misplaced); an unknown key isn't named when it may be part of a password: any unknown key under `auth`, and one with no value anywhere outside module settings (YAML splits `{password: a,b}` at the comma, so `b` arrives as a key with no value). A near-miss of a key its section defines names that key instead (`auth.<an unknown key close to "password", not shown since it may be, or hold, part of a password: fix its spelling, or, if a value inside {…} has a comma of its own (not the one between entries), quote that value>`), so `pasword` gets the typo's help and `Spring,Password1` doesn't print `Password1` (#469 QA round 2). The quoting hint is conditional and names the comma it means (a value's own, not the one between entries, which a model could otherwise take for "the split" and quote a whole entry, dropping it; #469 QA round 6), since a typo and a split both fit, and a split can leave a fragment with a value too (`{password: Spring,Password1: x}` gives a key `Password1` with the value `x`; #469 QA rounds 4 and 5). Outside `auth`, such a fragment with a value is still named (a misplaced password under `connection`, `logging` or a module containing both `,` and `: `, in flow style): contrived, and hiding every valued unknown key would hide the plain typos this check exists to name. One with no near-miss lists the section's valid keys instead. A key YAML read as something other than text is hidden in every error about it: `modules: {…, 7319}` fails both as a key that isn't text and as a module that isn't a mapping, and neither names it (#469 QA round 4). A key with no value directly under `modules` that isn't a module (`modules: {…, Zq,Hx}`) is still named in its "should be a dictionary" error: contrived, and hiding it would hide a real module name given no value. A file that isn't valid UTF-8 is reported as `The config file isn't valid UTF-8 (line N). Save it as UTF-8 …`, never Python's decode error, which quotes the byte; a key YAML read as something other than text (`{password: Summer,2024}` splits off a key `2024`, here or in a module's `settings`) is never named or quoted, only the mapping it's in; a near-miss is matched in any case (`USERNAME` reads close to `username`); `connection.host` is never shown in an error (it can hold a password, `admin:…@nas`; #511 PR 2), and `setup --list` shows it only when it's a bare host the server would accept (and a config's name only when it's text); an `instance_id`, set or derived, isn't quoted either, whatever its type (`instance_id: 123` used to show `(got 123)`); never a whole section or config, which a root or missing-field error carries as its input (env-var overrides such as `SYNOLOGY_PASSWORD` included), and never a plain value given where a section belongs (`connection: https://admin:…@nas`). A value the YAML file reuses through an anchor (`&p` under `auth`, `*p` elsewhere) is judged where it's used, so it can be shown: not handled, as contrived. Every config file is parsed through `parse_config_text` (`load_config` and `setup --list`; the shared `global.yaml` state file too, without the key checks, where a value it can't build is read as no state, #475), which turns any error building a value (`!!int`, `!!bool`, `!!timestamp` on text) into `ConfigValueError` without its text. A YAML error names the line and column (where the value starts, for an unterminated quote or bracket; a character YAML refuses is given as an offset) and the config key on that line (only a key the config defines, so a password continued on its own line isn't taken for one unless that line starts with a config key's name), never the text: PyYAML's own message quotes the line, and its problem text can quote the value (`found undefined alias 'Hunter2x'` for `password: *Hunter2x`; `!!int Hunter2x` raises a `ValueError` naming it). Unknown keys within `modules.*.settings` (named in the warning, valueless or not: settings hold no credentials), and unknown module names, are warnings (allows modules to evolve their settings without the core schema needing updates).

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
   The root must be a mapping (an empty file is treated as an empty mapping).
   A repeated key, or a colon-less key inside `{…}`, is refused here (#464).
   An empty section becomes a mapping when an environment override fills it (and in
   validation either way, #492); a non-mapping section is refused before applying
   that override.
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

**Decision: Modules may optionally declare a Pydantic settings model. If they do, the core validates against it. If not, the module takes no settings, and any it's given are reported as unknown.**

The module interface for this:

```python
@dataclass
class ModuleInfo:
    name: str
    description: str
    required_apis: list[ApiRequirement]
    tools: list[ToolInfo]
    settings_schema: type[ModuleSettings] | None = None  # Optional; a Pydantic model
```

If `settings_schema` is provided:
- The core validates `modules.*.settings` against it at startup (`server.module_settings`)
- Invalid settings produce clear error messages with field names and expected types
- The module receives a validated, typed settings object (`ctx.settings_as(FileStationSettings)`)

If `settings_schema` is `None`:
- The module receives no settings
- Any key there produces a warning ("<module> has no settings"), but not an error

This gives a module with settings full type safety.

Example for File Station:

```python
class FileStationSettings(ModuleSettings):
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
