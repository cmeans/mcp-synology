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
| 0.10.0rc6 | Five local-path parameters sent the model to the server instructions for the allowed folders, which claude.ai never receives | #386 |
| 0.10.0rc6 | `get_system_info` promised the CPU, which only an administrator account gets | #386 |
| 0.10.0rc6 | A later page of a listing said "Showing 2 of 5 items" without saying which two | #386 |

## Clients tested, and how they differ

Run at least two clients. They differ in ways that change what a test can show. What
follows is what we observed in the 0.10.0rc5 test; clients change, so check again.

| Client | How it reaches the server | What we observed |
|---|---|---|
| **Claude Code** | Starts the server itself (stdio); one server process for the session | The server's instructions reached the model. No reply time limit. Fast: after the first call, a read cost one DSM round trip. Good for checking instructions, long operations (`file_task` hand-offs) and parallel calls |
| **claude.ai, through a local device bridge** (the desktop app) | The app starts the server on the user's machine; claude.ai reaches it through the bridge | **The server's instructions didn't reach the model** (shown with blind questions), so anything the model must know has to be in tool descriptions and replies. Tool titles and annotations were dropped too. Replies were cut off after about 60 s, and the bridge dropped once for about a minute. A few seconds per call |
| **An agent on a remote machine calling tools one at a time** (a separate script per call) | Each call starts a fresh server process, logs in, makes one call and exits; in our setup over plain HTTP through a VPN | Nothing kept in memory carried over between calls (caches, session). Every call paid a login, so it was slow (about 20 to 25 s per call). The harness didn't show the model the tool schemas, so it retried on argument names (`path` vs `paths`); those are harness errors, not findings. Plain HTTP showed the server's plain-HTTP warnings |

The three ran the same kind of plan in about 8, 12 and 27 minutes. We saw nothing to
attribute the difference to the server; per-call overhead explains it.

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
