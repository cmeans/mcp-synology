# Photos Module — Tool Specifications

> **Version:** 0.1 (draft for review) | **Updated:** 2026-10-17. Written before the code (#258), from live request and reply captures. Tool count: 4 READ + 15 WRITE = 19.
> **Parent doc:** `docs/specs/architecture.md`
>
> **Verified on:** DSM 7.1 with Synology Photos 1.3.4 and DSM 7.2.2 with Photos 1.9.1 (vdsm; non-admin and admin personas), and a real NAS (DS1618+, DSM 7.1.1, Photos 1.3.4; non-admin, personal space with 944 items and 150 face clusters). Request shapes were first taken from the Photos web client (`react.js` in 1.3.4, `react_bundle.js` in 1.9.1), since Synology publishes no Photos API guide, then sent and their replies recorded. A claim marked **verified** has a capture in the fixtures; one marked **web client** comes only from the client's code and is checked at the implementation PR's vdsm gate (open item 1).
>
> **Fixtures:** `tests/modules/photos/fixtures/dsm-7.1-photos-1.3.4.json`, `dsm-7.2.2-photos-1.9.1.json` (vdsm, synthetic library) and `dsm-7.1.1-photos-1.3.4-people.json` (the real NAS, People only, names and filenames anonymized). Keys are capture labels; each value holds the request as sent and DSM's reply.
>
> Once the module ships, `tests/test_module_specs.py` keeps this spec and the code in sync (tool headings, tiers, parameter names, LLM-facing descriptions).

## Module Metadata

```python
MODULE_INFO = ModuleInfo(
    name="photos",
    description="Browse, search, organize and share a Synology Photos library",
    required_apis=[
        ApiRequirement(api_name="SYNO.Foto.Browse.Item", min_version=2),
        ApiRequirement(api_name="SYNO.Foto.Browse.Folder", min_version=2),
        ApiRequirement(api_name="SYNO.Foto.Browse.Album", min_version=1, optional=True),
        ApiRequirement(api_name="SYNO.Foto.Browse.NormalAlbum", min_version=1, optional=True),
        ApiRequirement(api_name="SYNO.Foto.Browse.ConditionAlbum", min_version=2, optional=True),
        ApiRequirement(api_name="SYNO.Foto.Browse.Person", min_version=1, optional=True),
        ApiRequirement(api_name="SYNO.Foto.Browse.GeneralTag", min_version=1, optional=True),
        ApiRequirement(api_name="SYNO.Foto.Browse.Geocoding", min_version=1, optional=True),
        ApiRequirement(api_name="SYNO.Foto.Search.Search", min_version=1, optional=True),
        ApiRequirement(api_name="SYNO.Foto.Search.Filter", min_version=2, optional=True),
        ApiRequirement(api_name="SYNO.Foto.Thumbnail", min_version=2, optional=True),
        ApiRequirement(api_name="SYNO.Foto.Download", min_version=1, optional=True),
        ApiRequirement(api_name="SYNO.Foto.Upload.Item", min_version=1, optional=True),
        ApiRequirement(api_name="SYNO.Foto.BackgroundTask.File", min_version=1, optional=True),
        ApiRequirement(api_name="SYNO.Foto.BackgroundTask.Info", min_version=1, optional=True),
        ApiRequirement(api_name="SYNO.Foto.Sharing.Passphrase", min_version=1, optional=True),
        ApiRequirement(api_name="SYNO.Foto.Sharing.Misc", min_version=1, optional=True),
        ApiRequirement(api_name="SYNO.Foto.PhotoRequest", min_version=1, optional=True),
        ApiRequirement(api_name="SYNO.Foto.UserInfo", min_version=1, optional=True),
        ApiRequirement(api_name="SYNO.Foto.Browse.Concept", min_version=2, optional=True),  # 1.9.1
        ApiRequirement(api_name="SYNO.Foto.Setting.Admin", min_version=1, optional=True),  # read only
        ApiRequirement(api_name="SYNO.Foto.Setting.TeamSpace", min_version=1, optional=True),  # read only
        # Shared Space mirrors (SYNO.FotoTeam.*) are resolved per call, not required.
    ],
    tools=[...],  # 4 READ + 15 WRITE, below
    settings_schema=PhotosSettings,
)


class PhotosSettings(BaseModel):
    # Local folders upload_photos may read from and download_photos may write to, added
    # to the top-level allowed_local_paths, exactly as File Station's and Download
    # Station's lists (#257, core/local_paths.allowed_local_roots): with no top-level
    # list, this list, or else the Downloads folder.
    allowed_local_paths: list[str] | None = None
    # Seconds copy_photos / move_photos wait before handing the task off (default 30,
    # cap 600), as File Station's copy_move_timeout (#245).
    task_timeout: int = 30
```

The module is **opt-in**, like Download Station: add `photos` under `modules:`. It doesn't depend on File Station: upload, download, delete, copy and move all go through the Photos APIs. If Synology Photos isn't installed, every call fails with `api_not_found`.

---

## Design Conventions

### Personal space and Shared Space

Photos keeps two libraries, and the **API prefix** picks one; there's no space parameter in any request.

| Space | API prefix | Root folder | Backed by |
|---|---|---|---|
| Personal | `SYNO.Foto.*` | `/` of the user's own space (id varies; `Folder.get` with `id: 0` returns it) | `homes/<user>/Photos` |
| Shared | `SYNO.FotoTeam.*` | id `1` | the `photo` shared folder |

- Every tool that touches items, folders, tags, people or places takes `space` (`personal`, default, or `shared`) and sends to the matching prefix.
- **Albums, sharing links, photo requests and task status are personal-only APIs** (`SYNO.Foto.Browse.Album`, `NormalAlbum`, `ConditionAlbum`, `Sharing.*`, `PhotoRequest`, `BackgroundTask.Info`), so album and share-link tools take no `space`. Two exceptions take one anyway: `photo_request`'s `space` picks the destination library (`library: personal_space|shared_space`, verified for personal), and `photo_task` follows tasks of both spaces, since Shared Space task status is read through `SYNO.Foto.BackgroundTask.Info` too (verified).
- **Favorites work in both spaces on 1.9.1:** `SYNO.FotoTeam.Browse.Item set_favorite` v7 and `get` with `additional: ["favorite"]` (verified, admin). Whether a Shared Space favorite is per user wasn't checked (open item 1).
- **IDs belong to a space.** A Shared Space item ID looked up through `SYNO.Foto.Browse.Item get` returns an empty list (verified), not an error. Tools never mix IDs across spaces; a reply's `owner_user_id` of `0` means Shared Space.

### Prerequisites and the errors that report them

These are setup problems, not tool misuse, so each message names the fix.

| Code | When (verified) | Message gist / suggestion |
|---|---|---|
| **160** | The user lacks the Photos application privilege (`SYNO.Foto.AppInstance`). Every `SYNO.Foto*` call answers 160. | "This account can't use Synology Photos." An admin grants it in Control Panel > Application Privileges > Synology Photos. |
| **602** | The user home service is off, so there's no personal space. | "Personal space needs the user home service." An admin enables it in Control Panel > User & Group > Advanced > User Home. |
| **801** | Shared Space is disabled, **or** the user has no Shared Space permission. DSM answers the same code for both (verified: disabled on the real NAS; enabled but not granted on vdsm). | "Shared Space is off, or this account has no access to it." An admin enables it in Synology Photos > Settings > Shared Space and grants access there. |
| **803** | The user has Shared Space access (role `entry`) but no permission on that folder (verified on both versions: `Item.list`, `Folder.create`, `BackgroundTask.File delete`). | "No permission for this Shared Space folder." A Shared Space manager grants folder permissions in Synology Photos; the reply passes DSM's code through. |

160, 801 and 803 never trigger a re-auth (they aren't session errors). Shared Space access is granted with `Setting.TeamSpace update_permission` (`list: [{action, type, id, permission: "entry"|"management", auto_backup}]`, verified); folder-level view/download/upload rights sit on top of `entry` (`SYNO.FotoTeam.Sharing.FolderPermission`, out of scope, Decision 6).

### Transport

Chosen **per call on its merits** (maintainer, 2026-10-17: no transport is forced; GET-only no longer applies as a blanket rule). Both GET and form POST work for every JSON call here on both DSM versions (two full capture runs, one each way), so correctness doesn't decide it; these do:

| Calls | Transport | Why |
|---|---|---|
| Every JSON-returning call (browse, search, edits, albums, sharing, requests, tasks, deletes) | **Form POST**, values JSON-encoded, `api`/`method`/`version` in the body, `_sid` in the query string | Values include free text and secrets (share passwords, descriptions, names, search terms) that GET would put in DSM's and any proxy's access logs, the problem #235 fixed for login. Bodies have no URL-length limit, and calls carry up to 100 IDs per batch and nested condition objects (the 414 seen with Download Station's GET, #123). It's what both web clients send, so it's the path Synology tests. |
| `Upload.Item upload_to_folder` | **Multipart POST** (file part `file`, other fields JSON-encoded) | The only option for a file body. |
| `Thumbnail get`, `Download download` (binary replies) | **GET**, streamed through `DsmClient.download()` | Their values are only IDs, a cache key and a size, with nothing sensitive; the existing streamed-download path (temp file, rename, size checks) is GET, as File Station's `download_file`; and a GET can be retried without side effects. Form POST also works for both (verified). |

- **`api`/`method`/`version` must be in a POST body.** A POST that has them only in the query string and an empty body (a method with no parameters) fails with **101** (verified on both versions).
- **Every value is JSON-encoded**, strings included (`name="My album"`), as File Station's JSON-format APIs require (#121): an unquoted value that is valid JSON is read as that type (a folder renamed to `2024` would get the number). `Thumbnail get` accepts its values quoted or unquoted (verified).
- The 1.9.1 web client fetches thumbnails from a new path (`synofoto/api/v2/p|t/Thumbnail/get`); the `entry.cgi` form used here still works on 1.9.1 (verified).
- **Implementation:** `DsmClient.request(..., post_form=True)` already sends `api`/`method`/`version` in the body with only `_sid` in the query string, and keeps the re-auth retry (#235). Only its documented scope changes: the `CLAUDE.md` #235 bullet and the comment in `request()`.
- **Empty-body 101, verified:** a POST of `UserInfo me` with `api`/`method`/`version` only in the query string and an empty body answered 101 on both versions (`qa1_post-empty-body-query-only`).

### Versions

Each call uses the lowest version that supports what it sends, verified on both DSM versions; a feature that needs a newer version uses it only where the NAS registers it, and is refused locally elsewhere.

- **Base pins (both versions), every call that isn't v1:**
  - `Browse.Item`: list, get, set, list_with_filter, count_with_filter at v2. `list` is v2 because v1 rejects `rating` in `additional` (120, verified on both: `qa1_item-list-rating-v1`).
  - `Browse.Folder`: list, get, set_cover at v2.
  - `Browse.Album list` v2.
  - `ConditionAlbum`: create, set_condition, peek_item_count at v2.
  - `Browse.Person merge` v2.
  - `Search.Filter list` v2; `Sharing.Misc list_shared_with_me_album` v2; `Thumbnail get` v2.
  - Everything else at v1, including `Browse.Item` count/delete/add_tag/remove_tag/get_tag/get_exif, `NormalAlbum`, `Download download`, `Upload.Item`, `BackgroundTask.*` and `Setting.*`.
- **Favorites (1.9.1, `Browse.Item` v7 only):** `set_favorite`, `get`/`list` with `additional: ["favorite"]`, `list` with `favorite_only`, and `list_with_filter`/`count_with_filter` with `favorite: [true]` all need **v7**. v1–v6 answer 120 ("favorite is not supported"; verified each). **On 1.3.4 the same favorite filters are silently ignored** (every item comes back, verified), so the tool refuses them locally there.
- **Concepts (1.9.1):** `Browse.Concept` v2 and `Browse.Item list` v7 with `concept_id`.
- 1.9.1 registers higher versions of most APIs (`Browse.Item` v7, `Album` v5, `Upload.Item` v8…). They're used only for the features above; the 1.9.1 web client itself uses lower versions than the server registers for several APIs, so "max version" is never assumed safe.

### Edits rewrite the original files

Photos writes ratings, descriptions, tags, taken-time changes and rotations **into the original file** and then re-indexes it (verified on both versions: the first such edit grew the JPEG by about 2.5 KB, later ones by a few hundred bytes). So the file's modification time and size change, which backup and sync tools will see as a new version. Every write tool that does this says so in its description.

**Tag IDs aren't stable on 1.9.1.** After a re-index on DSM 7.2.2, an existing tag came back with a new ID (5 became 7), and a smart album condition still holding ID 5 then failed with **950**. On 7.1 the ID stayed put. So:

- tools take tag **names** and resolve them to IDs immediately before each call (`GeneralTag.list`), never from a cache;
- `GeneralTag.create` is idempotent by name (it returns the existing tag's ID, verified), so "create if missing" is just `create`;
- `add_tag` for one item sends all its tags in a single call. Several `add_tag` calls in quick succession on one item overwrite each other (seen on the real NAS while bulk tagging, #248).

### Writes that report success for nothing

DSM answers **success** for these when an ID doesn't exist (verified on both versions unless noted): `Browse.Item delete`, `Browse.Album delete`, `Browse.Item add_tag` (`error_list: []`), `NormalAlbum add_item` with a missing item, `Browse.Folder delete` on 1.3.4 (`error_list: []`; 1.9.1 answers 117), and `Person merge` with a missing `merged_id` (it returns the target unchanged; real NAS, 1.3.4). **Writes without permission do the same.** A Shared Space user with only `entry` access (no folder permission) got **success** for `Browse.Item set` (`error_list: []`), `Browse.Item add_tag` (`error_list: []`) and `Browse.Item delete`, and none took effect: admin read-backs showed the rating and tags unchanged and the item still there (verified on both versions, `qa1_team_*`, `qa2_*`). `BackgroundTask.File delete` answered 803 for the same user. That user's `Browse.Item get` answers **success with an empty list** for those items (not 803; verified on both), exactly as for an unknown ID, and `get_tag` an empty `intersection`/`union`.

So the ID check below catches this case too, before anything is sent: an item the user can't see comes back missing from `get`, and is reported as "not found, or no permission for its Shared Space folder". Deletes also use the background task, which answers 803 on its own (see Deletes).

So every tool that takes IDs **checks them first** (one `get` or `list` call) and reports unknown IDs as `not_found` without sending the write. `error_list` in a reply lists IDs DSM did reject (seen for `add_tag` on an item still being indexed); those are reported per item.

### Missing features (by API, not by choice)

Checked against both web clients and by calling the methods (103 = no such method):

- **No tag rename or delete method** (`GeneralTag.set`/`delete` answer 103 on both versions; 1.9.1 adds only `suggest`). But **a tag exists only while some item has it** (verified on both versions): `remove_tag` from its last items made it vanish from `list` and `get` at once, with no `item_count: 0` leftover, and a tag that was created but never attached never appears. So renaming or deleting a tag is `tag_photos` over every item that has it (found with `list_photos(tag=…)`).
- **No location editing:** `Item.set` with `gps` answers 101.
- **No "albums containing this item" lookup.**
- **Favorites exist only on 1.9.1:** `Browse.Item set_favorite` v7 (`id: [..]`, `favorite: bool`), read with `additional: ["favorite"]`, listed with `favorite_only: true` or the `favorite: [true]` filter (all v7, see Versions). 1.3.4 has no way to set one (its heart button only changes the page; `set_favorite` answers 103), and its `Favorite list` returns items without a flag to set them by. On 1.9.1 `Favorite list` answers 103.
- **No Photos trash:** neither version has a recycle bin or "recently deleted" API; recovery is the DSM share recycle bin (see Deletes).
- `Upload.Item upload_check`, `Download download_original`: 103 on both.

### Version support matrix

From both web clients' version maps and the server's `SYNO.API.Info`, with every row this module relies on verified by calls on both versions.

| | DSM 7.1 / Photos 1.3.4 | DSM 7.2.2 / Photos 1.9.1 |
|---|---|---|
| `SYNO.Foto*` / `SYNO.FotoTeam*` APIs registered | 39 / 21 | 46 / 27 (adds Concept, Similar, SimilarItem, SimilarTimeline, RegeneratePreview, Upload.Face, Udc; none removed) |
| Max versions | `Browse.Item` 2, `Album` 2, `ConditionAlbum` 2, `Upload.Item` 1 | `Browse.Item` 7, `Album` 5, `ConditionAlbum` 4, `Upload.Item` 8 |
| Everything in this spec not listed below | ✓ | ✓ (same base pins) |
| Set or read a favorite; favorite filters | ✗ → `not_supported` (set: 103; filters silently ignored, so refused locally) | ✓ v7 |
| `Favorite list` | ✓ (not used) | ✗ 103 |
| Concepts (object and scene albums) | ✗ → `not_supported` | ✓ `Browse.Concept` v2 |
| `BackgroundTask.File delete` | ✓ | ✓ |
| `Search.Search list_album` | ✓ (not used) | ✗ 103 |
| `Download` `download_type` (JPEG conversion) | ✗ | ✓ v2 (open item 5) |
| Similar-photo stacks | ✗ | ✓ (open item 3) |

An action the NAS's Photos version lacks returns `not_supported` naming the version, never a raw 103. Tools and parameters aren't hidden per version.

### Indexing lag

An upload returns an item ID at once, but Photos indexes it afterwards (seconds on vdsm):

- until metadata indexing finishes, `time` is the upload's `mtime`, not the taken date, and `resolution` is `0×0`;
- thumbnails go `converting` → `ready`. **A thumbnail requested while `converting` gets HTTP 404 with an HTML page** (verified), so `download_photos` checks the item's thumbnail status first and reports "not generated yet; try again shortly";
- `Index get` reports pending counts per stage (`basic`, `metadata`, `thumbnail`, `geo_coding`, `face_extraction`, …).

### Deletes and the recycle bin

Item and folder deletes use **`BackgroundTask.File delete`** (`item_id: [..]`, `folder_id: [..]`), a background task like copy and move: it exists on both versions (verified), the 1.9.1 web client uses it, and a large delete then reports progress and hands off instead of blocking. `Browse.Item delete` and `Browse.Folder delete` also work synchronously on both versions (verified) but are not used.

Photos has no trash of its own. Deletes go to the **DSM recycle bin of the share that holds the space**, when that share has one. Verified on the real NAS: a deleted personal-space folder appeared under `/home/#recycle/Photos/`. Photos 1.9.1's `Setting.Admin get` v1 reports `enable_personal_dsm_recycle_bin` and `enable_shared_dsm_recycle_bin` (whether the backing share has a recycle bin; the 1.9.1 client warns admins when it's off). A **non-admin can read it** (verified on both versions); 1.3.4's reply has no such fields. The delete tools read it on 1.9.1 and say "moved to the recycle bin" or "deleted permanently (no recycle bin on that share)"; on 1.3.4, or if the read fails, they say "moved to the share's recycle bin if it has one". Reading this setting (and `Setting.TeamSpace get` for `enable_person`) is in scope; changing Photos settings is not (Decision 6).

### Background tasks (copy, move, delete)

`BackgroundTask.File copy|move|delete` returns a `task_info` (`id`, `status` `waiting|processing|aborting|done`, `total`, `completion`, `skip`, `overwrite`, `error`). `copy_photos`, `move_photos`, `delete_photos` and `delete_photo_folders` poll `BackgroundTask.Info get_status` (1 s, as the web client) until `done` or `task_timeout`, then hand off with a handle `photos:<id>` that `photo_task` follows up on (a delete task with a small batch finished within 2 s on vdsm), the same pattern as File Station's `file_task` (#245). A finished task stays listed until `clear_completed_task`; an unknown ID returns an empty list (status) or **117** (abort).

### Identifiers and names

Tools take the IDs list calls return, and also **names where they're unique**: tags (always by name), albums and people (by name when exactly one matches; otherwise `invalid_parameter` listing the matches with IDs). Folders take an ID or a path inside the space (`/Trips/2024`, resolved with `Folder.get` by `name`, verified). Places take IDs from `list_photo_collections(type="place")`.

### Formatting

Plain text through the shared formatters. `time` (taken time) is Unix seconds and shown as UTC (`YYYY-MM-DD HH:MM:SS UTC`); `indexed_time` and task `create_time` are **milliseconds**. Sizes are binary units. Shared links are shown in full: they are what the user asked for.

### Errors

Photos-specific codes, dispatched when the API name starts with `SYNO.Foto`. Codes 105, 106, 107 and 119 fall through to the common handlers (105 never re-auths).

| Code | Seen when (verified) | Mapped to |
|---|---|---|
| 101 | `Item.set` with an unsupported field (`gps`) | `invalid_parameter` |
| 103 | Method doesn't exist on this Photos version | `not_supported` (a **new** `ErrorCode`; `docs/error-codes.md` gets a section for it) |
| 117 | `abort_task` for an unknown task (both versions); `Browse.Folder delete` on a missing ID (1.9.1 only) | `not_found` |
| 120 | Missing or ill-typed parameter (`errors.name`, `errors.reason`: `required`, `type`, `condition`, or a sentence such as "favorite is not supported") | `invalid_parameter` naming the field |
| 160 | No Photos application privilege | `permission_denied` (see Prerequisites) |
| 602 | No personal space (user home service off) | `unavailable` (see Prerequisites) |
| 620 | Upload of a file Photos doesn't support (`.txt`) | `invalid_parameter` |
| 621 | Upload into a folder that doesn't exist | `not_found` |
| 642 | Folder doesn't exist (`Folder.get`) | `not_found` |
| 801 | Shared Space disabled or not granted | `permission_denied` (see Prerequisites) |
| 803 | No permission on that Shared Space folder | `permission_denied` (see Prerequisites) |
| 901 | People (face recognition) disabled (from the 1.3.4 client; not reproduced) | `unavailable` |
| 950 | Smart album condition references a tag/person/folder that no longer exists | `invalid_parameter` |
| 953 | Smart album condition with `user_id` null (verified on both, `qa1_condalbum-create-null-user`). A condition with no `user_id` key at all gets 120 (`name: condition`, `reason: type`) from both `create` and `peek_item_count` (verified on both, `qa2_condalbum-create-no-user-key`, `qa1_condalbum-peek-no-user`) | `invalid_parameter` |

The 1.3.4 upload code treats 604, 605 and 622 as non-retryable too; their meanings weren't reproduced and they map to `dsm_error` with the code.

---

## READ Tier Tools (4 tools)

### 1. `list_photos`

Browse or search items.

**Permission tier:** READ

**DSM API:** one of `Browse.Item list` v2 (a single folder, album, person, tag or place), `Browse.Item list_with_filter` v2 (date range, type, rating, favorite, or combinations), `Search.Search list_item` v1 (keyword); the total from the matching `count` / `count_with_filter` / `count_item`

**Tool description (LLM-facing):**
> List photos and videos in Synology Photos, newest first by default. Narrow by one of folder, album,
> person or place, plus tag, date range (start_date/end_date, YYYY-MM-DD), type (photo/video),
> min_rating, favorite or concept (Photos 1.9.1+); or search by keyword (filename, description and tags). Returns id,
> filename, taken time, type, size and folder; paged with offset/limit (default 50) with the total.
> space: personal (default) or shared. Use the ids with get_photo, download_photos, tag_photos and
> the other photo tools. For files outside Photos, use list_files.

**Parameters:**

| Name | Type | Required | Default | Description |
|------|------|----------|---------|-------------|
| `space` | `str` | No | `"personal"` | `personal` or `shared`. |
| `folder` | `str` | No | — | Folder ID or path in the space. **One level** (Photos has no recursive folder listing). |
| `album` | `str` | No | — | Album ID or unique name (personal only). Can't be combined with other filters. |
| `person` | `str` | No | — | Person ID or unique name. |
| `place` | `int` | No | — | Place ID from `list_photo_collections(type="place")`. |
| `tag` | `str` | No | — | Tag name. |
| `keyword` | `str` | No | — | Search text. Can't be combined with other filters. |
| `start_date` | `str` | No | — | `YYYY-MM-DD` (UTC), inclusive. |
| `end_date` | `str` | No | — | `YYYY-MM-DD` (UTC), inclusive. |
| `type` | `str` | No | `"all"` | `all`, `photo` or `video`. |
| `min_rating` | `int` | No | — | 1–5. |
| `favorite` | `bool` | No | — | Only favorites. Photos 1.9.1+; on 1.3.4, `not_supported`. |
| `concept` | `int` | No | — | Concept ID from `list_photo_collections(type="concept")`. Photos 1.9.1+. |
| `sort_by` | `str` | No | `"taken"` | `taken`, `filename`, `size` or `type` (DSM `takentime`, `filename`, `filesize`, `item_type`). |
| `sort_direction` | `str` | No | `"desc"` | `asc` or `desc`. |
| `offset` | `int` | No | `0` | |
| `limit` | `int` | No | `50` | 1–500. |

**Response shape:**

```
Photos in /Trips/2024 (personal space; newest first)
==============================================================================
  ID   Filename          Taken                    Type   Size      Folder
  ───  ────────────────  ───────────────────────  ─────  ────────  ───────────
  41   beach-sunset.jpg  2024-07-04 19:30:00 UTC  photo  50.1 KiB  /Trips/2024
  36   clip-test.mp4     2024-07-06 07:00:00 UTC  video  22.2 KiB  /Trips/2024

2 item(s) total; showing 1–2.
```

The heading names the source (folder path, album, person, tag, place, concept or `search "<keyword>"`) and the filters. A full page ends with `Pass offset=N for the next page.`; an offset past the end shows `No items to display.` and the total. A row still being indexed has `(indexing)` after its taken time.

**Design notes:**
- **One source, filters on top.** A single `folder`/`person`/`tag`/`place` with no other filter uses `Item.list` (`folder_id`, `person_id`, `general_tag_id`, `geocoding_id`; all but `geocoding_id` verified, that one from the web client). Anything else uses `list_with_filter`, whose filter keys are lists of IDs: `folder: [id]`, `person`, `general_tag`, `geocoding`, `time: [{start_time, end_time}]`, `item_type` (`[-1]` photos, `[-2]` videos; verified on both versions), `rating` (`[min..5]`), and on 1.9.1 `favorite: [true]` (v7). With `person` and `general_tag` the web client adds `person_policy`/`general_tag_policy: "or"`.
- `album` uses `Item.list` with `album_id` (normal and smart albums alike, verified); `list_with_filter` has no album key, so combining is refused locally with `invalid_parameter`.
- `keyword` uses `Search.Search list_item`. It matches filename substrings (verified: `beach` found `beach-sunset.jpg`). Description and tag matching come from the web client and are checked at the vdsm gate.
- **`list` replies carry no total.** The footer total comes from the matching count call: `Item.count` takes `folder_id` or `person_id` (verified) or `geocoding_id` (web client), so tag totals use `count_with_filter` and album totals `Album.get`'s `item_count`.
- `Item.list` runs at v2 because v1 rejects `rating` in `additional` (120, `reason: condition`, verified on both); v7 on 1.9.1 when `favorite` or `concept` is involved.
- Rows show `taken` from `time`; an item still being indexed shows its upload time (see Indexing lag).

---

### 2. `list_photo_collections`

Albums, people, tags, places and folders.

**Permission tier:** READ

**DSM API:** by `type`: `Browse.Album list` v2 (+ `Sharing.Misc list_shared_with_me_album` v2 for `shared_with_me`), `Browse.Person list` v1, `Browse.GeneralTag list` v1, `Browse.Geocoding list` v1, `Browse.Concept list` v2 (1.9.1), `Browse.Folder list` v2; totals from each API's `count`

**Tool description (LLM-facing):**
> List Synology Photos collections with their ids and item counts. type: album (yours, with sharing
> status), shared_with_me (albums others shared with you), person (people from face recognition;
> include_hidden for hidden ones), tag, place, concept (things and scenes Photos recognized; 1.9.1+),
> or folder (subfolders of parent, default the space root). name filters by substring. Paged with offset/limit (default 100). space: personal (default)
> or shared; albums are personal only.

**Parameters:**

| Name | Type | Required | Default | Description |
|------|------|----------|---------|-------------|
| `type` | `str` | **Yes** | — | `album`, `shared_with_me`, `person`, `tag`, `place`, `concept` or `folder`. |
| `space` | `str` | No | `"personal"` | Ignored for `album` and `shared_with_me`. |
| `name` | `str` | No | — | Case-insensitive substring; applied client-side over all pages. |
| `parent` | `str` | No | — | `folder` only: parent folder ID or path (default the root). |
| `include_hidden` | `bool` | No | `false` | `person` and `concept`: `show_hidden` (for people also `show_more`, which adds people with few photos). |
| `offset` | `int` | No | `0` | |
| `limit` | `int` | No | `100` | |

**Response shape:** a table per type. Albums: id, name, type (`normal`/`condition`), items, shared (privacy type), date range. People: id, name (`(unnamed)` for `""`), items, hidden. Tags: id, name, items. Places: id, name, country, items. Folders: id, path, shared. Concepts: id, name, items, hidden. Every table ends with `N <type>(s) total; showing a–b.`

**Design notes:**
- Folder `name` in replies is the **full path** within the space (`/mcp-spec-capture/2024`).
- Places: `Geocoding list` returns `name`, `country`, `first_level`, `second_level`, `item_count`; the place hierarchy (country > state > city) is in `Search.Filter list`'s `geocoding` tree.
- Person names can be empty (an unnamed face cluster). Real data: 150 clusters, most named.
- Concepts: `list` (`additional: ["thumbnail"]`, `show_hidden`), reply fields `name`, `item_count`, `visibility` (web client). Synthetic vdsm images formed none, so the reply shape is checked at the real-NAS gate on a 1.9.1 NAS, or marked unverified.

---

### 3. `get_photo`

One or more items in full.

**Permission tier:** READ

**DSM API:** `Browse.Item get` v2 (`additional`: description, tag, exif, resolution, orientation, gps, address, rating, person, video_meta, thumbnail; plus `favorite` at v7 on 1.9.1); `Browse.Item get_exif` v1 for the full EXIF table

**Tool description (LLM-facing):**
> Get full details for Synology Photos items by id: filename, folder, taken time, size, resolution,
> rating, favorite (1.9.1+), description, tags, people, GPS location and address, camera details and the full EXIF
> table (exif=true). Up to 20 ids. space: personal (default) or shared.

**Parameters:**

| Name | Type | Required | Default | Description |
|------|------|----------|---------|-------------|
| `ids` | `list[int]` | **Yes** | — | Item IDs from `list_photos`. |
| `space` | `str` | No | `"personal"` | |
| `exif` | `bool` | No | `false` | Adds `get_exif`'s full key/value list (one call per item). |

**Response shape:** one block per item, then a line per ID not found.

```
Item 41: beach-sunset.jpg
  Folder:       /Trips/2024 (personal space)
  Taken:        2024-07-04 19:30:00 UTC
  Size:         50.1 KiB · 1600×1200 · photo
  Rating:       4 · favorite: yes (1.9.1+)
  Description:  spec capture
  Tags:         beach, 2024
  People:       Person 1, (unnamed)
  Location:     41.8781, -87.6298 · Chicago, Cook County, Illinois, United States
  Camera:       MCPTest SeedCam 1 · lens — · f/— · — s · ISO —

Not found: 999999
```

With `exif=true`, a `EXIF` table (key, value) from `get_exif` follows each block.

**Design notes:**
- An unknown ID is simply missing from `list` (no error); the tool reports it as not found.
- `address` holds country/state/county/city/town/district/village with `_id` twins; `gps` is `{latitude, longitude}`; `exif` (summary) has camera, lens, aperture, exposure_time, focal_length, iso as strings, often empty.
- `rating` 0 means unrated.

---

### 4. `download_photos`

Originals or thumbnails to a local folder.

**Permission tier:** READ

**DSM API:** `Download download` v1 (`item_id: [id]`, `force_download: true`) for originals; `Thumbnail get` v2 (`id` = the thumbnail's `unit_id`, `cache_key`, `type: "unit"`, `size`) for previews

**Tool description (LLM-facing):**
> Download Synology Photos items to a local folder by id: the original file, or a JPEG preview
> (size xl ~1280px, m ~320px, sm ~240px). Only writes inside the allowed local folders, and never
> overwrites unless overwrite=true. Up to 50 ids. space: personal (default) or shared. For files
> outside Photos, use download_file.

**Parameters:**

| Name | Type | Required | Default | Description |
|------|------|----------|---------|-------------|
| `ids` | `list[int]` | **Yes** | — | |
| `dest_folder` | `str` | No | — | Local folder; default as `download_file`. Must be inside the allowed local folders (#257). |
| `size` | `str` | No | `"original"` | `original`, `xl`, `m` or `sm`. |
| `space` | `str` | No | `"personal"` | |
| `overwrite` | `bool` | No | `false` | |

**Annotations:** READ tier (it only reads the NAS), but `readOnlyHint=false`, `destructiveHint=true`, as `download_file`: it writes, and with `overwrite=true` replaces, files on this machine.

**Response shape:**

```
Downloaded 2 of 3 item(s) to /home/user/Downloads (original)
  41  beach-sunset.jpg   50.1 KiB
  36  clip-test.mp4      22.2 KiB
  12  not downloaded: the m preview isn't generated yet (converting); try again shortly
```

**Design notes:**
- **One request per item**, written under its filename (`<stem>_<size>.jpg` for previews). Two or more `item_id`s in one request make DSM return a zip (verified, `application/zip`), which the tool avoids so names and errors stay per item.
- A preview needs the item's thumbnail status `ready` for that size (from `get` with `additional: ["thumbnail"]`). `converting` gets 404/HTML from DSM (verified), reported as not generated yet; `broken` (e.g. `preview` for photos) as unavailable.
- `cache_key` isn't checked by DSM (a bogus key still returned the image on both versions), but the tool sends the real one, as the client does.
- Uses `DsmClient.download()` (streamed, written to a temp file then renamed), with the local-path confinement File Station's `download_file` uses.

---

## WRITE Tier Tools (15 tools)

### 5. `upload_photos`

**Permission tier:** WRITE

**DSM API:** `Upload.Item upload_to_folder` v1 (multipart: `file`, `duplicate`, `name`, `mtime`, `target_folder_id`); optional `NormalAlbum add_item` v1

**Tool description (LLM-facing):**
> Upload local photos or videos into a Synology Photos folder (default the space root). on_duplicate:
> rename (default, keeps both) or skip. Optionally add them to an album. Only reads files inside the
> allowed local folders. Photos indexes new items over the next seconds to minutes. space: personal
> (default) or shared.

**Parameters:**

| Name | Type | Required | Default | Description |
|------|------|----------|---------|-------------|
| `local_paths` | `list[str]` | **Yes** | — | Files, inside the allowed local folders. |
| `folder` | `str` | No | `"/"` | Folder ID or path in the space; must exist. |
| `space` | `str` | No | `"personal"` | |
| `on_duplicate` | `str` | No | `"rename"` | `rename` or `skip` (DSM `duplicate`: `rename` / `ignore`). |
| `album` | `str` | No | — | Album ID or unique name to add the uploads to (personal only). |

DSM's `action` per file: `new`, `rename` (kept both, as `name_1.jpg`) or `ignore` (skipped; the reply's `id` is the **existing** item's).

**Response shape:**

```
Uploaded 3 file(s) to /Trips/2024 (personal space)
  beach-sunset.jpg    new      id 41
  beach-sunset.jpg    renamed  id 47 (as beach-sunset_1.jpg)
  forest-trail.jpg    skipped  id 12 (already there)
  notes.txt           failed   Photos doesn't support this file type (620)
Added 2 item(s) to album "Trip 2024".
Photos indexes new items over the next seconds to minutes; taken dates and previews appear then.
```

**Design notes:**
- `mtime` is the local file's modification time in seconds; Photos uses it as the taken time until EXIF is indexed.
- Unsupported file types fail with **620**, a missing folder with **621** (verified).
- A file name that's valid JSON (`2024`) is still sent JSON-quoted.

---

### 6. `photo_album`

**Permission tier:** WRITE

**DSM API:** `NormalAlbum create|add_item|delete_item|copy` v1; `Album set_name|set_cover` v1; `ConditionAlbum create|set_condition` v2 (+ `peek_item_count` v2)

**Tool description (LLM-facing):**
> Create or change a Synology Photos album. action: create (optionally with item ids), rename, add
> or remove items (the photos stay in their folders), set_cover, copy, create_smart or set_condition
> (a smart album that fills itself from folder, people, tags, places, dates, type and rating
> conditions). Personal space albums only. To delete albums use delete_photo_albums.

**Parameters:**

| Name | Type | Required | Default | Description |
|------|------|----------|---------|-------------|
| `action` | `str` | **Yes** | — | `create`, `rename`, `add`, `remove`, `set_cover`, `copy`, `create_smart` or `set_condition`. |
| `album` | `str` | No | — | Album ID or unique name (all actions except `create`/`create_smart`). |
| `name` | `str` | No | — | New name (`create`, `create_smart`, `rename`, `copy`). |
| `ids` | `list[int]` | No | — | Items (`create`, `add`, `remove`); one for `set_cover`. |
| `condition` | `dict` | No | — | `create_smart` / `set_condition`; see design notes. |

**Annotations:** `destructiveHint=true`: `remove`, `rename` and `set_condition` replace what the album held, and annotations can't depend on `action` (#183).

**Response shape:** one line per action, then the album as it now is.

```
Created album "Trip 2024" (id 9) with 2 item(s).
Album 9: Trip 2024 · normal · 2 item(s) · 2024-07-04 – 2024-07-06 · not shared
```

`create_smart` / `set_condition` add `Matches N item(s) now.` from `peek_item_count`. `add`/`remove` list IDs DSM rejected in `error_list`, and IDs that don't exist (checked first).

**Design notes:**
- Album names needn't be unique: DSM created a second album with the same name (verified). `create` warns when the name already exists; name lookup then needs the ID.
- `set_cover` takes `id_item` as a **scalar** (unlike `Folder.set_cover`).
- **Smart album condition** (tool input → DSM): `folders` → `folder_filter: [ids]`, `people` → `person: [ids]` + `person_policy` (`any`/`all` → `or`/`and`), `tags` → `general_tag` + `general_tag_policy`, `places` → `geocoding`, `start_date`/`end_date` → `time: [{start_time, end_time}]`, `type` → `item_type` (`[]`, `[-1]` photos, `[-2]` videos), `ratings` → `rating: [..]`, `keywords` → `keyword` + `keyword_policy`; `user_id` is filled in (the caller's id from `UserInfo me`, or `0` for a Shared Space source). The tool previews the match count with `peek_item_count` and returns it.
- A condition DSM can't use fails with 953 (e.g. `user_id` missing or null) or 950 (a referenced tag/person/folder no longer exists; see tag IDs above). A condition with only `user_id` is accepted and matches everything (verified), so the tool requires at least one other key.

---

### 7. `photo_share_link`

**Permission tier:** WRITE

**DSM API:** `Sharing.Passphrase set_shared` v1 (`policy: "album"`, `album_id`, `enabled`), `update` v1 (`passphrase`, `password`, `expiration`, `permission`), `get_permission` v1; `Sharing.Misc list_user_group` v1

**Tool description (LLM-facing):**
> Share a Synology Photos album by link, change its link settings, or stop sharing. action: share
> (returns the link), update or stop. public: none, view or download; password (empty string
> removes it); expires (YYYY-MM-DD, or never); users/groups with view, download or upload roles.
> Personal space albums only.

**Parameters:**

| Name | Type | Required | Default | Description |
|------|------|----------|---------|-------------|
| `action` | `str` | **Yes** | — | `share`, `update` or `stop`. |
| `album` | `str` | **Yes** | — | Album ID or unique name. |
| `public` | `str` | No | — | `none`, `view` or `download` (DSM privacy `private` / `public-view` / `public-download`). |
| `password` | `str` | No | — | Set; `""` removes. |
| `expires` | `str` | No | — | `YYYY-MM-DD` (end of day UTC) or `never` (`0`). |
| `members` | `list[dict]` | No | — | `{name, type: user|group, role: view|download|upload|none}`; `none` removes. Names resolved with `list_user_group`. |

**Annotations:** `destructiveHint=true`: `stop` and `update` replace the link's settings (an old password or public access is gone).

**Response shape:**

```
Album 9 "Trip 2024" is shared.
  Link:      https://nas.example:5001/mo/sharing/6kJOJFJVq
  Public:    view · password: yes · expires 2030-03-17 23:59:59 UTC
  Members:   alex (download), family (view)
```

`stop` says `Album 9 is no longer shared; the link stops working (DSM keeps it, so sharing again may restore it).` On an HTTP connection, a line says the password crossed the network unencrypted.

**Design notes:**
- `share` sends `set_shared` `enabled: true`, then `update` for any settings; the reply has `passphrase` and `sharing_link` (`http(s)://<host>/mo/sharing/<passphrase>`). `stop` sends `enabled: false`; its reply still carries the passphrase and link (verified), so re-sharing probably restores the same link (checked at the vdsm gate).
- `update`'s `permission` entries: public `{action: "update", role: "view"|"download", member: {type: "public"}}` or `{action: "delete", member: {type: "public"}}`; members `{action, role, member: {type, id}}`.
- Over plain HTTP the web client RSA/AES-wraps `password` (`encryption: ["password"]`); over HTTPS it's sent as is. The tool sends it as is in the POST body and, on an HTTP connection, says the password crossed the network unencrypted (#193).
- `Album.get` with `additional: ["sharing_info"]` reads the current state (`privacy_type`, `enable_password`, `expiration`, `is_expired`, `sharing_link`).
- Shared Space folder links (`SYNO.FotoTeam.Sharing.FolderPermission`) are out of scope for now (open item 3).

---

### 8. `photo_folder`

**Permission tier:** WRITE

**DSM API:** `Browse.Folder create` v1 (`target_id`, `name`), `rename` v1 (`id`, `name`), `set_cover` v2 (`id`, `id_item: [..]`; verified on both, `qa1_folder-set-cover`)

**Tool description (LLM-facing):**
> Create or rename a folder in Synology Photos, or set its cover photo. action: create (name inside
> parent, default the space root), rename or set_cover. space: personal (default) or shared. To copy
> or move folders use copy_photos / move_photos; to delete, delete_photo_folders. For folders
> outside Photos, use create_folder and rename.

**Parameters:**

| Name | Type | Required | Default | Description |
|------|------|----------|---------|-------------|
| `action` | `str` | **Yes** | — | `create`, `rename` or `set_cover`. |
| `folder` | `str` | No | — | Folder ID or path (`rename`, `set_cover`). |
| `parent` | `str` | No | `"/"` | `create` only. |
| `name` | `str` | No | — | New leaf name (`create`, `rename`). |
| `item_id` | `int` | No | — | `set_cover`. |
| `space` | `str` | No | `"personal"` | |

**Annotations:** `destructiveHint=true`, `idempotentHint=true`: `rename` and `set_cover` replace the previous name or cover (as `edit_person`). File Station's `rename` is non-destructive only because DSM refuses an existing name there; whether Photos does is open item 1.

**Response shape:** `Created /Trips/2024/Day 1 (id 33, personal space).` · `Renamed folder 33 to /Trips/2024/Day One.` · `Folder 33's cover is now item 41.`

**Design notes:** reply `folder` has `id`, full-path `name`, `parent`. Creating a folder whose name exists in the parent: checked at the vdsm gate (the capture created same-named folders only under different parents).

---

### 9. `copy_photos`

**Permission tier:** WRITE

**DSM API:** `BackgroundTask.File copy` v1 (`target_folder_id`, `item_id: [..]`, `folder_id: [..]`, `action`); `BackgroundTask.Info get_status` v1

**Tool description (LLM-facing):**
> Copy Synology Photos items and/or folders into another folder of the same or the other space.
> on_conflict: skip (default) or overwrite. Waits up to task_timeout seconds; a longer copy keeps
> running on the NAS and the reply gives a handle for photo_task. Overwrite replaces files at the
> destination.

**Parameters:**

| Name | Type | Required | Default | Description |
|------|------|----------|---------|-------------|
| `ids` | `list[int]` | No | — | Items. At least one of `ids` / `folders`. |
| `folders` | `list[str]` | No | — | Folder IDs or paths. |
| `destination` | `str` | **Yes** | — | Folder ID or path. |
| `space` | `str` | No | `"personal"` | Space of the source and of `destination` unless `dest_space` says otherwise. |
| `dest_space` | `str` | No | — | `personal` or `shared`; cross-space copies are allowed (web client). |
| `on_conflict` | `str` | No | `"skip"` | `skip` or `overwrite`. |

**Annotations:** `destructiveHint=true`: with `on_conflict="overwrite"` it replaces files at the destination (as `copy_files`, #183).

**Response shape:**

```
Copied 3 item(s) and 1 folder to /Albums/Best (personal space): 4 done, 1 skipped (already there), 0 overwritten.
```

Still running at `task_timeout`: `Still copying on the NAS (2 of 9 done). Handle: photos:14 — check with photo_task.` Failed items from `get_error_detail` follow as `  item 41: <reason>` lines.

**Design notes:** the task's `skip` and `overwrite` counters are reported (verified: a repeat copy with `skip` finished with `skip: 1`). `get_error_detail` (scalar `id`) lists failed items. Cross-space: the call goes to the **destination's** API and `extra_info` carries `{"version": 2, "source_library": "personal_space"|"shared_space"}` as a JSON **string**, JSON-encoded again (web client; cross-space verified at the vdsm gate). `Browse.Item copy/move` also exist (they require `action`) but the web client doesn't use them; neither does this module.

---

### 10. `move_photos`

**Permission tier:** WRITE

**DSM API:** `BackgroundTask.File move` v1; `BackgroundTask.Info get_status` v1

**Tool description (LLM-facing):**
> Move Synology Photos items and/or folders into another folder of the same or the other space.
> on_conflict: skip (default) or overwrite. Waits up to task_timeout seconds; a longer move keeps
> running on the NAS and the reply gives a handle for photo_task. Overwrite replaces files at the
> destination.

**Parameters:** (as `copy_photos`)

| Name | Type | Required | Default | Description |
|------|------|----------|---------|-------------|
| `ids` | `list[int]` | No | — | |
| `folders` | `list[str]` | No | — | |
| `destination` | `str` | **Yes** | — | |
| `space` | `str` | No | `"personal"` | |
| `dest_space` | `str` | No | — | |
| `on_conflict` | `str` | No | `"skip"` | |

**Annotations:** `destructiveHint=true`: it removes the sources, and with `on_conflict="overwrite"` replaces files at the destination (as `move_files`).

**Response shape:** as `copy_photos`, with "Moved".

---

### 11. `photo_task`

**Permission tier:** WRITE (only write connections start these tasks, and `stop` changes NAS state)

**DSM API:** `BackgroundTask.Info get_status` v1 (`id: [..]`), `list_user_task` v1, `abort_task` v1 (`id: [..]`), `clear_completed_task` v1

**Tool description (LLM-facing):**
> Follow up on a Synology Photos copy, move or delete that is still running. With a handle (photos:<id>):
> action=status shows progress; action=stop stops it (it may then be partly done). Without a handle:
> lists this user's copy, move and delete tasks. action=clear removes finished tasks from that list.

**Parameters:**

| Name | Type | Required | Default | Description |
|------|------|----------|---------|-------------|
| `handle` | `str` | No | — | `photos:<task id>`. |
| `action` | `str` | No | `"status"` | `status`, `stop` or `clear`. |

**Annotations:** `readOnlyHint=false`, `destructiveHint=true`: `stop` can leave a move or delete half done (as `file_task`, #183).

**Response shape:** with a handle, one line: `photos:14 copy to /Albums/Best: processing, 2 of 9 done (0 skipped, 0 overwritten), running 1 min 12 s.` or the final result in the starting tool's shape. Without a handle, a table: handle, operation, target folder, status, done/total, started (UTC). `clear` says how many finished tasks were removed.

**Design notes:** an unknown ID: `get_status` returns an empty list, `abort_task` 117; both are `not_found`. Tasks are per user and cover both spaces.

---

### 12. `tag_photos`

**Permission tier:** WRITE

**DSM API:** `Browse.GeneralTag list|create` v1; `Browse.Item add_tag|remove_tag|get_tag` v1 (`id: [..]`, `tag: [..]`)

**Tool description (LLM-facing):**
> Add or remove tags on Synology Photos items. Tags are given by name; add creates missing tags.
> Photos writes tags into the original files and re-indexes them, so file dates and sizes change.
> A tag disappears once no item has it, so to delete a tag remove it from every item that has it
> (list_photos tag=...); to rename, add the new name to those items, then remove the old one. space:
> personal (default) or shared.

**Parameters:**

| Name | Type | Required | Default | Description |
|------|------|----------|---------|-------------|
| `action` | `str` | **Yes** | — | `add` or `remove`. |
| `ids` | `list[int]` | **Yes** | — | Items. |
| `tags` | `list[str]` | **Yes** | — | Tag names. |
| `space` | `str` | No | `"personal"` | |

**Annotations:** `destructiveHint=true`: `remove` takes tags off items (and a tag with no items left disappears).

**Response shape:**

```
Added tags beach, 2024 to 3 item(s) (personal space).
  Rejected by DSM: 41 (error_list; e.g. still being indexed)
  Not found, or no permission for its Shared Space folder: 999999
Photos rewrites tagged files, so their dates and sizes change.
```

**Design notes:** names are resolved right before the call (see tag IDs); one `add_tag` per batch of up to 100 items carries all the tags, never repeated calls on the same item. The reply reports `error_list` items. `get_tag` (`intersection`, `union`) confirms the result. Tags are per space.

---

### 13. `edit_photo`

**Permission tier:** WRITE

**DSM API:** `Browse.Item set` v2 (`id: [..]` + `rating` | `description` | `time` | `shift_time` | `rotate_action`); 1.9.1: `Browse.Item set_favorite` v7 (`id: [..]`, `favorite`)

**Tool description (LLM-facing):**
> Edit Synology Photos items: rating (0-5, 0 clears), description, taken time (set to a date-time,
> or shift by hours), rotate (left or right), favorite (Photos 1.9.1+). Applies to every id given.
> Photos writes these into the original files, so file dates and sizes change. Location can't be
> edited. space: personal (default) or shared.

**Parameters:**

| Name | Type | Required | Default | Description |
|------|------|----------|---------|-------------|
| `ids` | `list[int]` | **Yes** | — | |
| `space` | `str` | No | `"personal"` | |
| `rating` | `int` | No | — | 0–5; outside that, DSM answers 120 (`reason: condition`, verified). |
| `description` | `str` | No | — | `""` clears. |
| `taken_time` | `str` | No | — | ISO 8601 date-time (UTC if no offset) → `time` (Unix seconds). |
| `shift_hours` | `float` | No | — | → `shift_time` (seconds). |
| `rotate` | `str` | No | — | `left` (`counter_clockwise`, verified) or `right` (`clockwise`, in the client's enum, checked at the vdsm gate). |
| `favorite` | `bool` | No | — | 1.9.1+ (verified set and unset). On 1.3.4, `not_supported`. |

**Annotations:** `destructiveHint=true`, `idempotentHint=true`: each field replaces the previous value (a description or taken time is lost), and repeating the call changes nothing more.

**Response shape:**

```
Edited 2 item(s) (personal space): rating 4, description.
  Rejected by DSM: 52 (error_list)
  Not found, or no permission for its Shared Space folder: 999999
Photos rewrites edited files, so their dates and sizes change.
```

**Design notes:** DSM accepted two fields in one `set` (verified), but the tool sends one `set` per field, as the web client does, so a failure names the field. Batches of 100 IDs (a **new** module constant, as the web client's `BATCH_UPDATE_SIZE`). `taken_time` and `shift_hours` are mutually exclusive. IDs are checked with `get` first, which also catches items in a Shared Space folder the user has no permission for: `set` would answer success and do nothing (see Writes that report success).

---

### 14. `edit_person`

**Permission tier:** WRITE

**DSM API:** `Browse.Person set` v1 (`id`, `name`), `show` v1 (`id: [..]`, `show`), `set_cover` v1 (`id`, `photo_id`)

**Tool description (LLM-facing):**
> Change a person (face-recognition group) in Synology Photos: rename (empty name clears it), hide,
> unhide, or set_cover to one of their photos. Use list_photo_collections type=person for ids. To
> merge people use merge_people. space: personal (default) or shared.

**Parameters:**

| Name | Type | Required | Default | Description |
|------|------|----------|---------|-------------|
| `action` | `str` | **Yes** | — | `rename`, `hide`, `unhide` or `set_cover`. |
| `person` | `str` | **Yes** | — | Person ID or unique name. |
| `name` | `str` | No | — | `rename`. |
| `item_id` | `int` | No | — | `set_cover`: a photo of that person. |
| `space` | `str` | No | `"personal"` | |

**Annotations:** `destructiveHint=true`, `idempotentHint=true`: `rename` and `set_cover` replace the previous name or cover.

**Response shape:** the person after the change: `Person 12: Jo (70 items, visible, cover: item 26).`

**Design notes:**
- rename, hide/unhide and `set_cover` verified on the real NAS, and reverted.
- **A person's `cover` is a face ID, not an item ID:** `set_cover` takes `photo_id` (an item of that person) and DSM picks that item's face, so `cover` came back as face 1810 for item 2121. Restoring needs the item, which the person's thumbnail `cache_key` names (`26_…` is item 26); sending the old `cover` value as `photo_id` failed with 117.
- `set` returns the person (`id`, `name`, `item_count`, `cover`, `show`).
- People in Shared Space need Shared Space People enabled: `enable_person` in `Setting.TeamSpace get`, off on a fresh install and readable by a non-admin (verified on both). When it's off, the tool says so instead of showing an empty list.

---

### 15. `photo_request`

**Permission tier:** WRITE

**DSM API:** `PhotoRequest create|list|update|delete` v1; `Sharing.Passphrase get_photo_request_info` v1

**Tool description (LLM-facing):**
> Manage Synology Photos photo requests: links that let others upload into one of your folders.
> action: create (subject, folder, optional description, size limit in MiB, expiry date), list,
> update or delete. Returns the request link.

**Parameters:**

| Name | Type | Required | Default | Description |
|------|------|----------|---------|-------------|
| `action` | `str` | **Yes** | — | `create`, `list`, `update` or `delete`. |
| `request` | `str` | No | — | The request's passphrase (from `list`) for `update`/`delete`. |
| `subject` | `str` | No | — | |
| `description` | `str` | No | — | |
| `folder` | `str` | No | — | Destination path (`folder_home_path`). |
| `space` | `str` | No | `"personal"` | `library`: `personal_space` / `shared_space`. |
| `max_file_mib` | `int` | No | — | `filesize_limit` in bytes; `0` none. |
| `expires` | `str` | No | — | `YYYY-MM-DD` or `never`. |

**Annotations:** `destructiveHint=true`: `delete` removes a request whose link others may hold, and `update` replaces its settings.

**Response shape:** `create` and `update`:

```
Photo request "mcp spec" → /Trips/2024 (personal space)
  Link:      https://nas.example:5001/mo/request/IQwpTmKug
  Limit:     10 MiB per file · expires never
```

`list` shows a table (request, subject, folder, size limit, expires, link); `delete` confirms the request.

**Design notes:** `create` sends `passphrase: ""`; the reply has `passphrase`, `sharing_link` (`…/mo/request/<passphrase>`), `folder_id`, `is_folder_valid`. `delete` takes `passphrase: [..]`. `PhotoRequest get` exists (verified) though the client doesn't call it.

---

### 16. `merge_people`

**Permission tier:** WRITE

**DSM API:** `Browse.Person merge` v2 (`target_id`, `merged_id: [..]`, `name`)

**Tool description (LLM-facing):**
> Merge people (face-recognition groups) in Synology Photos into one person, keeping the target's
> id and the given name. This can't be undone. space: personal (default) or shared.

**Parameters:**

| Name | Type | Required | Default | Description |
|------|------|----------|---------|-------------|
| `target` | `str` | **Yes** | — | Person ID or unique name to keep. |
| `merge` | `list[str]` | **Yes** | — | Person IDs or unique names merged into the target. |
| `name` | `str` | No | — | Name after the merge; default the target's. |
| `space` | `str` | No | `"personal"` | |

**Annotations:** `destructiveHint=true` (irreversible).

**Response shape:** `Merged 2 people into person 12 "Jo" (now 84 items).`, then any IDs or names not found (checked first, nothing merged if any is missing).

**Design notes:** a missing `merged_id` is silently ignored by DSM (verified), so all IDs are checked first. Not run with real IDs during capture (irreversible); the request shape is the web client's, and the reply shape (the target person) is from the missing-ID call.

---

### 17. `delete_photos`

**Permission tier:** WRITE

**DSM API:** `BackgroundTask.File delete` v1 (`item_id: [..]`, `folder_id: []`); `BackgroundTask.Info get_status` v1

**Tool description (LLM-facing):**
> Delete Synology Photos items (the files themselves) by id. They go to the recycle bin of the share
> that holds the space if it has one, otherwise they're gone. A large delete keeps running on the NAS
> and the reply gives a handle for photo_task. Confirm with the user first.
> space: personal (default) or shared. To only take items out of an album, use photo_album remove.

**Parameters:**

| Name | Type | Required | Default | Description |
|------|------|----------|---------|-------------|
| `ids` | `list[int]` | **Yes** | — | |
| `space` | `str` | No | `"personal"` | |

**Annotations:** `destructiveHint=true`.

**Response shape:** `Deleted 3 item(s) (personal space); they're in the recycle bin of the share.` (or `deleted permanently: that share has no recycle bin`, or `in the share's recycle bin if it has one` when the setting can't be read; see Deletes). Not-found IDs and DSM-rejected items follow per line; a long delete hands off like `copy_photos`.

**Design notes:** IDs are checked with `get` first; unknown ones are reported, not sent (the synchronous delete reports success for them; the task's behavior with an unknown ID is checked at the vdsm gate). The server instructions carry the same confirm-first guidance as `delete_files`.

---

### 18. `delete_photo_folders`

**Permission tier:** WRITE

**DSM API:** `BackgroundTask.File delete` v1 (`item_id: []`, `folder_id: [..]`); `BackgroundTask.Info get_status` v1

**Tool description (LLM-facing):**
> Delete folders in Synology Photos with everything in them (files and subfolders). They go to the
> recycle bin of the share that holds the space if it has one, otherwise they're gone. A large delete
> keeps running on the NAS and the reply gives a handle for photo_task. Confirm with the user first.
> space: personal (default) or shared.

**Parameters:**

| Name | Type | Required | Default | Description |
|------|------|----------|---------|-------------|
| `folders` | `list[str]` | **Yes** | — | Folder IDs or paths. The space root is refused locally. |
| `space` | `str` | No | `"personal"` | |

**Annotations:** `destructiveHint=true`.

**Response shape:** as `delete_photos`, naming the folders and the item count they held (`Deleted /Trips/2023 (128 items) …`).

**Design notes:** verified (with `Browse.Folder delete`): deleting a folder removed its items and subfolders, and on the real NAS it landed in `/home/#recycle/Photos/<name>`. Failed items come from `get_error_detail`.

---

### 19. `delete_photo_albums`

**Permission tier:** WRITE

**DSM API:** `Browse.Album delete` v1 (`id: [..]`)

**Tool description (LLM-facing):**
> Delete Synology Photos albums (normal or smart) and their share links. The photos themselves stay.
> Confirm with the user first.

**Parameters:**

| Name | Type | Required | Default | Description |
|------|------|----------|---------|-------------|
| `albums` | `list[str]` | **Yes** | — | Album IDs or unique names. |

**Annotations:** `destructiveHint=true`.

**Response shape:** `Deleted 2 album(s): 9 "Trip 2024", 12 "Best of"; their photos stay where they are.` Albums that didn't exist (checked first) are listed.

**Design notes:** destructive because an album's selection and share link can't be recovered, though no files are deleted.

---

## Resolved Design Decisions

### 1. Transport per call ✓
Maintainer, 2026-10-17: whatever is best for each call, not one forced rule. See Transport.

### 2. Tags by name, never by cached ID ✓
Forced by 1.9.1 re-numbering tags on re-index. See Edits rewrite the original files.

### 3. Validate IDs before writes, read edits back ✓
DSM reports success for unknown IDs on six write methods, and for `Item.set`/`add_tag`/`delete` by a Shared Space user without folder permission while doing nothing. That user's `get` returns those items as missing, so one check covers both.

### 4. Tool set versus #258 ✓ (this spec)
- `edit_tag` dropped (maintainer, 2026-10-17: no extra tool). Photos has no tag rename or delete method, but a tag with no items disappears, so both are `tag_photos` over the tag's items, as its description says. `delete_photo_collections` became `delete_photo_albums`.
- `edit_photo` lost location (no API) and its copy/move half; copy and move are `copy_photos` / `move_photos` (destructive-annotated because of `overwrite`, like File Station's `copy_files` / `move_files`), taking items and folders together, as the API does.
- `photo_folder` lost copy/move for the same reason.
- `download_photo` became `download_photos` (several ids per call).
- `photo_task` kept; handles `photos:<id>`, covering deletes too.
- Additions from 1.9.1: `favorite` (`list_photos`, `get_photo`, `edit_photo`) and concepts (`list_photos`, `list_photo_collections`).
- Count 19 (4 READ + 15 WRITE), one more than #258's estimate.

### 5. Titles ✓
`Tool.title` on the tools whose name alone is ambiguous next to File Station: `photo_folder` ("Photos: Folder"), `copy_photos` ("Photos: Copy"), `move_photos` ("Photos: Move"), `delete_photos` ("Photos: Delete items"), `delete_photo_folders` ("Photos: Delete folders"), `upload_photos` ("Photos: Upload"), `download_photos` ("Photos: Download"). `ToolInfo` gains an optional `title`.

### 6. Out of scope for now ✓
Maintainer, 2026-10-17 (the first group), with the 1.9.1-only additions found afterwards treated the same unless the maintainer says otherwise:
- Shared Space folder sharing (`SYNO.FotoTeam.Sharing.FolderPermission` / `FolderBatchPermission`)
- face-level People edits (`separate`, `delete_face`, `list_face`)
- photo requests' guest uploads
- timeline and streaming (`Browse.Timeline`, `Browse.Unit`, `Streaming`)
- Photos administration: changing `Setting.*`, `Index` reindex. *Reading* `Setting.Admin get` (recycle-bin flags) and `Setting.TeamSpace get` (`enable_person`) is in scope; both work for a non-admin (verified)
- added from the 1.9.1 diff: similar-photo stacks (`Browse.Similar*`), concept edits (`set_visibility`, `hide_item`, `set_cover`), manual face tagging (`Person add_face`, `Upload.Face`), `RegeneratePreview`, album and folder zip downloads

---

## Remaining Open Items

1. **From the web client only, checked at the implementation PR's vdsm gate:** keyword search over descriptions and tags; `Item.list` by `geocoding_id` and `Item.count` by `geocoding_id`; `rotate=right`; a same-name folder in one parent; cross-space copy and move (`extra_info`); `BackgroundTask.File delete` with an unknown ID; re-sharing after `stop`; the recycle-bin flags turning off when a share's recycle bin is off; whether a Shared Space favorite is per user; error 901 (People disabled); People and concepts on vdsm (clusters may not form without a GPU; then they're fixture- and real-NAS-tested only); a Shared Space user **with** folder view rights trying to edit or delete (needs `FolderPermission`, out of scope; the entry-only case is verified).
2. **A 119 right after login** was seen once on 7.2.2 (the first call of a fresh session; the retry was clean). It looks like #176's flake; the client's normal re-auth covers it.
3. **Out of scope:** see Resolved Design Decision 6.
4. **Payload:** measured in the implementation PR, with `PAYLOAD_BUDGET_CHARS` raised and the growth stated.
5. **JPEG conversion on download (1.9.1):** `Download download` v2 takes `download_type` (`source`, `optimized_jpeg`, `original_size_jpeg`), which would let `download_photos` hand HEIC originals over as JPEG. Not verified; proposed as a `download_photos` option for 1.9.1 if wanted.
