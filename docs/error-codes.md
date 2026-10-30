# mcp-synology Error Codes

This is the reference for every `code` value mcp-synology can return in a
structured error envelope. It is linked from the `help_url` field of those
envelopes. Each section's heading matches the `code` value exactly, so you
can jump directly from any error to the section that explains it.

If you arrived here from an error envelope, scroll to the section matching the
error's `code` field. If you're browsing, the sections below are grouped by
category.

For behavior that looks wrong but returns no error, see
[troubleshooting.md](troubleshooting.md), organized by module.

> **Convention:** all section headings use the literal error code (e.g.
> `auth_failed`). The unit test in `tests/core/test_help_urls.py` enforces a
> 1:1 mapping between codes registered in `core/errors.HELP_URLS` and headings
> here, so renaming a section will fail CI.

---

_Authentication and access_

## auth_failed

**DSM rejected the credentials, requires 2FA, or the account is locked.**

Common causes:

- Wrong username or password in the credential store.
- 2FA is enforced on the account but has not been bootstrapped via `mcp-synology setup`.
- 2FA is set up, but DSM rejected the stored device token (the message says so),
  for example because the trusted device was removed in **Personal → Security →
  Sign-in Activity**.
- The account is disabled in DSM.
- The IP has been auto-blocked after too many failed attempts.

Fix:

1. Run `mcp-synology check -v`. The verbose output exercises the full auth chain
   and reports which step fails (lookup → request → response).
2. If 2FA is required, or DSM rejected the stored device token, run
   `mcp-synology setup` and complete the OTP exchange. This stores a new device
   token in the keyring so subsequent runs do not need the OTP code.
3. If the IP is auto-blocked, sign in to DSM directly and clear the entry at
   **Control Panel → Security → Account → Auto Block → Allow / Block List**.
4. Verify the account has access to FileStation: **Control Panel → User & Group
   → [account] → Edit → Applications**. The account must be allowed to use
   File Station.

`auth_failed` is **not** retryable. mcp-synology will not auto-retry on this
code — fix the underlying credential or 2FA state before re-running.

## permission_denied

