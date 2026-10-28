# Live testing a release candidate

CI tests every module against a virtual DSM of each supported version (see the
[README](../README.md)). That shows DSM does what the code expects. It can't show what a
model does with the tools: whether a description steers it to the right call, whether a
reply says what happened, or whether a hint names something the model can act on. A live
test answers that. One or more AI clients drive a release candidate against a real NAS,
following a written plan, and report what each tool said.

This page is the run-book: how to build a candidate, set up test accounts, write a plan,
run it with several clients at once, and triage what comes back. It's how each mcp-synology
release is checked before it ships.

## What live tests have found

Each of these passed the unit tests and the vdsm suite, and was found by a model reading
real output. "Found" names the release candidate under test (not a published release):

| Found | Problem | Fixed in |
|---|---|---|
| 0.10.0rc3 | `get_dir_size` of a missing path answered 0 B | #332 |
| 0.10.0rc3 | The Recycle Bin column of `list_shares` appeared only after a recycle-bin check, and said `unknown` for every other share | #358 |
| 0.10.0rc4 | Photos taken times were labeled UTC, but they're the camera's local clock | #342 |
| 0.10.0rc4 | A Download Station size DSM doesn't know showed as `0 B` | #355 |
| 0.10.0rc5 | `get_schedule`'s grid put each hour's cell left of its label | #360 |
| 0.10.0rc5 | `list_recycle_bin` said "Use offset=N to see more" but had no `offset` | #364 |
| 0.10.0rc5 | File Station's 407 (a permission refusal) read as "Unknown error" | #365 |
| 0.10.0rc5 | A copy stopped by a quota gave a bare code, and didn't say it had left 18 GiB at the destination | #366 |
| 0.10.0rc6 | Five local-path parameters sent the model to the server instructions for the allowed folders, which claude.ai never receives | #390 |
| 0.10.0rc6 | `get_system_info` promised the CPU, which only an administrator account gets | #390 |
| 0.10.0rc6 | A later page of a listing said "Showing 2 of 5 items" without saying which two | #390 |

## Clients tested, and how they differ

Run at least two clients. They differ in ways that change what a test can show. What
follows is what we observed in the 0.10.0rc5 test; clients change, so check again.

| Client | How it reaches the server | What we observed |
|---|---|---|
| **Claude Code** | Starts the server itself (stdio); one server process for the session | The server's instructions reached the model. No reply time limit. Fast: after the first call, a read cost one DSM round trip. Good for checking instructions, long operations (`file_task` hand-offs) and parallel calls |
| **claude.ai, through a local device bridge** (the desktop app) | The app starts the server on the user's machine; claude.ai reaches it through the bridge | **The server's instructions didn't reach the model** (shown with blind questions), so anything the model must know has to be in tool descriptions and replies. Tool titles and annotations were dropped too. Replies were cut off after about 60 s, and the bridge dropped once for about a minute. A few seconds per call |
| **An agent on a remote machine calling tools one at a time** (a separate script per call) | Each call starts a fresh server process, logs in, makes one call and exits; in our setup over plain HTTP through a VPN | Nothing kept in memory carried over between calls (caches, session). Every call paid a process start and a login, so it was the slowest of the three. The harness didn't show the model the tool schemas, so it retried on argument names (`path` vs `paths`); those are harness errors, not findings. Plain HTTP showed the server's plain-HTTP warnings |

Speed depends on the setup (network, VPN, the client), so it isn't recorded here. We saw
nothing to attribute the differences to the server; per-call overhead explains them.

## 1. Build the candidate

Test the exact commit you mean to release, and keep it apart from anything in daily use.

1. Pin the commit: `git worktree add --detach <dir> <sha>` (then work in `<dir>`), or clone
   and check it out.
2. In that copy only, set `[project].version` in `pyproject.toml` to the candidate's
   (`0.10.0rc5`), so `mcp-synology --version` can tell it from an installed release. Don't
   commit it.
3. `uv build --wheel`, and install the wheel into **its own venv**. Never upgrade a venv that
   something else depends on.
4. Make a **copy** of each config the clients will use (the format is in
   [`config-schema-spec.md`](specs/config-schema-spec.md); [`examples/`](../examples/) has
   samples). Point each client's MCP server command at the venv's `bin/mcp-synology serve
   --config <copy>`, and restart the client: the server reads its config only at startup.
   - **A copy for a different DSM account needs its own `instance_id`** (and `alias`).
     Credentials are kept in the keyring per `instance_id`, so a copy that keeps the
     original's shares its credentials, and running `setup` on it replaces the original's.
     A copy for the same account can keep it.
