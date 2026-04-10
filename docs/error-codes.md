# mcp-synology Error Codes

This is the reference for every `code` value mcp-synology can return in a
structured error envelope. It is linked from the `help_url` field of those
envelopes. Each section's heading matches the `code` value exactly, so you
can jump directly from any error to the section that explains it.

If you arrived here from an error envelope, scroll to the section matching the
error's `code` field. If you're browsing, the sections below are grouped by
category.

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
- The account is disabled in DSM.
- The IP has been auto-blocked after too many failed attempts.

Fix:

1. Run `mcp-synology check -v`. The verbose output exercises the full auth chain
   and reports which step fails (lookup → request → response).
2. If 2FA is required, run `mcp-synology setup` and complete the OTP exchange.
   This stores the device token in the keyring so subsequent runs do not need
   the OTP code.
3. If the IP is auto-blocked, sign in to DSM directly and clear the entry at
   **Control Panel → Security → Account → Auto Block → Allow / Block List**.
4. Verify the account has access to FileStation: **Control Panel → User & Group
   → [account] → Edit → Applications**. The account must be allowed to use
   File Station.

`auth_failed` is **not** retryable. mcp-synology will not auto-retry on this
code — fix the underlying credential or 2FA state before re-running.

## permission_denied

**The account authenticated but DSM rejected the operation (DSM error 105).**

This is *not* a session issue. mcp-synology specifically does **not** re-authenticate
on code 105 — that would mask the real problem and risk lockout.

Common causes:

- The DSM user is not in the `administrators` group, and the operation requires
  admin (e.g., `get_resource_usage`, `get_system_info`).
- The shared folder ACL excludes this user.
- The user has read-only access to a folder being written to.
- The user is restricted to certain shares and the request targets a different one.

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

## api_not_found

**DSM does not expose the API the tool needs.**

Common causes:

- DSM version is too old (mcp-synology requires DSM 7.0 or later).
- The DSM package providing the API is not installed (e.g., File Station).
- The API exists but the package is stopped.

Fix:

1. Check the DSM version: **Control Panel → Info Center → General → DSM Version**.
2. Verify File Station is installed and running: **Package Center → Installed**.
   If missing, install it from **Package Center**.
3. Run `mcp-synology check -v`. Debug logging dumps the cached API list returned
   by `SYNO.API.Info`, which shows exactly which APIs the NAS reports.
4. If the API should exist but is not listed, restart the relevant package via
   **Package Center → [package] → Action → Stop / Run**, or reboot the NAS.

---

_Paths and files_

## not_found

**The path could not be resolved on the NAS, or a local file in upload/download
does not exist.**

Common causes:

- Typo or wrong case in the share name (case sensitivity depends on the volume's filesystem).
- Share name omitted: paths must start with a real share, e.g. `/volume1` is wrong, `/photos` is right.
- The user lacks list permission on a parent directory, so DSM reports "not
  found" rather than "denied" — DSM does this deliberately to avoid leaking
  directory existence.
- For local-side errors: the local file (upload) or destination folder
  (download) does not exist or is given as a relative path that resolves
  somewhere unexpected.

Fix:

1. Call `list_shares` to see exact share names. They are case-sensitive on
   Btrfs and case-insensitive on ext4 — when in doubt, match the displayed case.
2. Call `list_files` on the parent directory to confirm the target exists with
   the expected name and case.
3. For uncertain locations, call `search_files` with a substring of the name.
4. If the share lists fine but a subdirectory says "not found", check
   `permission_denied` — the user may lack list permission on the parent.
5. For local paths in upload/download: always pass absolute paths. Relative
   paths resolve against the mcp-synology process working directory, which is
   not the same as your shell's working directory when launched by Claude
   Desktop or another MCP client.

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
- The `new_name` argument to `rename` contains `/` (path separators are not
  allowed in rename — use copy/move to relocate a file).
- A path is empty, all whitespace, or has no share component.
- A search keyword normalizes to empty after wildcard processing.

Fix:

- Strip or substitute the forbidden character set above. DSM enforces the
  strict union of Windows and POSIX filename rules — even if your client OS
  allows a character, DSM may not.
- For `rename`: pass only the new base name, not a path. To move a file across
  directories, use the copy or move tool.
- For paths: ensure the first component is a real share name from `list_shares`.
- For search: provide a keyword with at least one non-wildcard character.

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

Fix:

1. Check NAS storage: **Storage Manager → Storage** in DSM. Free space, expand
   the volume, or move data off it.
2. Check user quota: **Control Panel → User & Group → [account] → Edit →
   Quota**. Quotas can fill long before the underlying volume does, and the
   error looks the same.
3. For local downloads: `df -h` on the destination filesystem.
4. After freeing space, the operation is safe to retry — `disk_full` is marked
   `retryable=true` in the error envelope.

## timeout

**A long-running DSM background task did not complete within the timeout window.**

Affects search, copy, move, delete, and `get_dir_size` — these all run as
asynchronous DSM tasks that mcp-synology polls.

Common causes:

- The operation is genuinely large (deleting a directory tree with millions of
  files; searching a non-indexed share).
- The DSM search service is overloaded. Search on shares with
  `has_not_index_share=true` is unreliable under load.
- The NAS is starved for CPU or I/O by another workload (Plex transcoding,
  SMR drive write amplification, RAID resync, snapshot replication).
- Orphaned background tasks from prior aborted runs are competing for resources.

Fix:

1. Check NAS health first: call `get_resource_usage` to confirm CPU and RAM
   are not pinned. If they are, fix that before retrying.
2. For search: narrow the path to a single share rather than the root, use a
   more specific keyword, and avoid rapid-fire repeats. The search service can
   exhaust itself and refuse new tasks.
3. For copy, move, and delete: split the operation into smaller batches.
4. To clear suspected orphaned tasks: stop and start the File Station package
   from **Package Center**. mcp-synology cleans up its own tasks via
   `try/finally`, but tasks from earlier crashed runs may persist.
5. `timeout` is marked `retryable=true`, but only retry after addressing the
   root cause. Blind retry on an overloaded NAS makes the situation worse.

## unavailable

**mcp-synology called DSM successfully, but DSM returned an empty payload
where data was expected.**

Affects `get_system_info`, `get_resource_usage`, and similar metric tools.
The API responded `success=true` but the data block was missing or empty.

Common causes:

- DSM is in a half-initialized state immediately after boot or upgrade.
  System monitor services start asynchronously and may not be ready yet.
- The user lacks permission to read the requested fields. DSM strips fields
  silently in some cases rather than returning a permission error.
- The DSM package providing the metric is stopped.

Fix:

1. Wait one to two minutes after a NAS reboot before calling system tools.
2. Verify the DSM user is in the `administrators` group for system-level tools.
   See `permission_denied` for the rationale.
3. Open **Resource Monitor** in DSM directly. If the panel is empty or
   incomplete there too, the issue is on the NAS side, not in mcp-synology.
4. Run `mcp-synology check -v` and reproduce the call — the debug log shows
   the raw DSM response, which usually tells you which field is missing.

---

_Catch-all_

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
