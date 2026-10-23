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
real output:

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

## Clients tested, and how they differ

Run at least two clients. They differ in ways that change what a test can show.

| Client | How it reaches the server | What to know |
|---|---|---|
| **Claude Code** | Starts the server itself (stdio); one server process for the session | Shows the server's instructions to the model. No reply time limit. Fast: after the first call, a read costs one DSM round trip. Good for checking instructions, long operations (`file_task` hand-offs) and parallel calls |
| **claude.ai, through a local device bridge** (the desktop app) | The app starts the server on the user's machine; claude.ai reaches it through the bridge | **The server's instructions don't reach the model** (shown with blind questions in the 0.10.0rc5 test), so anything the model must know has to be in tool descriptions and replies. Tool titles and annotations are dropped too. A reply is cut off after about 60 s, and the bridge sometimes drops for a minute. Expect a few seconds per call |
| **An agent on a remote machine calling tools one at a time** (a separate script per call) | Each call starts a fresh server process, logs in, makes one call and exits; here over plain HTTP through a VPN | Nothing kept in memory carries over between calls (caches, session). Every call pays a login, so it's slow (about 20 to 25 s per call in the 0.10.0rc5 test). If the harness doesn't show the model the tool schemas, expect retries on argument names (`path` vs `paths`); those are harness errors, not findings. Plain HTTP shows the server's plain-HTTP warnings |

In the 0.10.0rc5 test the three ran the same kind of plan in about 8, 12 and 27 minutes.
None of the difference was the server.

## 1. Build the candidate

Test the exact commit you mean to release, and keep it apart from anything in daily use.

1. Pin the commit: `git worktree add --detach <dir> <sha>`, or clone and check it out.
2. In that copy only, set `version` in `pyproject.toml` to the candidate's (`0.10.0rc5`), so
   `mcp-synology --version` can tell it from an installed release. Don't commit it.
3. `uv build --wheel`, and install the wheel into **its own venv**. Never upgrade a venv that
   something else depends on.
4. Make a **copy** of each config the clients will use, and point the clients at the
   candidate's `mcp-synology` with those copies. Restart each client: the server reads its
   config only at startup.
5. `mcp-synology check --config <copy>` for each one: "Authentication successful!"

When a module is new to a config, use the top-level `allowed_local_paths`, or give that
module its own list. A module's list doesn't reach other modules, and without one a module
falls back to the Downloads folder, which may not exist on that machine.

## 2. Test accounts

One account can't show everything. Use at least an admin, a regular user and a restricted
user. A read-only connection (`permission: read` for every module) can reuse the regular
user.

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
- Its password goes into the keyring with `mcp-synology setup --config <its config>`, run
  in your own terminal, so it never passes through a chat.

DSM's quota counts the files an account **owns**. To test it, the restricted account has to
do the writing: copy something big into the test share from another account, then copy it
again as the restricted account.

## 3. Write the plan

A plan is a list of numbered steps, grouped in sections, each saying what to call and with
what. These rules came from earlier runs:

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
- **Cleanup is the last section**, and deletes only after the maintainer's OK. Leave recycle
  bins alone unless asked.
- **Cover what changed since the last candidate**, then a short regression pass: list,
  search, upload, download (a file, and a folder as `.zip`), rename, copy, move, delete,
  restore, `get_dir_size` on a missing path.

## 4. Run it

- Testers can run in parallel when their scratch names don't overlap. Order still matters
  where they share state: deleting into a recycle bin another tester is about to search
  changes that tester's results.
- Each tester records **exactly what came back**: tool output quoted, errors in full. It
  marks PASS, FAIL or SURPRISE where it has an opinion; triage decides.
- Results go somewhere everyone can read: a shared notes store, or a file per tester.
- Some things can't be tested on every NAS. On a Btrfs volume, a copy within the volume is a
  clone and finishes in seconds, and a delete into the recycle bin is a move, so neither hands
  off to `file_task`. A permanent delete of a large folder (from inside `#recycle`) does.

## 5. Triage

Compare every step with the answer key, then sort what's left:

- **FAIL:** the server did the wrong thing. Fix it before the release.
- **SURPRISE that's a product problem:** a reply that misleads the model or the user (a hint
  that names a parameter the tool doesn't have, an error that hides a partial result). Fix
  it now, or file it, and record the decision.
- **SURPRISE that's DSM's behavior:** document it in the module spec (and in the DSM API
  notes #270 plans, once they exist).
- **The client or the harness:** not a server issue. Note it here, under the client table,
  if it's new.
- **Known issues:** list the open ones in the answer key, so they aren't counted again.

A real-NAS finding that vdsm can reproduce gets a vdsm test, so CI checks it on every DSM
version from then on (#365's `list_files` on a folder the test user can't read is one).

Then delete the test data (with the maintainer's OK), and record what was removed.
