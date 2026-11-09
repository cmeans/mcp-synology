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

- **One entry point (#510).** Every path argument of every tool goes through `filestation/paths.py` `resolve_paths` before any request: (1) the part rules on the raw input (below: padding, NUL, #604's characters, empty parts, `.`/`..`), (2) the share check, (3) the argument's role, from one table (`PATH_PARAMS`). The operation functions take a `ResolvedPath` (a `str` from `resolve_paths`, or one the server builds from a checked one, such as a share's `#recycle`), so `mypy src/` rejects a tool that skips it. Guard tests: `tests/modules/filestation/test_paths.py` runs every tool and path argument through a table of hostile spellings, asserting a refusal naming the argument or the exact path sent, and fails if a tool has a path argument with no role; `tests/vdsm/test_vdsm_paths.py` pins DSM's behavior behind the rules on all four DSM versions.

  | Role | Arguments | What it refuses beyond the part rules |
  |---|---|---|
  | READ | `list_files.path`, `search_files.folder_path`, `get_file_info.paths`, `get_dir_size.path`, `download_file.path`, `copy_files.paths` | nothing: a shared folder is fine (`copy_files` of one copies its contents) |
  | WRITE_TARGET | `delete_files.paths`, `rename.path`, `move_files.paths` | a shared folder (#451, #479), a whole bin (`/<share>/#recycle`, a home bin `/home/#recycle` or `/homes/<user>/#recycle`, #479), a user's home folder `/homes/<user>` (an administrator's Rename of one left the user's `/home` empty; DSM made a new one at the user's next write and the files stayed under the new name, so renaming back failed with 1200: vdsm DSM 7.1 and 7.4.1, #494) |
  | DEST | `copy_files`, `move_files`, `restore_from_recycle_bin` and `upload_file`'s `dest_folder` | when the tool creates missing parents (`upload_file`'s `create_parents`): a missing part whose name the new-name rules refuse (below; `name_problem`, the same list as for a new name), since DSM would create it as given (`/video/Uploads ` beside `Uploads`, vdsm DSM 7.1 and 7.4.1), or one holding an escape as replies write it, most likely copied from a listing (#606). Only such parts are looked up |
  | NEW_FOLDER | `create_folder.paths` | a one-part path (a shared folder, not a folder in one), a bad new name (below), and with `force_parent` a missing parent whose name the new-name rules refuse or that holds an escape as replies write it (as for DEST) |
  | SHARE | `list_recycle_bin.share`, `restore_from_recycle_bin.share` | anything but a shared folder's name |

  Restore's items are relative to the bin and resolved on its share by `resolve_bin_items` (the same part rules; an item naming the whole bin is refused). **A name a tool creates** (`rename`'s `new_name`, `upload_file`'s `filename` or the local file's name, the last part of a `create_folder` path) goes through `refuse_bad_name`: refused when empty or blank, longer than 255 bytes in UTF-8 (the most ext4 and Btrfs allow; checked first, so the Unicode checks never see a longer argument), with a lone surrogate (a local file name that isn't UTF-8 decodes to one, and the request couldn't encode it), with a `/`, a NUL or a control character other than a tab inside it (a tab is fine: DSM keeps it; maintainer, 2026-10-31), a direction-changing character (Unicode's bidi controls, U+061C included; U+202E shows `invoice<RLO>txt.exe` as `invoiceexe.txt`) or a line or paragraph separator, no visible character at all (only whitespace, default-ignorable characters, combining marks or the Braille blank U+2800), an invisible character where Unicode doesn't use one (`invisible_problem`, built on Unicode's definitions and data files, not a judgment of ours; maintainer, 2026-10-31, after prior-art research on #515): a `Default_Ignorable_Code_Point` character (a zero-width space, a soft hyphen, a word joiner, a BOM, a Hangul filler, a combining grapheme joiner, `report.pdf<VS16>`) anywhere, except a ZWNJ or ZWJ where UTS #39 section 3.1.1.1 allows one in a word (Persian ZWNJ between joining letters; ZWNJ or ZWJ after a virama, so Malayalam chillu in the old spelling; not the rule's single-script condition, left to #516), two uses the Unicode core spec documents beyond those patterns (a ZWJ before a virama, Bengali ra-phala `র<ZWJ><U+09CD>যালি`; a ZWNJ between any two Arabic-script letters, Persian `در<ZWNJ>ها`), a ZWJ between two emoji, VS15/VS16 after an emoji emoji-variation-sequences.txt registers (not `😀`+VS16) or a keycap base before U+20E3, only the variation selectors Unicode has registered (VS1–VS7 and VS17–VS48; VS8–VS14 and VS49–VS256 are in no registered sequence and only hide a value, "variation-selector smuggling"), VS1–VS7 only in a sequence StandardizedVariants.txt registers, never after ASCII (`standardized_variants.py`, generated by `scripts/gen_standardized_variants.py` from Unicode's file; `café<VS1>.pdf` is refused, fullwidth `！<VS2>` passes), an ideographic variation selector (VS17–VS48) after an ideograph, at most 4 of these per name (each can carry about 5 bits and a run changes tokenization; emoji VS15/VS16 and Mongolian selectors don't count; a constant, not a setting: a real name it refuses is reported as an issue; maintainer, QA round 2 on #515), a Mongolian selector after a Mongolian letter (not after another selector: they're Mongolian-script themselves and chained into hidden runs), and tags only in the three RGI subdivision flags (England, Scotland, Wales: UTS #51; any other tag run, a well-formed subdivision id included, can spell words a model reads; maintainer, QA round 2 on #515); so `ab<ZWNJ>cd` and `rep<ZWJ>ort.pdf` are refused; the Braille blank at an end; an interlinear annotation character (U+FFF9–U+FFFB, which draw nothing though not default-ignorable); a combining mark first in the name; or the same combining mark, or run of marks, twice in a row in its NFD (UTS #39 section 5.4: it draws as one; U+0344 typed twice repeats U+0308 U+0301; a character's own decomposition, typed out or not, is no repeat, so NFD Gurung Khema passes like the precomposed form, but only that pair: U+1611E three or more times in a row, however it's typed (U+16121 twice, or U+16121 before another vowel sign that starts with U+1611E; after one, the NFD has no three in a row), is; a refusal names a mark taken from inside a character with that character, `(U+0F71, part of U+0F75)`; its "more than 4 marks" check isn't used: Tibetan stacks reach that many). So `नमस्ते`, NFD `Café`, `❤<VS16>`, `1<VS16><U+20E3>`, the Scotland flag (🏴 + tags `gbsct` + U+E007F), emoji newer than Python's own Unicode data, and a Persian word with a ZWNJ are ordinary names; `shown()` quotes by the same rule, writing each character that draws nothing as `\uXXXX` (`repr` alone leaves U+3164 or VS16 as is); a refusal for one of these says to retype the name without the character (or repeated marks) it names, except the cap's, which names each character in a variant form and says to write fewer (or report a real name that needs more), and the length limit's, which says to shorten the name; an upload's local file name keeps the advice to pass `filename`, since it can't be retyped; `shown()` quotes a part over 255 bytes without checking it (no NAS name is that long, and the check's repeated-mark search grows with the cube of a long run of different marks); the Egyptian hieroglyph format controls U+13430–1343F are refused like U+FFF9–FFFB; UTS #39's identifier profile, restriction levels and mixed-script checks aren't used: they refuse emoji, spaces and multilingual names. Also refused: `.` or `..` (padded too), whitespace at its start or end, or `#recycle` where it would be taken for a bin: directly in a shared folder or in a user's home folder `/homes/<user>` (DSM lets one be created where there's no bin folder yet; vdsm DSM 7.1 and 7.4.1, #494). For the same reason a copy, move or restore that would put a folder named `#recycle` there is refused (`refuse_planted_bin`), and so is a restore that would put an item back inside a bin, with a `dest_folder` in one or, with none, an item whose original folder is in one (`refuse_restore_into_a_bin`: the reply would say "Restored"). Refusals quote the caller's own text and return it as `value`; errors that name a checked path use `shown()`.
- **NUL (#472, #510):** a NUL in any path is refused: DSM cuts the path at the NUL, so `getinfo` of `<dir>/a<NUL>x` returned `a`, and `Delete` of it **deleted `a`** (vdsm DSM 7.1 and 7.4.1). **A character a reply shows escaped (#604)** is refused too: a control character other than a tab, a line or paragraph separator, a bidi control or a tag character. The client escapes those in every DSM reply (`core/formatting.py` `escape_nas_text`), so a name holding a newline can't write a line of its own into a reply, and a name shown escaped never matches its item, so taking one would act on nothing or another item. Such an item can be listed but not acted on through the tools until it's renamed in DSM. A NAS path refused as missing that holds an escape exactly as replies write one (`core/formatting.py` `holds_escape`: `\n`, `\r`, and the `\x..`, `\u....` or `\U........` of a character that's escaped) gets that advice instead of only "check with list_files" (`helpers.missing_paths_suggestion`: `rename`, `list_files`, `get_dir_size`, `download_file` and the missing-source check of copy, move, delete and restore), as does a missing `dest_folder`, where it replaces the advice to create the folder (`create_folder` would make a look-alike with a literal backslash), and DSM's own 408; when every missing path holds one, the reply gives only that advice. A missing parent folder holding one that `upload_file` (`create_parents`) or `create_folder` (`force_parent`) would create is refused before anything is made (`paths._refuse_bad_created_parents`), as such a folder would list just like the real one; a real backslash before `n` or `r` (`C:\new folder`) can't be told apart, so such a parent is refused too, and the refusal says to create it on its own with `create_folder` first (a new folder's own name isn't checked this way): passed as shown, the name matches nothing, and a listing shows the same escape again, so "check with list_files" alone would loop (the 0.11.0rc1 live-test rehearsal on seska). Other characters are taken exactly: a backslash is an ordinary character (`/writable\..\testshare` → 408), and `//` inside a path is refused as an empty part (DSM answers 408; it doesn't collapse it). `#recycle` is matched exactly: `#RECYCLE` is another folder (408).
- **Input:** Accept paths with or without leading `/`. Normalize internally by prepending `/` if missing.
- **Paths are taken exactly (#488).** Whitespace before a leading `/` is dropped as padding (` /video/x`), and a path of whitespace only is empty; nothing else is stripped. DSM keeps whitespace in a name exactly: on vdsm (DSM 7.1, 7.2.2, 7.3.2 and 7.4.1, as the test user on `/writable` and as admin on `/testshare`, identical everywhere; output on #488), `CreateFolder` made `foo`, `foo `, ` foo`, `foo<TAB>`, `foo<NBSP>` and `" "` side by side, `getinfo` and `List` on each path returned exactly that item, `Rename` of `…/foo ` renamed `foo ` (not `foo`), and `Delete` of `…/ foo` left `foo` alone. The tools used to strip the whole path, so `/video/x/foo ` reached `/video/x/foo`, a different item, and `/video/x/ ` reached `/video/x` itself; now each reaches the item it names, or gets `not_found` naming it (from the missing-path check, or for `list_files` from DSM's 408, which names no path itself), with a padded path quoted, `'/video/x/foo '`, so the stray whitespace is visible (#489). A path without a leading `/` keeps its leading whitespace as part of the share name, which then isn't a share. **A name a tool creates** (`rename`'s `new_name`, `upload_file`'s `filename` or the local file's name, `create_folder`'s new folder) **is refused if it starts or ends with whitespace**, as `invalid_parameter`: a model almost never means `report.pdf `, and SMB and Windows clients handle such names badly. Existing items with such names are still reached by their exact paths; a `create_folder` parent that exists is taken as given; a missing one it would create (`force_parent`) follows the new-name rules, and so does the new folder itself, even when a folder by that name already exists. Path refusals quote the path with `repr` (`Invalid path '/video/x/.. ': …`, `Unknown shared folder 'video '.`, #489).
- **Output:** Always return fully-qualified paths starting with `/shared_folder/...`.
- **Validation:** Check that the first path component corresponds to a known shared folder (from `SYNO.FileStation.List` / `list_share` cache). Return a clear error with `list_shares` guidance if not. Every path-taking tool does this in its wrapper before any DSM call (#160): the cache is per session, filled on first use, reloaded once when a path names a share it doesn't hold (so a new share is found), and cleared on re-auth. A share name in another case passes, and the path goes on to DSM with the name as `list_share` spells it (#353): DSM takes an ordinary share in any case (`/TESTSHARE/...` for `testshare`), but the user home service only as `home` (`/Home` and `/HOME` get 408 "No such file or directory": vdsm DSM 7.1 and 7.4.1, and seska's DSM 7.1.1 on Btrfs, admin and not). An unknown share is a non-retryable `not_found` naming the shares that exist (for an account that isn't an admin, the shares it can see). If DSM won't list shares, the check is skipped and DSM's own error stands. Without it, DSM answers an unknown share with misleading codes: 900 "Unexpected server error" (Delete) and 1100 "Insufficient filesystem permissions" (CreateFolder), on DSM 7.1 and 7.2.2.
- **No `.` or `..` parts (#449):** a path with a `.` or `..` part (`/video/../photo`, `/video/.`) is refused as `invalid_parameter` before the share check and before any request on that path, naming the tool argument it came from (`paths`, `dest_folder`, `folder_path`, `share`, or `default_upload_dir` when `upload_file`'s destination came from that setting); `restore_from_recycle_bin`'s items (relative to the bin, so outside the share check) are checked the same way. `.hidden` and `a..b` are ordinary names; a padded one (` .. `, `..<TAB>`; any whitespace since #510) is refused too, since whether DSM trims it is unprobed (Download Station's destinations and Photos' folder paths follow the same rule since #510 PR 2). So an entry really named ` .. ` (possible on Linux, e.g. over SSH) can't be reached through these tools: handle it in DSM. DSM refused every such path probed, but the share check read only the first part, so `/video/../photo` passed as a `video` path and anything keyed on the share used the wrong one. What DSM does with them (vdsm, DSM 7.1, 7.2.2, 7.3.2 and 7.4.1, non-admin): List and `getinfo` answer 418 "Illegal name or path" for `/writable/..`, `/writable/../testshare`, `/writable/.`, `/testshare/Documents/..` and `/writable/../../etc`; CreateFolder in `/writable/../testshare` answers 1100. `/writable/sub/../` with no `sub` answers 408 instead, so DSM looked `sub` up; whether an existing `dir/../` resolves wasn't probed (it's refused here either way). The share check's `not_found` names the same argument (it used to say `path` for every argument).
- **Escaping:** The DSM API uses commas as separators in multi-path parameters. Commas in paths must be escaped to `\,` and backslashes to `\\`. The core client handles this transparently.

### Response Formatting

All tools use the shared core formatters (`format_table`, `format_key_value`, `format_status`, `format_tree`, `format_error`). Responses are plain text optimized for LLM consumption, not JSON.

### Async Operations

Several File Station APIs are non-blocking (start → poll status → stop/clean). These are abstracted internally — the tool starts the operation, polls at ~500ms intervals, reports progress via MCP progress notifications when available, and returns the final result.

- **Search and DirSize:** wait up to `search_timeout` / `dir_size_timeout` (default `async_timeout`, 120 s). On timeout, stop the background task and return an error with the last known progress.
- **Delete and CopyMove hand off (#245):** `delete_files` waits up to `delete_timeout`; `copy_files`, `move_files` and `restore_from_recycle_bin` up to `copy_move_timeout`. Unset, the wait is **30 s** (`async_timeout` doesn't apply), and it's capped at **600 s** whatever the setting, below known client timeouts. A task still running when the wait ends is **not stopped**: the reply is a normal (non-error) result saying it's still running on the NAS, with its progress and a handle (`delete:<taskid>`, `copy:…`, `move:…`, `restore:…`) for `file_task`. In a multi-path call the remaining paths aren't started and are listed as `Not started:`. A status request that gets no answer during the wait (a transport error: any timeout, a refused or dropped connection), an HTTP error status (`DsmHttpError`, e.g. a reverse proxy's 502 or 504, which is how a proxy reports the same stall; #282), an answer that isn't a DSM reply (`DsmReplyError`, e.g. a 200 text/html page from a captive portal or a reverse proxy's login; #282) or any other unexpected answer hands the task off too, without stopping it (maintainer decision 2026-10-17): the task is most likely still running, and stopping or losing it would cut a huge delete short or leave it unreported. Its reply says the NAS stopped answering, that it or a proxy answered with an error (naming the HTTP status), or that its status reply couldn't be read, and never quotes the error's text (see "Handoff reply"). A canceled call (the user, or a client's own timeout) and a DSM error code on a status request still stop the task, apart from a 599 (below, #521). If the `start` request itself gets no answer there's no task ID, so the network error surfaces as before. A task finishing within the wait gives the same result as before. The server keeps an in-memory record per handed-off task (last 50, 24 h, lost on restart) so `file_task` can give the original tool's result shape. A path that already has a running task of the same kind isn't started twice; the reply returns that task's handle.
- **Running-task discovery (#245):** where `file_task` is registered, the first successful client of a server process lists the user's running Delete/CopyMove tasks (`SYNO.FileStation.BackgroundTask` `list` v2), seeds the task records from each task's start params, and appends a one-time note to the first File Station reply (the server adds any update notice after it, for every module, #314) naming them and their handles. If that first reply is `file_task` about one handle, the note leaves that task out and is skipped when nothing else is running. A `file_task` list uses the note up without showing it, since the list shows the same tasks. Any failure is logged at DEBUG and ignored; nothing is shown when nothing is running.
- **Progress reporting:** Via `ctx.report_progress(current, total, message)` using the MCP progress notification protocol. Delete and CopyMove send it while waiting (about one every 2 s, only when the numbers change), e.g. `Deleting /s/x: 124,219 of 200,400 items`; CopyMove counts bytes. While DSM is still counting (`progress: -1`) a heartbeat with no total is sent. The progress value increases with every notification, as MCP requires: a heartbeat, or a count that doesn't exceed the last value sent (including the next path of a multi-path call, which shares the progress token), is raised by 0.001 and sent without a total, so it's never shown against the wrong total. A failing notification is logged at DEBUG and never ends the wait. Whether the client renders this visually depends on the client implementation.
- **Failures without an `error` key:** a finished task can still have failed. CopyMove lists a missing source under `errors` (`[{"code": 408, "path": ...}]`), and a Delete of a missing path finishes with `total: -1` (DSM 7.1 and 7.2.2). Both are reported as structured errors naming the path, never as success (#162).
- **Missing sources are refused up front:** `copy_files`, `move_files` and `restore_from_recycle_bin` check every source with `getinfo` (restore checks the full `#recycle` paths) and refuse the whole call before anything moves, the same policy as `delete_files`. **While a running task of this user lands the same name at the destination, a call is refused if either one replaces** (`tasks.py` `running_landing`, #587, checked before each path's start; the task's source and destination come from `BackgroundTask` `list`, on vdsm's four versions; a restore to several places also checks them all before it starts, so a clash visible then refuses the whole call). That's the one case `overwrite=false` is refused: the running task could replace what it lands. **Parallel calls on one client take turns:** one `asyncio.Lock` per normalized destination (`DsmClient.destination_lock`), held from before the check until the start returns, not across the wait, and never two at once. A task is in DSM's list as soon as its start returns (80 of 80, admin and regular user, all four versions), but a small move has usually finished by a parallel call's list, and a finished task doesn't count (38 of 40 parallel moves both ran, on all four versions), so the check also reads what parallel calls on this client will land and have started (`tasks.py` `tracked_landings` and `pending_landing`: every source of a call, and every item of a restore with its destination (an item inside another going to the same place counts as part of that one, and is checked there too), is recorded before anything starts and kept until the call returns, when a record whose source never started is marked withdrawn, so a parallel call that read it earlier doesn't refuse for it; a call's own record isn't a clash for its own sources, which `refuse_same_names` checks; a restore checks before it re-creates a folder; maintainer's decision, 2026-11-06; 40 of 40 then give one move and one refusal). Not covered: a call that starts after another has returned, another account's task, a start at the same moment from a second server process, DSM's tasks when its task list fails (logged as a warning; parallel calls through this server are still checked), a landing inside a running task's folder (#596), and a destination spelled differently. `overwrite=false` is the safe route, as it never replaces. **Two sources that would land at one destination under one name are refused with `overwrite=true`** (`refuse_same_names`, #495): each runs as its own DSM task, so the second replaces the first, and for a move or restore the first's original is already gone. A pre-QA probe on vdsm's `/home` as a non-admin, DSM 7.1, 7.2.2, 7.3.2 and 7.4.1 alike, found: the reply says `Moved 2 item(s)`, the replaced file isn't in the recycle bin, `X.TXT` and `x.txt` stay two files, two folders of one name merge (same-named items inside them could be replaced, and the check doesn't read types, so they're refused too; separate calls still merge), and a folder landing on a file fails under either `overwrite` value (401 from a move of the pair, 1004 from a copy; the exception to "Conflicts with `overwrite=false` are not errors" below), while a file landing on a folder is skipped, or fails with 1004 under `overwrite=true`. `tests/vdsm/test_vdsm_same_names.py` pins these. Names compare ignoring case anyway (`casefold`, in `helpers.name_key`): a USB or network share may treat `X.TXT` and `x.txt` as one, which vdsm can't test, and there the first would be lost, so a rare safe call is refused instead, and `overwrite=false` still does it. A source inside the folder another source lands in (`["/s/n", "/d/n/q"]` into `/d`) is refused too: its data is replaced before its own task runs, and the reply counts both. Not checked on a real NAS or a share with its own bin. **A skip reply warns against `overwrite=true` for an item the same call's own earlier source may be blocking** (#580): following it replaced that source, whose original a move had removed. It knows about that call only, and through a hand-off too, where it counts every source done or skipped at that destination (`file_task`'s final text and a hand-off's `Not started` list), since a skipped folder lands its other items and a hand-off can't tell it from a file (#585). A restore to several places compares each item's own destination, and a hand-off's `Named like` line there doesn't cover its later items: a case-folding share can't hold both names in one bin. It doesn't know what a finished call put there, or after a restart (a call still running is refused, #587), so a skip reply's plain advice to pass `overwrite=true` always carries that caution. Names compare ignoring case; the folders they land in compare exactly, so a restore of `Docs/x` and `docs/x` with no `dest_folder` isn't refused. Conflicts with `overwrite=false` are not errors: DSM skips them (`"skipstatus": {"status": "all"}`, no `errors` key) and copies the rest. `skipstatus` is `{"status": "none"}` when nothing was skipped and absent with `overwrite=true`; `"all"` means at least one item was skipped (a whole file, or some of a folder's children), not which. A result with skips starts with `[!]` and a count, then separate `Copied:`/`Moved:` and skip lists. Since `skipstatus` doesn't say what was skipped, each skipped source's type is read with `getinfo` (only when something was skipped): a file goes under `Skipped:` (`[!] Copied 0 of 1 item(s) to <dest>/; 1 skipped …`), and a folder under `Folders with items skipped (anything not already at the destination was copied):` (`… 1 folder with items skipped …`), because DSM still copies or moves the children that weren't at the destination. It doesn't claim anything else was copied: DSM doesn't say whether any children were left to copy. With a folder, K counts the sources done `in full`. A source whose type can't be read (a failed or unexpected `getinfo`) keeps the hedged `Skipped in whole or in part:` list and a note that a skipped folder's other parts still went through (#189). For a move or restore it says skipped items are still at the source (for a skipped folder, inside that folder; of unknown type, `at this path or under it`; otherwise in the source's parent), and names the sources it did remove. It suggests `overwrite=true`, with that caution. A multi-destination restore with a skip leads with a neutral `[!]` summary line, `Restored K of N item(s) from /<share> recycle bin; M with skips (that name is already at the destination, overwrite=false; details below):`, and the per-destination results below it say which files or folders were skipped (#171, #190). Because paths are processed one task at a time, a later failure names the items already done before it.

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

- **Path not found (408):** `"List files failed: /video/missing doesn't exist on the NAS."` (`not_found`; the path is quoted when padded). `list_files` also gets 408 for a file: then `"List files failed: /video/a.jpg is a file, not a folder."` (`invalid_parameter`); for a folder that a `getinfo` right after sees, `"List files failed: DSM couldn't list /video/new (DSM error 408), though it's a folder that exists."` (`filestation_error`, retryable). See Path Handling for the `getinfo` that tells them apart.
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
- Footer: `N of N items.` when everything fits; `"Showing 1–200 of 1,432 items. Use offset=200 to see more."` when truncated; `"Showing 1,401–1,432 of 1,432 items."` on a last page reached with `offset`. The range, not just the count, says where a page is (0.10.0rc6 live test), as `list_recycle_bin`'s pattern listing already did. An offset past the end says `No items from offset=9: there are 5.`, and a negative offset is read as 0 (#389).
- `#recycle` is listed like any folder unless `hide_recycle_in_listings` is on (off by default); `list_recycle_bin` lists its contents. **When hidden (#389),** DSM still counts it in its `total` and offsets, so where it can appear (a share's top level, with folders listed and any `pattern` matching it) the tool fetches one entry more than `limit` and, when `#recycle` isn't on the page, checks for it with one `getinfo`. The total then leaves it out on every page, the next-page offset is DSM's (it counts `#recycle`), a `#recycle` right after a page is skipped with it rather than promising a page that turns out empty, and the footer gives a count, not a range: `Showing 2 of 4 items (#recycle hidden). Use offset=3 to see more.` A later page that ends the listing adds `Last page.`, since a count can't show where it is. A page that held only the hidden `#recycle` while more items follow (a share's top level with `limit=1`) is titled with the folder's total and still says `Use offset=N to see more.` If that check fails, or its answer shows neither (a per-entry code such as 407), the listing still shows, with DSM's total and a count footer that says the total may include a hidden `#recycle` (a range could name positions the earlier pages didn't have); an empty page past the end says the same after its `there are N`. A `#recycle` entry is hidden on any path that lists it; a path with a `.` or `..` part (`/video/.`) is refused before listing (#449).
- **A path DSM answers 408 for** gets `not_found` naming it (`List files failed: '/video/Movies ' doesn't exist on the NAS.`; DSM's own message names no path). DSM answers 408 for a **file** too (a real NAS, DSM 7.1.1; vdsm 7.1, 7.2.2, 7.3.2 and 7.4.1, `test_vdsm_paths.py`), so one `getinfo` follows: when it reports `isdir: false` the reply is `invalid_parameter`, `… is a file, not a folder.`, pointing at `get_file_info`; when it reports `isdir: true` (the folder created between the two calls, or a List DSM refuses that getinfo allows), the reply is a retryable `filestation_error`, `DSM couldn't list … (DSM error 408), though it's a folder that exists.`, never "doesn't exist"; when it fails or shows nothing, the `not_found` stays. `list_recycle_bin` skips that check: any 408 on `#recycle` means no bin folder (a planted file named `#recycle` included) (#515).
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
- DSM's `getinfo` answers a missing path with a per-file `{"code": 408, "path": ...}` entry, not a failed request (DSM 7.1 and 7.2.2). That entry is the path's error, never a file row (#161). One path, or every path, missing is a structured `not_found` naming each path. A mixed request shows the found entries plus a `<path>: doesn't exist (DSM error 408)` line for each missing one (`no permission` for 105 or 407, `can't be read` otherwise; #620), then the escaped-name advice once if a missing path holds an escape as replies write one (#606).

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
| `exclude_pattern` | `str` | No | `None` | Wildcard pattern to exclude from results (e.g., `*.torrent`), case-insensitive, matched against the whole name. Only `*` and `?` are wildcards; `[...]` and commas match themselves (#396). Unlike DSM's own `pattern`, a word without wildcards matches only the whole name, and a comma doesn't separate alternatives. Applied client-side to the (at most `limit`) results DSM returns, so a reply can hold fewer than `limit`. |
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
- **Local path confinement:** `dest_folder` must resolve (after `~`, `..` and symlinks) to a path inside one of the allowed folders (the top-level `allowed_local_paths` plus File Station's own; with no top-level list, File Station's list or the user's Downloads folder; `default_download_dir` is always allowed, and a relative one is from the config file's folder (#553); #257). Otherwise it's `permission_denied`, listing the allowed folders, before any NAS request and before the folder's existence is checked. The message shows where the path resolves to only when the path itself is inside an allowed folder (a symlink there that leads out), so a refusal doesn't reveal what exists outside. A `~user` that doesn't exist gets the same refusal, so it doesn't reveal which local users exist either (#260). A path that can't be resolved (a NUL byte, a symlink loop) is `invalid_parameter`. The server instructions list the allowed folders under LOCAL PATHS, and `dest_folder`'s description names them too (`roots_for_description`), since some clients never pass the instructions to the model (0.10.0rc5 and rc6 live tests). See `core/local_paths.py` and `config-schema-spec.md`.

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
| `dest_folder` | `str` | No | `default_upload_dir` | NAS destination folder. With neither set, `invalid_parameter` (`param: dest_folder`) before any NAS request (#194). When the destination comes from `default_upload_dir`, an error about it (a `.` or `..` part, an unknown share) names `default_upload_dir` and says to fix the setting or pass `dest_folder`. |
| `filename` | `str` | No | `None` | Rename on upload (defaults to the local filename). A name `refuse_bad_name` refuses (Path Handling: the new-name rules, `name_problem`) is refused with `invalid_parameter` (`param: filename`, #449); an empty one means the local file's name, which follows the same rule (an error then names `local_path` and the local name, and suggests passing `filename`). Blank means any whitespace (a tab, a no-break space). |
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
- **An overwrite says what it replaced (#620, 0.11.0rc3 live test).** With `overwrite=true`, `upload_file` looks the target name up first (`getinfo`) and adds `It replaced the <name> that was there.` when it was; `copy_files`, `move_files` and `restore_from_recycle_bin` look up each source's name at the destination and add `Replaced what was already there: …` (files) and `Merged into the folder(s) already there, replacing files with the same names: …` (folders) to a finished result. Only the wording depends on it: a lookup that fails says nothing extra, and neither a hand-off reply nor a handed-off task's final result (from `file_task`) says it. The lookup runs alongside the source and destination checks and is awaited only for the reply, so it doesn't delay the call's landing records or starts (#597). A target that is the source itself, or that a restore re-created or an earlier item of the call landed, isn't called replaced. `get_file_info` words a missing path as `<path>: doesn't exist (DSM error 408)` (`no permission` for 105/407, `can't be read` otherwise).
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
> with list_recycle_bin: every path must be in that share's bin, or nothing is restored; an empty
> item, naming the whole bin, is refused. Without `dest_folder`, each item goes back to its original
> folder, which is re-created if it's gone. An item whose name is already at the destination is skipped and stays
> in the bin, unless `overwrite=true`, which replaces the existing one, which may then not be in the
> recycle bin; two items that would replace each other are refused. A long restore keeps running on
> the NAS and returns a handle for file_task. While this user's running task is landing the same name there, the call is refused if either one replaces. That can miss another account's task or a start at the same moment from another server process, and DSM's tasks if its task list fails; overwrite=false never replaces.

**Parameters:**

| Name | Type | Required | Default | Description |
|------|------|----------|---------|-------------|
| `share` | `str` | **Yes** | — | Shared folder name (e.g., `video`) or path (`/video`). |
| `paths` | `list[str]` | **Yes** | — | Paths within the recycle bin to restore: relative to `#recycle`, as `list_recycle_bin` shows them (`Shows/Ep1.mkv`), starting with `#recycle/`, or full (`/video/#recycle/Shows/Ep1.mkv`). Any path not in the bin refuses the whole call before anything moves. An item that names the bin itself is refused (below). |
| `dest_folder` | `str` | No | `None` | Destination folder; it must exist, and every item goes there flat. If omitted, restores to original location (inferred from `#recycle` directory structure), re-creating it if it's gone. |
| `overwrite` | `bool` | No | `false` | Overwrite if a file already exists at the restore destination; a replaced file may not be in the recycle bin. With `false`, it's skipped and stays in the bin. |

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
- DSM's recycle bin doesn't keep an empty folder: deleting `a/` with an empty `a/empty/` leaves no `empty` in the bin (vdsm, all four versions: `test_pattern_finds_a_file_deleted_from_a_subfolder` checks it), and an empty folder deleted on its own isn't kept either (seska, DSM 7.1.1, 2026-10-31; on its own, not checked on vdsm). A folder that holds a file is kept. `delete_files`' recycle-bin note says so (#479).
- **An item that names the whole bin is refused (#479).** An empty item (`""`, `"/"`, a blank one), `#recycle` or `/<share>/#recycle` resolves to the bin folder itself, and restoring it would send `CopyMove start` on `/<share>/#recycle` with `remove_src=true`, moving the whole bin out. It's refused as `invalid_parameter`, `param: paths`, before anything is checked or moved: `Restore refused: '' names the whole recycle bin of /video, not an item in it. Nothing was restored.` An item counts as in the bin only when it is `#recycle` or starts with `#recycle/`: `#recycled/x` is an item named `#recycled` inside the bin, not a folder beside it. Items are taken exactly (#488): only padding before a leading `/` is dropped (so a bin folder really named `" "` is given as `#recycle/ /x` or `/video/#recycle/ /x`, not ` /x`), so `Shows/ ` names an item `" "` in the bin's `Shows`, `Shows/Ep1.mkv ` a file with a trailing space, and `#recycle/ /` an item `" "` in the bin, never the folder above it or the bin itself (each item used to be stripped, so `Shows/ ` restored all of `Shows`). Each item's destination drops only the leading `/<share>/#recycle`: `a/video/#recycle/b.txt` goes back to `/video/a/video/#recycle`.
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
> a folder that already exists is not an error. A new folder's name can't have whitespace at its
> ends, an invisible character where Unicode doesn't use one (a zero-width space, a soft hyphen),
> control or direction-changing characters, or be `#recycle` at a shared or home folder's top.

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

**DSM's 1200 is told apart (#188).** DSM answers 1200, "Failed to rename", both for a name already taken in that folder and for a source that doesn't exist (vdsm DSM 7.1, 7.2.2, 7.3.2 and 7.4.1). After a 1200, `getinfo` says which, and only on positive evidence: a source it reports missing (408) is `not_found` ("… doesn't exist on the NAS", `param: path`), and when it shows both the source and the new path exist, `already_exists` ("… already exists. Rename doesn't replace an existing file or folder.", `param: new_name`). `already_exists` is never claimed for a rename to the item's own name or of a share root (no parent folder to look in); a `new_name` of `.` or `..` is refused before the rename request (the share check may list shares first, and an unknown share is reported first; #449). Anything else is reported as the 1200 with its table message, which names both causes: a reply with no entry, another code such as 407, or a check that fails (a DSM error or no answer at all; logged at WARNING). Each path takes one `getinfo`. The check runs only after a failure, so a rename that works costs nothing extra.

**A whole shared folder is refused (#479).** DSM's `Rename` on `/<share>`, as an administrator, answers success with the new path, and the shared folder then disappears from `list_share`, under its new name too, so every path and client config that names it breaks; a non-admin gets 1200 (vdsm DSM 7.1.1-42962 Update 9, 7.2.2-72806 Update 10, 7.3.2-86009 Update 5 and 7.4.1-90080 Update 1; probe results on #479). So `rename` refuses a `path` that names a whole shared folder as `invalid_parameter`, `param: path`, before the rename request: `Rename refused: '/video' is a whole shared folder. Renaming one through File Station makes it disappear from DSM's shared folders, under its new name too (tested on DSM 7.1 to 7.4.1). Nothing was renamed.`, suggesting DSM's Control Panel. The check runs after the share check, so a one-part path that isn't a share is an unknown share; `//video` is refused before it, as an empty part (Path Handling, #510). A share's whole bin, `/<share>/#recycle`, and a user's home folder, `/homes/<user>`, are refused as well, as for `move_files` (below; WRITE_TARGET).

**DSM API:** `SYNO.FileStation.Rename` / `rename`

**Tool description (LLM-facing):**
> Rename a file or folder in place. If the new name is already taken in that folder, DSM refuses and
> nothing is replaced. To move something to another folder, use move_files. A whole shared folder
> (`/video`), recycle bin or user's home folder (`/homes/<user>`) is refused.

**Parameters:**

| Name | Type | Required | Default | Description |
|------|------|----------|---------|-------------|
| `path` | `str` | **Yes** | — | Full path to the file or folder to rename. A whole shared folder or recycle bin is refused (below). |
| `new_name` | `str` | **Yes** | — | New name (filename/folder name only, not a full path; refused with `invalid_parameter` by `refuse_bad_name` when it breaks the new-name rules (Path Handling, #510); the message names which). A name already taken in the folder is refused by DSM (1200) and nothing is replaced. |

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
> created. With `overwrite=false` (default), items whose name is already at the destination are skipped and
> listed, and the rest are copied. With `overwrite=true`, two sources that would replace each other
> are refused, and a replaced file may not be in the recycle bin. If any source is missing, nothing
> is copied. A long copy keeps running on the NAS and returns a handle for file_task. While this user's running task is landing the same name there, the call is refused if either one replaces. That can miss another account's task or a start at the same moment from another server process, and DSM's tasks if its task list fails; overwrite=false never replaces.

**Parameters:**

| Name | Type | Required | Default | Description |
|------|------|----------|---------|-------------|
| `paths` | `list[str]` | **Yes** | — | Source file/folder paths to copy. |
| `dest_folder` | `str` | **Yes** | — | Destination folder path (must exist). |
| `overwrite` | `bool` | No | `false` | Overwrite existing files at destination; a replaced file may not be in the recycle bin. With `false`, items whose name is already there are skipped and listed (see Async Operations). |

**Response shape:**

```
✓ Copied 2 items to /video/Archive/:
  🎬 Severance.S02E09.mkv (1.7 GiB)
  📄 Severance.S02E09.srt (42 KiB)
```

**Design notes:**
- **Long operations (#245):** hands off after the capped wait instead of stopping (see "Async Operations"); the reply's handle works with `file_task`.
- **The destination is checked before anything starts (#344).** DSM answers a copy or move into a folder that doesn't exist with 1002, "Destination error during copy/move", whose suggestion points at permissions (0.10.0rc4 and rc5 live tests, seska DSM 7.1.1). So `getinfo` on the destination comes first: 408 is `not_found` ("the destination folder … doesn't exist. Nothing was copied.", `param: dest_folder`, suggesting `create_folder`, or, for a destination holding an escape as replies write one, the escaped-name advice instead, #606), and 407, a share the account has no rights to, is `permission_denied`. Any other DSM answer leaves it to the task, as does no answer, or an unreadable one, on a restore's later destination (#588); with nothing started, those stop the call. The same check covers `move_files` and a restore with `dest_folder`. A folder refused by a Deny ACL inside a share the account can use still gets DSM's 1002, because `getinfo` reads the folder's entry from its parent (seska, a restricted test account); with missing folders caught first, 1002's suggestion (write permission, ownership) fits what's left.

---

### 13. `move_files`

Move files or folders to a new location. **Source files are removed after successful transfer.**

**Permission tier:** WRITE

**Annotations:** `destructiveHint=true`: it removes the sources, and with `overwrite=true` replaces files at the destination.

**DSM API:** `SYNO.FileStation.CopyMove` / `start` (with `remove_src=true`)

**Tool description (LLM-facing):**
> Move files or folders into a destination folder; a folder moves with everything in it, and the
> sources are removed. To keep the sources, use copy_files. `dest_folder` must already exist; it
> isn't created. With `overwrite=false` (default), items whose name is already at the destination are skipped,
> listed and left at the source. With `overwrite=true`, two sources that would replace each other
> are refused, and a replaced file may not be in the recycle bin. If any source is missing, nothing
> is moved. A whole shared folder
> (`/video`), recycle bin or user's home folder (`/homes/<user>`) is refused as a source: move its
> items by path. A long move keeps running on the NAS and returns a handle for file_task. While this user's running task is landing the same name there, the call is refused if either one replaces. That can miss another account's task or a start at the same moment from another server process, and DSM's tasks if its task list fails; overwrite=false never replaces.

**Parameters:**

| Name | Type | Required | Default | Description |
|------|------|----------|---------|-------------|
| `paths` | `list[str]` | **Yes** | — | Source file/folder paths to move. A whole shared folder or recycle bin is refused (below). |
| `dest_folder` | `str` | **Yes** | — | Destination folder path (must exist). |
| `overwrite` | `bool` | No | `false` | Overwrite existing files at destination; a replaced file may not be in the recycle bin. With `false`, items whose name is already there are skipped and listed (see Async Operations). |

**Response shape:**

```
✓ Moved 2 items to /video/TV Shows/Severance/Season 2/:
  🎬 Severance.S02E10.mkv (1.8 GiB)
  📄 Severance.S02E10.srt (45 KiB)

Source files have been removed from /video/Downloads/.
```

**Design notes:**
- **Long operations (#245):** hands off after the capped wait instead of stopping (see "Async Operations"); the reply's handle works with `file_task`.
- **A whole shared folder is refused as a source (#479).** DSM's `CopyMove` with `remove_src=true` on `/<share>` takes the shared folder itself out of `list_share` for an account with write access (the non-admin test user on `/writable`; admin wasn't probed, since the admin share had been renamed away by then), and the reply read as a skip (`Moved 0 of 1 item(s) … 1 skipped in whole or in part`); the recycle bin, inside the share, can't bring it back (vdsm DSM 7.1, 7.2.2, 7.3.2 and 7.4.1; probe results on #479). So `move_files` refuses a source that names a whole shared folder as `invalid_parameter`, `param: paths`, before anything is checked or moved: `Move files refused: '/video' is a whole shared folder. Moving one would remove the shared folder itself from DSM, beyond the recycle bin's reach (tested on DSM 7.1 to 7.4.1, as a non-admin with write access). Nothing was moved.`, suggesting moving its items by path (leaving out its `#recycle`), `copy_files` to keep it, or DSM's Control Panel. The check runs after the share check; `//video` is refused before it as an empty part, and `/video/.` as a dot part (Path Handling, #510). A user's home folder, `/homes/<user>`, is refused too (WRITE_TARGET), and so is moving or copying a folder named `#recycle` into a shared folder or a user's home folder, where it would be taken for the bin. A share's whole bin, `/<share>/#recycle`, is refused the same way for `move_files` and `rename` (`… is the shared folder's whole recycle bin. Moving it would take every user's deleted items out of the bin. Nothing was moved.`, suggesting `restore_from_recycle_bin`), for the reason a restore of the bin itself is refused; what DSM does with it is unprobed. Items inside the bin aren't refused here. `copy_files` takes a shared folder: DSM copies its contents into a new folder of that name, and the share stays.

---

### 14. `delete_files`

Delete files or folders.

**Permission tier:** WRITE

**Annotations:** `destructiveHint=true`.

**DSM API:** `SYNO.FileStation.Delete` / `start` (async) or `delete` (sync)

**Tool description (LLM-facing):**
> Delete files or folders. A folder is deleted with everything in it (`recursive`, default true). On
> a share with a recycle bin, deleted items go to the bin (empty folders aren't kept):
> list_recycle_bin shows them and restore_from_recycle_bin brings them back. Without a bin, and for
> anything inside a bin (`/<share>/#recycle/…`), deletion is permanent. Paths are exact, not
> wildcard patterns, and if any path is missing nothing is deleted. A whole shared folder
> (`/video`), recycle bin (`/video/#recycle`) or user's home folder (`/homes/<user>`) is refused:
> delete its items by path. So is a path with a `.`, `..` or empty part, or a NUL. A long delete
> keeps running on the NAS and returns a handle for file_task.

**Parameters:**

| Name | Type | Required | Default | Description |
|------|------|----------|---------|-------------|
| `paths` | `list[str]` | **Yes** | — | File/folder paths to delete. Exact paths: wildcards aren't expanded, and a missing path refuses the whole call. A whole shared folder or recycle bin, or a path with a `.`, `..` or empty part or a NUL, is refused (below). A whitespace-only part is a name like any other (#488). |
| `recursive` | `bool` | No | `true` | Delete non-empty folders recursively. With `false`, a folder that isn't empty is refused and left in place. |

**A whole shared folder is refused (#451).** On DSM 7.2.2, 7.3.2 and 7.4.1, File Station's `Delete` on `/<share>` deletes the shared folder itself, for any account with write access (admin or not): it disappears from `list_share` for everyone, and the recycle bin, which lives inside the share, can't bring it back. DSM 7.1 refuses it (900 for a non-admin, 407 for admin). Verified on vdsm, DSM 7.1.1-42962 Update 9, 7.2.2-72806 Update 10, 7.3.2-86009 Update 5 and 7.4.1-90080 Update 1 (probe results on #451). So `delete_files` refuses a path that names a whole shared folder (`/video`, `video/`, ` /video`, `/video//`) as `invalid_parameter`, `param: paths`, before any delete is sent (the tool may first refresh the share list): `Delete files refused: '/video' is a whole shared folder. …` (the message says DSM 7.2.2 to 7.4.1, tested, and likely later), suggesting `list_files` and deleting its items by their own paths (leaving out its `#recycle`, #479) after the user agrees, or DSM's Control Panel to delete the shared folder itself (`SYNO.Core.Share` `delete`, administrator-only, isn't exposed). The part rules every tool shares refuse a `.`, `..` or empty part and a NUL first (Path Handling, #510: `/video/.`, `/video/x/..`, `//video`, a dot padded with any whitespace; a NUL because DSM cuts the path there, so a delete of `<dir>/a<NUL>x` deleted `a`, vdsm DSM 7.1 and 7.4.1), with the generic messages (`Invalid path '…': '.' and '..' parts aren't allowed.`, `…an empty part (`//`) isn't allowed.`, `…a NUL character isn't allowed. …`). They run on the paths as given, before the share check; the whole-shared-folder check runs after it, so a one-part path that isn't a share (`/vidoe`) is an unknown share, not a shared folder. A whitespace-only part was refused here too until #488: the path normalization stripped it (`/video/x/ ` was sent as `/video/x`, deleting the folder: QA round 2). Paths are taken exactly now, so `/video/x/ ` names an item `" "` in `/video/x` and goes to the missing-path check like any other. DSM refuses `.` and `..` itself, but the refusal doesn't rest on it: on vdsm (all four versions, 2026-10-30; output on #451), `delete_files` before this change refused `/writable/.` and `/writable/probe-sub/..` (an existing folder) through its missing-path check, since `getinfo` answers 418, as the test user; and a raw `Delete start` on `/writable/.` failed with a File Station error ("Unexpected server error"), as the test user and as admin. The shared folder stayed every time. Emptying a shared folder safely, with the user's confirmation, is #484 (a first design, in #480, was dropped for this refusal; the issue keeps its lessons).

**A whole recycle bin is refused (#479).** File Station's `Delete` on `/<share>/#recycle` removes the bin folder itself, with everything in it, other users' deleted items included, for an administrator and for a non-admin with write access to the share alike; the share's bin setting stays on, and the next delete on that share re-creates `#recycle` (owner root, mode 777) with the deleted item in it (seska, DSM 7.1.1-42962 Update 9, 2026-10-31, two test shares; probe results on #479; and on vdsm, all four versions, in `/home`'s bin, the only vdsm share with one: #409's test, `tests/vdsm/test_vdsm_recycle.py`, which now removes the bin with a raw `Delete` and checks that `delete_files` refuses it). So `delete_files` refuses `/<share>/#recycle` (any spelling that normalizes to it) as `invalid_parameter`, `param: paths`: `Delete files refused: '/video/#recycle' is the shared folder's whole recycle bin. Deleting it would permanently remove everything in the bin, other users' deleted items included. Nothing was deleted.`, suggesting deleting the items `list_recycle_bin` lists by their own paths after the user agrees, or DSM's Control Panel to empty the bin. Items inside the bin are still deleted, permanently, as before. **A user's home bin too (pre-QA):** the user home service gives each user a bin, which the user sees as `/home/#recycle` and an administrator as `/homes/<user>/#recycle`, the same folder (vdsm, all four versions: what the test user deletes into `/home/#recycle` is listed under `/homes/mcptest/#recycle`, `test_a_users_home_bin_is_homes_user_recycle`). A home folder's bin holds only its user's items, so its refusals say so: the caller's own `/home/#recycle` is `your home folder's whole recycle bin` (deleting it would `permanently remove everything in the bin`; moving it `would take your deleted items out of the bin`), with no Control Panel advice. And `/homes/<user>/#recycle` is refused the same way, by `delete_files`, `move_files` and `rename`: `Delete files refused: '/homes/alice/#recycle' is the whole recycle bin of alice's home folder (alice's /home/#recycle). Deleting it would permanently remove everything in the bin. Nothing was deleted.`, suggesting `list_files` and deleting its items by their own paths. Since #610 that refusal says deleting would remove everything in the bin `into /homes's own recycle bin if that's on, otherwise permanently`, and its suggestion says where kept items go and that deleting them there removes them for good.

**A failure partway (#480).** A multi-path delete that fails partway (a DSM error on a later item's start, status or task; a 599 on a status read counts as done if the path is gone, and otherwise hands off, #521) gets `param: paths` and the failed path as `value`, appends `Failed on: <path>.` when the message doesn't already name that path itself (DSM's error on a start or a status request names no path, and one naming a path inside it doesn't name it), then `Already deleted before this failure: …` and `Not started: …`, and isn't retryable once something was deleted (the same call again is refused, since the deleted paths are missing): the suggestion keeps the cause's own advice and adds to retry only the paths not deleted, checking the failed one with `list_files` first unless that advice already says to check with `list_files` (a start with no answer, a path not found when the delete ran). A start that gets no answer (a transport error) when other paths were deleted or are still to come is `timeout` (for a timeout) or `unavailable`, not retryable: `Delete files failed: DSM didn't answer for /video/b (ReadTimeout); its delete may have started.`, with the same two lists; alone, it surfaces as the server's usual timeout or connection error, as before. (A status with no answer hands the item off, as any delete does.) A copy, move or restore start with no answer in a multi-path call says the same, with its own verb (#588); its other failures on a later start are #482.

**Response shape (recycle bin enabled):**

```
✓ Deleted 3 items:
  🎬 /video/Downloads/old_video.mkv (2.1 GiB)
  📄 /video/Downloads/old_video.srt (38 KiB)
  📁 /video/Downloads/temp/ (3 items, 156 MiB)

Recycle bin is enabled on /video — files can be recovered with list_recycle_bin and restore_from_recycle_bin. Empty folders aren't kept in the bin.
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
- **Deleting from the bin is permanent (#220):** a path inside `/<share>/#recycle` is treated as permanent whatever the share's setting, because DSM keeps no second copy (seen on DSM 7.1.1: `list_recycle_bin` showed 0 items afterward). Such a share isn't probed for its recycle status unless the call also deletes paths outside its bin.
- **A user's home bin through `/homes` follows `/homes`'s own bin (#610):** an administrator reaches a user's home bin as `/homes/<user>/#recycle` (#479), and to DSM that's a folder inside the `homes` share. A delete there goes into `/homes`'s own bin when that's on: seska (DSM 7.1.1), found in the 0.11.0rc2 live test, where the reply said permanent and the items were in `/homes/#recycle/<user>/#recycle/…`. It's permanent when that bin is off: vdsm, all four versions, where `homes` has its bin off (`SYNO.Core.Share set` answered 403 for turning it on, on DSM 7.1 and 7.4.1), so only the permanent case runs there. So `/homes`'s bin is checked once per call (`probe_recycle_status`, the cached answer when there is one), and that one answer covers any other `/homes` path the same call deleted, so the reply never says both "can be recovered" and "couldn't check" about `/homes`. On, the reply says `Deleted`, where the items went (listing each at its new path), that `list_recycle_bin share=homes` with a pattern finds them and `restore_from_recycle_bin share=homes` brings them back with a `dest_folder` outside the bin (a restore into the user's bin is refused), that empty folders aren't kept, and that deleting them in `/homes/#recycle/<user>/#recycle` removes them for good. Off, `Permanently deleted`. A probe that can't tell (no answer, a DSM error other than 408, or no `/homes/#recycle` with `/homes`'s own setting unreadable: after the delete that most likely means off, as #409 reads it for other shares, but it isn't proven, and this check never guesses) says it couldn't check: unlike other shares' hopeful default (#255), never `can still be recovered`. `/homes/#recycle/…` itself is `/homes`'s own bin: permanent, as above.
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

A copy, move or restore also lists any source DSM skipped before this one, on a line of its own, never under `Already moved` (#585): `Skipped in whole or in part, as an item of that name is already at the destination (not compared): /v/b/x.` A move or restore adds that a skipped file is still at the source and a skipped folder keeps the items skipped, since DSM's skip on a folder means only some of its items. The task record keeps the list, so the final result (after `Before this task:`), a failed task and the status, stop and no-such-task replies say it too, with what was already done.

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
> Follow up on a delete, copy, move or restore handed off to the NAS. Those tools hand
> off long operations and return a handle such as `delete:FileStation_…`, because the NAS keeps
> working after the reply. With a handle: `action="status"` shows progress, or the final result
> if DSM still has it; `action="stop"` stops it, and the operation may then be partly done. Without a
> handle: lists this user's running operations, which is useful in a new chat or after a restart.
> Handles only work for the same NAS user. Don't poll in a loop: check when the user asks, or before
> doing something that depends on the result.

**Parameters:**

| Name | Type | Required | Default | Description |
|------|------|----------|---------|-------------|
| `handle` | `str` | No | `None` | The handle from a delete/copy/move/restore reply. Omit it to list running operations. |
| `action` | `"status"` or `"stop"` | No | `"status"` | `status` (default) or `stop`. |

**Response shapes:**

- **No handle:** a table of this user's running Delete/CopyMove tasks (handle, operation, path(s), started, progress), or `No File Station delete, copy or move tasks are running for this user.` On a DSM without `BackgroundTask` `list` (102-104), it says listing isn't available and to use the handle from the reply.
- **status, running:** `Still deleting on the NAS: /Backups/X (124,219 of 200,400 items, 62%; running for 14 min). Handle: ...` (the running time comes from the task ID, which begins with the start time in epoch seconds: in decimal on the real NAS (`FileStation_1792248483…`), in hex on vdsm 7.1 (`FileStation_6AD3911E…`). An ID of any other shape, or a time in the future or over 30 days old, gives no running time. `BackgroundTask` `list`'s `crtime` isn't the start: on a real NAS it read about 9 minutes after a 1 TB delete started, so it's not used, and the list's Started column comes from the task ID too, `unknown` otherwise). It says an unchanged count doesn't mean the task is stuck (for a delete: progress counts finished items, and one large file can take many minutes), that it keeps running without anything more, that stopping and starting again doesn't make it faster, and how to stop it if the user wants it canceled.
- **status, finished:** the original tool's result (deleted paths and recycle-bin wording, copy/move/restore skips and errors) when the server holds a record for the handle (from the handoff or from discovery), except that a copy, move or restore names the sources removed rather than saying a folder's were, and says what was done or skipped before this task (#585); otherwise a generic result from DSM's final status (`Delete finished: 200,400 items processed.`, any DSM errors and skips) and a suggestion to list the path. DSM forgets a finished task after one read, so the final status is kept in the record.
- **599 while a call waits on its own task (#521):** seen twice on vdsm's DSM 7.2.2 CI runs as a Delete's status read. The stop that followed logged `Unknown error`, the text for File Station's 401, and a Delete stop answers 401 for any task DSM doesn't have, so that doesn't show it finished. Not reproduced in 840 tasks, so the cause isn't known (#586). It isn't a verdict: the status is read up to 3 times in all, stopping after about 10 s if its answers are slow, and if it clears the wait goes on. A wait that ends during those reads counts as lost too. After that, a Delete's path is checked, within 5 s: gone means done, reported as a normal delete. A cancel during that check stops the task. Otherwise, or with no clear answer, it's handed off: the reply says how it ended isn't known, with the handle, what's done, what wasn't started, which it says to run after checking this one (a delete's path; a copy, move or restore's destination, and a move or restore's source, since what's there may be only part of it), and that `file_task` with no handle lists what's running. A copy, move or restore gets that same reply. Nothing is stopped. Each 599 is logged at WARNING with the task ID, so a run's log keeps it.
- **status, unknown handle (599):** the remembered final result if there is one. Otherwise, with a record (a task that finished with no status read to catch it, one that was stopped, or one DSM keeps answering 599 for): `DSM has no task for the delete of /Backups/X: it may have finished, failed, been stopped or still be running. Call file_task with no handle to see what's running. List the path to see what's left.` For a copy, move or restore, it says to list the destination, and for a move or restore the source, as what's there may be only part of it. Without one: `DSM has no task with this handle: it may have finished, failed, been stopped or still be running, or have been started by a different DSM user. Call file_task with no handle to see what's running. List the path to see what's left.` A 599 is read 3 times in all first, stopping once a read returns after about 10 s, as in a call's own wait (#521), so one 599 can't drop a stop; with a final result remembered, one read is enough, since that result can't change.
- **stop without a handle** (omitted, empty or blank): `invalid_parameter` (param `handle`) saying stopping needs a handle, with a suggestion to call `file_task` without a handle to list the running operations. Nothing is listed or stopped, and DSM isn't called.
- **stop:** reads the task's `status` first, because CopyMove `stop` answers success for any task ID: finished (read or not), forgotten, made up or another user's (vdsm 7.1 and 7.2.2). Unknown (599): with a final result remembered, that result, prefixed with `That task had already finished, so nothing was stopped.` Otherwise a stop is sent anyway, since the reads may be wrong and a task ID is unique (another user's task isn't touched, on vdsm), and the reply is what `status` gives plus `A stop was sent, but DSM can't confirm it.`; refused, it goes as below. Finished: `That task had already finished, so nothing was stopped.` and the final result, which is kept in the record. Still running: `stop`, then `Stopped <kind> of <path>; it may be partly done. List the path to see what's left.` A stop answered with 401 or 599 reads the status again: finished gives the finished reply above, no such task gives `status`'s reply plus `A stop was sent, but DSM can't confirm it.`, and still running says DSM didn't accept the stop, to try again, and if DSM keeps refusing, to stop it in DSM's File Station.
- **DSM doesn't answer (transport error):** a `timeout` (or `unavailable`) error naming the handle, saying for the status read that nothing was stopped (after a stop DSM refused, that it wasn't accepted), or for the stop that it may or may not have taken effect, and suggesting `file_task` status or listing the path (#237).
- **An HTTP error status or a non-DSM reply (#282), or a 414 (#325):** an `unavailable` error naming the handle: ``Check task failed for `<handle>`: …``, or for a stop ``Stop task failed for `<handle>`: …``, ending `Nothing was stopped.` when the status read failed (`The stop wasn't accepted.` after a stop DSM refused), or when the stop request itself got a 414 (rejected before anything acts on it), and otherwise `The stop may or may not have taken effect.` when the stop request failed. `retryable` is the error's own: true for a 5xx or a non-DSM reply, false for a 4xx such as a proxy's 403.

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
| 408 | No such file/directory | Generic: "No such file or directory"; `copy_files`, `move_files`, `delete_files`: "… refused: not found: …". `list_files`: "… {path} doesn't exist on the NAS." (`not_found`); on a file: "… {path} is a file, not a folder." (`invalid_parameter`); on a folder `getinfo` then sees: "… DSM couldn't list {path} (DSM error 408), though it's a folder that exists." (`filestation_error`, retryable) | Use `list_files` or `search_files` to find the correct path; `get_file_info` for a file; retry for a folder that exists. |
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

Module caches recycle bin status per share at initialization. `delete_files` response adapts based on status. Two convenience tools added: `list_recycle_bin` (READ) and `restore_from_recycle_bin` (READ). `#recycle` is hidden from `list_files` results only when `hide_recycle_in_listings` is on (off by default). User treats delete as done — recycle bin info is a safety net.

### 2. Batch Move Confirmation ✓

Atomic tools with search → review → move pattern taught via MCP server instructions. `search_files` gains `exclude_pattern` for client-side filtering. Skills can encode per-user rules for power users but aren't required.

### 3. Human-Readable File Sizes ✓

Size parameters accept `int` (bytes) or `str` (`"500MiB"`, `"2GiB"`, `"1.5TiB"`; `KB`/`MB`/`GB`/`TB` mean the same binary units). Case-insensitive, decimals allowed. Documented in MCP server instructions.

### 4. Tool Description Strategy ✓

Detailed per-tool descriptions (~200 words max), shared knowledge in `MCPServer(instructions=...)`, no cross-tool repetition.

---

## Remaining Open Items

1. **Emoji vs text markers:** Should output use emoji (📁🎬📄) or plain text ([DIR]/[FILE])? Configurable via `file_type_indicator` module setting — defaults to emoji.
