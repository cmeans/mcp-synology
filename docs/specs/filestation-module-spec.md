# File Station Module — Tool Specifications

> **Version:** 0.6 | **Updated:** 2026-10-11. Every LLM-facing description refreshed to the shipped text (all 14 had drifted since 0.4); `get_file_info`'s `additional` is exposed (#146); timestamps are labeled UTC (#137). Earlier: 0.4 (2026-03-18) added upload_file (WRITE) and download_file (READ). Tool counts: 7 READ + 7 WRITE = 14.
> **Parent doc:** `docs/specs/architecture.md`
>
> Kept in sync with the code by `tests/test_module_specs.py`: tool headings, permission tiers, parameter names and the exact LLM-facing descriptions must match what ships.

## Module Metadata

```python
MODULE_INFO = ModuleInfo(
    name="filestation",
    description="Manage files and folders on the Synology NAS via File Station",
    required_apis=[
        ApiRequirement(api_name="SYNO.FileStation.Info", min_version=1),
        ApiRequirement(api_name="SYNO.FileStation.List", min_version=1),
        ApiRequirement(api_name="SYNO.FileStation.Search", min_version=1),
        ApiRequirement(api_name="SYNO.FileStation.DirSize", min_version=1),
        ApiRequirement(api_name="SYNO.FileStation.CreateFolder", min_version=1),
        ApiRequirement(api_name="SYNO.FileStation.Rename", min_version=1),
        ApiRequirement(api_name="SYNO.FileStation.CopyMove", min_version=1),
        ApiRequirement(api_name="SYNO.FileStation.Delete", min_version=1),
        ApiRequirement(api_name="SYNO.FileStation.Upload", min_version=1, optional=True),
        ApiRequirement(api_name="SYNO.FileStation.Download", min_version=1, optional=True),
    ],
    tools=[...],  # See individual tool specs below; 7 READ + 7 WRITE = 14 total
)
```

---

## Design Conventions

### Path Handling