**The account authenticated but DSM rejected the operation (DSM error 105, or File
Station's 407, "Operation not permitted").**

This is *not* a session issue. mcp-synology specifically does **not** re-authenticate
on code 105 or 407 — that would mask the real problem and risk lockout.

File Station answers 407 for a path this account may not use. On a real NAS (DSM 7.1.1)
a non-admin service account got it for `list_files /home`, where `list_shares` showed
`home` on volume `var` with 0 B free; why DSM refused that account its home isn't known.

Common causes:

- The DSM user is not in the `administrators` group, and the operation requires
  admin (e.g., `get_resource_usage`). `get_system_info` never refuses a non-admin: it
  shows the fields every account gets and says which need an administrator.
- The shared folder ACL excludes this user.
- The user has read-only access to a folder being written to.
- The user is restricted to certain shares and the request targets a different one.
- Download Station: the account has no Download Station application privilege,
  or the operation needs a Download Station manager/admin (`set_download_config`,
  and the weekly plan and limited-speed rates in `get_schedule` / `set_schedule`).
- Storage: Storage Manager answers only a DSM administrator, so every storage tool
  refuses any other account (DSM's 105; `USB list` answers 404 instead on DSM 7.2.2 and
  later). Use an administrator connection for the `storage` module.
  For `set_download_config` mcp-synology refuses on its own, because DSM answers
  "success" to non-managers while ignoring the change.
- Synology Photos (DSM codes 160, 801, 803): **160** means the account has no Synology
  Photos application privilege; **801** that Shared Space is off or the account has no
  access to it (DSM uses one code for both); **803** that the account has Shared Space
  access but no permission on that folder.

Fix:

1. Identify which DSM account mcp-synology is using: check your config file or
   run `mcp-synology check -v`.
2. For shared-folder operations: **Control Panel → Shared Folder → [folder] →
   Edit → Permissions**. Confirm the account has the access level the tool needs
   (read-only is not enough for upload, copy, move, delete, rename, create_folder).
3. For system-level tools: add the account to `administrators` via **Control
   Panel → User & Group → [account] → Edit → User Groups**. If you cannot grant
   admin, those tools are not usable for this account — that is a deliberate
   DSM restriction, not an mcp-synology bug.
4. If you are using a dedicated service account (recommended): each share you
   want to access needs explicit permission for that account. Service accounts
   start with no access.
5. For Download Station: grant the application privilege via **Control Panel →
   Application Privileges → Download Station**. Manager-only operations
   additionally need an admin account.
6. For Synology Photos: 160 is fixed in **Control Panel → Application Privileges →
   Synology Photos**; 801 in **Synology Photos → Settings → Shared Space** (enable it,
   then grant the account access); 803 by a Shared Space manager, who grants folder
   permissions in Synology Photos.

**Local paths (`upload_file`, `download_file`, `create_download`'s `torrent_file_path`).**
The same code, with no DSM error number, means mcp-synology refused a local path outside
the allowed folders before contacting the NAS. The message lists them. Use a path inside
one of them, or list the folder in `allowed_local_paths`: at the top level of the config
for every module, or in the module's settings (`"/"` allows everything on macOS and
Linux). With no top-level list, the default is your Downloads folder, and a module's own
list replaces it (include `~/Downloads` if you still want it); with a top-level list, a
module's list adds to it. File Station's `default_download_dir` is always allowed. Paths are compared after resolving `~`,
`..` and symlinks, so a symlink that points outside is refused too.

## api_not_found

**DSM does not expose the API the tool needs.**

Common causes:

- DSM version is too old (mcp-synology requires DSM 7.0 or later).
- The DSM package providing the API is not installed (e.g., File Station).
- The API exists but the package is stopped.
- Storage: `SYNO.Storage.CGI.Storage` (Storage Manager) is missing, or `list_snapshots`
  is asked on a NAS without the **Snapshot Replication** package. A storage section whose
  API is missing doesn't fail its tool: it's listed under "Not shown" instead.

Fix:

1. Check the DSM version: **Control Panel → Info Center → General → DSM Version**.
2. Verify File Station is installed and running: **Package Center → Installed**.
   If missing, install it from **Package Center**.
3. Run `mcp-synology check -v`. Debug logging dumps the cached API list returned
   by `SYNO.API.Info`, which shows exactly which APIs the NAS reports.
4. If the API should exist but is not listed, restart the relevant package via
   **Package Center → [package] → Action → Stop / Run**, or reboot the NAS.

## not_supported

**The NAS's version of the package doesn't have the action asked for.**

DSM answers code 103 ("method does not exist") when an API lacks a method, and some
features exist only in newer package versions. mcp-synology reports both as
`not_supported`, naming the version where it can, instead of a bare DSM code. For
features DSM silently ignores on older versions (Synology Photos 1.3.4 returns every
item for a favorites filter), mcp-synology refuses before calling DSM.

Examples: favorites and concepts need Synology Photos 1.9.1 or later (DSM 7.2).

Storage: DSM answers 121 when the NAS doesn't have a feature (deduplication on a
DS1618+, for example). In an optional section that's a "not available on this NAS" note,
not an error; only a required call (`load_info`, `Core.Share list`, the snapshot list)
fails with `not_supported`. `list_snapshots` also reports `not_supported` when Snapshot
Replication doesn't offer the version-2 snapshot list (version 1 lists no snapshots).

Fix:

1. Update the package (and DSM if the newer package needs it) in **Package Center**.
2. Otherwise, do it another way: the message says what this version lacks.

---

_Paths and files_

## not_found

**The path could not be resolved on the NAS, a local file in upload/download
does not exist, or a Download Station task ID matched no task.**

Common causes:

- Typo in the share name. Case doesn't matter to the File Station tools: they send a share name as `list_shares` lists it (`/Home` goes to DSM as `/home`). Only if DSM won't list shares does the name go as given, and then the user home service takes only `/home` in lowercase.
- Share name omitted: paths must start with a real share, e.g. `/volume1` is wrong, `/photos` is right.
- The user lacks list permission on a parent directory, so DSM reports "not
  found" rather than "denied" — DSM does this deliberately to avoid leaking
  directory existence.
- For local-side errors: the local file (upload) or destination folder
  (download) does not exist or is given as a relative path that resolves
  somewhere unexpected.
- For Download Station tasks: the task ID doesn't exist, or the task belongs to
  another user. DSM answers both the same way, and an account that isn't a
  Download Station manager sees only its own tasks.
