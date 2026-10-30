# Download Station Module — Tool Specifications

> **Version:** 0.6 | **Updated:** 2026-10-11. First living spec for this module (#137), written from the shipped code and the behavior verified against live DSM in #123/#125, #128/#130, #124/#133, #140/#141, #143/#144, #136/#146 and #145. Tool count: 5 READ + 7 WRITE = 12.
> **Parent doc:** `docs/specs/architecture.md`
>
> **Verified on:** DSM 7.1 with Download Station 3.9-4627, and DSM 7.2.2 with Download Station 4.1-5012 (vdsm, admin and non-admin personas), plus a real NAS (DSM 7.1.1, 1,214 tasks) for scale. Where the official DS Web API guide and DSM disagree, this spec follows DSM.
>
> Kept in sync with the code by `tests/test_module_specs.py`: tool headings, permission tiers, parameter names and the exact LLM-facing descriptions must match what ships. The pre-verification design note this module was first built from was never committed and is superseded by this document.

## Module Metadata

```python
MODULE_INFO = ModuleInfo(
    name="downloadstation",
    description="Manage Synology Download Station tasks, schedule, and configuration",
    required_apis=[
        ApiRequirement(api_name="SYNO.DownloadStation.Task", min_version=1),
        ApiRequirement(api_name="SYNO.DownloadStation.Info", min_version=1, optional=True),
        ApiRequirement(api_name="SYNO.DownloadStation.Schedule", min_version=1, optional=True),
        ApiRequirement(api_name="SYNO.DownloadStation.Statistic", min_version=1, optional=True),
        ApiRequirement(api_name="SYNO.DownloadStation.RSS.Site", min_version=1, optional=True),
        ApiRequirement(api_name="SYNO.DownloadStation.RSS.Feed", min_version=1, optional=True),
        ApiRequirement(api_name="SYNO.DownloadStation.BTSearch", min_version=1, optional=True),
        ApiRequirement(api_name="SYNO.DownloadStation2.Task", min_version=1, optional=True),
        ApiRequirement(api_name="SYNO.DownloadStation2.Settings.Scheduler", min_version=1, optional=True),
    ],
    tools=[...],  # 5 READ + 7 WRITE, below
    settings_schema=DownloadStationSettings,  # no fields yet
)
```

Like every module, it loads only when it's listed: add `downloadstation` under the top-level `modules:` key of the config file (or the per-NAS file). The tools register from the config alone. If the Download Station package isn't installed on the NAS, every call fails with `api_not_found`. The RSS and BT search APIs are declared for the planned Phase 3 and have no tools yet.

---

## Design Conventions

### Two API families

| Family | Used for | Notes |
|---|---|---|
| v1 `SYNO.DownloadStation.*` (`Task`, `Info`, `Schedule`, `Statistic`) | Listing, task info, pause/resume/edit/delete, config, schedule flags, stats | Task `status` is a **string** (`"downloading"`, `"seeding"`, …). There's no server-side status filter. Global config writes go through `Info.setserverconfig`; there is no `setconfig`. |
| DS2 `SYNO.DownloadStation2.*` (`Task`, `Settings.Scheduler`) | URI and torrent creation (returns task IDs), the true queue total, the weekly plan and its limited-speed rates | JSON-format API: string values are JSON-encoded. `Settings.Scheduler.set` does **no** server-side validation and stores whatever it is given. |

### Transport

- **Everything is GET,** per the project rule, with **one scoped exception:** URI `Task.create` (DS2 v2, and the v1 fallback) is sent as a form POST through `DsmClient.request_form_post()`. A GET URL past about 8 KB gets HTTP 414 from DSM (a magnet with many trackers, or a URI list), and a GET would put source-URL credentials in access logs. Nothing else may use `request_form_post()`.
- **Torrent and NZB upload** is a DS2 multipart POST (`create_download_task_with_file()`, file part named `torrent`).
- **Version pins:** `Task.edit` is pinned to exactly v2 (it doesn't exist at v1). DS2 `Task.create` and DS2 `Task.list` are pinned to v2.
- **Never retried blindly:** both create paths, URI create (`request_form_post()`) and torrent/NZB upload (`create_download_task_with_file()`), send through `_guarded_post()`. A timeout maps to `timeout`, a connection failure or a non-2xx response to `unavailable`, and an HTTP 414 to `invalid_parameter`, all with `retryable: false` and a "check before retrying" suggestion, because DSM may already have created the task. A message is never empty (#155).

### Statuses

`status_filter` groups DSM's string statuses:

| Filter | DSM statuses |
|---|---|
| `downloading` | `waiting`, `downloading`, `finishing`, `hash_checking`, `filehosting_waiting`, `extracting` |
| `finished` | `finished`, `seeding` |
| `paused` | `paused` |
| `error` | `error` |
| `all` | everything |

A failed task's status carries DSM's reason from `status_extra.error_detail`: `error (broken_link)` for a source that answered 404 (seen on DSM 7.2.2). It's shown in both `list_downloads` rows and `get_download_info`'s Status line (#147).

### Destinations

Download Station takes share-relative paths (`video/Torrents`), while File Station shows `/share/...`. DSM rejects the slash form: 403 "Destination doesn't exist" on create, and 100 "Unknown error" on edit (DSM 7.2.2). So `create_download`, `edit_download` and `set_download_config(default_destination=...)` trim surrounding whitespace and drop leading and trailing slashes before sending (`" /writable "` → `writable`). A destination with no share left (`/`, empty) gets `invalid_parameter`, naming the caller's argument (`destination`, or `default_destination` for `set_download_config`) (#147). A folder that doesn't exist still fails at DSM, since **Download Station doesn't create missing folders** (#296; verified on vdsm 7.1 and 7.2.2 for a non-admin, see `test_missing_destination_folder_then_create_folder`): create's 403 and edit's whole-call 100 both suggest a share-relative path (`video/Torrents`, not the `/volume1/video/Torrents` real path that `get_file_info` prints) and, for a new folder, creating it first with File Station's `create_folder` (a WRITE tool) or, on a connection without it, asking the user to create it in DSM, and retrying; edit's also says to check the folder with File Station's `list_files`. Each names File Station, because on a Download-Station-only connection `create_folder` isn't registered and nothing else ties it to the File Station module. Whether DSM answers 403 or 402 to an existing folder the user can't write is not captured. Edit keeps 100's `dsm_error` code, since 100 isn't specific, but says the destination is the likely cause (#147). Destinations are displayed as DSM stores them, share-relative.

### Managers and non-managers

`ds_is_manager()` reads `is_manager` from `SYNO.DownloadStation.Info` / `getinfo`. It returns `None` when that can't be determined, and callers then keep their default behavior instead of failing. A non-manager is an account with the Download Station app privilege that isn't in DSM's `administrators` group: adding an account to that group makes `is_manager` true. Verified on DSM 7.1 (Download Station 3.9) and 7.2.2 (4.1) on vdsm, 2026-10-19: the non-admin `mcptest` read `is_manager: false` and didn't see the admin's task; after `synogroup --memberadd administrators mcptest` and a new login it read `true` and saw the admin's task. Removing an account from the group again wasn't tested, nor were DSM 7.2's delegated admin roles. We found no separate manager setting; Synology's API guide defines `is_manager` only as "If the logged in user is manager".

- **Sees only their own tasks,** in v1 `Task.list` and in DS2's total alike (verified on both versions). List output says "your tasks only".
- **Gets a sanitized, per-user view from `Info.getconfig`:** a null default destination, zeroed rates. `get_download_config` labels it as such.
- **NAS-wide config writes:** DSM answers success to them but ignores them, so `set_download_config` **refuses** the rate fields and sends nothing.
- **`default_destination` is per user:** DSM applies it to the calling user only.
- **DS never starts a non-manager's tasks** until that user has a default destination. They stay `waiting` forever (`download_queue` status 1, pid -1) until the user has a `user_setting` row, which setting their own default creates. `create_download` handles this, see below.
- **The weekly plan** (DS2 `Settings.Scheduler`) is admin-only, and refused with 105. That 105 never triggers a re-auth. The schedule on/off flag (v1 `Schedule`) is NAS-wide, and **any** DS user can toggle it.

### Another user's task looks like a missing one

Probed on both versions (#136): a non-manager targeting another user's task gets exactly what a nonexistent ID gets.

| Call | Result |
|---|---|
| `resume`, `edit`, `delete` | per-task error **544** |
| `pause` in a mixed list | per-task **404** |
| `pause` when **no** ID can be paused | whole-call **101**, also for the caller's **own** finished or already-paused task |
| `getinfo` | whole-call **404** (even if one of the IDs is the caller's) |

The tools render per-task 544 and 404 as `not found (no such task, or not yours)`, add a hint pointing to `list_downloads`, and turn pause's whole-call 101 into an `invalid_parameter` error that names all three causes it covers: no such task, another user's task, or a task that can't be paused (finished, or already paused). Its suggestion is to check each task's status with `list_downloads` (#146). A 101 from any other method keeps its normal meaning.

### Secrets in URLs

DSM returns tracker announce URLs and task source URIs verbatim. A private tracker's announce URL embeds the user's **passkey** (`/announce/<passkey>`), and a source URI can carry `user:pass@` or tokens. Everything that shows a URL goes through `core/redaction.py` (#143):

- userinfo becomes `***@`
- every query value and path-parameter value (`;sid=`) becomes `***`
- a token inside a path segment becomes `***`: a run of `[A-Za-z0-9_-]` holding an alphanumeric stretch of 16+ characters with both a letter and a digit, or a UUID. So `<passkey>.torrent` becomes `***.torrent`, while names of short words (`ubuntu-24-04-desktop-amd64`) are kept
- the fragment is dropped
- a magnet keeps only `xt`, `dn`, `xl` and `kt` as they are, redacts its URL-valued parameters (`tr`, `ws`, `as`, `xs`) recursively, and masks every other value. A tracker URL pasted unencoded splits on `&`, so its `passkey=` lands at the top level, where it's masked

That covers `get_download_info`'s Trackers table and URI line, and every `DsmClient` DEBUG request log. DSM still receives the real values.

Task titles get a narrower pass (`redact_title`): DSM titles an HTTP task with its URL's tail, query string included (`file.bin&_sid=...`, seen on vdsm). So `?key=`, `&key=` and `;key=` values in a title, and any whole URL in it, are masked in `list_downloads` and `get_download_info`. Whether token-shaped words are masked depends on the task type (`tasks._title`):
- **BT:** kept, because a BT title is the torrent's name (`Show_Series_1_2026_1080p`).
- **Every other type** (`http`, `ftp`, `nzb`, `emule`, or no type): masked as in a URL path, because the title is the URL's last segment. A private tracker's `…/<passkey>.torrent` link is titled `<passkey>.torrent` and shows as `***.torrent` (#144).

### Formatting

Plain text through the shared formatters. Speeds use `format_speed` (`—` for 0), sizes are binary units, timestamps are **UTC and say so** (`YYYY-MM-DD HH:MM:SS UTC`, `—` for 0 / never; #137), consistent with `search_files` treating naive input dates as UTC.

### Errors

DS-specific codes (`core/downloadstation_errors.py`) are dispatched when the API name begins with `SYNO.DownloadStation`. Their meanings differ from File Station's use of the same numbers. Codes 105 and 106 still fall through to the common handlers, so **105 never re-auths** for DS calls either.

| Code | Meaning | Suggestion (abridged) |
|---|---|---|
| 400 | File upload failed | Check the .torrent/.nzb is readable and within DSM's size limit |
| 401 | Max number of tasks reached | Pause or delete finished tasks first |
| 402 | Destination denied | Check write permission on the destination share |
| 403 | Destination doesn't exist | Reported as `not_found` (#334), with `param: destination` on `create_download`. Pass it share-relative; a new folder must be created first with File Station's `create_folder`, or by the user in DSM (Download Station doesn't create folders), then retry |
| 404 | Invalid task id | No such task, or another user's; run `list_downloads` |
| 405 | Invalid task action | Not valid for the task's current state: resuming an active or finished task, or pausing a paused or finished task (per task, in a list that has other valid IDs) |
| 406 | No default destination | Set one with `set_download_config` |
| 407 | Set destination failed | Path invalid or no write permission |
| 408 | File doesn't exist | A referenced .torrent/.nzb on the NAS is missing |
| 120 (common) | JSON-format API parameter error | Mapped to `invalid_parameter` with a specific message |
| 544 (per task) | Not found, or not yours | Rendered as above; not in the code map because it only appears per task |

Tool input is validated locally first and fails before any DSM call. A bad value gets `invalid_parameter` naming the parameter, plus `valid` values where there's a fixed set (status filters, sort keys, the `uri`/`torrent_file_path` choice). A `torrent_file_path` that isn't a `.torrent` or `.nzb` file gets `invalid_parameter`; one outside the allowed local folders (the top-level `allowed_local_paths` plus the module's own, #257) gets `permission_denied`, both before the file is read or its existence checked, so a refusal never reveals what exists outside. A missing file inside the folders gets `not_found`.

---

## READ Tier Tools (5 tools)

### 1. `list_downloads`

List the queue, paged, with the true total.

**Permission tier:** READ

**DSM API:** `SYNO.DownloadStation.Task` / `list` v1 (`additional=detail,transfer`); `SYNO.DownloadStation2.Task` / `list` v2 for the total

**Tool description (LLM-facing):**
> List download tasks in the Download Station queue. Returns a table with id, title, type
> (bt/http/ftp/nzb), status, size, progress%, download and upload speed, ETA, and completion time.
> Paged (25 by default); the footer gives the true total and the next offset. A status filter or
> sort covers the whole queue, not just one page: sort_by=completed with limit=1 gives the most
> recently finished task. Accounts that are not Download Station managers see only their own tasks
> (the output says so), so an empty list does not mean the NAS is idle.

**Parameters:**

| Name | Type | Required | Default | Description |
|------|------|----------|---------|-------------|
| `status_filter` | `str` | No | `"all"` | `all`, `downloading` (also waiting, finishing, hash checking, waiting for a file host, or extracting), `finished` (also seeding), `paused` or `error` (see Statuses). |
| `sort_by` | `str` | No | `"queue"` | `queue` (DSM order: creation order, oldest first), `created` or `completed` (newest first; tasks that never completed sort last). |
| `offset` | `int` | No | `0` | First task to show (0-based) within the filtered and sorted list. Must be 0 or more; a negative offset is rejected locally with `invalid_parameter` (#141). The footer gives the next offset. |
| `limit` | `int` | No | `25` | Page size. Must be 1 or more, checked locally (#141). |

**Response shape:**

```
Download Station queue (all)
=================================================================================================================================
  ID         Title                    Type  Status   Size    Progress                Down  Up          ETA  Completed
  ─────────  ───────────────────────  ────  ───────  ──────  ──────────────────────  ────  ──────────  ───  ───────────────────────
  dbid_2554  Mrs Biggs  (2012) 720p   bt    seeding  4.2 GiB  4.2 GiB / 4.2 GiB (100%)  —     —           —    2025-09-27 13:23:43 UTC
  dbid_4010  Cleddau Series 2 (2026)  bt    seeding  6.5 GiB  6.5 GiB / 6.5 GiB (100%)  —     100.9 KiB/s  —    2025-10-11 20:01:33 UTC

1,214 task(s) total; showing 1–2. Pass offset=2 for the next page.
```

The heading adds `; newest completed first` (or `created`) for a sort, and `; your tasks only` for a non-manager. An offset past the end shows the heading, `No items to display.` (no column headers) and `N task(s) total; offset X is past the end.`

**Design notes:**
- **DSM's v1 `total` is the page size, not the queue size** (`limit=5` gives `total=5`; `limit=-1` returns everything). #140 found the tool reporting "100 task(s) total" for 1,214. So:
  - **Unfiltered, queue order:** one v1 page. A short page gives the total (`offset + rows`) with no extra call. A full page, or an empty page past offset 0, takes the total from DS2 `Task.list` (`limit=1`: 51 ms and under 1 KB at 1,214 tasks). If DS2 is absent or refuses, the fallback is a v1 `limit=-1` listing without `additional` (373 ms, 192 KB).
  - **Filtered or sorted:** fetch every task (`limit=-1` with `detail,transfer`: 750 ms, 846 KB at 1,214 tasks), filter, sort, then page. The total is the number of matches. A filter used to see only the first page.
- DS2's total is scoped to the caller, like v1's list: a non-manager's count is their own tasks. That's verified on both versions.
- **A size of 0 means DSM doesn't know it yet** (#345), e.g. an HTTP download whose server sends no size (seska, DSM 7.1.1, at 70.1 MiB downloaded). `Size` then reads `unknown` and `Progress` `70.1 MiB downloaded (size unknown)`, here and in `get_download_info`. Only a finished or seeding task with nothing downloaded keeps `0 B`, as a real empty download. On vdsm (DSM 7.1, 7.2.2, 7.3.2, 7.4.1), a download whose server sends no `Content-Length` reads size 0 while it runs, and DSM fills in the real size when it completes (at `finishing` on 7.1 and 7.4.1, at `finished` on 7.2.2 and 7.3.2), so a finished task normally shows its size; `test_unknown_size_reads_unknown_until_dsm_knows_it` pins both.
- `Down` and `Up` are the current speeds. An all-seeding queue used to look idle because only the download speed was shown (#145).
- `sort_by="completed", limit=1` answers "what finished last?" in one call. Creation order isn't completion order, because seeders vary.
- The default page is 25 to save model context (100 rows came to about 15 KB).

---

### 2. `get_download_info`

Everything about one task.

**Permission tier:** READ

**DSM API:** `SYNO.DownloadStation.Task` / `getinfo` v1 (`additional=detail,transfer,file,tracker,peer`)

**Tool description (LLM-facing):**
> Get detailed information for a specific download task: detail (status, destination, URI), transfer
> (size downloaded/uploaded, speed, peers), files (per-file selection for BT), trackers (BT only),
> peers (BT only). Use list_downloads first to find the task_id. For totals across all tasks, use
> get_download_stats; for settings, use get_download_config.

**Parameters:**

| Name | Type | Required | Default | Description |
|------|------|----------|---------|-------------|
| `task_id` | `str` | **Yes** | — | A `dbid_…` ID from `list_downloads`. |

**Response shape:** a `Task` block (ID, title, type, status, size); `Detail` (destination, URI, priority, created, started, completed); `Transfer` (downloaded/total, uploaded, download and upload speed); then, for BT, `Files` (name, size, progress, priority), `Trackers` (URL, status, peers, seeds) and `Peers` (address, client, progress, speeds) tables.

```
Trackers
=========================================================
  Tracker                             Status   Peers  Seeds
  ──────────────────────────────────  ───────  ─────  ─────
  https://tvchaosuk.com/announce/***  Success  0      70
```

**Design notes:**
- Tracker URLs and the source URI are redacted (see Secrets in URLs). The passkey above is masked.
- An unknown ID, or another user's task, fails with 404, whose suggestion mentions both. An empty `tasks` array is also treated as not found.
- File sizes in the BT tables arrive as strings from DSM; they're converted before formatting.

---

### 3. `get_download_stats`

Queue-wide throughput.

**Permission tier:** READ

**DSM API:** `SYNO.DownloadStation.Statistic` / `getinfo` v1

**Tool description (LLM-facing):**
> Get current Download Station throughput statistics: total download and upload speed across all
> tasks, plus eMule download/upload speed when the eMule service is enabled. For per-task speeds use
> list_downloads.

**Parameters:** None.

**Response shape:**

```
Download Station throughput
====================================
  Download (total):  —
  Upload (total):    176.0 KiB/s
```

**Design notes:**
- `Download (eMule)` and `Upload (eMule)` are added only when DSM returns the eMule fields (eMule enabled). A real 0 is shown as `—`.
- DSM's `Statistic` doesn't break speeds down by BT or HTTP/FTP, and the description no longer claims it does (#137). `list_downloads` gives per-task speeds.

---

### 4. `get_download_config`

The global configuration, or a non-manager's own view of it.

**Permission tier:** READ

**DSM API:** `SYNO.DownloadStation.Info` / `getconfig` v1

**Tool description (LLM-facing):**
> Get Download Station global configuration: default destination, BT/HTTP/FTP/NZB/eMule max speeds,
> eMule and auto-unzip state. For accounts that are not Download Station managers DSM returns a
> per-user view (labeled as such), not the NAS-wide settings. Use get_schedule for the weekly plan.

**Parameters:** None.

**Response shape:**

```
Download Station configuration
=======================================
  Default destination:  video/Torrents
  BT max download:      unlimited
  BT max upload:        unlimited
  HTTP max download:    unlimited
  FTP max download:     unlimited
  NZB max download:     unlimited
  eMule enabled:        no
  eMule max download:   unlimited
  eMule max upload:     20 KB/s
  Auto-unzip enabled:   no
```

**Design notes:**
- Rates are KB/s; 0 means unlimited.
- For a non-manager, the title adds `(your user view — not the NAS-wide settings)`. DSM returns that user's own default destination (or null) and zeroed rates.

---

### 5. `get_schedule`

The schedule flags, limited-speed rates and weekly plan.

**Permission tier:** READ

**DSM API:** `SYNO.DownloadStation.Schedule` / `getconfig` v1 (flags); `SYNO.DownloadStation2.Settings.Scheduler` / `get` v1 (plan and rates, admin)

**Tool description (LLM-facing):**
> Get the Download Station schedule: whether it is enabled, the limited-speed (alt) download/upload
> rates, and the weekly plan as a 7-day × 24-hour grid where each hour is full speed, limited, or no
> download. The weekly plan needs a Download Station manager/admin account; otherwise only the flags
> are shown.

**Parameters:** None.

**Response shape:**

```
Download Station schedule
==========================================
  Enabled:                 no
  eMule schedule enabled:  no
  Limited download rate:   unlimited
  Limited upload rate:     unlimited

     00 01 02 03 04 05 06 07 08 09 10 11 12 13 14 15 16 17 18 19 20 21 22 23
Sun  #  #  #  #  #  #  #  #  #  #  #  #  #  #  #  #  #  #  #  #  #  #  #  #
...
Sat  #  #  #  #  #  #  #  #  #  #  #  #  #  #  #  #  #  #  #  #  #  #  #  #

Legend: # = full speed   ~ = limited (alt speed)   . = no download
Hours are the NAS's local time.
```

**Design notes:**
- The plan is stored as 168 characters, day-major from Sunday, 24 per day: `0` no download, `1` full speed, `2` limited.
- **The hours are the NAS's local time** (verified on vdsm DSM 7.2.2, DS 4.1, and DSM 7.1, DS 3.9, with the NAS switched to Calcutta, +05:30, so local and UTC hours differ). With only the current local hour set to `0`, a new HTTP task and a BitTorrent task both stayed `waiting`. With only the current UTC hour set to `0`, the HTTP task finished and the BitTorrent task went to `downloading`. The plan applies to HTTP tasks as well as BitTorrent. A newly enabled plan applied within 5 s. After the NAS's zone changes, DS keeps using the old zone for about a minute (51–76 s on 7.2.2), then the new one. The grid says the hours are the NAS's local time, and so does `set_schedule`'s description, so a plan isn't written in the user's zone by mistake.
- A malformed stored plan is still rendered, never rejected: unreadable cells show `?`, plus a note. Other clients can store anything, and a read must not fail on it.
- When DS2 is absent, or refuses (105 for non-admins, with no re-auth), only the flags are shown, followed by `Note: weekly plan not available (<reason>).`
- DS2's `enable_schedule` is the same flag as v1's `enabled`.

---

## WRITE Tier Tools (7 tools)

### 6. `create_download`

Create tasks from URIs, or from a local .torrent / .nzb file.

**Permission tier:** WRITE

**DSM API:** `SYNO.DownloadStation2.Task` / `create` v2 (form POST for URIs, multipart for files); v1 `SYNO.DownloadStation.Task` / `create` (form POST) as the fallback and for credentials

**Tool description (LLM-facing):**
> Create one or more download tasks, from URIs or from a local .torrent / .nzb file. `destination`
> must be an existing folder, share-relative (DS doesn't create folders; make a new one first with
> File Station's `create_folder` when File Station write is enabled, or ask the user to create it in
> DSM). Omit it to use your default destination (a non-manager who has none must set it first with
> `set_download_config`, because DS won't start their tasks until then). Returns the new task IDs
> when DSM provides them (not for URLs with `username` / `password`); long magnet links and URI
> lists are supported.

**Parameters:**

| Name | Type | Required | Default | Description |
|------|------|----------|---------|-------------|
| `uri` | `str` | One of `uri` / `torrent_file_path` | — | Comma-separated URIs: HTTP, HTTPS, FTP, magnet, etc.; commas inside a URI are kept (only a comma followed by a scheme starts another URI). Not together with `torrent_file_path`. |
| `torrent_file_path` | `str` | One of `uri` / `torrent_file_path` | — | Local path to a `.torrent` or `.nzb` file (uploaded from this machine), inside the allowed local folders, which its description names (`roots_for_description`). |
| `destination` | `str` | No | the user's default | Share-relative destination folder (`video/Torrents`); it must already exist. |
| `username` | `str` | No | — | User name for a protected URL (not for torrent files). |
| `password` | `str` | No | — | Password for a protected URL (not for torrent files). |

**Response shape:** `Created 2 download task(s): dbid_4011, dbid_4012.`, or, when DSM returns no IDs: `Created download task(s) for 2 URI(s); DSM did not return task IDs — use list_downloads to find them.`, followed by any destination note.

**Design notes:**
- **Path choice:**
  - URIs without credentials go to **DS2** v2, which returns the new task IDs.
  - If DS2 is absent, or answers 102/103/104, the request falls back to **v1**. Other DS2 errors are reported, not retried.
  - URIs **with** `username`/`password` always use v1, with the credentials in the POST body, because DS2's credential fields are unverified. v1 returns no IDs.
  - Files go to DS2 as multipart.
- **The destination is always explicit** (#133). DS2 `Task.create` answers 120 to **any** user who omits `destination`, even an admin with a default set. So the tool fills in the user's own default from `Info.getconfig`, then:
  - **no default and no `destination`:** refused with `invalid_parameter` for any account. A non-manager is told to set their default with `set_download_config`; anyone else is told to pass `destination`.
  - **a non-manager with an explicit `destination` but no default:** the task is created, with the note `Download Station won't start your tasks until your default destination is set; they stay waiting.`
  - **the Info API absent, or its config unreadable:** the request goes ahead unchanged.
- **Commas inside a URI (#397).** A comma is legal in a URL (`https://h/a,b.iso`) and common in a magnet's `dn` name, so `uri` is split only where a comma is followed by a scheme with `//` (`http://`, `ftp://`, `ed2k://`) or `magnet:`, `thunder:`, `flashget:`, `qqdl:`; any other comma stays in its URI. DS2 gets the URIs as a JSON list, so a comma needs nothing more (verified on DSM 7.1, 7.2.2, 7.3.2 and 7.4.1: the task's URI reads back unchanged). v1 takes one comma-separated string: an unescaped comma inside a URI makes the create fail, and `\,` is kept as a plain comma (verified on the same four versions), so the v1 fallback escapes it, as File Station's multi-path parameters do.
- Source URIs are redacted in DEBUG logs. The POST body DSM receives is not.
- Validation: exactly one of `uri` / `torrent_file_path`; credentials only with `uri`; a non-empty URI list. For a file, in this order: a `.torrent` or `.nzb` extension (any case), inside the allowed local folders (resolved after `~`, `..` and symlinks; #257), then it must exist.

---

### 7. `delete_download`

Remove tasks.

**Permission tier:** WRITE

**Annotations:** `destructiveHint=true`: a deleted task can't be restored, and an unfinished task's partial data is discarded.

**DSM API:** `SYNO.DownloadStation.Task` / `delete` v1

**Tool description (LLM-facing):**
> Remove one or more download tasks. A deleted task can't be restored (its source URL is gone with
> it), so name the tasks and confirm with the user before deleting. Files from finished tasks are
> always kept. For unfinished tasks the partial data is discarded (irreversible); don't rely on
> `force_complete` to keep it.

**Parameters:**

| Name | Type | Required | Default | Description |
|------|------|----------|---------|-------------|
| `task_ids` | `list[str]` | **Yes** | — | Task IDs to remove, from `list_downloads`. |
| `force_complete` | `bool` | No | `false` | Asks DSM to keep an unfinished task's partial file; passed through to DSM, which may discard it anyway and refuses it on finished tasks. See below. |

**Response shape:** a per-task table (`ok`, `not found (no such task, or not yours)`, or `error N`) under a title such as `Delete download — 1 of 2 task(s) removed; completed files kept; partial data of unfinished tasks discarded`. When nothing was removed, the title says `0 of N task(s) removed; nothing was deleted`. With `force_complete=true`, the unfinished part reads `force_complete requested, but DSM may still discard partial data; check the destination`.

**Design notes:**
- **Verified on both versions** (#128/#130):
  - Files from **finished** tasks are always kept.
  - An **unfinished** HTTP task's partial data is discarded, **with or without** `force_complete`, whether the task was downloading or paused. DSM's guide says `force_complete` moves the incomplete file into the destination, but neither version did, so the tool never claims a move.
  - On a **finished** task, `force_complete=true` is refused per task with **405**, and the task stays listed. The title counts only removed tasks, and a hint says to retry those with `force_complete=false`.
- Not-found and another user's tasks: see the conventions above.

---

### 8. `pause_download`

**Permission tier:** WRITE

**DSM API:** `SYNO.DownloadStation.Task` / `pause` v1

**Tool description (LLM-facing):**
> Pause one or more download tasks. When some of the tasks can be paused, an already-paused or
> finished one is refused for its id (error 405) or shown as `not paused: already finished`, and the
> others are paused. When none can be paused (one finished or already-paused task alone, a missing
> id, or another user's task), the whole call fails with invalid_parameter; list_downloads shows each
> task's status.

**Parameters:**

| Name | Type | Required | Default | Description |
|------|------|----------|---------|-------------|
| `task_ids` | `list[str]` | **Yes** | — | Task IDs to pause, from `list_downloads`. |

**Response shape:** a per-task table under `Pause download — N task(s)`, plus the not-found hint when needed.

**Design notes:** pause is the one method that fails the **whole call** with 101 when no ID can be paused. That covers a missing ID, another user's task, and the caller's own finished or already-paused task (probed on DSM 7.1 and 7.2.2), so it becomes `invalid_parameter` naming the IDs and all three causes, not `not_found`. Within a mixed list, an already-paused or finished task is refused **per task** with 405 while the others are paused; the whole-call 101 happens only when nothing in the call is pausable (QA on #153, DSM 7.1 and 7.2.2). **Download Station 3.9.5 (DSM 7.1.1, a real DS1618+) once answered a finished task's pause with a per-task `ok`, and the task stayed finished** (0.10.0rc9 live test, #413: the task had finished 2 s earlier; resume of the same task got 405). In the 0.10.0rc10 live test the same NAS gave the whole-call 101 for a lone task that had finished about 20 s earlier, and for a lone already-paused task, as vdsm does (#421). The description therefore names both outcomes and keeps the per-task ones to calls where something else can be paused. So after a pause, the tasks DSM answered `ok` for are read back (`Task getinfo` v1, `additional=detail`), and a finished one's row says `not paused: already finished`. If the read-back fails (a DSM error or no answer), DSM's rows stand. Resume reads nothing back.

---

### 9. `resume_download`

**Permission tier:** WRITE

**DSM API:** `SYNO.DownloadStation.Task` / `resume` v1

**Tool description (LLM-facing):**
> Resume one or more paused download tasks. An active or finished task is refused for that id (error
> 405), while the others are resumed. list_downloads with status_filter=`paused` finds the paused
> tasks.

**Parameters:**

| Name | Type | Required | Default | Description |
|------|------|----------|---------|-------------|
| `task_ids` | `list[str]` | **Yes** | — | IDs of paused tasks to resume, from `list_downloads`. |

**Response shape:** a per-task table under `Resume download — N task(s)`, plus the not-found hint when needed.

**Design notes:** pause and resume share `_task_state_change`. Per-task 544 means not found or not yours. Resuming an active (including `waiting`) or finished task is refused per task with 405; it isn't a no-op (DSM 7.1 and 7.2.2).

---

### 10. `edit_download`

**Permission tier:** WRITE

**Annotations:** `destructiveHint=true`, `idempotentHint=true`: it replaces a task's destination (the previous one is lost), and repeating a call changes nothing more. `pause_download` and `resume_download` keep the defaults (`destructiveHint=false`) because they only toggle a task's state and lose nothing.

**DSM API:** `SYNO.DownloadStation.Task` / `edit`, pinned to **exactly v2**

**Tool description (LLM-facing):**
> Edit task parameters. Currently supports `destination` (change the task's output share). Requires
> a newer Download Station (Task API v2); older versions get a clear error. This changes only the
> listed tasks; for global settings, use set_download_config. A finished task's file isn't moved: the
> reply says `not changed` for it.

**Parameters:**

| Name | Type | Required | Default | Description |
|------|------|----------|---------|-------------|
| `task_ids` | `list[str]` | **Yes** | — | Task IDs to edit, from `list_downloads`. |
| `destination` | `str` | **Yes** | — | New share-relative destination (the only editable field); the folder must already exist. |

**Response shape:** a per-task table under `Edit download — N task(s), destination=<dest>`.

**Design notes:** `edit` doesn't exist at v1. A NAS whose `Task` API stops at v1 gets a clear `api_not_found` ("requires a newer Download Station"), not a raw "method does not exist". `destination` is optional in the signature so that omitting it gets a clear validation error. A whole-call DSM 100 is edit's answer to a folder that doesn't exist (see Destinations). **An edit of a finished task: success with no per-task result, and nothing changes** (Download Station 3.9.5 on DSM 7.1.1, 0.10.0rc9 live test, #412; the file stays where it was). Any task DSM returns no per-task result for is read back (`Task getinfo` v1, `additional=detail`; when one ID is missing or another user's, `getinfo` fails the whole call with 404, so each ID is then read alone) and its row says `ok` (its destination is now the new one: the share name compared ignoring case, since DSM takes it in any case and stores it as typed, and the folders below it exactly, since they're case-sensitive: on vdsm `QaCase418` and `qacase418` are two folders, QA on #418), `not changed: finished (Download Station doesn't move a finished task's file)`, `not changed: destination is still <dest>`, or not found. If some tasks got rows from DSM and the others can't be read back, those say `no result from DSM; check with get_download_info`. If none got a row and they can't be read back: `[!] Edit download — … : DSM accepted the request but reported no per-task result, and the tasks couldn't be read back. Check each with get_download_info.` On vdsm (unfinished tasks) DSM returns a row per task, which is shown as before.

---

### 11. `set_download_config`

**Permission tier:** WRITE

**Annotations:** `destructiveHint=true`, `idempotentHint=true`: it replaces global values (the previous ones are lost), and repeating a call changes nothing more.

**DSM API:** `SYNO.DownloadStation.Info` / `setserverconfig` v1

**Tool description (LLM-facing):**
> Set Download Station global configuration. All parameters are optional: only the fields you pass
> are updated. The rate fields require a Download Station manager/admin account: DSM silently
> ignores them from other accounts, so the tool refuses them. Any user can set
> `default_destination`: DSM applies it per user, and DS won't start a non-manager's tasks until it
> is set.

**Parameters:**

| Name | Type | Required | Default | Description |
|------|------|----------|---------|-------------|
| `bt_max_download` | `int` | No | — | NAS-wide BitTorrent download limit, KB/s; 0 = unlimited. Manager only. |
| `bt_max_upload` | `int` | No | — | As above. |
| `emule_max_download` | `int` | No | — | As above. |
| `emule_max_upload` | `int` | No | — | As above. |
| `default_destination` | `str` | No | — | Default destination share, used when `create_download` gets none. Applied **per user**. |

**Response shape:** the fields sent, under `Set download config — N field(s) updated`. For a non-manager, the title adds `(your own default destination, not the NAS-wide one)`.

**Design notes:**
- Only the supplied fields are sent. Passing none fails with `invalid_parameter`, listing the valid fields.
- A non-manager passing any rate field is refused with `permission_denied`, naming the fields, and **nothing** is sent, not even an accompanying `default_destination`. That's because DSM would answer success and ignore the rates.

---

### 12. `set_schedule`

**Permission tier:** WRITE

**Annotations:** `destructiveHint=true`, `idempotentHint=true`: it replaces the NAS-wide schedule, and repeating a call changes nothing more.

**DSM API:** `SYNO.DownloadStation2.Settings.Scheduler` / `set` v1 (plan and rates); `SYNO.DownloadStation.Schedule` / `setconfig` v1 (flags)

**Tool description (LLM-facing):**
> Set the Download Station schedule; get_schedule shows the current one. All parameters are
> optional: only what you pass is updated. `enabled` / `emule_enabled` toggle whether the schedule
> applies (DSM lets any Download Station user toggle this NAS-wide flag). `schedule_plan` is in the
> NAS's local hours, and `download_rate` / `upload_rate` are the limited speed. The plan and rates
> need a Download Station manager/admin account; if that write is refused, nothing is applied.

**Parameters:**

| Name | Type | Required | Default | Description |
|------|------|----------|---------|-------------|
| `enabled` | `bool` | No | — | Whether the schedule applies (NAS-wide; any DS user). |
| `emule_enabled` | `bool` | No | — | The same, for the eMule schedule. |
| `schedule_plan` | `str` | No | — | 168 characters of `0`/`1`/`2`, Sunday 00:00 first, 24 per day, NAS local time. |
| `download_rate` | `int` | No | — | Limited download speed (hours marked `2`), KB/s (0 = unlimited). |
| `upload_rate` | `int` | No | — | Limited upload speed (hours marked `2`), KB/s (0 = unlimited). |

**Response shape:** the fields applied, under `Set download schedule — N field(s) updated`. The plan is shown as `<set to 168-char plan>`, not echoed back.

**Design notes:**
- **Validation before anything is sent:** the plan must be exactly 168 characters of `0`/`1`/`2` (DS2 stores anything), and the rates must be ≥ 0. Passing no fields fails with `invalid_parameter`.
- **Order:** the DS2 write (plan and rates) goes **first**. If it's refused (105 for non-admins, or DS2 absent), nothing was applied and the error says so. If the later v1 flag write fails, the error names what was applied and what wasn't.
- DS2 is a JSON-format API, so the plan is sent JSON-encoded.

---

## Resolved Design Decisions

### 1. Follow DSM, not the guide ✓

The DS Web API guide is wrong in several places: `force_complete` doesn't move partial data, v1 `total` isn't the queue size, and `Task.edit` doesn't exist at v1. Every behavior here was measured on vdsm (both versions) or on a real NAS. Test fixtures are real captures (`tests/modules/downloadstation/fixtures/`), and mocks route by DSM `api`/`method`/`version`.

### 2. Refuse what DSM would silently ignore ✓

A non-manager's rate writes, an invalid weekly plan, and a create with no destination and no default would all "succeed" at the API and do nothing, or something worse. The tools refuse them with a reason instead.

### 3. Say what can't be told apart ✓

Another user's task and a missing ID are indistinguishable at the API, so the message names both possibilities instead of guessing.

## Remaining Open Items

- **Phase 3:** BT search and RSS feeds (the APIs are already declared as optional).
- `edit_download` supports only `destination`. There's no per-file selection inside a torrent.
- **#142:** model-facing tool-call evals, record and replay.