- **Input:** Accept paths with or without leading `/`. Normalize internally by prepending `/` if missing.
- **Output:** Always return fully-qualified paths starting with `/shared_folder/...`.
- **Validation:** Check that the first path component corresponds to a known shared folder (from `SYNO.FileStation.List` / `list_share` cache). Return a clear error with `list_shares` guidance if not. Every path-taking tool does this in its wrapper before any DSM call (#160): the cache is per session, filled on first use, reloaded once when a path names a share it doesn't hold (so a new share is found), and cleared on re-auth. Share names match case-insensitively, as DSM does (`/TESTSHARE/...` works for share `testshare`). An unknown share is a non-retryable `not_found` naming the shares that exist (for an account that isn't an admin, the shares it can see). If DSM won't list shares, the check is skipped and DSM's own error stands. Without it, DSM answers an unknown share with misleading codes: 900 "Unexpected server error" (Delete) and 1100 "Insufficient filesystem permissions" (CreateFolder), on DSM 7.1 and 7.2.2.
- **Escaping:** The DSM API uses commas as separators in multi-path parameters. Commas in paths must be escaped to `\,` and backslashes to `\\`. The core client handles this transparently.

### Response Formatting

All tools use the shared core formatters (`format_table`, `format_key_value`, `format_status`, `format_tree`, `format_error`). Responses are plain text optimized for LLM consumption, not JSON.

### Async Operations

Several File Station APIs are non-blocking (start → poll status → stop/clean). These are abstracted internally — the tool starts the operation, polls at ~500ms intervals, reports progress via MCP progress notifications when available, and returns the final result.

- **Default timeout:** 120 seconds (configurable per-tool via module config if needed).
- **On timeout:** Stop the background task, return an error with the last known progress.
- **Progress reporting:** Via `ctx.report_progress(current, total)` using the MCP progress notification protocol. Whether the client renders this visually depends on the client implementation.
- **Failures without an `error` key:** a finished task can still have failed. CopyMove lists a missing source under `errors` (`[{"code": 408, "path": ...}]`), and a Delete of a missing path finishes with `total: -1` (DSM 7.1 and 7.2.2). Both are reported as structured errors naming the path, never as success (#162).
- **Missing sources are refused up front:** `copy_files`, `move_files` and `restore_from_recycle_bin` check every source with `getinfo` (restore checks the full `#recycle` paths) and refuse the whole call before anything moves, the same policy as `delete_files`. Conflicts with `overwrite=false` are not errors: DSM skips them (`"skipstatus": {"status": "all"}`, no `errors` key) and copies the rest. `skipstatus` is `{"status": "none"}` when nothing was skipped and absent with `overwrite=true`; `"all"` means at least one item was skipped (a whole file, or some of a folder's children), not which. A result with skips starts with `[!]` and a count, then separate `Copied:`/`Moved:` and skip lists. Since `skipstatus` doesn't say what was skipped, each skipped source's type is read with `getinfo` (only when something was skipped): a file goes under `Skipped:` (`[!] Copied 0 of 1 item(s) to <dest>/; 1 skipped …`), and a folder under `Folders with items skipped (anything not already at the destination was copied):` (`… 1 folder with items skipped …`), because DSM still copies or moves the children that weren't at the destination. It doesn't claim anything else was copied: DSM doesn't say whether any children were left to copy. With a folder, K counts the sources done `in full`. A source whose type can't be read (a failed or unexpected `getinfo`) keeps the hedged `Skipped in whole or in part:` list and a note that a skipped folder's other parts still went through (#189). For a move or restore it says skipped items are still at the source (for a skipped folder, inside that folder; otherwise in the source's parent), and names the sources it did remove. It suggests `overwrite=true`. A multi-destination restore with a skip leads with a neutral `[!]` summary line, `Restored K of N item(s) from /<share> recycle bin; M with skips (already at the destination, overwrite=false; details below):`, and the per-destination results below it say which files or folders were skipped (#171, #190). Because paths are processed one task at a time, a later failure names the items already done before it.

### Human-Readable Size Parsing

Any parameter accepting file sizes (`size_from`, `size_to`) accepts both raw bytes (as integers) and human-readable strings. Parsing rules:

- Case-insensitive: `"500mib"`, `"500MiB"`, `"500MB"` all valid
- Supported units: `B`, `KiB`, `MiB`, `GiB`, `TiB`, `PiB`, all binary (1 KiB = 1024 bytes). `KB`, `MB`, `GB`, `TB`, `PB` are accepted too and mean the same binary units, because that's what users type
- Output uses the same binary units and labels them `KiB`, `MiB`, `GiB`, `TiB`, `PiB` (`core/formatting.format_size`), so any size the tools print, `PiB` included, is accepted back as input
- Decimal values allowed: `"1.5GB"`, `"0.5TB"`
- Plain integers treated as bytes: `1048576` = 1 MiB
- Invalid input returns a clear parse error with examples

### Recycle Bin Awareness

Synology's recycle bin is implemented as a `#recycle` subfolder within each shared folder. When enabled on a shared folder, deleted files are moved there rather than permanently removed. Restoring files is done by moving them back out of `#recycle`.

**At module initialization:**
- Query `SYNO.FileStation.Info` / `get` to check NAS-level settings
- Query `list_share` with `additional=["perm"]` to identify which shares exist
- For each share, check if `#recycle` exists (lightweight: attempt `list` on `/<share>/#recycle/` with `limit=0`)
- Cache recycle bin status per shared folder

**Impact on tools:**
- `delete_files` response indicates whether the recycle bin caught the deleted files (based on cached status of the target share)
- `list_recycle_bin` and `restore_from_recycle_bin` convenience tools wrap the standard list/move operations

### Error Handling

Tools return errors using `format_error(operation, error, suggestion)`. Common patterns:

- **Path not found (408):** `"Path '/video/missing' not found. Use list_files to browse available files."`
- **Permission denied (105):** `"Permission denied for '/admin_share'. The MCP service account may not have access to this shared folder."`
- **File exists (414):** `"A file or folder already exists at the destination. Use overwrite=true to replace it."`
- **Name conflict (418/419):** `"Invalid file name. Synology does not allow these characters: / \\ : * ? \" < > |"`
- **Disk full (416):** `"No space left on the target volume."`

API-specific error codes are mapped to human-readable messages with actionable suggestions.

### Tool Description Strategy

MCP tool descriptions are the primary way the LLM learns what each tool does. Given that MCP resources and prompts are not yet well-leveraged by most clients, tool descriptions should be self-sufficient:

- **Each tool description includes:** one-sentence purpose, key behavioral notes (destructive? async?), and the most important parameter names.
- **Avoid repetition across tools.** Shared knowledge (path format, size parsing, common patterns) goes in the MCP server's `instructions` field (set via `MCPServer(instructions=...)`), which the LLM receives once at connection time.
- **Server instructions include:** path format guidance, size parsing examples, the search → review → act workflow pattern, recycle bin location convention, and the `list_shares` "start here" recommendation.
- **Keep tool descriptions under ~200 words** to avoid context window bloat when the LLM has many tools available.

---

## READ Tier Tools (7 tools)

### 1. `list_shares`

List all shared folders (top-level mount points) on the NAS.

**Purpose:** Entry point for file navigation. The LLM should call this first to discover available paths before attempting any file operations.

**Permission tier:** READ

**DSM API:** `SYNO.FileStation.List` / `list_share`

**Tool description (LLM-facing):**
> List all shared folders on the NAS. This is the starting point for file navigation — call this first to discover available paths. Shows each share's volume and that volume's free space (shared by every share on it); for how much space a share uses, call get_dir_size.

**Parameters:**

| Name | Type | Required | Default | Description |
|------|------|----------|---------|-------------|
| `additional` | `list[str]` | No | `["real_path", "owner", "perm", "volume_status"]` | Metadata fields to include. Options: `real_path`, `size`, `owner`, `time`, `perm`, `mount_point_type`, `volume_status`. DSM's `list_share` returns no `size` for a share (seen on DSM 7.1 and 7.2), so the default doesn't ask for it (#151). |
| `sort_by` | `str` | No | `"name"` | Sort field. Options: `name`, `user`, `mtime`, `crtime`. (DSM also accepts `size`, but with no share size it has no useful effect: on vdsm, `size` with `desc` just reverses name order.) |
| `sort_direction` | `str` | No | `"asc"` | Sort direction: `asc` or `desc`. |

**Response shape:**

```
Shared Folders on [NAS hostname]
═══════════════════════════════════════════════════
  Name     Path      Volume   Free               Owner  Recycle Bin
  ───────  ────────  ───────  ─────────────────  ─────  ───────────
  docker   /docker   volume1  1.5 TiB of 36.6 TiB  admin  enabled
  music    /music    volume1  1.5 TiB of 36.6 TiB  admin  enabled
  photo    /photo    volume1  1.5 TiB of 36.6 TiB  admin  enabled
  video    /video    volume2  812 GiB of 7.3 TiB   admin  enabled

4 shared folders found.

Free space is per volume: shares on one volume share it. For how much space a share uses, call get_dir_size on its path.

Paths shown above are the root for all file operations.
Example: to list files in the video share, use list_files(path="/video")
```

**Design notes:**
- The footer text teaches the LLM how to use the paths in subsequent tool calls.
- Volume comes from the first component of `real_path`; Free comes from `volume_status` (`freespace` of `totalspace`, marked `read-only` when `readonly`). A missing field shows `—`, and the per-volume note appears only when some Free value is shown. There is deliberately no per-share size: DSM doesn't send one, and the old Size column read as "0 B" for every share (#151).
- The "Recycle Bin" column shows enabled/disabled status per share (from the cached startup check).
- When `additional` includes `perm`, include a column indicating read-only vs read-write access for the service account.

---

### 2. `list_files`

List files and folders within a given directory path.

**Permission tier:** READ

**DSM API:** `SYNO.FileStation.List` / `list`

**Tool description (LLM-facing):**
> List files and folders in a directory. Supports glob pattern filtering, file type filtering,
> sorting, and pagination.

**Parameters:**

| Name | Type | Required | Default | Description |
|------|------|----------|---------|-------------|
| `path` | `str` | **Yes** | — | Folder path to list (e.g., `/video/TV Shows`). |
| `additional` | `list[str]` | No | `["size", "time"]` | Metadata fields to include. Options: `real_path`, `size`, `owner`, `time`, `perm`, `type`, `mount_point_type`. |
| `pattern` | `str` | No | `None` | Glob pattern filter (e.g., `*.mkv`, `*holiday*`). |
| `filetype` | `str` | No | `"all"` | Filter by type: `all`, `file`, `dir`. |
| `sort_by` | `str` | No | `"name"` | Sort field. Options: `name`, `size`, `user`, `mtime`, `crtime`, `atime`, `type`. |
| `sort_direction` | `str` | No | `"asc"` | Sort direction: `asc` or `desc`. |
| `offset` | `int` | No | `0` | Starting index for pagination. |
| `limit` | `int` | No | `200` | Max items to return (API max: 5000). |

**Response shape:**

```
Contents of /video/TV Shows (5 items)
====================================================
  Type  Name           Size    Modified
  ────  ─────────────  ──────  ───────────────────────
  📁     Breaking Bad/  —       2024-08-15 10:30:00 UTC
  📁     Severance/     —       2024-08-15 10:30:00 UTC
  📁     The Bear/      —       2024-08-15 10:30:00 UTC
  📄     show.nfo       2 KiB    2024-04-03 11:25:00 UTC
  🎬     clip.mp4       284 MiB  2024-08-15 10:30:00 UTC

5 of 5 items.
```

**Design notes:**
- `📁` for directories, `📄` for generic files, `🎬` for video files. Configurable fallback to text markers (`[DIR]`/`[FILE]`).
- Directories show `—` for size (use `get_dir_size` for directory sizes).
- Trailing `/` on directory names for visual distinction.
- Footer: `N of N items.` when everything fits, or `"Showing 200 of 1,432 items. Use offset=200 to see more."` when truncated.
- `#recycle` folders are **hidden by default**. They appear only via `list_recycle_bin` or direct path access.

---

### 3. `get_file_info`

Get detailed metadata for one or more specific files or folders.

**Permission tier:** READ

**DSM API:** `SYNO.FileStation.List` / `getinfo`

**Tool description (LLM-facing):**
> Get detailed metadata for specific files or folders: size, owner, timestamps, permissions, and real path. `additional` optionally picks the DSM fields to fetch: real_path, size, owner, time, perm, type (the file's extension type) and mount_point_type (shown only for a mount point). Default: real_path, size, owner, time, perm. Unknown values are rejected.

**Parameters:**

| Name | Type | Required | Default | Description |
|------|------|----------|---------|-------------|
| `paths` | `list[str]` | **Yes** | — | One or more file/folder paths. |
| `additional` | `list[str]` | No | `["real_path", "size", "owner", "time", "perm"]` | Metadata fields to include. Same options as `list_files`. |

**Response shape (single file):**

```
File Info: /video/Movies/Dune Part Two (2024)/Dune.Part.Two.2024.mkv
═══════════════════════════════════════════════════
  Name:           Dune.Part.Two.2024.mkv
  Path:           /video/Movies/Dune Part Two (2024)/Dune.Part.Two.2024.mkv
  Real path:      /volume1/video/Movies/Dune Part Two (2024)/Dune.Part.Two.2024.mkv
  Type:           File
  Size:           18.4 GiB
  Owner:          admin (users)
  Modified:       2025-02-18 13:42:05 UTC
  Created:        2025-02-18 13:40:12 UTC
  Accessed:       2025-03-15 20:30:00 UTC
  Permissions:    rwxr-xr-x (755)
```

**Response shape (multiple files):** Uses `format_table` with one row per file.

**Design notes:**
- Sizes are human-readable with 1 decimal place. A file's Size is shown only when DSM returned `size`: with an `additional` that leaves it out, there's no Size line (or `—` in the multi-path table), never a false "0 B" (#146). `list_files` and `search_files` do the same.
- `type` renders as "File type" (DSM's extension type, e.g. `ISO`; empty for a folder) and `mount_point_type` as "Mount point", each only when non-empty (#146).
- Timestamps are UTC and labeled as such (`2025-02-18 13:42:05 UTC`), consistent with `search_files` treating naive input dates as UTC (#137).
- For multiple paths, report individual failures inline alongside successes.
- DSM's `getinfo` answers a missing path with a per-file `{"code": 408, "path": ...}` entry, not a failed request (DSM 7.1 and 7.2.2). That entry is the path's error, never a file row (#161). One path, or every path, missing is a structured `not_found` naming each path. A mixed request shows the found entries plus a `Not found: <path>` line for each missing one.

---

### 4. `search_files`

Search for files by name, type, size, or modification date within a given scope.

**Permission tier:** READ

**DSM API:** `SYNO.FileStation.Search` (start → list → stop → clean)

**Tool description (LLM-facing):**
> Search recursively for files by keyword, extension, size, or modification date. The pattern
> parameter is a keyword/substring match on filenames (not glob). Use extension for file type
> filtering (e.g., extension="mkv"). Accepts human-readable sizes like "500MiB". mtime_from / mtime_to
> accept ISO 8601 ("2026-04-01T12:00:00+00:00"), a calendar date ("YYYY-MM-DD"), or Unix epoch
> seconds; naive dates are treated as UTC. For directory contents or sizes, use list_files or
> get_dir_size instead.

**Parameters:**

| Name | Type | Required | Default | Description |
|------|------|----------|---------|-------------|
| `folder_path` | `str` | **Yes** | — | Root folder to search within (e.g., `/video`). |
| `pattern` | `str` | No | `None` | Filename glob pattern (e.g., `*.mkv`, `*Severance*`). |
| `extension` | `str` | No | `None` | File extension filter (e.g., `mkv`, `srt`). Without the dot. |
| `filetype` | `str` | No | `"all"` | Filter: `all`, `file`, `dir`. |
| `size_from` | `str \| int` | No | `None` | Minimum file size. Accepts bytes (int) or human-readable (`"500MiB"`, `"2GiB"`; `"500MB"` means the same). |
| `size_to` | `str \| int` | No | `None` | Maximum file size. Same format as `size_from`. |
| `mtime_from` | `str` | No | `None` | Modified after this date (ISO 8601 or `YYYY-MM-DD`). |
| `mtime_to` | `str` | No | `None` | Modified before this date. |
| `exclude_pattern` | `str` | No | `None` | Glob pattern to exclude from results (e.g., `*.torrent`). Applied client-side after DSM returns results. |
| `recursive` | `bool` | No | `true` | Search subdirectories. |
| `limit` | `int` | No | `500` | Max results to return. |
| `additional` | `list[str]` | No | `["size", "time"]` | Metadata fields for results. |

**Response shape:**

```
Search results in /video (pattern: *Severance*, excluding: *.torrent)
═══════════════════════════════════════════════════
  Type  Name                         Path                                          Size        Modified
  ────  ───────────────────────      ──────────────────────────────────────        ─────────   ──────────────
  🎬    Severance.S02E10.mkv         /video/Downloads/                             1.8 GiB      2025-03-10
  📄    Severance.S02E10.srt         /video/Downloads/                             45 KiB       2025-03-10

2 results found (1 excluded by filter).
```

**Design notes:**
- Async search fully abstracted internally. The task ID, `pattern` and `extension` are sent JSON-quoted, because DSM parses File Station values as JSON first (#121). A `list` reply without `total`/`files` (a bare `{"finished": true}`) is DSM's answer for a task it doesn't know, so it's reported as a lost, retryable task, never as 0 results. A full, finished reply is trusted on the first poll.
- `exclude_pattern` is applied **client-side** because the Synology Search API doesn't support exclusion natively. The response notes how many items were filtered out for transparency.
- If 0 results, suggest broadening the pattern or checking the folder path.
- Timeout: 60 seconds. If hit, return partial results with a note.

---

### 5. `get_dir_size`

Calculate the total size of a directory (recursive).

**Permission tier:** READ

**DSM API:** `SYNO.FileStation.DirSize` (start → status → stop)

**Tool description (LLM-facing):**
> Calculate the total size of a directory, including all files and subdirectories. Returns total size,
> file count, and directory count. This is the best tool for answering 'how much space does X use'
> questions.

**Parameters:**

| Name | Type | Required | Default | Description |
|------|------|----------|---------|-------------|
| `path` | `str` | **Yes** | — | Directory path to measure. |

**Response shape:**

```
Directory Size: /video/TV Shows/The Bear
═══════════════════════════════════════════════════
  Total size:     42.6 GiB
  Files:          186
  Directories:    12
```

---

### 6. `list_recycle_bin`

List contents of a shared folder's recycle bin.

**Permission tier:** READ

**DSM API:** `SYNO.FileStation.List` / `list` (targeting `/<share>/#recycle/`)

**Tool description (LLM-facing):**
> List the contents of a shared folder's recycle bin. Shows recently deleted files that can be
> restored.

**Parameters:**

| Name | Type | Required | Default | Description |
|------|------|----------|---------|-------------|
| `share` | `str` | **Yes** | — | Shared folder name (e.g., `video`) or path (e.g., `/video`). |
| `pattern` | `str` | No | `None` | Glob pattern filter within the recycle bin. |
| `sort_by` | `str` | No | `"mtime"` | Sort field. Default: modification time (most recently deleted first). |
| `sort_direction` | `str` | No | `"desc"` | Sort direction. Default: descending (newest first). |
| `limit` | `int` | No | `100` | Max items to return. |

**Response shape:**

```
Contents of /video/#recycle (3 items)
====================================================
  Type  Name           Size    Modified
  ────  ─────────────  ──────  ───────────────────────
  🎬     old-movie.mkv  4.5 GiB  2024-03-14 12:30:00 UTC
  📁     Season 3/      —       2024-03-08 09:00:00 UTC
  📄     notes.txt      1 KiB    2024-03-01 11:00:00 UTC

3 of 3 items.
```

**Design notes:**
- Convenience wrapper around `list_files` targeting `/<share>/#recycle/`; it returns `list_files`' output as is (title `Contents of /<share>/#recycle`, a `Modified` column).
- Default sort is most recently deleted first.
- If recycle bin is not enabled: `"Recycle bin is not enabled on /video. Deleted files cannot be recovered."`
- The `#recycle` folder preserves original directory structure. Top-level listing shown; deeper browsing possible via `list_files` on `#recycle` subpaths.

---

### 7. `download_file`

Download a NAS file to a local directory on this machine.

**Permission tier:** READ

**DSM API:** `SYNO.FileStation.Download` / `download` (GET, binary response)

**Tool description (LLM-facing):**
> Download a NAS file to a local directory on this machine. Provide the NAS file path. dest_folder is
> optional if default_download_dir is configured. Does not overwrite existing local files by default.

**Parameters:**

| Name | Type | Required | Default | Description |
|------|------|----------|---------|-------------|
| `path` | `str` | **Yes** | — | NAS file path to download. |
| `dest_folder` | `str` | No | `default_download_dir` | Local directory to save the file to. With neither set, `invalid_parameter` (`param: dest_folder`) before any NAS request (#194). |
| `filename` | `str` | No | `None` | Rename on download (defaults to the NAS filename). Must be a plain file name: no `/` or `\`, not `.` or `..`, no NUL, no drive (`D:x`); otherwise `invalid_parameter` before the NAS is contacted. The same rule applies to a name taken from the NAS path. |
| `overwrite` | `bool` | No | `false` | Replace existing local file. |

**Response shape:**

```
[+] Downloaded movie.mkv (4.2 GiB) to /home/user/Downloads/movie.mkv
```

**Design notes:**
- Single-file download only in v1. Multi-file download (returns zip) deferred.
- Binary data streamed to disk in 64 KB chunks — never buffered entirely in memory.
- If the NAS returns `Content-Type: application/json` instead of binary, it's an error envelope — parsed and raised.
- **Never destroys or escapes local files.** The body streams to a temp file `.mcp-synology-<hex>.part` in `dest_folder`, flushed to disk (`fsync`) and then moved onto the destination only when complete. On any failure (DSM error, dropped connection, cancellation) only the temp file is removed; an existing destination is never truncated or deleted. A hard crash (power loss, SIGKILL) can leave a `.part` file behind.
- **`overwrite=false` holds to the end:** the finished file is published with a hard link, which fails if the destination exists (falling back to a final existence check where links aren't supported), so a file created during the download is never replaced; that is `already_exists`. With `overwrite=true` the destination is replaced and keeps its permission bits. Replacing needs free space for both copies until the rename.
- **Containment:** the resulting file must sit directly in `dest_folder`; anything else is `invalid_parameter`.
- **Checked before downloading:** a folder at the destination is `invalid_parameter` in both modes (it can't be replaced). With `overwrite=false`, any name already there, including a dangling symlink, is `already_exists`. With `overwrite=true`, a symlink at the destination is replaced by the downloaded file; its target is untouched. Local write errors name the destination file, never the internal temp file.
- **Annotations:** READ tier (it only reads the NAS), but `readOnlyHint=false`, `destructiveHint=true`, because it writes and can replace a file on this machine. Tiers guard the NAS only (see `architecture.md`).
- The MCP server runs locally, so `dest_folder` refers to a path on the machine running the server.
- **Local path confinement:** `dest_folder` must resolve (after `~`, `..` and symlinks) to a path inside one of the `allowed_local_paths` folders (default: the user's Downloads folder; `default_download_dir` is always allowed). Otherwise it's `permission_denied`, listing the allowed folders, before any NAS request and before the folder's existence is checked. The message shows where the path resolves to only when the path itself is inside an allowed folder (a symlink there that leads out), so a refusal doesn't reveal what exists outside. A path that can't be resolved (a NUL byte, a symlink loop) is `invalid_parameter`. The server instructions list the allowed folders under LOCAL PATHS. See `local_paths.py` and `config-schema-spec.md`.

---

## WRITE Tier Tools (7 tools)

### 8. `upload_file`

Upload a local file from this machine to a NAS folder.

**Permission tier:** WRITE

**Annotations:** `destructiveHint=true`: with `overwrite=true` it replaces the NAS file. Annotations are static, so they can't depend on `overwrite` (#183).

**DSM API:** `SYNO.FileStation.Upload` / `upload` (POST multipart — the ONE exception to the GET-only rule)

**Tool description (LLM-facing):**
> Upload a local file from this machine to a NAS folder. Provide the local file path. dest_folder is
> optional if default_upload_dir is configured. Does not overwrite existing NAS files by default.

**Parameters:**

| Name | Type | Required | Default | Description |
|------|------|----------|---------|-------------|
| `local_path` | `str` | **Yes** | — | Local file path on this machine. |
| `dest_folder` | `str` | No | `default_upload_dir` | NAS destination folder. With neither set, `invalid_parameter` (`param: dest_folder`) before any NAS request (#194). |
| `filename` | `str` | No | `None` | Rename on upload (defaults to the local filename). |
| `overwrite` | `bool` | No | `false` | Replace existing NAS file. |
| `create_parents` | `bool` | No | `true` | Create missing parent directories on the NAS. |

**Response shape:**

```
[+] Uploaded report.pdf (2.4 MiB) to /documents/reports/
```

**Design notes:**
- Upload uses POST multipart — the only DSM API call that requires POST. Documented clearly in the client method.
- File is streamed from disk, not buffered in memory.
- On session error (106/107/119), re-auth and retry with a fresh file handle.
- **`overwrite` is sent only when true (#195).** DSM's Upload reads `overwrite=false` as *skip*: it answers success with `data.blSkip: true` and leaves the existing file alone (verified on DSM 7.2.2; it took ~10 s). With `overwrite` omitted, DSM fails with 414 instead (also after ~10 s). So `overwrite=false` omits the parameter, and both 414 and a `blSkip: true` answer become `already_exists` (`param: filename`, value = the effective name) with a suggestion to use `overwrite=true`. A skipped upload is never reported as uploaded.
- The MCP server runs locally, so `local_path` refers to a path on the machine running the server.
- **Local path confinement:** `local_path` must resolve (after `~`, `..` and symlinks) to a path inside one of the `allowed_local_paths` folders (default: the user's Downloads folder; `default_download_dir` is always allowed). Otherwise it's `permission_denied`, listing the allowed folders, before any NAS request; a symlink inside an allowed folder that points outside is refused. The server instructions list the allowed folders under LOCAL PATHS. See `local_paths.py` and `config-schema-spec.md`.

---

### 9. `restore_from_recycle_bin`

Restore files from a shared folder's recycle bin.

**Permission tier:** WRITE

**Annotations:** `destructiveHint=true`: with `overwrite=true` it replaces the file at the original location (#183).

**DSM API:** `SYNO.FileStation.CopyMove` / `start` (moving from `#recycle` back out)

**Tool description (LLM-facing):**
> Restore deleted files from a shared folder's recycle bin to their original location or a specified
> destination.

**Parameters:**

| Name | Type | Required | Default | Description |
|------|------|----------|---------|-------------|
| `share` | `str` | **Yes** | — | Shared folder name (e.g., `video`). |
| `paths` | `list[str]` | **Yes** | — | Paths within the recycle bin to restore (relative to `#recycle`, or full paths). |
| `dest_folder` | `str` | No | `None` | Destination folder. If omitted, restores to original location (inferred from `#recycle` directory structure). |
| `overwrite` | `bool` | No | `false` | Overwrite if a file already exists at the restore destination. |

**Response shape:**

```
✓ Restored 2 items from /video recycle bin:
  🎬 old_episode.mkv (1.2 GiB) → /video/TV Shows/Some Show/Season 1/
  📄 old_episode.srt (38 KiB) → /video/TV Shows/Some Show/Season 1/
```

**Design notes:**
- **Permission tier is WRITE.** Although the intent is recovery/undo, the operation physically moves files. Keeping it at WRITE is the principled choice — a user who has only opted in to READ-tier tools should not have tools that modify the filesystem.
- Original location inferred by stripping the `#recycle` path component: `/video/#recycle/Shows/Ep1.mkv` → `/video/Shows/Ep1.mkv`.
- If `dest_folder` is specified, all files go there flat.
- If original parent no longer exists, auto-create it.

---

### 10. `create_folder`

Create one or more new folders.

**Permission tier:** WRITE

**Annotations:** `idempotentHint=true` (creating an existing folder succeeds); `destructiveHint=false`, since it only adds folders.

**DSM API:** `SYNO.FileStation.CreateFolder` / `create`

**Tool description (LLM-facing):**
> Create one or more new folders. Creates parent directories automatically by default. Idempotent —
> creating an existing folder succeeds.

**Parameters:**

| Name | Type | Required | Default | Description |
|------|------|----------|---------|-------------|
| `paths` | `list[str]` | **Yes** | — | Full paths of folders to create (e.g., `["/video/TV Shows/New Show/Season 1"]`). |
| `force_parent` | `bool` | No | `true` | Create parent directories if they don't exist. |

**Response shape:**

```
✓ Created 1 folder:
  📁 /video/TV Shows/New Show/Season 1
```

---

### 11. `rename`

Rename a file or folder.

**Permission tier:** WRITE

**Annotations:** defaults (`destructiveHint=false`). It has no `overwrite` option, and DSM refuses an existing target name (error 1200, both names left intact; verified on DSM 7.1 and 7.2.2), so it never replaces anything (#183).

**DSM API:** `SYNO.FileStation.Rename` / `rename`

**Tool description (LLM-facing):**
> Rename a file or folder. Provide the full current path and the new name (just the name, not a full
> path).

**Parameters:**

| Name | Type | Required | Default | Description |
|------|------|----------|---------|-------------|
| `path` | `str` | **Yes** | — | Full path to the file or folder to rename. |
| `new_name` | `str` | **Yes** | — | New name (filename/folder name only, not a full path). |

**Response shape:**

```
✓ Renamed:
  /video/TV Shows/Severence → /video/TV Shows/Severance
```

---

### 12. `copy_files`

Copy files or folders to a destination.

**Permission tier:** WRITE

**Annotations:** `destructiveHint=true`: with `overwrite=true` it replaces files at the destination (#183).

**DSM API:** `SYNO.FileStation.CopyMove` / `start` (with `remove_src=false`)

**Tool description (LLM-facing):**
> Copy files or folders to a destination folder. Source files remain in place. Set overwrite=true to
> replace existing files.

**Parameters:**

| Name | Type | Required | Default | Description |
|------|------|----------|---------|-------------|
| `paths` | `list[str]` | **Yes** | — | Source file/folder paths to copy. |
| `dest_folder` | `str` | **Yes** | — | Destination folder path (must exist). |
| `overwrite` | `bool` | No | `false` | Overwrite existing files at destination. |

**Response shape:**

```
✓ Copied 2 items to /video/Archive/:
  🎬 Severance.S02E09.mkv (1.7 GiB)
  📄 Severance.S02E09.srt (42 KiB)
```

---

### 13. `move_files`

Move files or folders to a new location. **Source files are removed after successful transfer.**

**Permission tier:** WRITE

**Annotations:** `destructiveHint=true`: it removes the sources, and with `overwrite=true` replaces files at the destination.

**DSM API:** `SYNO.FileStation.CopyMove` / `start` (with `remove_src=true`)

**Tool description (LLM-facing):**
> Move files or folders to a new location. Source files are REMOVED after transfer. Set overwrite=true
> to replace existing files.

**Parameters:**

| Name | Type | Required | Default | Description |
|------|------|----------|---------|-------------|
| `paths` | `list[str]` | **Yes** | — | Source file/folder paths to move. |
| `dest_folder` | `str` | **Yes** | — | Destination folder path (must exist). |
| `overwrite` | `bool` | No | `false` | Overwrite existing files at destination. |

**Response shape:**

```
✓ Moved 2 items to /video/TV Shows/Severance/Season 2/:
  🎬 Severance.S02E10.mkv (1.8 GiB)
  📄 Severance.S02E10.srt (45 KiB)

Source files have been removed from /video/Downloads/.
```

---

### 14. `delete_files`

Delete files or folders.

**Permission tier:** WRITE

**Annotations:** `destructiveHint=true`.

**DSM API:** `SYNO.FileStation.Delete` / `start` (async) or `delete` (sync)

**Tool description (LLM-facing):**
> Delete files or folders. If the share has a recycle bin, files can be recovered. Otherwise deletion
> is permanent, as is deleting inside /<share>/#recycle.

**Parameters:**

| Name | Type | Required | Default | Description |
|------|------|----------|---------|-------------|
| `paths` | `list[str]` | **Yes** | — | File/folder paths to delete. |
| `recursive` | `bool` | No | `true` | Delete non-empty folders recursively. |

**Response shape (recycle bin enabled):**

```
✓ Deleted 3 items:
  🎬 /video/Downloads/old_video.mkv (2.1 GiB)
  📄 /video/Downloads/old_video.srt (38 KiB)
  📁 /video/Downloads/temp/ (3 items, 156 MiB)

Recycle bin is enabled on /video — files can be recovered with list_recycle_bin and restore_from_recycle_bin.
```

**Response shape (no recycle bin):**

```
✓ Permanently deleted 2 items:
  📄 /docker/temp/old_config.json (4 KiB)
  📄 /docker/temp/old_log.txt (128 KiB)

⚠ Recycle bin is NOT enabled on /docker — these files cannot be recovered.
```

**Response shape (deleted from the recycle bin, #220):**

```
✓ Permanently deleted 2 items:
  📄 /video/#recycle/old_video.srt (38 KiB)
  📁 /video/#recycle/temp/ (3 items, 156 MiB)

⚠ Deleted from the recycle bin on /video — permanent, these cannot be recovered.
```

When the same call also deletes paths outside the bin, the header says `Deleted`, the share's recycle-bin note adds ", except the items deleted from the recycle bin itself", and the `⚠` line ends with `:` and lists the bin paths.

**Design notes:**
- Response adapts based on cached recycle bin status of the target share.
- User treats delete as done either way — the recycle bin note is a safety net, not a required step.
- If paths span shares with different recycle bin statuses, group the response accordingly.
- **Deleting from the bin is permanent (#220):** a path inside `/<share>/#recycle` (or the bin folder itself) is treated as permanent whatever the share's setting, because DSM keeps no second copy (seen on DSM 7.1.1: `list_recycle_bin` showed 0 items afterward). Such a share isn't probed for its recycle status unless the call also deletes paths outside its bin.
- **Missing paths (#162):** every path is checked with `getinfo` before anything is deleted. Any missing path refuses the whole call with `not_found` naming the missing paths and saying nothing was deleted. When a missing path contains `*` or `?`, the suggestion explains that paths aren't wildcard patterns and points to `search_files`. DSM itself finishes a Delete task for a missing path without an error (`total: -1`, empty `path`), so that status is also treated as not found, which covers a path that disappears between the check and the delete.
- **Per-item errors and overlaps:** a Delete task can finish with an `errors` list and no `error` key, e.g. 900 for a non-recursive delete of a non-empty folder, which DSM leaves in place. That is a structured error (with a `recursive=true` hint), never "Deleted". Paths listed twice, or inside another listed path, are collapsed before any task runs (delete, move and restore), and a later failure names the paths already deleted. Copy collapses only paths listed twice: copying a folder and a path inside it is two copies (`dest/a` and `dest/b`).
- **Task cleanup:** DSM releases a finished Delete task itself and answers `stop` with error 401 (DSM 7.1 and 7.2.2), so `stop` runs only for a task that didn't finish (timeout, polling error, cancellation).

---

## Module-Level Error Codes

| Code | DSM Meaning | User Message | Suggestion |
|------|-------------|--------------|------------|
| 400 | Invalid parameter | "Invalid parameter in the file operation request." | Check path format and parameter values. |
| 401 | Unknown error | "An unknown file operation error occurred." | Retry or check NAS logs. |
| 402 | System too busy | "The NAS is too busy to process this request." | Wait and retry. |
| 408 | No such file/directory | "Path not found: '{path}'." | Use `list_files` or `search_files` to find the correct path. |
| 414 | File already exists | "A file already exists at the destination." | Use `overwrite=true` to replace. |
| 415 | Disk quota exceeded | "Disk quota exceeded." | Free space or contact NAS administrator. |
| 416 | No space left | "No space left on the target volume." | Free space on the NAS. |
| 418 | Illegal name or path | "Invalid file/folder name or path." | Avoid characters: `/ \\ : * ? " < > \|` |
| 419 | Illegal file name | "Invalid file name." | Avoid characters: `/ \\ : * ? " < > \|` |
| 421 | Device or resource busy | "The file is in use by another process." | Wait and retry. |
| 599 | No such task | "Background task not found (may have already completed)." | — |

---

## Batch Move Workflow Pattern

**Decision:** Keep tools atomic. The LLM handles multi-step workflows naturally. Teach the search → review → act pattern via MCP server instructions.

### The Pattern

For the weekly "move new episodes" workflow:

1. **Search** — `search_files(folder_path="/video/Downloads", pattern="*Severance*", exclude_pattern="*.torrent")`
2. **Review** — LLM presents matches to the user with a count: "Found 2 files matching *Severance* (excluding .torrent files): Severance.S02E10.mkv (1.8 GiB) and Severance.S02E10.srt (45 KiB). Move these to /video/TV Shows/Severance/Season 2/?"
3. **Act** — User confirms → `move_files(paths=[...], dest_folder="/video/TV Shows/Severance/Season 2/")`

### Why Atomic Tools Beat a Super-Tool

- The LLM naturally confirms before destructive operations.
- `exclude_pattern` on `search_files` handles the "everything except .torrent" case.
- The user sees exactly what will be moved before it happens.
- Each step is independently useful (search without moving, move without searching).
- Easier to test, debug, and compose in novel ways.

### Skill Enhancement (for power users)

Users with access to Skills can encode show-specific rules:

- Which extensions to group together (`.mkv` + `.srt` + `.nfo` but not `.torrent`)
- Default source and destination paths per show
- Naming conventions and automatic season detection from filenames

This is outside the MCP server's scope but worth documenting as a power-user pattern. Skills are not universally available (e.g., not in VS Code chat), so the core tools must work well without them.

---

## MCP Server Instructions

The `MCPServer(instructions=...)` string provides shared knowledge at connection time:

```
You are connected to a Synology NAS via the mcp-synology File Station module.

PATH FORMAT:
All file paths start with a shared folder name: /video/..., /music/..., etc.
Call list_shares first to discover available shared folders and their permissions.

FILE SIZES:
Sizes are binary (1 KiB = 1024 bytes). Size parameters accept "500MiB", "2GiB",
"1.5TiB"; KB/MB/GB/TB mean the same binary units.

WORKING WITH FILES:
- Start with list_shares to discover available paths
- Use list_files to browse directories, search_files to find specific files
- get_file_info for detailed metadata, get_dir_size for directory totals

MOVING AND ORGANIZING FILES:
When a user asks to move or organize files:
1. Use search_files to find matching files. Use exclude_pattern to filter out
   unwanted file types (e.g., exclude_pattern="*.torrent" when moving media).
2. Present the results with a count and confirm with the user before proceeding.
3. Use move_files or copy_files with the confirmed paths.
Always search first and confirm before destructive operations.

RECYCLE BIN:
Some shares have a recycle bin enabled (shown in list_shares output).
Deleted files on those shares can be recovered:
- list_recycle_bin to see recently deleted files
- restore_from_recycle_bin to recover them
The recycle bin lives at /<share>/#recycle/ internally.
```

---

## Resolved Design Decisions

### 1. Recycle Bin Awareness ✓

Module caches recycle bin status per share at initialization. `delete_files` response adapts based on status. Two convenience tools added: `list_recycle_bin` (READ) and `restore_from_recycle_bin` (READ). `#recycle` is hidden from normal `list_files` results. User treats delete as done — recycle bin info is a safety net.

### 2. Batch Move Confirmation ✓

Atomic tools with search → review → move pattern taught via MCP server instructions. `search_files` gains `exclude_pattern` for client-side filtering. Skills can encode per-user rules for power users but aren't required.

### 3. Human-Readable File Sizes ✓

Size parameters accept `int` (bytes) or `str` (`"500MiB"`, `"2GiB"`, `"1.5TiB"`; `KB`/`MB`/`GB`/`TB` mean the same binary units). Case-insensitive, decimals allowed. Documented in MCP server instructions.

### 4. Tool Description Strategy ✓

Detailed per-tool descriptions (~200 words max), shared knowledge in `MCPServer(instructions=...)`, no cross-tool repetition.

---

## Remaining Open Items

1. **Emoji vs text markers:** Should output use emoji (📁🎬📄) or plain text ([DIR]/[FILE])? Configurable via `file_type_indicator` module setting — defaults to emoji.
