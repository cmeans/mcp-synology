# File Station Module — Tool Specifications

> **Version:** 0.4 | **Updated:** 2026-03-18 — Added upload_file (WRITE) and download_file (READ) tools for local↔NAS file transfer. Tool counts: 7 READ + 7 WRITE = 14.
> **Parent doc:** `mcp-synology-architecture.md` v0.3

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
- **Validation:** Check that the first path component corresponds to a known shared folder (from `SYNO.FileStation.List` / `list_share` cache). Return a clear error with `list_shares` guidance if not.
- **Escaping:** The DSM API uses commas as separators in multi-path parameters. Commas in paths must be escaped to `\,` and backslashes to `\\`. The core client handles this transparently.

### Response Formatting

All tools use the shared core formatters (`format_table`, `format_key_value`, `format_status`, `format_tree`, `format_error`). Responses are plain text optimized for LLM consumption, not JSON.

### Async Operations

Several File Station APIs are non-blocking (start → poll status → stop/clean). These are abstracted internally — the tool starts the operation, polls at ~500ms intervals, reports progress via MCP progress notifications when available, and returns the final result.

- **Default timeout:** 120 seconds (configurable per-tool via module config if needed).
- **On timeout:** Stop the background task, return an error with the last known progress.
- **Progress reporting:** Via `ctx.report_progress(current, total)` using the MCP progress notification protocol. Whether the client renders this visually depends on the client implementation.

### Human-Readable Size Parsing

Any parameter accepting file sizes (`size_from`, `size_to`) accepts both raw bytes (as integers) and human-readable strings. Parsing rules:

- Case-insensitive: `"500mb"`, `"500MB"`, `"500Mb"` all valid
- Supported units: `B`, `KB`, `MB`, `GB`, `TB` (binary: 1 KB = 1024 bytes)
- Decimal values allowed: `"1.5GB"`, `"0.5TB"`
- Plain integers treated as bytes: `1048576` = 1 MB
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
- **Avoid repetition across tools.** Shared knowledge (path format, size parsing, common patterns) goes in the MCP server's `instructions` field (set via `FastMCP(instructions=...)`), which the LLM receives once at connection time.
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
> List all shared folders on the NAS. This is the starting point for file navigation — call this first to discover available paths. Returns folder names, paths, sizes, and permissions. Use these paths as the root for all other file operations (e.g., the path "/video" from results becomes the prefix for list_files, move_files, etc.).

**Parameters:**

| Name | Type | Required | Default | Description |
|------|------|----------|---------|-------------|
| `additional` | `list[str]` | No | `["real_path", "size", "owner", "perm"]` | Metadata fields to include. Options: `real_path`, `size`, `owner`, `time`, `perm`, `mount_point_type`, `volume_status`. |
| `sort_by` | `str` | No | `"name"` | Sort field. Options: `name`, `size`, `user`, `mtime`, `crtime`. |
| `sort_direction` | `str` | No | `"asc"` | Sort direction: `asc` or `desc`. |

**Response shape:**

```
Shared Folders on [NAS hostname]
═══════════════════════════════════════════════════
  Name          Path        Size      Owner     Recycle Bin
  ─────────     ────────    ──────    ──────    ───────────
  docker        /docker     1.2 TB    admin     enabled
  music         /music      856 GB    admin     enabled
  photo         /photo      2.1 TB    admin     enabled
  video         /video      4.8 TB    admin     enabled

4 shared folders found.

Paths shown above are the root for all file operations.
Example: to list files in the video share, use list_files(path="/video")
```

**Design notes:**
- The footer text teaches the LLM how to use the paths in subsequent tool calls.
- The "Recycle Bin" column shows enabled/disabled status per share (from the cached startup check).
- When `additional` includes `perm`, include a column indicating read-only vs read-write access for the service account.

---

### 2. `list_files`

List files and folders within a given directory path.

**Permission tier:** READ

**DSM API:** `SYNO.FileStation.List` / `list`

**Tool description (LLM-facing):**
> List files and folders in a directory. Supports glob pattern filtering (e.g., "*.mkv"), file type filtering, sorting, and pagination. Returns file names, types, sizes, and modification dates by default. Use the 'additional' parameter to request more metadata fields.

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
Contents of /video/TV Shows (12 items)
═══════════════════════════════════════════════════
  Type  Name                         Size        Modified
  ────  ───────────────────────      ─────────   ──────────────
  📁    Breaking Bad/                —           2024-08-15 10:30
  📁    The Bear/                    —           2025-01-22 14:15
  📁    Severance/                   —           2025-03-10 09:45
  🎬    sample_clip.mp4              284 MB      2025-03-14 22:10