- For storage tools: a disk, volume or shared folder that isn't on the NAS. The error's
  `valid` field lists the ones that are (`get_disk` takes `sda`, `/dev/sda` or `Drive 1`;
  `get_volume` takes `/volume1` or `volume_1`; `list_snapshots` takes a shared folder's
  name, in any case).

Fix:

1. Call `list_shares` to see the share names. The File Station tools send a
   share name as `list_shares` lists it, in any case you give (`/BACKUPS` goes
   to DSM as `/Backups`, `/Home` as `/home`). If DSM won't list shares, the
   name goes as given: use `/home` in lowercase, since DSM takes the user home
   service only that way.
2. Call `list_files` on the parent directory to confirm the target exists with
   the expected name and case.
3. For uncertain locations, call `search_files` with a substring of the name.
4. If the share lists fine but a subdirectory says "not found", check
   `permission_denied` — the user may lack list permission on the parent.
5. For local paths in upload/download: always pass absolute paths. Relative
   paths resolve against the mcp-synology process working directory, which is
   not the same as your shell's working directory when launched by Claude
   Desktop or another MCP client.
6. For Download Station task IDs: call `list_downloads` to see the IDs this
   account can act on.

## already_exists

**A file with the target name already exists at the destination.**

This fires for upload, download, rename, copy, and move when the destination
is occupied and the caller did not opt in to overwriting.

Fix:

- For `upload_file`, `download_file`, copy, and move: pass `overwrite=true` to
  replace the existing file. Only do this if you actually want the existing
  data gone — overwrite is **not** transactional, so a failure mid-write can
  leave the destination in a corrupted state.
- For `rename`: choose a different target name, or `delete_files` the existing
  target first.
- If you need atomicity: write to a temporary name and rename on success
  rather than relying on `overwrite=true`.

## invalid_parameter

**The input contains a value mcp-synology or DSM rejects before sending the
operation.**

Common causes:

- A filename contains a forbidden character: `/ \ : * ? " < > |`.
- The `new_name` argument to `rename`, or `upload_file`'s `filename`, contains `/`
  or a NUL, is blank, or is `.` or `..` (padded with spaces too); the message says which (path separators
  are not allowed in a name — use copy/move to relocate a file).
- A bad configured `default_upload_dir` (a `.` or `..` part here, an unknown share
  under `not_found`) is named as `param: default_upload_dir`: fix the setting or pass
  `dest_folder`.
- A path is empty, all whitespace, or has no share component.
- A File Station path, a `restore_from_recycle_bin` item, or a Download Station
  destination has a `.` or `..` part (`/video/../photo`, `video/./x`).
- `upload_file` or `download_file` was called without `dest_folder`, and no
  default is configured (`default_upload_dir` or `default_download_dir` in the
  `filestation` module settings). The error's `param` is `dest_folder`.
- The request was too large for DSM to accept (HTTP 414 "Request-URI Too Large").
  Long magnet links and multi-URI lists are sent in the request body to avoid
  this; if it still happens, split the request.
- A Download Station `schedule_plan` is not exactly 168 characters of `0`, `1`
  and `2`. DSM stores whatever it is given, so mcp-synology validates it first.
- DSM error 120: a JSON-format DSM API (e.g. `SYNO.DownloadStation2.*`) rejected
  a parameter's type.
- `pause_download` with DSM error 101: none of the tasks could be paused. DSM
  gives the same answer for a missing ID, another user's task, and the
  caller's own task that is finished or already paused. Check each task's
  status with `list_downloads`.
- `file_task` was given something that isn't a task handle (param `handle`).
  A handle is `delete:`, `copy:`, `move:` or `restore:` followed by DSM's task
  ID, e.g. `delete:FileStation_179219158016597225`. It's checked before any
  DSM call, so a malformed handle never reaches the NAS.
- `file_task` with `action="stop"` and no handle (or an empty one). Stopping
  needs a handle; nothing is listed or stopped.

Fix:

- Strip or substitute the forbidden character set above. DSM enforces the
  strict union of Windows and POSIX filename rules — even if your client OS
  allows a character, DSM may not.
- For `rename`: pass only the new base name, not a path. To move a file across
  directories, use the copy or move tool.