5. `mcp-synology check --config <copy>` for each one: "Authentication successful!"

When a module is new to a config, use the top-level `allowed_local_paths`, or give that
module its own list. A module's list doesn't reach other modules, and without one a module
falls back to the Downloads folder, which may not exist on that machine.

## 2. Test accounts

One account can't show everything. Use at least an admin, a regular user and a restricted
user. A read-only connection (`permission: read` under `modules.<name>` for every module)
can reuse the regular user.

**The restricted test account** finds the permission and quota problems. Create it in DSM:

- **Not** in `administrators`.
- Applications: **File Station**, **Download Station** and **Synology Photos** allowed.
- Read/write on **one test share** (say `Test-Resources`, with its recycle bin on) and no
  rights on the others. DSM still lists every share; inside the others it answers 407.
- A **folder it's denied**: in DSM's File Station, the folder's Properties → Permission →
  Create → the account → Type **Deny**, Apply to **All**, every permission ticked. Listing it
  gives 407, not 105.
- A **quota** (say 20 GB) on the test share's volume. A copy that runs past it fails with 415
  part-way, and leaves what it wrote.
- Its password goes into the keyring with `mcp-synology setup --config <its config>` (a
  config with its own `instance_id`; see step 4 above). `setup` asks for the username and
  password; run it in your own terminal, so the password never passes through a chat.

DSM's quota counts the files an account **owns**, so the restricted account has to do the
writing. Copy a large folder into the test share as another account; then, as the
restricted account, copy it to another folder in that share. The new files are its own,
and the copy runs into the quota.

## 3. Write the plan

A plan is a list of numbered steps, grouped in sections, each saying what to call and with
what. Whoever runs the release (the maintainer, below) writes the plan and keeps the answer
key. Start from [the template](live-test-template.md), which also has example steps from
earlier rounds and the answer key's format. These rules came from earlier runs:

- **No expected answers in the plan.** Keep them in a separate answer key that testers are
  told not to read. A tester who reads "Expected: …" before acting can't show what the
  server's text alone would make it do.
- **Ask the instruction questions first**, before the tester has read anything else: "Which
  tools aren't available on this connection, and how would I turn them on?", "What time is it
  on the NAS, and what's that in UTC?", "Where can downloads be saved on this computer?". A
  model that has the server's instructions answers from them; one that doesn't, guesses.
- **Starting takes one message.** The plan says everything else, including where to report.
  Tag each section with the testers who run it.
- **Every tester gets its own scratch names**: a NAS folder, a local folder, Photos folder and
  album names, its own Download Station tasks. Only one tester changes NAS-wide settings
  (Download Station config and schedule), and restores them afterward.
- **Each step fits in under 60 s** for clients with a reply limit. Use small files for
  routine steps. Large operations go to a client without the limit.
- **No secrets in the plan.** Write "any throwaway password", not a literal one: a notes
  store may redact it, and the tester then uses the redacted text.
- **Give every key a config needs.** A tester that patches its own config to get past a step
  hides a setup gap.
- **Cleanup is the last section.** It starts only when every tester is done or stopped, even
  for items only one tester used: a delete can change shared state, such as a recycle bin,
  that another tester is reading (it did in the 0.10.0rc6 test). It deletes only after the
  maintainer's OK. Leave recycle bins alone unless asked.
- **Cover what changed since the last candidate**, then a short regression pass: list,
  search, upload, download (a file, and a folder as `.zip`), rename, copy, move, delete,
  restore, `get_dir_size` on a missing path.

## 4. Run it

- Testers can run in parallel when their scratch names don't overlap. Order still matters
  where they share state: deleting into a recycle bin another tester is about to search
  changes that tester's results.
- Each tester records **exactly what came back**: tool output quoted, errors in full. It
  marks PASS, FAIL or SURPRISE where it has an opinion; triage decides. A SURPRISE counts
  against the round, not as a pass: report a round as "N pass, M fail, K surprises".
- Results go somewhere everyone can read: a shared notes store, or a file per tester.
- Some things can't be tested on every NAS. On our Btrfs volume (DSM 7.1.1), a copy of
  tens of GiB within the volume finished in seconds (a clone, most likely), and a delete into
  the recycle bin is a move, so neither handed off to `file_task`. A permanent delete of a
  large folder (from inside `#recycle`) did.

## 5. Triage

