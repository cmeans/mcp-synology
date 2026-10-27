# File Station Module — Tool Specifications

> **Version:** 0.7 | **Updated:** 2026-10-16. A long `delete_files`, `copy_files`, `move_files` or `restore_from_recycle_bin` hands its DSM task off after a capped wait instead of stopping it, reports progress while it waits, and the new `file_task` (WRITE) follows up on or stops a handed-off task (#245). 0.6.1: `list_shares`, `list_files` and `search_files` no longer take `additional`, which accepted fields their tables never showed (#149). 0.6 (2026-10-11): every LLM-facing description refreshed to the shipped text (all 14 had drifted since 0.4); `get_file_info`'s `additional` is exposed (#146); timestamps are labeled UTC (#137). Earlier: 0.4 (2026-03-18) added upload_file (WRITE) and download_file (READ). Tool counts: 7 READ + 8 WRITE = 15.
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
    tools=[...],  # See individual tool specs below; 7 READ + 8 WRITE = 15 total
)
```

---

## Design Conventions

### Path Handling

- **Input:** Accept paths with or without leading `/`. Normalize internally by prepending `/` if missing.
- **Output:** Always return fully-qualified paths starting with `/shared_folder/...`.
- **Validation:** Check that the first path component corresponds to a known shared folder (from `SYNO.FileStation.List` / `list_share` cache). Return a clear error with `list_shares` guidance if not. Every path-taking tool does this in its wrapper before any DSM call (#160): the cache is per session, filled on first use, reloaded once when a path names a share it doesn't hold (so a new share is found), and cleared on re-auth. A share name in another case passes, and the path goes on to DSM with the name as `list_share` spells it (#353): DSM takes an ordinary share in any case (`/TESTSHARE/...` for `testshare`), but the user home service only as `home` (`/Home` and `/HOME` get 408 "No such file or directory": vdsm DSM 7.1 and 7.4.1, and seska's DSM 7.1.1 on Btrfs, admin and not). An unknown share is a non-retryable `not_found` naming the shares that exist (for an account that isn't an admin, the shares it can see). If DSM won't list shares, the check is skipped and DSM's own error stands. Without it, DSM answers an unknown share with misleading codes: 900 "Unexpected server error" (Delete) and 1100 "Insufficient filesystem permissions" (CreateFolder), on DSM 7.1 and 7.2.2.
- **Escaping:** The DSM API uses commas as separators in multi-path parameters. Commas in paths must be escaped to `\,` and backslashes to `\\`. The core client handles this transparently.

### Response Formatting

All tools use the shared core formatters (`format_table`, `format_key_value`, `format_status`, `format_tree`, `format_error`). Responses are plain text optimized for LLM consumption, not JSON.

### Async Operations

Several File Station APIs are non-blocking (start → poll status → stop/clean). These are abstracted internally — the tool starts the operation, polls at ~500ms intervals, reports progress via MCP progress notifications when available, and returns the final result.

- **Search and DirSize:** wait up to `search_timeout` / `dir_size_timeout` (default `async_timeout`, 120 s). On timeout, stop the background task and return an error with the last known progress.
- **Delete and CopyMove hand off (#245):** `delete_files` waits up to `delete_timeout`; `copy_files`, `move_files` and `restore_from_recycle_bin` up to `copy_move_timeout`. Unset, the wait is **30 s** (`async_timeout` doesn't apply), and it's capped at **600 s** whatever the setting, below known client timeouts. A task still running when the wait ends is **not stopped**: the reply is a normal (non-error) result saying it's still running on the NAS, with its progress and a handle (`delete:<taskid>`, `copy:…`, `move:…`, `restore:…`) for `file_task`. In a multi-path call the remaining paths aren't started and are listed as `Not started:`. A status request that gets no answer during the wait (a transport error: any timeout, a refused or dropped connection), an HTTP error status (`DsmHttpError`, e.g. a reverse proxy's 502 or 504, which is how a proxy reports the same stall; #282), an answer that isn't a DSM reply (`DsmReplyError`, e.g. a 200 text/html page from a captive portal or a reverse proxy's login; #282) or any other unexpected answer hands the task off too, without stopping it (maintainer decision 2026-10-17): the task is most likely still running, and stopping or losing it would cut a huge delete short or leave it unreported. Its reply says the NAS stopped answering, that it or a proxy answered with an error (naming the HTTP status), or that its status reply couldn't be read, and never quotes the error's text (see "Handoff reply"). A canceled call (the user, or a client's own timeout) and a DSM error code on a status request still stop the task. If the `start` request itself gets no answer there's no task ID, so the network error surfaces as before. A task finishing within the wait gives the same result as before. The server keeps an in-memory record per handed-off task (last 50, 24 h, lost on restart) so `file_task` can give the original tool's result shape. A path that already has a running task of the same kind isn't started twice; the reply returns that task's handle.
- **Running-task discovery (#245):** where `file_task` is registered, the first successful client of a server process lists the user's running Delete/CopyMove tasks (`SYNO.FileStation.BackgroundTask` `list` v2), seeds the task records from each task's start params, and appends a one-time note (after any update notice) to the first File Station reply naming them and their handles. If that first reply is `file_task` about one handle, the note leaves that task out and is skipped when nothing else is running. A `file_task` list uses the note up without showing it, since the list shows the same tasks. Any failure is logged at DEBUG and ignored; nothing is shown when nothing is running.
- **Progress reporting:** Via `ctx.report_progress(current, total, message)` using the MCP progress notification protocol. Delete and CopyMove send it while waiting (about one every 2 s, only when the numbers change), e.g. `Deleting /s/x: 124,219 of 200,400 items`; CopyMove counts bytes. While DSM is still counting (`progress: -1`) a heartbeat with no total is sent. The progress value increases with every notification, as MCP requires: a heartbeat, or a count that doesn't exceed the last value sent (including the next path of a multi-path call, which shares the progress token), is raised by 0.001 and sent without a total, so it's never shown against the wrong total. A failing notification is logged at DEBUG and never ends the wait. Whether the client renders this visually depends on the client implementation.
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

**Probe and cache:**
- A share's bin is checked on first use (by `delete_files`, `list_recycle_bin`, or `list_shares` for every share): `list` on `/<share>/#recycle/` with `limit=1` (DSM's `limit=0` lists everything). Success means on. Any other answer than 408 (105, another DSM error, no answer) can't tell.
- **408 (no `#recycle`) doesn't mean off (#409).** DSM creates `#recycle` when the share is created and again at the first delete into the bin, so the folder is also missing on a share whose bin is on when nothing has been deleted into it yet (a new account's home) or when someone deleted `#recycle` itself. On a 408 the share's own setting is read: `SYNO.Core.Share list` with `additional=["recyclebin"]` gives `enable_recycle_bin` per share; for `/home` it's the user home service's `enable_recycle_bin` (`SYNO.Core.User.Home get`, Control Panel > User > Advanced), since the `homes` share's own flag can disagree (vdsm: `homes` off while every home's bin is on; seska: both on). Only an administrator can read it (a non-admin gets 105, seska DSM 7.1.1). Its answer is definite. Without it, the 408 counts as off for that call but isn't cached: `delete_files` checks after the delete, when a bin that's on has re-created `#recycle`, so its "permanent" warning holds; `list_shares` shows `none yet`; `list_recycle_bin` says there's no bin folder (below).
- Only definite answers are cached (on: `#recycle` lists, or the share's setting; off: the share's setting), per session, and the cache is cleared on re-auth. A `#recycle` that lists turns a cached off back on; a 408 from `list_recycle_bin` only drops a cached on. A share that can't be told is checked again next time; meanwhile `delete_files` treats it as on, as before (#255). Before #409, one 408 cached the bin as off for the rest of the session: later deletes into it read as permanent, and `list_recycle_bin` refused to list them.

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
> List all shared folders on the NAS: the top-level folders every file path starts with (`/video`).
> Call this first to discover paths. Shows each share's volume, that volume's free space (shared by
> every share on it) and whether its recycle bin is on; for how much space a share uses, call
> get_dir_size.

**Parameters:**

| Name | Type | Required | Default | Description |
|------|------|----------|---------|-------------|
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
- **The "Recycle Bin" column is always shown (#255, #335).** Each share's status comes from the session's recycle-bin cache; a share not in it is checked with the probe described in Recycle Bin Awareness above, 4 at a time, and the tool waits at most 15 s for all of them together, then cancels the rest (`RECYCLE_PROBES_AT_ONCE`, `RECYCLE_COLUMN_TIMEOUT` in `listing.py`), so a busy NAS adds at most 15 s. It reads `enabled` (the probe lists, or the share's setting is on), `disabled` (the share's setting is off: admin only), `none yet` (408, and the setting can't be read; #409) or `couldn't check` (105, another DSM error, no answer, or not done in the 15 s), and a note under the table says what `none yet` and `couldn't check` mean. Only `enabled` and `disabled` are cached, so a `couldn't check` share is asked again on the next call; its log WARNING is given once per share per server run, then DEBUG. Before, the column appeared only once `delete_files` or `list_recycle_bin` had touched some share, and showed `unknown` for every other share (0.10.0rc3 live test, seska), and a probe that failed was cached as enabled.
- No `additional` parameter (#149): the tool always fetches `real_path`, `owner` and `volume_status`, the fields its columns show. It used to accept any field (`perm`, `time`, `mount_point_type`, …) and silently drop the ones it doesn't show.

---

### 2. `list_files`

List files and folders within a given directory path.

**Permission tier:** READ

**DSM API:** `SYNO.FileStation.List` / `list`

**Tool description (LLM-facing):**
> List the files and folders directly inside one folder (one level; subfolders' contents aren't
> listed). To find files anywhere below a folder by name, extension, size or date, use search_files.
> Folders show no size; for a folder's total, use get_dir_size. DSM sorts folders and files
> separately: ascending lists folders first, descending files first. A long listing is paged, and
> the reply says which offset is next.

**Parameters:**

| Name | Type | Required | Default | Description |
|------|------|----------|---------|-------------|
| `path` | `str` | **Yes** | — | Folder path to list (e.g., `/video/TV Shows`). |
| `pattern` | `str` | No | `None` | Name filter, matched by DSM: case-insensitive; `*` and `?` wildcards (with one, the whole name must match: `b2*`); a bare word matches anywhere in the name (`b2` finds `ab2c.log`); commas separate alternatives (`*.mkv,*.mp4`); no `[...]` classes. Measured on DSM 7.1 and 7.3.2 (#333). |
| `filetype` | `str` | No | `"all"` | Filter by type: `all`, `file`, `dir`. |
| `sort_by` | `str` | No | `"name"` | Sort field. Options: `name`, `size`, `user`, `mtime`, `crtime`, `atime`, `ctime`, `type`; aliases `modified`, `date`, `time` (mtime), `created` (crtime), `accessed` (atime), `changed` (ctime), `owner` (user). |
| `sort_direction` | `str` | No | `"asc"` | Sort direction: `asc` or `desc`. |
| `offset` | `int` | No | `0` | Starting index for pagination. |
| `limit` | `int` | No | `200` | Max items to return (API max: 5000). `0` returns them all, as DSM's `limit=0` does. |

**Response shape:**

```
Contents of /video/TV Shows (5 items)
====================================================
  Type  Name           Size    Modified
  ────  ─────────────  ──────  ───────────────────────
  📁     Breaking Bad/  —        2024-08-15 10:30:00 UTC
  📁     Severance/     —        2024-08-15 10:30:00 UTC
  📁     The Bear/      —        2024-08-15 10:30:00 UTC
  🎬     clip.mp4       284 MiB  2024-08-15 10:30:00 UTC
  📄     show.nfo       2 KiB    2024-04-03 11:25:00 UTC
DSM sorts folders and files separately; folders come first.

5 of 5 items.
```

**Design notes:**
- `📁` for directories, `📄` for generic files, `🎬` for video files. Configurable fallback to text markers (`[DIR]`/`[FILE]`).
- Directories show `—` for size (use `get_dir_size` for directory sizes).
- Trailing `/` on directory names for visual distinction.
- Footer: `N of N items.` when everything fits; `"Showing 1–200 of 1,432 items. Use offset=200 to see more."` when truncated; `"Showing 1,401–1,432 of 1,432 items."` on a last page reached with `offset`. The range, not just the count, says where a page is (0.10.0rc6 live test), as `list_recycle_bin`'s pattern listing already did.
- `#recycle` folders are **hidden by default**. They appear only via `list_recycle_bin` or direct path access.
- **DSM sorts by type first (#333):** ascending puts every folder before the files, descending puts the files first, and each group is sorted on its own (measured for `name`, `size`, `mtime` and `ctime` on DSM 7.1 and 7.3.2, and on seska's 7.1.1). A listing that mixes the two says so after the table: `DSM sorts folders and files separately; folders come first.` (`files` for descending). Without it, a listing sorted by date looked unsorted where the groups meet (0.10.0rc3 live test).

---

### 3. `get_file_info`

Get detailed metadata for one or more specific files or folders.

**Permission tier:** READ

**DSM API:** `SYNO.FileStation.List` / `getinfo`

**Tool description (LLM-facing):**
> Get detailed metadata for files or folders whose paths you know: size, owner, timestamps,
> permissions and real path on the volume. To see what's in a folder, use list_files; for a folder's
> total size, use get_dir_size. A path that doesn't exist is reported as not found.

**Parameters:**

| Name | Type | Required | Default | Description |
|------|------|----------|---------|-------------|
| `paths` | `list[str]` | **Yes** | — | One or more file/folder paths. |
| `additional` | `list[str]` | No | `["real_path", "size", "owner", "time", "perm"]` | Metadata fields to include. Options: `real_path`, `size`, `owner`, `time`, `perm`, `type`, `mount_point_type`. The only File Station tool with this parameter: the listing tools show fixed columns (#149). |

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
> Search a folder and everything below it for files and folders by name, extension, size or modified
> date. For what's in one known folder, use list_files; for how much space a folder uses, use
> get_dir_size, not a search. With no filter, it returns everything under the folder, up to `limit`.
> A search that runs out of time returns what it found so far and says so.

**Parameters:**

| Name | Type | Required | Default | Description |
|------|------|----------|---------|-------------|
| `folder_path` | `str` | **Yes** | — | Root folder to search within (e.g., `/video`). |
| `pattern` | `str` | No | `None` | Name to look for. A bare word is wrapped as `*word*`, so it matches anywhere in the name (`Severance` finds `Severance.S02E10.mkv`); with `*` or `?` it's sent as a wildcard pattern as is. A pure extension pattern (`*.mkv`) is sent as `extension` instead. |
| `extension` | `str` | No | `None` | File extension filter (e.g., `mkv`, `srt`). Without the dot. |
| `filetype` | `str` | No | `"all"` | Filter: `all`, `file`, `dir`. |
| `size_from` | `str \| int` | No | `None` | Minimum file size. Accepts bytes (int) or human-readable (`"500MiB"`, `"2GiB"`; `"500MB"` means the same). |
| `size_to` | `str \| int` | No | `None` | Maximum file size. Same format as `size_from`. |
| `mtime_from` | `str` | No | `None` | Modified at or after this time (ISO 8601, `YYYY-MM-DD` or epoch seconds). Inclusive, in whole seconds: a file modified at exactly this time matches, and one at T+0.9 s counts as T, so it doesn't match `mtime_from=T+1` (DSM 7.2.2 and 7.3.2, #306). |
| `mtime_to` | `str` | No | `None` | Modified before this time. **Exclusive**: a file modified at exactly this time doesn't match (DSM 7.2.2 and 7.3.2, #306). |
| `exclude_pattern` | `str` | No | `None` | Glob pattern to exclude from results (e.g., `*.torrent`), case-insensitive. Applied client-side to the (at most `limit`) results DSM returns, so a reply can hold fewer than `limit`. |
| `recursive` | `bool` | No | `true` | Search subdirectories; `false` searches only `folder_path` itself. |
| `limit` | `int` | No | `500` | Max results to return. When DSM found more, the reply says so. |

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
- **Nothing to match on:** with `filetype=all` and no pattern, extension, size or date, DSM refuses the search with error 600, "Search folder access denied" (DSM 7.2.2 and 7.3.2; it came back as `filestation_error`). On 7.2.2, `filetype=file` or `dir` alone works, and so does an empty-looking filter such as `size_from=0`, while an empty `pattern` or no `filetype` at all is refused too. So the tool sends `pattern="*"` when `filetype=all` and nothing else is sent, which returns everything under the folder (#306). The heading shows only the caller's own filters.
- **How often DSM loses a task** (DSM 7.2.2 and 7.3.2, #306): 0 of 20 back-to-back searches with the task ID quoted, against 15 and 13 of 20 with it unquoted. That's the #121 cause, and why the tool quotes it.
- `exclude_pattern` is applied **client-side** because the Synology Search API doesn't support exclusion natively. The response notes how many items were filtered out for transparency.
- If 0 results, suggest broadening the pattern or checking the folder path.
- Timeout: `search_timeout` (default `async_timeout`, 120 s). If hit, return the results found so far with a note saying the search timed out.

---

### 5. `get_dir_size`

Calculate the total size of a directory (recursive).

**Permission tier:** READ

**DSM API:** `SYNO.FileStation.DirSize` (start → status → stop)

**Tool description (LLM-facing):**
> Calculate the total size of a folder, including everything below it: total size, file count and
> folder count. This is the tool for 'how much space does X use' questions. Counts only what this
> DSM user can read: a subfolder it can't read is left out without notice. A very large tree can
> take minutes, and the call gives up after a timeout (120 s unless the server is configured
> otherwise).

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

**Missing or unreadable path (#332):** DSM's DirSize task on a path that doesn't exist finishes at once with 0 bytes, 0 files and 0 directories (seen on a real NAS, DSM 7.1.1), which reads as an empty folder. On a path the user can't read, it finishes with `errors` and no totals: `{"errors": [{"code": 407, "path": "/homes/admin"}], "finished": true}` (vdsm 7.1). A folder the user is denied by an ACL entry or a POSIX mode (such as root's `700`) finishes with 0 bytes and no `errors` (vdsm 7.1 and 7.2.2, #336), but `getinfo` with `additional=["perm"]` reports it with `perm.acl.read: false`. So `get_dir_size` checks the path with `getinfo` (with `perm`) first: a `{"code": 408}` entry is `not_found`, and a `407` entry or `acl.read: false` is `permission_denied`, with no task started. If `getinfo` gets a DSM error or an unreadable reply (such as a proxy's 502), or reports another code, the task runs. A finished status that carries `errors` is then mapped from the entry for the requested path (else the first, and the message names that entry's path): 408 `not_found`, 407 `permission_denied`, any other code as for any DSM error; entries with no integer code give `filestation_error`. It is never formatted as 0 B. A network error on the check ends the call, as it would at the task's start. A subfolder the user can't read gets no `errors` entry either: DSM leaves it out of the totals without notice (vdsm 7.1 and 7.2.2). Finding it would take a walk of the whole tree, so the tool description says the totals count only what the DSM user can read (#336).

---

### 6. `list_recycle_bin`

List contents of a shared folder's recycle bin.

**Permission tier:** READ

**DSM API:** `SYNO.FileStation.List` / `list` (targeting `/<share>/#recycle/`)

**Tool description (LLM-facing):**
> List what's in one shared folder's recycle bin: the files and folders deleted from that share.
> list_shares shows which shares have a bin; for files that weren't deleted, use list_files. Without
> pattern, only the bin's top level is listed, and DSM sorts folders and files separately, as in
> list_files. With pattern, the bin is searched, subfolders included (up to 200 folders and 5,000
> items; the reply says when it stops early), and each match shows its path in the bin. Modified is
> the file's own time, not when it was deleted.

**Parameters:**

| Name | Type | Required | Default | Description |
|------|------|----------|---------|-------------|
| `share` | `str` | **Yes** | — | Shared folder name (e.g., `video`) or path (e.g., `/video`). |
| `pattern` | `str` | No | `None` | Name filter, matched as `list_files` matches it (case-insensitive; `*`/`?`; a bare word matches anywhere; commas for alternatives), but **across the bin, subfolders included** (#333), up to 200 folders and 5,000 items (`_WALK_MAX_FOLDERS`, `_WALK_MAX_ENTRIES`); the reply says when the search stops early (#375). |
| `sort_by` | `str` | No | `"mtime"` | Sort field. `mtime` is the file's own modified time, kept when it went into the bin, **not** the deletion time. `ctime` (alias `changed`) changes when the file is moved into the bin, so it's the closest to a deletion time, but any later metadata change updates it too. |
| `sort_direction` | `str` | No | `"desc"` | Sort direction. Default: descending. |
| `limit` | `int` | No | `100` | Max items to return. `0` returns them all, as DSM's `limit=0` does; with `pattern`, so does any value below 0. |
| `offset` | `int` | No | `0` | Items to skip, for the next page: the reply's "Use offset=N to see more" names it. With `pattern`, it skips that many matches. |

**Response shape:**

```
Contents of /video/#recycle (3 items)
====================================================
  Type  Name           Size    Modified
  ────  ─────────────  ──────  ───────────────────────
  🎬     old-movie.mkv  4.5 GiB  2024-03-14 12:30:00 UTC
  📄     notes.txt      1 KiB    2024-03-01 11:00:00 UTC
  📁     Season 3/      —        2024-03-08 09:00:00 UTC
DSM sorts folders and files separately; files come first.

3 of 3 items.
```

**Design notes:**
- Without `pattern`: a wrapper around `list_files` targeting `/<share>/#recycle/`; it returns `list_files`' output as is (title `Contents of /<share>/#recycle`, a `Modified` column, and the folders-and-files sort note). Only the bin's top level is listed.
- **With `pattern`: the bin is searched, subfolders included, up to a cap (#333, #375).** DSM matches `pattern` only at the level listed, so a file deleted from a subfolder never matched (0.10.0rc3 live test on seska: `pattern=b2*` answered 0 items while `mcp-rc3/photos-zip/b2.txt` was in the bin, and restoring that path worked). DSM's Search can't help: it refuses `#recycle` with 600 "Search folder access denied", for the admin too (seska 7.1.1). So the tool walks the bin, one `List.list` per folder (pages of 1,000), and matches names locally with DSM's rules (`dsm_pattern_matcher`). It stops at 200 folders or 5,000 entries and then says so, so a partial search never reads as complete. `limit` and `offset` page the matches, as they page a listing: "Showing 3–4 of 31 matches. Use offset=4 to see more." (#364; before, the note gave only the count and the tool had no `offset`). A subfolder that can't be listed is named (the first five, then a count of the rest) and skipped. Each item's path in the bin is built from the walk, not taken from DSM's `path`, whose case can differ from the share name given (ordinary share names match case-insensitively: `share="Video"` lists `/video/#recycle`; the user home service doesn't, and `/Home` gets 408: vdsm DSM 7.1 and 7.4.1, and seska's DSM 7.1.1 on Btrfs, #353). Matches are sorted locally by the requested field, not grouped by type (`name` orders by the path in the bin; `type`, `user` and other fields the search can't sort by fall back to `mtime`, and the reply says so), and shown with paths relative to the bin, which `restore_from_recycle_bin` takes as is:

  ```
  Matches for "b2*" in /home/#recycle (1 found in 4 folder(s), subfolders included)
  =================================================================================
    Type  Path in bin                Size  Modified
    ────  ─────────────────────────  ────  ───────────────────────
    📄     mcp-rc3/photos-zip/b2.txt  2 B   2026-10-19 10:58:00 UTC

  Paths are relative to the recycle bin, as a restore takes them.
  ```
- **What `Modified` means here:** an item keeps its own mtime in the bin (seska: `desktop.ini` deleted in 2026 shows 2019), so the default sort isn't "most recently deleted first". `ctime` moves when the item goes into the bin (seska: `mcp-rc3`, `mcp-rc4` show their deletion times), though a later metadata change also updates it (several older items share one `ctime`).
- If the share's setting (admin only) says the bin is off: `"Recycle bin is not enabled on /video. Deleted files cannot be recovered."` If there's no `#recycle` and the setting can't be read (or says on, the folder having been deleted): `"/video has no recycle bin folder (#recycle), so there's nothing in it to list: its bin is off, or nothing has been deleted into it yet (DSM creates the folder at the first delete). After a delete there, delete_files says whether the files can be recovered."` (#409). DSM answers 408 for `/<share>/#recycle` both when the bin is off and when the share isn't there, so this is said only after `/<share>` itself lists. A share that isn't there is a non-retryable `not_found` ("No shared folder /Home on this NAS. The user home folder is /home (lowercase)." — the hint only for `home` in another case), and no recycle status is kept for it (#353). The share check above normally catches both cases first; this one covers DSM refusing to list shares.
- The `#recycle` folder preserves the original directory structure. Without `pattern`, only the top level is listed; `list_files` on a `#recycle` subpath browses deeper.

---

### 7. `download_file`

Download a NAS file, or a folder as a ZIP of it, to a local directory on this machine.

**Permission tier:** READ

**DSM API:** `SYNO.FileStation.Download` / `download` (GET, binary response)

**Tool description (LLM-facing):**
> Download a NAS file to a folder on this machine (where this server runs). A folder downloads as a
> ZIP of it, saved as `<folder>.zip`; a whole shared folder can't be downloaded. An existing local
> file is not replaced unless `overwrite` is true.

**Parameters:**

| Name | Type | Required | Default | Description |
|------|------|----------|---------|-------------|
| `path` | `str` | **Yes** | — | NAS file or folder path to download. A share's top level (`/video`) is refused (`invalid_parameter`). |
| `dest_folder` | `str` | No | `default_download_dir` | Local directory to save the file to, inside the allowed local folders (see Local path confinement). With neither set, `invalid_parameter` (`param: dest_folder`) before any NAS request (#194). |
| `filename` | `str` | No | `None` | Rename on download (defaults to the NAS filename, or `<folder>.zip` for a folder; a given `filename` is used as is). Must be a plain file name: no `/` or `\`, not `.` or `..`, no NUL, no drive (`D:x`); otherwise `invalid_parameter` before the NAS is contacted. The same rule applies to a name taken from the NAS path. |
| `overwrite` | `bool` | No | `false` | Replace existing local file (for a folder, the `.zip`). With `false`, an existing name is `already_exists` and the local file is kept. |

**Response shape:**

```
[+] Downloaded movie.mkv (4.2 GiB) to /home/user/Downloads/movie.mkv
```

**Design notes:**
- **Folders (#327, closes #330):** DSM sends a folder below a share as a ZIP of it (`PK\x03\x04`; vdsm 7.1 and 7.2.2). `download_file` reads `isdir` from its `getinfo` check and saves it as `<folder>.zip` (unless `filename` is given), and the reply says it's a ZIP archive of the folder. Its size isn't known ahead, so there's no disk-space pre-check for it. A share's top level (a path with one component) is refused up front: DSM answers it with a 502 (404 as admin). If `getinfo` can't answer, a folder is saved under its bare name, as before. Downloading several files at once is still out of scope.
- Binary data streamed to disk in 64 KB chunks — never buffered entirely in memory.
- If the NAS returns `Content-Type: application/json` instead of binary, it's an error envelope — parsed and raised. JSON that isn't DSM's error envelope (not an object, or an object without an `error` object) is `unavailable` (#292).
- **DSM sends `application/octet-stream` for every download** (`mode=download`), whatever the file: a real `.html`, a `.json` shaped like an error envelope, `.txt`, binary or no extension (verified on DSM 7.1 and 7.2.2, #292; `mode=open` sends `text/plain` for text files, never `text/html`). So an HTML answer (`text/html` or `application/xhtml+xml`) on a 200 is a reverse proxy's or captive portal's page, not the file: it's `unavailable` ("the answer wasn't a DSM reply"), and nothing is written. A missing file gets HTTP 502 with DSM's own HTML error page, the same status a proxy gives for a server error. A path the user can't read gets the same answer, and so does a share's top level (502, or 404 as admin); a folder below a share downloads as a ZIP (see Folders). So `download_file` reads its `getinfo` check first and sends no download for a missing path (the `{"code": 408}` entry: `not_found`), a path the user can't read (`407`: `permission_denied`) or a share's top level (`invalid_parameter`). A 5xx for a file `getinfo` found, or after a `getinfo` that got no DSM answer either (a proxy's 5xx or page, or no answer at all), is a retryable `unavailable`; once the NAS answers, a missing file is `not_found`. Otherwise, for a folder or another `getinfo` error entry, a 502 stays `filestation_error` (#324).
- **Never destroys or escapes local files.** The body streams to a temp file `.mcp-synology-<hex>.part` in `dest_folder`, flushed to disk (`fsync`) and then moved onto the destination only when complete. On any failure (DSM error, dropped connection, cancellation) only the temp file is removed; an existing destination is never truncated or deleted. A hard crash (power loss, SIGKILL) can leave a `.part` file behind.
- **`overwrite=false` holds to the end:** the finished file is published with a hard link, which fails if the destination exists (falling back to a final existence check where links aren't supported), so a file created during the download is never replaced; that is `already_exists`. With `overwrite=true` the destination is replaced and keeps its permission bits. Replacing needs free space for both copies until the rename.
- **Containment:** the resulting file must sit directly in `dest_folder`; anything else is `invalid_parameter`.
- **Checked before downloading:** a folder at the destination is `invalid_parameter` in both modes (it can't be replaced). With `overwrite=false`, any name already there, including a dangling symlink, is `already_exists`. With `overwrite=true`, a symlink at the destination is replaced by the downloaded file; its target is untouched. Local write errors name the destination file, never the internal temp file.
- **Annotations:** READ tier (it only reads the NAS), but `readOnlyHint=false`, `destructiveHint=true`, because it writes and can replace a file on this machine. Tiers guard the NAS only (see `architecture.md`).
- The MCP server runs locally, so `dest_folder` refers to a path on the machine running the server.
- **Local path confinement:** `dest_folder` must resolve (after `~`, `..` and symlinks) to a path inside one of the allowed folders (the top-level `allowed_local_paths` plus File Station's own; with no top-level list, File Station's list or the user's Downloads folder; `default_download_dir` is always allowed; #257). Otherwise it's `permission_denied`, listing the allowed folders, before any NAS request and before the folder's existence is checked. The message shows where the path resolves to only when the path itself is inside an allowed folder (a symlink there that leads out), so a refusal doesn't reveal what exists outside. A `~user` that doesn't exist gets the same refusal, so it doesn't reveal which local users exist either (#260). A path that can't be resolved (a NUL byte, a symlink loop) is `invalid_parameter`. The server instructions list the allowed folders under LOCAL PATHS, and `dest_folder`'s description names them too (`roots_for_description`), since some clients never pass the instructions to the model (0.10.0rc5 and rc6 live tests). See `core/local_paths.py` and `config-schema-spec.md`.

---

## WRITE Tier Tools (8 tools)

### 8. `upload_file`

Upload a local file from this machine to a NAS folder.

**Permission tier:** WRITE

**Annotations:** `destructiveHint=true`: with `overwrite=true` it replaces the NAS file. Annotations are static, so they can't depend on `overwrite` (#183).

**DSM API:** `SYNO.FileStation.Upload` / `upload` (POST multipart — one of the exceptions to the GET-only rule, with Download Station `Task.create` (#123) and the `SYNO.API.Auth` login (#235); see CLAUDE.md)

**Tool description (LLM-facing):**
> Upload one file from this machine (where this server runs) to a NAS folder; a local folder can't
> be uploaded. An existing NAS file of the same name is not replaced unless `overwrite` is true.

**Parameters:**

| Name | Type | Required | Default | Description |
|------|------|----------|---------|-------------|
| `local_path` | `str` | **Yes** | — | Local file path on this machine, inside the allowed local folders (see Local path confinement). A folder is refused (`not_found`: only a file uploads). |
| `dest_folder` | `str` | No | `default_upload_dir` | NAS destination folder. With neither set, `invalid_parameter` (`param: dest_folder`) before any NAS request (#194). |
| `filename` | `str` | No | `None` | Rename on upload (defaults to the local filename). |
| `overwrite` | `bool` | No | `false` | Replace existing NAS file. With `false`, an existing name is `already_exists` and the NAS file is kept (see #195 below). |
| `create_parents` | `bool` | No | `true` | Create `dest_folder` and missing parent directories on the NAS. With `false`, `dest_folder` must already exist. |

**Response shape:**

```
[+] Uploaded report.pdf (2.4 MiB) to /documents/reports/
```

**Design notes:**
- Upload uses POST multipart, one of the scoped exceptions to the GET-only rule (with Download Station `Task.create`, #123, and the login, #235; see CLAUDE.md). Documented clearly in the client method.
- File is streamed from disk, not buffered in memory.
- On session error (106/107/119), re-auth and retry with a fresh file handle.
- **`overwrite` is sent only when true (#195).** DSM's Upload reads `overwrite=false` as *skip*: it answers success with `data.blSkip: true` and leaves the existing file alone (verified on DSM 7.2.2; it took ~10 s). With `overwrite` omitted, DSM fails with 414 instead (also after ~10 s). So `overwrite=false` omits the parameter, and both 414 and a `blSkip: true` answer become `already_exists` (`param: filename`, value = the effective name) with a suggestion to use `overwrite=true`. A skipped upload is never reported as uploaded.
- The MCP server runs locally, so `local_path` refers to a path on the machine running the server.
- **Local path confinement:** `local_path` must resolve (after `~`, `..` and symlinks) to a path inside one of the allowed folders (the top-level `allowed_local_paths` plus File Station's own; with no top-level list, File Station's list or the user's Downloads folder; `default_download_dir` is always allowed; #257). Otherwise it's `permission_denied`, listing the allowed folders, before any NAS request; a symlink inside an allowed folder that points outside is refused. A `~user` that doesn't exist gets the same refusal, so the reply doesn't reveal which local users exist (#260). The server instructions list the allowed folders under LOCAL PATHS, and `local_path`'s description names them too. See `core/local_paths.py` and `config-schema-spec.md`.

---

### 9. `restore_from_recycle_bin`

Restore files from a shared folder's recycle bin.

**Permission tier:** WRITE

**Annotations:** `destructiveHint=true`: with `overwrite=true` it replaces the file at the original location (#183).

**DSM API:** `SYNO.FileStation.CopyMove` / `start` (moving from `#recycle` back out)

**Tool description (LLM-facing):**
> Restore deleted files or folders from one shared folder's recycle bin. Check what's there first
> with list_recycle_bin: every path must be in that share's bin, or nothing is restored. Without
> `dest_folder`, each item goes back to its original folder, which is re-created if it's gone. An
> item already at the destination is skipped and stays in the bin, unless `overwrite=true`, which
> replaces the existing one. A long restore keeps running on the NAS and returns a handle for
> file_task.

**Parameters:**

| Name | Type | Required | Default | Description |
|------|------|----------|---------|-------------|
| `share` | `str` | **Yes** | — | Shared folder name (e.g., `video`) or path (`/video`). |
| `paths` | `list[str]` | **Yes** | — | Paths within the recycle bin to restore: relative to `#recycle`, as `list_recycle_bin` shows them (`Shows/Ep1.mkv`), starting with `#recycle/`, or full (`/video/#recycle/Shows/Ep1.mkv`). Any path not in the bin refuses the whole call before anything moves. |
| `dest_folder` | `str` | No | `None` | Destination folder; it must exist, and every item goes there flat. If omitted, restores to original location (inferred from `#recycle` directory structure), re-creating it if it's gone. |
| `overwrite` | `bool` | No | `false` | Overwrite if a file already exists at the restore destination. With `false`, it's skipped and stays in the bin. |

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
- **If the original folder no longer exists, it's re-created first (#333).** A file restored out of a folder that was deleted along with it has nowhere to go, and DSM refuses the move with 1002 "Destination error" (vdsm DSM 7.1 and 7.3.2). Before #333 this was promised here but not done. The tool checks each inferred destination with `getinfo`, creates the missing ones with `CreateFolder` `force_parent=true` (deepest first, so a parent made along the way isn't created twice), and the reply ends `Re-created <folders> (no longer there) to restore into.` If a `CreateFolder` fails partway, its error is returned and any folders already made stay. An explicit `dest_folder` that doesn't exist is never created: it's refused as `not_found` before anything moves (#344; see `copy_files`).
- DSM's recycle bin doesn't keep an empty folder: deleting `a/` with an empty `a/empty/` leaves no `empty` in the bin (vdsm DSM 7.1 and 7.3.2). A folder that holds a file is kept.
- **Long operations (#245):** hands off after the capped wait instead of stopping (see "Async Operations"); the reply's handle works with `file_task`.

---

### 10. `create_folder`

Create one or more new folders.

**Permission tier:** WRITE

**Annotations:** `idempotentHint=true` (creating an existing folder succeeds); `destructiveHint=false`, since it only adds folders.

**DSM API:** `SYNO.FileStation.CreateFolder` / `create`

**Tool description (LLM-facing):**
> Create one or more folders, each given as a full path starting with its shared folder (`/video/TV
> Shows/New Show`). By default (`force_parent=true`), missing folders above it are created too, and
> a folder that already exists is not an error.

**Parameters:**

| Name | Type | Required | Default | Description |
|------|------|----------|---------|-------------|
| `paths` | `list[str]` | **Yes** | — | Full paths of folders to create (e.g., `["/video/TV Shows/New Show/Season 1"]`). |
| `force_parent` | `bool` | No | `true` | Create parent directories if they don't exist. With `false`, each parent must already exist. |

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

**DSM's 1200 is told apart (#188).** DSM answers 1200, "Failed to rename", both for a name already taken in that folder and for a source that doesn't exist (vdsm DSM 7.1, 7.2.2, 7.3.2 and 7.4.1). After a 1200, `getinfo` says which, and only on positive evidence: a source it reports missing (408) is `not_found` ("… doesn't exist on the NAS", `param: path`), and when it shows both the source and the new path exist, `already_exists` ("… already exists. Rename doesn't replace an existing file or folder.", `param: new_name`). `already_exists` is never claimed for a rename to the item's own name, to `.` or `..`, or of a share root (no parent folder to look in). Anything else is reported as the 1200 with its table message, which names both causes: a reply with no entry, another code such as 407, or a check that fails (a DSM error or no answer at all; logged at WARNING). Each path takes one `getinfo`. The check runs only after a failure, so a rename that works costs nothing extra.

**DSM API:** `SYNO.FileStation.Rename` / `rename`

**Tool description (LLM-facing):**
> Rename a file or folder in place. If the new name is already taken in that folder, DSM refuses and
> nothing is replaced. To move something to another folder, use move_files.

**Parameters:**

| Name | Type | Required | Default | Description |
|------|------|----------|---------|-------------|
| `path` | `str` | **Yes** | — | Full path to the file or folder to rename. |
| `new_name` | `str` | **Yes** | — | New name (filename/folder name only, not a full path; a `/` is refused with `invalid_parameter`). A name already taken in the folder is refused by DSM (1200) and nothing is replaced. |

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
> Copy files or folders into a destination folder; a folder is copied with everything in it, and the
> sources stay in place. To move instead, use move_files. `dest_folder` must already exist; it isn't
> created. With `overwrite=false` (default), items already at the destination are skipped and
> listed, and the rest are copied. If any source is missing, nothing is copied. A long copy keeps
> running on the NAS and returns a handle for file_task.

**Parameters:**

| Name | Type | Required | Default | Description |
|------|------|----------|---------|-------------|
| `paths` | `list[str]` | **Yes** | — | Source file/folder paths to copy. |
| `dest_folder` | `str` | **Yes** | — | Destination folder path (must exist). |
| `overwrite` | `bool` | No | `false` | Overwrite existing files at destination. With `false`, items already there are skipped and listed (see Async Operations). |

**Response shape:**

```
✓ Copied 2 items to /video/Archive/:
  🎬 Severance.S02E09.mkv (1.7 GiB)
  📄 Severance.S02E09.srt (42 KiB)
```

**Design notes:**
- **Long operations (#245):** hands off after the capped wait instead of stopping (see "Async Operations"); the reply's handle works with `file_task`.
- **The destination is checked before anything starts (#344).** DSM answers a copy or move into a folder that doesn't exist with 1002, "Destination error during copy/move", whose suggestion points at permissions (0.10.0rc4 and rc5 live tests, seska DSM 7.1.1). So `getinfo` on the destination comes first: 408 is `not_found` ("the destination folder … doesn't exist. Nothing was copied.", `param: dest_folder`, suggesting `create_folder`), and 407, a share the account has no rights to, is `permission_denied`. Any other answer, or none, leaves it to the task. The same check covers `move_files` and a restore with `dest_folder`. A folder refused by a Deny ACL inside a share the account can use still gets DSM's 1002, because `getinfo` reads the folder's entry from its parent (seska, a restricted test account); with missing folders caught first, 1002's suggestion (write permission, ownership) fits what's left.

---

### 13. `move_files`

Move files or folders to a new location. **Source files are removed after successful transfer.**

**Permission tier:** WRITE

**Annotations:** `destructiveHint=true`: it removes the sources, and with `overwrite=true` replaces files at the destination.

**DSM API:** `SYNO.FileStation.CopyMove` / `start` (with `remove_src=true`)

**Tool description (LLM-facing):**
> Move files or folders into a destination folder; a folder moves with everything in it, and the
> sources are removed. To keep the sources, use copy_files. `dest_folder` must already exist; it
> isn't created. With `overwrite=false` (default), items already at the destination are skipped,
> listed and left at the source. If any source is missing, nothing is moved. A long move keeps
> running on the NAS and returns a handle for file_task.

**Parameters:**

| Name | Type | Required | Default | Description |
|------|------|----------|---------|-------------|
| `paths` | `list[str]` | **Yes** | — | Source file/folder paths to move. |
| `dest_folder` | `str` | **Yes** | — | Destination folder path (must exist). |
| `overwrite` | `bool` | No | `false` | Overwrite existing files at destination. With `false`, items already there are skipped and listed (see Async Operations). |

**Response shape:**

```
✓ Moved 2 items to /video/TV Shows/Severance/Season 2/:
  🎬 Severance.S02E10.mkv (1.8 GiB)
  📄 Severance.S02E10.srt (45 KiB)

Source files have been removed from /video/Downloads/.
```

**Design notes:**
- **Long operations (#245):** hands off after the capped wait instead of stopping (see "Async Operations"); the reply's handle works with `file_task`.

---

### 14. `delete_files`

Delete files or folders.

**Permission tier:** WRITE

**Annotations:** `destructiveHint=true`.

**DSM API:** `SYNO.FileStation.Delete` / `start` (async) or `delete` (sync)

**Tool description (LLM-facing):**
> Delete files or folders. A folder is deleted with everything in it (`recursive`, default true). On
> a share with a recycle bin, deleted items go to the bin: list_recycle_bin shows them and
> restore_from_recycle_bin brings them back. Without a bin, and for anything inside a bin
> (`/<share>/#recycle`), deletion is permanent. Paths are exact, not wildcard patterns, and if any
> path is missing nothing is deleted. A long delete keeps running on the NAS and returns a handle
> for file_task.

**Parameters:**

| Name | Type | Required | Default | Description |
|------|------|----------|---------|-------------|
| `paths` | `list[str]` | **Yes** | — | File/folder paths to delete. Exact paths: wildcards aren't expanded, and a missing path refuses the whole call. |
| `recursive` | `bool` | No | `true` | Delete non-empty folders recursively. With `false`, a folder that isn't empty is refused and left in place. |

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
- **Task cleanup:** DSM releases a finished Delete task itself and answers `stop` with error 401 (DSM 7.1 and 7.2.2), so `stop` runs only for a task that didn't finish (a DSM error while polling, cancellation). A task still running when the wait ends, or whose status request DSM doesn't answer, is handed off, not stopped (#245).
- **Handoff reply (#245):**

```
Still deleting on the NAS: /Backups/X (8 of 342 items, 2%). This operation keeps running after this reply, so nothing more needs to be done to finish it.
Handle: `delete:FileStation_179219158016597225`
To check on it, call file_task with this handle. To stop it, call file_task with action="stop" (it may then be partly done).
Not started: /Backups/Y, /Backups/Z. Run them again once this one finishes.
```

When DSM stops answering status requests during the wait (2026-10-17), the first line instead says so; the progress part appears only if DSM answered at least once. An answer that isn't a DSM reply reads `The NAS's status reply couldn't be read while deleting /Backups/X (not a DSM reply; a proxy or captive portal may be answering). It's probably still running there; don't start it again.`; an HTTP error status reads `The NAS, or a proxy in front of it, answered a status request with an error while deleting /Backups/X (HTTP 504). …`; any other unexpected answer names only its exception type. The error's text is never shown:

```
The NAS stopped answering while deleting /Backups/X (ReadTimeout; last progress: 8 of 342 items, 2%). It's probably still running there; don't start it again.
Handle: `delete:FileStation_179219158016597225`
To check on it, call file_task with this handle. To stop it, call file_task with action="stop" (it may then be partly done).
```

---

### 15. `file_task`

Follow up on, list, or stop a delete, copy, move or restore that was handed off to run on the NAS (#245).

**Permission tier:** WRITE (only write connections can start these tasks, and `stop` changes NAS state)

**Annotations:** `readOnlyHint=false`, `destructiveHint=true`: `action="stop"` can leave a move or delete half done, and annotations are static, so they follow the worst case (#183).

**DSM API:** `SYNO.FileStation.BackgroundTask` / `list` v2 (no handle); `SYNO.FileStation.Delete` or `SYNO.FileStation.CopyMove` / `status`, `stop` v2 (the handle's prefix picks the API; task IDs are JSON-quoted, #121)

**Tool description (LLM-facing):**
> Follow up on a delete, copy, move or restore that is still running on the NAS. Those tools hand
> off long operations and return a handle such as `delete:FileStation_…`, because the NAS keeps
> working after the reply. With a handle: `action="status"` shows progress, or the final result
> if DSM still has it; `action="stop"` stops it, and the operation may then be partly done. Without a
> handle: lists this user's running operations, which is useful in a new chat or after a restart.
> Handles only work for the same NAS user.

**Parameters:**

| Name | Type | Required | Default | Description |
|------|------|----------|---------|-------------|
| `handle` | `str` | No | `None` | The handle from a delete/copy/move/restore reply. Omit it to list running operations. |
| `action` | `"status"` or `"stop"` | No | `"status"` | `status` (default) or `stop`. |

**Response shapes:**

- **No handle:** a table of this user's running Delete/CopyMove tasks (handle, operation, path(s), started, progress), or `No File Station delete, copy or move tasks are running for this user.` On a DSM without `BackgroundTask` `list` (102-104), it says listing isn't available and to use the handle from the reply.
- **status, running:** `Still deleting on the NAS: /Backups/X (124,219 of 200,400 items, 62%; running for 14 min). Handle: ...` (the running time comes from the task ID, which begins with the start time in epoch seconds: in decimal on the real NAS (`FileStation_1792248483…`), in hex on vdsm 7.1 (`FileStation_6AD3911E…`). An ID of any other shape, or a time in the future or over 30 days old, gives no running time. `BackgroundTask` `list`'s `crtime` isn't the start: on a real NAS it read about 9 minutes after a 1 TB delete started, so it's not used, and the list's Started column comes from the task ID too, `unknown` otherwise). It says an unchanged count doesn't mean the task is stuck (for a delete: progress counts finished items, and one large file can take many minutes), that it keeps running without anything more, that stopping and starting again doesn't make it faster, and how to stop it if the user wants it canceled.
- **status, finished:** the original tool's result (deleted paths and recycle-bin wording, copy/move/restore skips and errors) when the server holds a record for the handle (from the handoff or from discovery); otherwise a generic result from DSM's final status (`Delete finished: 200,400 items processed.`, any DSM errors and skips) and a suggestion to list the path. DSM forgets a finished task after one read, so the final status is kept in the record.
- **status, unknown handle (599):** the remembered final result if there is one. Otherwise, with a record (a task that finished with no status read to catch it, or one that was stopped): `The delete of /Backups/X is no longer running on the NAS: it finished or was stopped, and DSM no longer has its result. List the path to see what's left.` Without one: `No running task with this handle. It finished, was stopped, or was started by a different DSM user. List the path to see what's left.`
- **stop without a handle** (omitted, empty or blank): `invalid_parameter` (param `handle`) saying stopping needs a handle, with a suggestion to call `file_task` without a handle to list the running operations. Nothing is listed or stopped, and DSM isn't called.
- **stop:** reads the task's `status` first, because CopyMove `stop` answers success for any task ID: finished (read or not), forgotten, made up or another user's (vdsm 7.1 and 7.2.2). Unknown (599): the same reply as `status` gives, plus `Nothing was stopped.` when no final result is remembered, prefixed with `That task had already finished, so nothing was stopped.` when a final result is remembered. Finished: `That task had already finished, so nothing was stopped.` and the final result, which is kept in the record. Still running: `stop`, then `Stopped <kind> of <path>; it may be partly done. List the path to see what's left.` A stop answered with 401 or 599 (it finished in between) says the task had already finished or isn't running.
- **DSM doesn't answer (transport error):** a `timeout` (or `unavailable`) error naming the handle, saying for the status read that nothing was stopped, or for the stop that it may or may not have taken effect, and suggesting `file_task` status or listing the path (#237).
- **An HTTP error status or a non-DSM reply (#282), or a 414 (#325):** an `unavailable` error naming the handle: ``Check task failed for `<handle>`: …``, or for a stop ``Stop task failed for `<handle>`: …``, ending `Nothing was stopped.` when the status read failed, or when the stop request itself got a 414 (rejected before anything acts on it), and otherwise `The stop may or may not have taken effect.` when the stop request failed. `retryable` is the error's own: true for a 5xx or a non-DSM reply, false for a 4xx such as a proxy's 403.

**Design notes:**
- **Handle format:** `delete:<taskid>`, `copy:<taskid>`, `move:<taskid>` or `restore:<taskid>`. Backticks, quotes, surrounding spaces and a trailing period are stripped. Anything else is `invalid_parameter` (param `handle`) before any DSM call, so a handle can never reach another API.
- **Records:** in memory, per server instance (one DSM user), last 50 handles for 24 h, lost on restart. DSM's own per-user 599 covers other users' handles.

---

## Module-Level Error Codes

| Code | DSM Meaning | User Message | Suggestion |
|------|-------------|--------------|------------|
| 400 | Invalid parameter | "Invalid parameter in the file operation request." | Check path format and parameter values. |
| 401 | Unknown error | "An unknown file operation error occurred." | Retry or check NAS logs. |
| 402 | System too busy | "The NAS is too busy to process this request." | Wait and retry. |
| 407 | Operation not permitted | "Operation not permitted" (reported as `permission_denied`, like 105; never re-auths) | Check the folder's permissions in DSM, or use an account with access. Seen as `list_files /home` for a non-admin service account whose `home` share showed volume `var`, 0 B free (seska DSM 7.1.1, 0.10.0rc5 live test; the cause isn't known). |
| 408 | No such file/directory | "Path not found: '{path}'." | Use `list_files` or `search_files` to find the correct path. |
| 414 | File already exists | "A file already exists at the destination." | Use `overwrite=true` to replace. |
| 415 | Disk quota exceeded | "Disk quota exceeded." (reported as `disk_full`, like 416) | The account's or the shared folder's quota is full: free space within it, or have the administrator raise it. A copy or move that hits it part-way leaves what it wrote at the destination, and its status says `processed_size: 0` (seska DSM 7.1.1: 18 GiB left), so the error names the destination. |
| 416 | No space left | "No space left on the target volume." | Free space on the NAS. |
| 418 | Illegal name or path | "Invalid file/folder name or path." | Avoid characters: `/ \\ : * ? " < > \|` |
| 419 | Illegal file name | "Invalid file name." | Avoid characters: `/ \\ : * ? " < > \|` |
| 421 | Device or resource busy | "The file is in use by another process." | Wait and retry. |
| 599 | No such task | "Background task not found (may have already completed)." | — |
| 1200 | Failed to rename | "The path may not exist, or the new name may already be taken in that folder" (from `rename`, told apart into `not_found` or `already_exists` by `getinfo` when it can, #188) | Check both with `list_files`, then retry. |

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

File Station's guidance is in `src/mcp_synology/instructions/filestation.md` and `filestation-write.md`. The server sends `filestation.md` after the core `server.md` only when the `filestation` module is enabled, at either permission (#169; the order is in `config-schema-spec.md` under **Built-in instructions**). It covers only what no single tool's description says (#377): the path format and calling `list_shares` first, the default download folder, and that a share's bin is its `#recycle` folder. `filestation-write.md` follows it only when the WRITE tools are registered (`permission: write` or `admin`, #318): the search → review → act pattern for moving files, and handed-off long operations (`file_task`). When to use each tool, and which to use instead, is in the tools' own descriptions (#373). At `permission: read` only the PERMISSIONS section names the WRITE tools, as withheld, and LOCAL PATHS names `download_file` alone. Size units are in the core `server.md`, since the other modules show sizes too. When `upload_file` or `download_file` is registered, the generated LOCAL PATHS section names the allowed local folders, after the local OS and home folder from `local.md`.

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