Showing 1–12 of 12 items.
```

**Design notes:**
- `📁` for directories, `📄` for generic files, `🎬` for video files. Configurable fallback to text markers (`[DIR]`/`[FILE]`).
- Directories show `—` for size (use `get_dir_size` for directory sizes).
- Trailing `/` on directory names for visual distinction.
- Pagination footer when truncated: `"Showing 1–200 of 1,432 items. Use offset=200 to see more."`
- `#recycle` folders are **hidden by default**. They appear only via `list_recycle_bin` or direct path access.

---

### 3. `get_file_info`

Get detailed metadata for one or more specific files or folders.

**Permission tier:** READ

**DSM API:** `SYNO.FileStation.List` / `getinfo`

**Tool description (LLM-facing):**
> Get detailed metadata for specific files or folders: size, owner, timestamps, permissions, and real path. Accepts one or more paths.

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
  Size:           18.4 GB
  Owner:          admin (users)
  Modified:       2025-02-18 13:42:05
  Created:        2025-02-18 13:40:12
  Accessed:       2025-03-15 20:30:00
  Permissions:    rwxr-xr-x (755)
```

**Response shape (multiple files):** Uses `format_table` with one row per file.

**Design notes:**
- Sizes are human-readable with 1 decimal place.
- Timestamps in local NAS time.
- For multiple paths, report individual failures inline alongside successes.

---

### 4. `search_files`

Search for files by name, type, size, or modification date within a given scope.

**Permission tier:** READ

**DSM API:** `SYNO.FileStation.Search` (start → list → stop → clean)

**Tool description (LLM-facing):**
> Search for files by name pattern, extension, size range, or modification date. Searches recursively by default. Supports glob patterns (e.g., "*Severance*") and extension filtering (e.g., "mkv"). Size parameters accept human-readable values like "500MB" or "2GB". Use exclude_pattern to filter out unwanted matches (e.g., "*.torrent").

**Parameters:**

| Name | Type | Required | Default | Description |
|------|------|----------|---------|-------------|
| `folder_path` | `str` | **Yes** | — | Root folder to search within (e.g., `/video`). |
| `pattern` | `str` | No | `None` | Filename glob pattern (e.g., `*.mkv`, `*Severance*`). |
| `extension` | `str` | No | `None` | File extension filter (e.g., `mkv`, `srt`). Without the dot. |
| `filetype` | `str` | No | `"all"` | Filter: `all`, `file`, `dir`. |
| `size_from` | `str \| int` | No | `None` | Minimum file size. Accepts bytes (int) or human-readable (`"500MB"`, `"2GB"`). |
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
  🎬    Severance.S02E10.mkv         /video/Downloads/                             1.8 GB      2025-03-10
  📄    Severance.S02E10.srt         /video/Downloads/                             45 KB       2025-03-10

2 results found (1 excluded by filter).
```

**Design notes:**
- Async search fully abstracted internally.
- `exclude_pattern` is applied **client-side** because the Synology Search API doesn't support exclusion natively. The response notes how many items were filtered out for transparency.
- If 0 results, suggest broadening the pattern or checking the folder path.
- Timeout: 60 seconds. If hit, return partial results with a note.

---

### 5. `get_dir_size`

Calculate the total size of a directory (recursive).

**Permission tier:** READ

**DSM API:** `SYNO.FileStation.DirSize` (start → status → stop)

**Tool description (LLM-facing):**
> Calculate the total size of a directory, including all files and subdirectories. Returns total size, file count, and directory count.

**Parameters:**

| Name | Type | Required | Default | Description |
|------|------|----------|---------|-------------|
| `path` | `str` | **Yes** | — | Directory path to measure. |

**Response shape:**

```
Directory Size: /video/TV Shows/The Bear
═══════════════════════════════════════════════════
  Total size:     42.6 GB
  Files:          186
  Directories:    12
```

---

### 6. `list_recycle_bin`

List contents of a shared folder's recycle bin.

**Permission tier:** READ

**DSM API:** `SYNO.FileStation.List` / `list` (targeting `/<share>/#recycle/`)

**Tool description (LLM-facing):**
> List the contents of a shared folder's recycle bin. Shows recently deleted files that can be restored with restore_from_recycle_bin. Only works on shares with the recycle bin enabled. Sorted by most recently deleted first by default.

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
Recycle Bin for /video (3 items)
═══════════════════════════════════════════════════
  Type  Name                         Size        Deleted
  ────  ───────────────────────      ─────────   ──────────────
  🎬    old_episode.mkv              1.2 GB      2025-03-15 18:20
  📄    old_episode.srt              38 KB       2025-03-15 18:20
  📁    temp_folder/                 —           2025-03-14 09:00