- For paths: ensure the first component is a real share name from `list_shares`,
  and write the path out in full from the shared folder, with no `.` or `..` parts.
- For a missing destination: pass `dest_folder` (a NAS folder for
  `upload_file`; for `download_file`, a local folder in
  `allowed_local_paths`), or set the matching default in the `filestation`
  module settings.
- For `file_task`: pass the handle exactly as the delete, copy, move or
  restore reply gave it (surrounding backticks or quotes are fine), or call
  `file_task` without a handle to list the running operations and their
  handles.

## filesystem_error

**A local OSError occurred reading or writing a file (uploads and downloads only).**

This is *not* a DSM error — it's the local operating system telling
mcp-synology that the file operation failed.

Common causes:

- Local file lacks read permission (upload) or destination folder lacks write
  permission (download).
- The destination folder does not exist.
- The local disk is read-only or the inode is locked.
- The path contains characters the local filesystem rejects.

Fix:

1. Check local permissions: `ls -la <path>`. The relevant user is whoever
   launched the mcp-synology process — for Claude Desktop, that is the user
   running Claude Desktop, which is **not** necessarily root or a privileged user.
2. For downloads: ensure the destination folder exists and is writable. Use
   absolute paths.
3. The error message includes the OSError details (ENOENT, EACCES, EROFS,
   etc.) — read those for the precise cause before guessing.

---

_Storage and resources_

## disk_full

**No space available on the destination volume, or the user's quota is exhausted.**

Maps to DSM error 416 (volume full) and 415 (quota exceeded). For
`download_file`, this also covers local ENOSPC.

A copy or move that runs out of space part-way leaves what it already wrote at the
destination, and DSM doesn't report how much: a copy stopped by a 20 GB quota left
18 GiB there while DSM's task status said nothing had been processed (DSM 7.1.1). So
the error names the destination ("Part of it may already be in …"); check it before
trying again, and delete the partial copy if you don't want it.

Fix:

1. Check NAS storage: **Storage Manager → Storage** in DSM. Free space, expand
   the volume, or move data off it.
2. Check user quota: **Control Panel → User & Group → [account] → Edit →
   Quota**. Quotas can fill long before the underlying volume does, and the
   error looks the same.
3. For local downloads: `df -h` on the destination filesystem.
4. After freeing space, the operation can be retried (`disk_full` is marked
   `retryable=true`), but first delete what a failed copy or move left at the
   destination, or retry with `overwrite=true`: with `overwrite=false` the retry skips
   files already there, a half-written one included.

## timeout

**A long-running DSM background task did not complete within the timeout window,
an HTTP request to DSM timed out, or the NAS couldn't be reached in time.**

Affects search and `get_dir_size`, which run as asynchronous DSM tasks that
mcp-synology polls and stops at their time limit, and a status or stop request
for a `file_task` or `photo_task` that DSM doesn't answer.
`create_download` can also time out on the HTTP request itself; DSM may still
have created the task, so check `list_downloads` before retrying (mcp-synology
never retries it automatically, to avoid duplicate downloads).

### The client's own timeout

