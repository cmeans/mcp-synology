# Troubleshooting

Things that look wrong but aren't errors, or whose cause isn't obvious, by module. Each entry
says what you see, why, and what to do.

If you're holding an error, start with its `code` in [error-codes.md](error-codes.md), the page
every error's `help_url` links to. This page covers the rest: a task that sits in `waiting`, a
date that reads back old, a reply that hands off instead of finishing.

Each cause says where it comes from: the DSM version (and package version where it matters)
when it's known, and the issue, PR, spec or error-codes section with the evidence. A cause that
hasn't been reproduced says so. "vdsm" is the virtual DSM the test suite runs on.

To see what mcp-synology sent and what DSM answered, turn on debug logging:
`SYNOLOGY_LOG_LEVEL=debug` for the server, or `mcp-synology check -v`. Passwords and session IDs
are masked.

**Sections:** [Connection and sign-in](#connection-and-sign-in) ·
[File Station](#file-station) · [Download Station](#download-station) ·
[Synology Photos](#synology-photos) · [System](#system)

---

## Connection and sign-in

### "The NAS at … accepted the connection but didn't answer within …"

**Why:** the NAS is reachable but too busy to answer in time, often on the first call after it
wakes or while it's under load. "… accepted the connection but dropped it" is the same, for a
NAS that closed the connection (busy or restarting). This isn't "Can't reach the NAS at …",
which means no answer came at all (#254, #281).

**What to do:** retry shortly. If it's often this slow, raise `connection.timeout` (default
30 s). For "Can't reach", check the NAS is on and reachable, and the config's `host`, `port`
and `https`. See [timeout](error-codes.md#timeout) and
[unavailable](error-codes.md#unavailable).

### "No connection to the NAS at … was free within N s"

**Why:** every connection to the NAS was in use, so the request was never sent (#281).

**What to do:** retry; nothing reached the NAS, so it's safe, and a retry usually clears it.

### "…: the answer wasn't a DSM reply (Content-Type text/html)"

**Why:** something in front of the NAS answered instead of DSM: a reverse proxy's or captive
portal's login page, or a wrong port. Downloads check this too, so an HTML page is never saved
as your file (#282, #283, #292). See [unavailable](error-codes.md#unavailable).

**What to do:** check what sits between this machine and the NAS, and the configured port. Run
`mcp-synology check`, which reports it as "didn't return a DSM API response".

### Sign-in worked, then fails with `auth_failed` on a 2FA account

**Why:** DSM no longer accepts the stored device token, for example because the trusted device
was removed in **Personal → Security → Sign-in Activity**. The message says DSM rejected the
token (see [auth_failed](error-codes.md#auth_failed)).

**What to do:** run `mcp-synology setup` and enter a new one-time code.

---

## File Station

### A delete, copy, move or restore returns a handle instead of a result

**Why:** it's still running on the NAS. The call waits up to `delete_timeout` or
`copy_move_timeout` (default 30 s), then hands the operation off rather than stopping it (#245).
The same happens if the NAS stops answering status requests during the wait.

**What to do:** check on it with `file_task` and the handle (`file_task` with no handle lists the
running operations). Don't start it again. Stop it with `file_task` `action="stop"` only if you
want it canceled; it may then be partly done. See
[Not an error: a handed-off operation](error-codes.md#not-an-error-a-handed-off-operation).

### A handed-off delete shows no progress for minutes

**Why:** a delete counts finished items, not bytes, so one large file shows no progress until
it's gone, and that can take many minutes on a busy NAS (#245; the server's instructions to the
model say the same). The count can also jump near the end: in a 329-item delete on a real NAS,
each item took about 15 minutes for a while, and then 179 items cleared in 16 minutes (#302).

**What to do:** wait, and check with `file_task`. Don't stop and restart it because progress
looks slow.

### `file_task` says it's "no longer running on the NAS … DSM no longer has its result"

**Why:** DSM can forget a finished delete before any status check sees it finish (on a real NAS,
after a 36-hour delete, it was gone within 16 minutes of the last check), and it forgets a
finished copy or move after one status read, so one that finished between checks ends the same
way. Then `file_task` has no result left to give: the task finished or was stopped, and the reply
can't say which. #302 tracks checking the path automatically.

**What to do:** list the path. For a delete, DSM removes a folder's contents before the folder,
so a missing top-level folder means the delete finished; a folder that's still there means it
was stopped or only partly done. For a copy or move, list the destination (and, for a move, the
source).

### "DSM lost the search task … so this is not a 0-result answer"

**Why:** DSM answered the search's status request as it does for a task it no longer knows. The
search didn't find nothing; it has no result (#121).

**What to do:** retry the search, or browse the likely folder with `list_files`.

### Something deleted inside `#recycle` can't be restored

**Why:** deleting inside a share's recycle bin (`/<share>/#recycle/…`) is permanent: DSM keeps
no second copy (seen on DSM 7.1.1, #220).

**What to do:** nothing can bring it back from the NAS; restore from a backup or snapshot.

---

## Download Station

### A new HTTP task shows `waiting` for a few seconds

**Why:** that's normal. A new HTTP task sat in `waiting` about 5 s before `downloading`, and a
resumed one about 2 s, on DSM 7.1 and 7.2.2 (idle vdsm, probed 2026-10-19; #208, #243). Pause
and resume themselves took effect at once there: `list_downloads` and `get_download_info`
showed the new state on the next call. A BitTorrent or magnet task can stay in `waiting` much
longer, until it finds peers.

**What to do:** nothing; check again after a few seconds. A task still in `waiting` after
minutes can have another cause, such as the one below.

### A non-admin's tasks stay `waiting` forever

**Why:** Download Station never starts a non-admin's tasks until that user has set their own
default destination, which DSM keeps per user (#124, #133). `create_download` says so in its
reply: "Download Station won't start your tasks until your default destination is set".
Download Station's API (`is_manager: false`) and mcp-synology's replies call such an account a
non-manager. Adding an account to DSM's `administrators` group made it a manager, and we found
no separate manager setting (Download Station spec).

**What to do:** set it as that user with `set_download_config(default_destination=...)`, or in
Download Station's own settings, signed in as that user.

### `create_download` fails with DSM error 403 ("Destination doesn't exist")

**Why:** one of two things:
- the destination includes the volume (`/volume1/video/TV`). Download Station takes paths that
  start at the share (`video/TV`). mcp-synology drops a leading slash, so File Station's form
  (`/video/TV`) works, but it can't remove a volume name (#147);
- the folder doesn't exist yet. Download Station doesn't create missing folders (DSM 7.1 and
  7.2.2, #296).

**What to do:** pass the destination share-relative. For a new folder, create it first with
File Station's `create_folder` (its path form, `/video/TV/New show`; needs File Station write
permission) or, without that, in DSM's own File Station, then retry. `edit_download` reports
the same problem as DSM error 100. See [dsm_error](error-codes.md#dsm_error).

### A task ID shows "not found (no such task, or not yours)"

**Why:** a non-admin (a non-manager, see above) sees only their own tasks, and DSM answers
another user's task exactly as it answers a missing one (DSM 7.1 and 7.2.2, #136).

**What to do:** check the ID with `list_downloads`, signed in as the task's owner, or use an
administrator account.

### `pause_download` fails for the whole call with `invalid_parameter`

**Why:** when none of the IDs can be paused, DSM fails the whole call (error 101), whether the
IDs are missing, another user's, or the caller's own finished or already-paused tasks (DSM 7.1
and 7.2.2, #136, #146). With at least one pausable ID, the others are refused one by one instead.

**What to do:** check each task's status with `list_downloads`. See
[invalid_parameter](error-codes.md#invalid_parameter).

### `create_download` returns no task IDs

**Why:** a URL created with `username` / `password` goes through Download Station's older API,
which doesn't return IDs. So does any create when the newer API isn't available (Download Station
spec, `create_download`).

**What to do:** find the task with `list_downloads`.

### `create_download` timed out, or got an HTTP error

**Why:** DSM may have created the task anyway, so mcp-synology never retries it.

**What to do:** check `list_downloads` before retrying, or you may get a duplicate. See
[timeout](error-codes.md#timeout).

### `delete_download(force_complete=true)` didn't keep the partial file

**Why:** DSM discards an unfinished HTTP task's partial data either way, though its API guide
says `force_complete` keeps it (Download Station 3.9 and 4.1, #128). On a finished task,
`force_complete=true` is refused (error 405) and the task stays listed.

**What to do:** let the task finish if you need the file; delete finished tasks with
`force_complete=false`.

---

## Synology Photos

### A copy, move or delete returns a `photos:<id>` handle instead of a result

**Why:** it's still running on the NAS. `copy_photos`, `move_photos`, `delete_photos` and
`delete_photo_folders` wait up to the Photos module's `task_timeout` setting (default 30 s, up
to 600), then hand the task off rather than stopping it (#286). The same happens if the NAS stops
answering status checks during the wait. These are Photos tasks: follow them up with
`photo_task`, not `file_task`.

**What to do:** check on it with `photo_task` and the handle; `photo_task` with no handle lists
this user's copy, move and delete tasks. Don't start it again. Stop it with `photo_task`
`action="stop"` only if you want it canceled; it may then be partly done. To wait longer before
the hand-off, raise `task_timeout` under the `photos` module's `settings`.

### `photo_task` says "no task photos:<id>"

**Why:** the handle isn't in this user's task list. Photos keeps finished tasks until they're
cleared (for example with `photo_task` `action="clear"`), and tasks belong to the DSM user who
started them (Photos spec, background tasks and `photo_task`). So the task finished and was
cleared, another DSM user started it, or the handle is mistyped.

**What to do:** check the result directly: `list_photos` on the destination folder, or the
source for a move or delete.

### A new upload shows the file's modification time, `0×0`, or `(indexing)`, and search can't find it

**Why:** an upload returns its ID at once, but Photos indexes it afterwards: until then the taken
time is the file's modification time, the resolution is `0×0`, and search doesn't include it
(Photos spec, "Indexing lag").

**What to do:** wait; Photos indexes new items over seconds to minutes. Rows still being indexed
show `(indexing)` after the taken time.

### `download_photos` says a preview "isn't generated yet"

**Why:** the thumbnail is still being made (`converting`). DSM answers a request for it with an
HTML error page (verified on DSM 7.1 and 7.2.2; Photos spec, `download_photos`), so
mcp-synology checks first and skips that item.

**What to do:** try again shortly.

### A taken-time change reads back as the old date

**Why:** Photos writes edits into the original file, then re-indexes it. A taken-time change
within seconds of an earlier edit to the same file (a tag, a rating) can be undone when that
earlier re-index reads the file's old date back (seen on DSM 7.2.2, Photos 1.9.1; #289).
Ratings and descriptions weren't affected.

**What to do:** wait a few seconds after other edits to a file, then set the time again.
`edit_photo` sends all its fields in one request for this reason, so prefer one call per item.

### Files' dates and sizes changed after editing in Photos

**Why:** Photos writes ratings, descriptions, tags, taken-time changes and rotations into the
original files, so backup and sync tools see new versions (DSM 7.1 and 7.2.2).

**What to do:** expect it; mcp-synology can't make Photos keep edits out of the files.

### A smart album condition fails with DSM error 950 after tagging

**Why:** on Photos 1.9.1 (DSM 7.2.2), a re-index can give an existing tag a new ID, so a
condition saved with the old ID no longer matches. On 1.3.4 (DSM 7.1) IDs stayed put (Photos
spec, "Tag IDs aren't stable on 1.9.1").

**What to do:** set the condition again with `photo_album` `action="set_condition"`, naming the
tags. mcp-synology looks tag names up just before each call, so the condition gets the current
IDs; DSM stores the IDs, so a later renumbering can break it again. See
[invalid_parameter](error-codes.md#invalid_parameter).

### An item, folder, tag or person ID is "not found", but it's there

**Why:** IDs belong to a space. A Shared Space ID looked up in your personal space (the default)
finds nothing, and the other way around (verified; Photos spec, spaces). Albums are different:
they exist only in the personal space, so album tools take no `space`.

**What to do:** pass `space="shared"` for Shared Space items.

### Favorites or concepts are refused with `not_supported`

**Why:** they need Synology Photos 1.9.1 (DSM 7.2); 1.3.4 (DSM 7.1) doesn't have them.

**What to do:** update DSM and Synology Photos. See [not_supported](error-codes.md#not_supported).

### Photos tools fail with DSM error 602 or 901

**Why:** 602: the account has no personal space, because the user home service is off. 901
(not reproduced; from Synology's own web client): People, face recognition, is turned off in
Synology Photos' settings. See [unavailable](error-codes.md#unavailable).

**What to do:** for 602, an administrator enables **Control Panel → User & Group → Advanced →
User Home**. For 901, turn People on in Synology Photos' settings.

---

## System

### "No system information available" or "No utilization data returned"

**Why (not verified):** DSM answered but filled in nothing. Right after a boot or upgrade, DSM
may still be starting its monitoring services. A non-admin isn't the cause of these messages:
`get_system_info` still shows a non-admin the basic DSM fields, and a non-admin's
`get_resource_usage` fails with `permission_denied` instead (System spec).

**What to do:** retry after a minute. See [unavailable](error-codes.md#unavailable).