To restore items, use restore_from_recycle_bin with the file paths shown above.
```

**Design notes:**
- Convenience wrapper around `list_files` targeting `/<share>/#recycle/`.
- Default sort is most recently deleted first.
- If recycle bin is not enabled: `"Recycle bin is not enabled on /video. Deleted files cannot be recovered."`
- The `#recycle` folder preserves original directory structure. Top-level listing shown; deeper browsing possible via `list_files` on `#recycle` subpaths.

---

### 7. `download_file`

Download a NAS file to a local directory on this machine.

**Permission tier:** READ

**DSM API:** `SYNO.FileStation.Download` / `download` (GET, binary response)

**Tool description (LLM-facing):**
> Download a NAS file to a local directory on this machine. Provide the NAS file path and a local destination directory. Does not overwrite existing local files by default.

**Parameters:**

| Name | Type | Required | Default | Description |
|------|------|----------|---------|-------------|
| `path` | `str` | **Yes** | — | NAS file path to download. |
| `dest_folder` | `str` | **Yes** | — | Local directory to save the file to. |
| `filename` | `str` | No | `None` | Rename on download (defaults to the NAS filename). |
| `overwrite` | `bool` | No | `false` | Replace existing local file. |

**Response shape:**

```
[+] Downloaded movie.mkv (4.2 GB) to /home/user/Downloads/movie.mkv
```

**Design notes:**
- Single-file download only in v1. Multi-file download (returns zip) deferred.
- Binary data streamed to disk in 64 KB chunks — never buffered entirely in memory.
- If the NAS returns `Content-Type: application/json` instead of binary, it's an error envelope — parsed and raised.
- Partial files are cleaned up on failure (network error, DSM error).
- The MCP server runs locally, so `dest_folder` refers to a path on the machine running the server.

---

## WRITE Tier Tools (7 tools)

### 8. `upload_file`

Upload a local file from this machine to a NAS folder.

**Permission tier:** WRITE

**DSM API:** `SYNO.FileStation.Upload` / `upload` (POST multipart — the ONE exception to the GET-only rule)

**Tool description (LLM-facing):**
> Upload a local file from this machine to a NAS folder. Provide the local file path and NAS destination folder. Does not overwrite existing NAS files by default.

**Parameters:**

| Name | Type | Required | Default | Description |
|------|------|----------|---------|-------------|
| `local_path` | `str` | **Yes** | — | Local file path on this machine. |
| `dest_folder` | `str` | **Yes** | — | NAS destination folder. |
| `filename` | `str` | No | `None` | Rename on upload (defaults to the local filename). |
| `overwrite` | `bool` | No | `false` | Replace existing NAS file. |
| `create_parents` | `bool` | No | `true` | Create missing parent directories on the NAS. |

**Response shape:**

```
[+] Uploaded report.pdf (2.4 MB) to /documents/reports/
```

**Design notes:**
- Upload uses POST multipart — the only DSM API call that requires POST. Documented clearly in the client method.
- File is streamed from disk, not buffered in memory.
- On session error (106/107/119), re-auth and retry with a fresh file handle.
- `SynologyFileExistsError` (414) caught with suggestion to use `overwrite=true`.
- The MCP server runs locally, so `local_path` refers to a path on the machine running the server.

---

### 9. `restore_from_recycle_bin`

Restore files from a shared folder's recycle bin.

**Permission tier:** WRITE

**DSM API:** `SYNO.FileStation.CopyMove` / `start` (moving from `#recycle` back out)

**Tool description (LLM-facing):**
> Restore deleted files from a shared folder's recycle bin to their original location or a specified destination. Use list_recycle_bin first to see what's available. If no dest_folder is specified, files are restored to their original location within the share.

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
  🎬 old_episode.mkv (1.2 GB) → /video/TV Shows/Some Show/Season 1/
  📄 old_episode.srt (38 KB) → /video/TV Shows/Some Show/Season 1/
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

**DSM API:** `SYNO.FileStation.CreateFolder` / `create`

**Tool description (LLM-facing):**
> Create one or more new folders. Creates parent directories automatically by default (like mkdir -p). Idempotent — creating a folder that already exists succeeds without error.

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

**DSM API:** `SYNO.FileStation.Rename` / `rename`

**Tool description (LLM-facing):**
> Rename a file or folder. Provide the full current path and the new name (just the name, not a full path). Does not move the item — use move_files to relocate.

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

**DSM API:** `SYNO.FileStation.CopyMove` / `start` (with `remove_src=false`)

**Tool description (LLM-facing):**
> Copy files or folders to a destination folder. Source files remain in place. For large files, progress is reported during the operation. Set overwrite=true to replace existing files at the destination.