The MCP client can give up on a call before mcp-synology does. Claude Code
aborts a tool call that sends no result and no progress notification for an
idle window: 30 minutes by default for a stdio server (5 minutes for HTTP
servers). That abort comes from the client, not from DSM or mcp-synology. To
raise the window, set `CLAUDE_CODE_MCP_TOOL_IDLE_TIMEOUT` (milliseconds; `0`
turns the check off), or add a `timeout` field (milliseconds) to this server's
MCP config entry, which also sets a hard limit per call; see
[Claude Code's MCP docs](https://code.claude.com/docs/en/mcp). Delete, copy,
move and restore send progress notifications while they wait, so they stay
under the idle limit. `get_dir_size` and `search_files` don't send progress
yet (#252), so a long one can hit it: raise the client's limit, or narrow the
path.

### Not an error: a handed-off operation

A delete, copy, move or restore that's still running when the call's wait
runs out (`delete_timeout` or `copy_move_timeout`, default 30 s, capped at
600 s) is **not** a timeout (#245). The NAS keeps working on it, and the tool
returns a normal result: "Still deleting on the NAS: … This operation keeps
running after this reply", with the progress and a handle such as
`delete:FileStation_179219158016597225`. In a multi-path call, the paths after
it are listed as "Not started". Follow up with `file_task`: `action="status"`
shows progress or the final result, `action="stop"` stops it (it may then be
partly done), and no handle lists the running operations. Only a canceled
call (the user, or a client's own timeout) or a DSM error stops the task.

The same goes for a NAS that stops answering status requests during the wait
(a network timeout or dropped connection), that answers one with an HTTP error
status (a reverse proxy's `502` or `504`, #282), or whose answer can't be read
(for example a login page from a captive portal or reverse proxy): the task
isn't stopped, and the reply starts "The NAS stopped answering while deleting
…", "The NAS, or a proxy in front of it, answered a status request with an
error while deleting … (HTTP 504 …)" or "The NAS's status reply couldn't be
read while deleting … (not a DSM reply; a proxy or captive portal may be
answering)", then "It's probably still running there; don't start it again.",
with the handle.
Check on it with `file_task` rather than starting it again.

`file_task` answers a handle DSM no longer knows with a normal reply, not an
error: the final result if this server remembers it, otherwise "No running
task with this handle. It finished, was stopped, or was started by a
different DSM user. List the path to see what's left." DSM forgets a finished
task after its final status has been read once, and never shows one user's
tasks to another.

Common causes:

- The operation is genuinely large (deleting a directory tree with millions of
  files; searching a non-indexed share).
- The NAS is starved for CPU or I/O by another workload (Plex transcoding,
  SMR drive write amplification, RAID resync, snapshot replication).
- Orphaned background tasks from prior aborted runs are competing for resources.
- The NAS can't be reached at all: it's off or asleep, the network or VPN is
  down, or the configured host or port is wrong. The message starts "Can't
  reach the NAS at …" and names the URL and the timeout.
- The NAS is reachable but too busy to answer the first connection (the API
  list and the login) in time. The message starts "The NAS at … accepted the
  connection but didn't answer within …" (#254). Retry shortly; if it's often
  this slow, raise `connection.timeout`. `mcp-synology check` says the same
  ("… accepted the connection but didn't answer in time").
- No connection was free in time for the API list or the login: "No
  connection to the NAS at … was free within N s (PoolTimeout…), so the
  request wasn't sent." Nothing reached the NAS, so it's safe to retry, and a
  retry usually clears it.
- DSM stopped answering partway through. While it deletes or copies very large
  files (multi-TB Time Machine bundles, VM images), DSM can stall for longer
  than the connection timeout (`connection.timeout`, default 30 s).
  `get_dir_size` then reports "DSM didn't answer a status request on path: …"
  and asks DSM to stop the task; the message says whether DSM confirmed the
  stop. (A copy, move, delete or restore is handed off instead; see above.)
  Other tools name the DSM call, e.g. "DSM didn't answer
  SYNO.FileStation.List/list_share in time" (an `unavailable` error, "The
  connection to DSM failed during …", if the connection dropped).
- `file_task` didn't get an answer to its status or stop request for a
  handle. For the status read, the message says nothing was stopped; for the
  stop, that it may or may not have taken effect. Stop requests wait up to
  90 s, longer than other requests: DSM 7.1 sometimes answers a Delete `stop`
  only after about 60 s, although the stop takes effect at once.
- `photo_task` didn't get an answer to its status or stop request: "Photo task
  failed for `photos:<id>`: DSM didn't answer the status request (ReadTimeout)."
  After a stop that went through, the message ends "The stop was requested; its
  status couldn't be read."; for the stop request itself, "The stop may or may
  not have taken effect.", or "Nothing was stopped." when the connection
  couldn't be opened ("DSM couldn't be reached for the stop request").

Fix:

**Can't reach the NAS?** If the message starts "Can't reach the NAS at …",
mcp-synology never got an answer from DSM, so the NAS-side checks below
can't help. (If it starts "The NAS at … accepted the connection but …"
instead, the NAS is reachable but busy: retry shortly, or raise
`connection.timeout`.) Check that the NAS is powered on and reachable from
this machine (LAN or VPN), and that `connection` `host`, `port` and `https`
in the config are right, then run `mcp-synology check -v --config <file>`,
which tests the connection and login step by step.

Otherwise:

1. Check NAS health first: call `get_resource_usage` to confirm CPU and RAM
   are not pinned. If they are, fix that before retrying.
2. For search: narrow the path to a single share rather than the root, use a
   more specific keyword. (Before 0.6.0, mcp-synology sent search task IDs
   unquoted, so DSM lost most tasks and never cleaned them up, which looked
   like an exhausted search service; see #121.)
3. For copy, move, delete and restore: a long operation isn't an error, and
   neither is a NAS that stops answering during the wait; both are handed off
   (see above). Call `file_task` with the handle, or with no handle, to see
   whether it's still running, and don't start it again. For a stalling DSM,
   a larger `connection.timeout` helps. For
   search and `get_dir_size`, the time limit is `async_timeout` (default
   120 s, or `search_timeout` and `dir_size_timeout`), counting the whole
   time, including slow status requests.
4. For a `file_task` or `photo_task` request DSM didn't answer: call it with
   the handle again to see whether the task is still running, or list the path.
5. To clear suspected orphaned tasks: stop and start the File Station package
   from **Package Center**. mcp-synology cleans up its own tasks via
   `try/finally`, but tasks from earlier crashed runs may persist. A handed-off
   delete, copy, move or restore is left running on purpose; stop it with
   `file_task` instead.
6. `timeout` is marked `retryable=true`, but only retry after addressing the
   root cause. Blind retry on an overloaded NAS makes the situation worse.
   The exception is a `create_download` timeout, which is marked
   `retryable=false` because DSM may already have created the task.

## unavailable

**mcp-synology couldn't connect to the NAS, or it called DSM successfully but
DSM returned an empty payload where data was expected, or DSM answered with an
HTTP error status, or the answer wasn't a DSM reply at all.**

The connection case (the message starts "Can't reach the NAS at …") means no
DSM answer arrived at all: the connection was refused or the host couldn't be
resolved or routed to. See **Can't reach the NAS?** under Fix. If the
message starts "The NAS at … accepted the connection but dropped it" instead,
the NAS answered and then closed the connection: it's reachable but busy or
restarting, so retry shortly (#254).

The HTTP-status case (e.g. `502 Bad Gateway` from DSM's web server while a
package such as Download Station is still starting) is reported with the
status in the message, along with the DSM API and method (for example
`SYNO.FileStation.List/list_share: DSM answered HTTP 500.`); the request URL is
never included, since it carries the password or session ID. A 5xx on any
other call is `retryable=true`, and a 4xx is `retryable=false`. As with
`timeout`, a `create_download` that fails this way may still have been applied,
so it is marked `retryable=false`; check `list_downloads` before retrying.

The not-a-DSM-reply case (#282): the answer wasn't a JSON object, typically a
reverse proxy's or captive portal's login page in front of the NAS. The message
names the API and method and the Content-Type (for example
`SYNO.FileStation.List/list_share: the answer wasn't a DSM reply (Content-Type
text/html).`), never the page itself. It's `retryable=true`, except for a
`create_download`, which may still have been applied. Check what's in front of
the NAS; `mcp-synology check` reports it as "didn't return a DSM API response".
Downloads (`download_file`, `download_photos`) check too: DSM never sends a
file as `text/html` or `application/xhtml+xml`, so such a page isn't saved, and
nothing is written; neither is JSON that isn't DSM's error envelope (#292).
`file_task` reports either case as "Check task failed for `<handle>`: …", or for
a stop "Stop task failed for `<handle>`: … Nothing was stopped." (the status
read failed) or "… The stop may or may not have taken effect." (the stop
request itself failed), with `retryable` set as above: a 4xx isn't retryable.
`photo_task` reports either case as "Photo task failed for `photos:<id>`: …",
ending "The stop was requested; its status couldn't be read." after a stop that
went through, or "The stop may or may not have taken effect." when the stop
request itself failed. A Photos copy, move or delete that's waiting hands the
task off instead, saying what the NAS answered.

Synology Photos also reports two setup problems this way: DSM code **602** (the
account has no personal space because the user home service is off; an administrator
enables it in **Control Panel → User & Group → Advanced → User Home**) and **901**
(People, face recognition, is turned off in Synology Photos' settings).

The empty-payload case affects `get_system_info`, `get_resource_usage`, and
similar metric tools. The API responded `success=true` but the data block was missing or empty.

Common causes:

- DSM is in a half-initialized state immediately after boot or upgrade.
  System monitor services start asynchronously and may not be ready yet.
- The user lacks permission to read the requested fields. DSM strips fields
  silently in some cases rather than returning a permission error.
- The DSM package providing the metric is stopped.

Fix:

**Can't reach the NAS?** If the message starts "Can't reach the NAS at …",
mcp-synology never got an answer from DSM, so the NAS-side checks below
can't help. (If it starts "The NAS at … accepted the connection but …"
instead, the NAS is reachable but busy: retry shortly, or raise
`connection.timeout`.) Check that the NAS is powered on and reachable from
this machine (LAN or VPN), and that `connection` `host`, `port` and `https`
in the config are right, then run `mcp-synology check -v --config <file>`,
which tests the connection and login step by step.

Otherwise:

1. Wait one to two minutes after a NAS reboot before calling system tools.
2. Verify the DSM user is in the `administrators` group for system-level tools.
   See `permission_denied` for the rationale.
3. Open **Resource Monitor** in DSM directly. If the panel is empty or
   incomplete there too, the issue is on the NAS side, not in mcp-synology.
4. Run `mcp-synology check -v` and reproduce the call — the debug log shows
   the raw DSM response, which usually tells you which field is missing.

---

_Catch-all_

## filestation_error

**A File Station API error that mcp-synology recognized as File-Station-scoped
but did not map to a more specific code above.**

This is the fall-through for `SYNO.FileStation.*` calls whose DSM error code
isn't in the specific-handler list (400–421 range is mostly covered; codes
like 402 "System too busy" and 900 "Unexpected server error" land here). The
envelope includes the DSM numeric code in the message text, so you can look
it up directly.

Fix:

1. Read the numeric DSM code from the message (e.g., "DSM error 402"). This
   is the authoritative identifier — the `filestation_error` label is just
   the bucket.
2. Cross-reference against the
   [Synology File Station API documentation](https://global.download.synology.com/download/Document/Software/DeveloperGuide/Package/FileStation/All/enu/Synology_File_Station_API_Guide.pdf)
   for that specific code.
3. **Code 402 ("System too busy"):** wait and retry. This is genuinely
   transient and often resolves on its own within a minute.
4. **Code 900 ("Unexpected server error"):** check **Log Center** in DSM for
   matching events. This usually indicates an internal DSM fault and may
   require a package restart or reboot.
5. **Code 600 ("Search folder access denied") from `search_files`:** before
   0.10.0, a search with no pattern, extension, size or date got this from
   DSM (with the default `filetype=all`), whatever the folder's permissions.
   It now sends `pattern="*"` in that case and lists everything (#306). If it
   still appears, check that the account can read the folder.
6. If you see a specific code repeatedly, [open an issue](https://github.com/cmeans/mcp-synology/issues/new)
   with the code and operation so we can add it to `core/errors.py`.

## dsm_error

**A DSM error that does not map to one of the specific codes above.**

Typically a background-task failure with a non-standard error structure, or a
DSM error code mcp-synology does not recognize yet.

Fix:

1. Run `mcp-synology check -v` and reproduce the failing operation. Debug logs
   include the raw DSM response with the exact error code.
2. Check **Log Center** in DSM for matching events around the same timestamp.
3. If you see a recurring DSM error code that mcp-synology does not handle
   specifically, [open an issue](https://github.com/cmeans/mcp-synology/issues/new)
   with the code, the operation that triggered it, and the debug log snippet.
   New codes are added to `core/errors.py` as we encounter them.
4. For background task failures (copy, move, delete): the task may have
   partially completed. List the destination to confirm state before retrying.
5. For Download Station's **403 "Destination doesn't exist"** on
   `create_download` (reported as `not_found` since #334, with
   `param: destination`), or error 100 on `edit_download`'s destination: Download
   Station doesn't create missing folders. Create the folder first with
   File Station's `create_folder` (its path form, `/video/TV/New show`; it needs
   the `filestation` module enabled with `permission: write`) or, without that,
   in DSM's own File Station, then retry with the destination share-relative
   (`video/TV/New show`) (#296). A `/volume1/...` real path gets the same 403:
   pass the path share-relative instead.