Compare every step with the answer key, then sort what's left:

- **FAIL:** the server did the wrong thing. Fix it before the release.
- **Every SURPRISE means the behavior or its documentation failed**, so each one gets a fix,
  an issue or a recorded decision. "By design" is an answer only when the tool's own text
  says so; when it doesn't, that's a documentation fix. Sort each one:
  - **Behavior:** a reply that misleads the model or the user (a hint that names a parameter
    the tool doesn't have, an error that hides a partial result). Fix it now, or file it.
  - **Documentation:** the tool did what the code means, but its description, its
    parameters' descriptions or its reply didn't say so. Fix the text. Check too whether the
    tester read the tool's documentation at all; if it didn't, that's also a client or
    harness finding.
  - **DSM's behavior:** document it in the module's spec under [`docs/specs/`](specs/), and
    in the tool's own text when the model needs to know it.
- **The client or the harness:** not a server issue. Note it here, under the client table,
  if it's new.
- **Known issues:** list the open ones in the answer key, so they aren't counted again.

A real-NAS finding that vdsm can reproduce gets a vdsm test, so CI checks it on every DSM
version from then on (#365's `list_files` on a folder the test user can't read is one).

Then delete the test data (with the maintainer's OK), and record what was removed.

## Comparing models

A live test varies the client. To compare models, hold the client fixed instead. The
README's [Tested models](../README.md#tested-models) results come from the run recorded
here; repeat it for a new release or a new model.

### Setup (2026-10-28)

- **Server:** mcp-synology 0.10.0, run from a checkout with all four modules enabled and
  write permission. One allowed local folder (`<local>` below) holds three JPEGs:
  `beach/beach-sand.jpg`, `beach-sea.jpg` and `beach-sunset.jpg`.
- **NAS:** the vdsm DSM 7.2.2 golden image (bake v8, Synology Photos 1.9.1), as the
  non-admin `mcptest` user, so the files the tasks name are the bake's (`/testshare/...`).
  `/writable` has no recycle bin. The image was restored fresh for each model. Haiku's
  rewritten tasks (t04b, t08b) then ran on the same NAS as its first attempts, which had
  left an album named "Haiku Beach" behind.
- **Client:** Claude Code 2.1.312, headless, one fresh session per task, with no tools but
  this server's. The model gets the server's instructions and full tool schemas, and
  can't read local files. Each task's prompt goes in on stdin:

  ```bash
  claude -p --model <model> --output-format stream-json --verbose \
    --setting-sources project --strict-mcp-config --mcp-config mcp.json \
    --allowedTools mcp__synology --tools "" < task.txt
  ```

  `mcp.json` names the server `synology`, matching `--allowedTools`. The config is an
  ordinary one with the connection, the allowed local folder and the four modules:

  ```json
  {
    "mcpServers": {
      "synology": {
        "command": "uv",
        "args": ["run", "--directory", "<checkout>", "mcp-synology", "serve",
                 "--config", "<config>.yaml"],
        "env": {"SYNOLOGY_USERNAME": "mcptest", "SYNOLOGY_PASSWORD": "<password>"}
      }
    }
  }
  ```

- **Order:** the tasks run in the order below, on one NAS, because later tasks use what
  earlier ones made (t06 deletes t05's copy; t09 uses t08b's album; t11 manages t10's
  download).

### Tasks and grading

A task **passes** when the model does what was asked with the tools or, where the tools
can't, says so accurately and doesn't claim otherwise. It **fails** when the answer is
wrong, or when the model leaves undone something the tools could do. The model's reply and
its tool calls were read for each task. Both models got the same prompts, word for word;
the names in them (`haiku-test`, "Haiku Beach") come from the first run, which was Haiku's.

| # | Task (shortened; exact prompts below) | What a pass needs | Sonnet 5.5 | Haiku 5.5 |
|---|---|---|---|---|
| t01 | NAS model, DSM version, RAM | VirtualDSM, DSM 7.2.2-72806, 2 GiB | ✅ | ✅ |
| t02 | How busy is the CPU? | Non-admin: says an administrator account is needed | ✅ | ✅ |
| t03 | Which shares can I access? Find every `.mkv` and its size | 3 shares; `/testshare/Media/sample.mkv`, 4 B | ✅ | ✅ |
| t04b | Find the Bambu Studio config file: path and contents | `/testshare/Documents/Bambu Studio/config.txt`; says it can't show the text (no tool returns file text, local reads off) | ✅ ¹ | ✅ ¹ |
| t05 | Create `/writable/haiku-test`, copy `report.txt` in, rename the copy, list the folder | `report-copy.txt` (53 B) in the folder | ✅ | ✅ |
| t06 | Delete `report-copy.txt` (confirmed); did it go to a recycle bin? Restore it if so | Permanently deleted, no bin on `/writable`, nothing to restore | ✅ | ✅ |
| t07 | Download `report.txt` to the local folder; where, how big | Saved there, 53 B | ✅ | ✅ |
| t08b | Upload the three named photos, put them in a new album "Haiku Beach", report item count and taken dates | Album with 3 items; dates 2025-07-04, 07-05, 07-06 as stored | ✅ | ✅ ² |
| t09 | Tag the album's photos "vacation", list that tag, delete the album (confirmed) but keep the photos | 3 tagged; album gone; photos still listed | ✅ | ✅ |
| t10 | Add a download of a thinkbroadband 5 MB test file into `/writable`; show the queue | Task created; status and progress shown | ✅ | ✅ |
| t11 | Pause the thinkbroadband download if running, delete it (confirmed); report speed limits and schedule | Finds the task (its title is `5MB.zip`; the source URL names thinkbroadband), deletes it, reports limits and schedule | ✅ | ❌ ³ |
| t12 | Compress `/testshare/Documents` into a zip on the NAS | Says the NAS has no compress tool; any workaround is stated as one | ✅ ⁴ | ✅ ⁴ |

1. Both found the file and said they couldn't show its contents. That's the harness (local
   file reading off) as much as the model.
2. Haiku's first `upload_photos` call was refused because two albums had that name (one from
   an earlier attempt); it retried with the album's ID.
3. Haiku listed the queue, saw only `5MB.zip`, and said there was no thinkbroadband
   download, so it deleted nothing. Sonnet called `get_download_info`, matched the source
   URL, and deleted it.
4. Both downloaded the folder as DSM's ZIP and uploaded it back, and said that's what they
   did.

<details>
<summary>The exact prompts</summary>

- **t01:** What model of NAS is this, which DSM version is it running, and how much RAM does it have?
- **t02:** How busy is the NAS's CPU right now?
- **t03:** Which shared folders can I access? Then find every .mkv file on the NAS and tell me its size.
- **t04b:** Somewhere on the NAS there's a Bambu Studio config file. Find it and tell me the full path and what's in it.
- **t05:** Create a folder /writable/haiku-test, copy /testshare/Documents/report.txt into it, rename the copy to report-copy.txt, then list what's in /writable/haiku-test.
- **t06:** Delete /writable/haiku-test/report-copy.txt (I confirm). Then tell me whether it went to a recycle bin, and if it did, restore it and show the folder again.
- **t07:** Download /testshare/Documents/report.txt to <local> on this computer, then tell me where it was saved and how big it is.
- **t08b:** Upload these three photos to my personal Synology Photos space: <local>/beach/beach-sand.jpg, <local>/beach/beach-sea.jpg and <local>/beach/beach-sunset.jpg. Put them in a new album called 'Haiku Beach', and tell me how many items the album has and the date each photo was taken.
- **t09:** In Synology Photos, tag every photo in the 'Haiku Beach' album with 'vacation', then list the photos tagged 'vacation'. After that, delete the album itself but keep the photos (I confirm), and show that the photos are still in my library.
- **t10:** Add a download of http://ipv4.download.thinkbroadband.com/5MB.zip into the writable share, then show me my downloads with their status and progress.
- **t11:** Pause my thinkbroadband download if it's still running, then delete it from the list (I confirm). Finally, tell me Download Station's current speed limits and whether a schedule is on.
- **t12:** Compress /testshare/Documents into a zip file on the NAS.

</details>

### What the run doesn't show

- **t04 and t08 were rewritten** after a first Haiku attempt (t04b, t08b replace them).
  t04 didn't say the file was on the NAS, so the model looked for a local Bambu Studio
  install. t08 named a local folder, which `upload_photos` doesn't take. Only the
  rewritten tasks are scored, for both models.
- **Synology Photos' personal folder already existed** in both scored t08b runs, so
  neither met #436 (an upload to the personal root fails with 117 until that folder
  exists). For Haiku, its first attempt had met it; for Sonnet, a setup check had created
  the folder.
- **One run per task per model.** A rerun can come out differently.
- **Cost and speed aren't compared.** Both change with pricing and load, outside this
  project's control.