**Parameters:**

| Name | Type | Required | Default | Description |
|------|------|----------|---------|-------------|
| `paths` | `list[str]` | **Yes** | — | Source file/folder paths to copy. |
| `dest_folder` | `str` | **Yes** | — | Destination folder path (must exist). |
| `overwrite` | `bool` | No | `false` | Overwrite existing files at destination. |

**Response shape:**

```
✓ Copied 2 items to /video/Archive/:
  🎬 Severance.S02E09.mkv (1.7 GB)
  📄 Severance.S02E09.srt (42 KB)
```

---

### 13. `move_files`

Move files or folders to a new location. **Source files are removed after successful transfer.**

**Permission tier:** WRITE

**DSM API:** `SYNO.FileStation.CopyMove` / `start` (with `remove_src=true`)

**Tool description (LLM-facing):**
> Move files or folders to a new location. Source files are REMOVED after successful transfer — this is destructive and cannot be undone via this tool. Set overwrite=true to replace existing files at the destination. Same-volume moves are instant; cross-volume moves show progress.

**Parameters:**

| Name | Type | Required | Default | Description |
|------|------|----------|---------|-------------|
| `paths` | `list[str]` | **Yes** | — | Source file/folder paths to move. |
| `dest_folder` | `str` | **Yes** | — | Destination folder path (must exist). |
| `overwrite` | `bool` | No | `false` | Overwrite existing files at destination. |

**Response shape:**

```
✓ Moved 2 items to /video/TV Shows/Severance/Season 2/:
  🎬 Severance.S02E10.mkv (1.8 GB)
  📄 Severance.S02E10.srt (45 KB)

Source files have been removed from /video/Downloads/.
```

---

### 14. `delete_files`

Delete files or folders.

**Permission tier:** WRITE

**DSM API:** `SYNO.FileStation.Delete` / `start` (async) or `delete` (sync)

**Tool description (LLM-facing):**
> Delete files or folders. If the target share has a recycle bin enabled, deleted files can be recovered using list_recycle_bin and restore_from_recycle_bin. If no recycle bin, deletion is permanent. Deletes non-empty folders recursively by default.

**Parameters:**

| Name | Type | Required | Default | Description |
|------|------|----------|---------|-------------|
| `paths` | `list[str]` | **Yes** | — | File/folder paths to delete. |
| `recursive` | `bool` | No | `true` | Delete non-empty folders recursively. |

**Response shape (recycle bin enabled):**

```
✓ Deleted 3 items:
  🎬 /video/Downloads/old_video.mkv (2.1 GB)
  📄 /video/Downloads/old_video.srt (38 KB)
  📁 /video/Downloads/temp/ (3 items, 156 MB)

Recycle bin is enabled on /video — files can be recovered with list_recycle_bin and restore_from_recycle_bin.
```

**Response shape (no recycle bin):**

```
✓ Permanently deleted 2 items:
  📄 /docker/temp/old_config.json (4 KB)
  📄 /docker/temp/old_log.txt (128 KB)

⚠ Recycle bin is NOT enabled on /docker — these files cannot be recovered.
```

**Design notes:**
- Response adapts based on cached recycle bin status of the target share.
- User treats delete as done either way — the recycle bin note is a safety net, not a required step.
- If paths span shares with different recycle bin statuses, group the response accordingly.

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
2. **Review** — LLM presents matches to the user with a count: "Found 2 files matching *Severance* (excluding .torrent files): Severance.S02E10.mkv (1.8 GB) and Severance.S02E10.srt (45 KB). Move these to /video/TV Shows/Severance/Season 2/?"
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

The `FastMCP(instructions=...)` string provides shared knowledge at connection time:

```
You are connected to a Synology NAS via the mcp-synology File Station module.

PATH FORMAT:
All file paths start with a shared folder name: /video/..., /music/..., etc.
Call list_shares first to discover available shared folders and their permissions.

FILE SIZES:
Size parameters accept human-readable values: "500MB", "2GB", "1.5TB".
Supported units: B, KB, MB, GB, TB (binary, 1 KB = 1024 bytes).

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

Size parameters accept `int` (bytes) or `str` (`"500MB"`, `"2GB"`, `"1.5TB"`). Case-insensitive, decimals allowed. Documented in MCP server instructions.

### 4. Tool Description Strategy ✓

Detailed per-tool descriptions (~200 words max), shared knowledge in `FastMCP(instructions=...)`, no cross-tool repetition.

---

## Remaining Open Items

1. **Emoji vs text markers:** Should output use emoji (📁🎬📄) or plain text ([DIR]/[FILE])? Configurable via `file_type_indicator` module setting — defaults to emoji.
