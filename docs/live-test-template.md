# Live test plan: template and examples

A starting point for the plan described in [the run-book](live-testing.md), step 3. Copy the
template, fill in the placeholders (`<like this>`), and replace the example sections with
steps for what changed since the last candidate. The examples after it are steps from earlier
rounds that worked well, each with a note on why.

The plan goes to the testers. The expected results don't: they go in a separate answer key
(format at the end) that testers are told not to read.

## Template

````markdown
# mcp-synology <candidate version> live test (<date>)

The candidate is built from <branch> at **<commit>**. It covers every change since
<previous candidate or release>: <PR numbers>. <N> testers run in parallel, each on its own
scratch items:

| Tag | Tester | Connections | NAS scratch | Local scratch | Photos names |
|---|---|---|---|---|---|
| **[A]** | <client> | `<connection>` (<account>, write), `<connection>` (<account>, read) | `/<share>/mcp-<rc>-a` | `<local folder>/mcp-<rc>-a` | folder `mcp-<rc>-a`, album `mcp-<rc>-a-album` |
| **[B]** | <client> | `<connection>` (<restricted account>, write) | `/<test share>/mcp-<rc>-b` | `<local folder>/mcp-<rc>-b` | folder `mcp-<rc>-b`, album `mcp-<rc>-b-album` |

Run only the sections tagged with your letter. Wherever a step says `<scratch>`, use your
NAS scratch path.

## Rules (everyone)

- **Start only when the maintainer says go.** Each client must be restarted on the
  candidate first.
- **Don't read the answer key** (<where it is>). This plan says what to do, not what should
  happen.
- For every step, record **exactly what came back**: quote the tool output, and for an error
  its whole text. Mark it PASS, FAIL or SURPRISE only where you have an opinion; triage
  decides.
- Run the steps in order. Don't skip a step because an earlier one failed: record the
  failure and go on.
- Touch only your own scratch items. **Ask the maintainer before deleting anything**,
  including at cleanup.
- Only [A] changes NAS-wide settings (Download Station config and schedule), and restores
  them at cleanup.
- Use your config as given. If a step needs a key it doesn't have, record that and go on;
  don't patch it.
- Where a step needs a password, use any throwaway one.
- <Per-client notes, e.g. "[A]: replies are cut off after about 60 s; every step is sized to
  finish well under that. If one is cut off, record that and go on.">

## Reporting

Write your results to <where: a shared notes entry or a file per tester>, and update it as
you go. The report has:
- a table (step, connection, result);
- then every FAIL and SURPRISE, with the exact output quoted;
- then what you cleaned up.

---

## 0. [A] How do you answer? (server instructions, by behavior)

Answer each question as you naturally would, **before** doing anything else in this plan,
and record your first answer verbatim. Don't try to quote or look up any instructions.

- **0a** On `<read-only connection>`: "Which mcp-synology tools aren't available on this
  connection, and how would I turn them on?"
- **0b** On `<connection>`: "What time is it on the NAS right now, and what's that in UTC?"
- **0c** On `<connection>`: "Where can downloads be saved on this computer?"

## 1. [A][B] <First change> (#<PR>)

Setup: <what to create first, in your scratch>.

- **1a** <tool> <arguments>. Quote <what>.
- **1b** ...

## <N>. [A][B] Quick regression

Setup: `create_folder <scratch>`, then inside it the folders `f1` and `f2`. Create the local
files `a.txt`, `b.txt` and `c.txt` (a few lines each) in your local scratch folder, and
`upload_file` all three into `<scratch>`. Quote the upload replies.

- **<N>a** `rename <scratch>/b.txt` to `b2.txt`. `copy_files <scratch>/b2.txt` into
  `<scratch>/f1`. `move_files <scratch>/c.txt` into `<scratch>/f2`. `list_files` each folder.
- **<N>b** `search_files` in `<scratch>` for `b2`, then with `extension="txt"`.
- **<N>c** `download_file <scratch>/f1/b2.txt` to your local scratch folder, then the folder
  `<scratch>/f2` to the same place (it arrives as a `.zip`). List what landed locally.
- **<N>d** `delete_files <scratch>/f1/b2.txt`, then `restore_from_recycle_bin` it.
- **<N>e** `get_dir_size <scratch>`, and `get_dir_size <scratch>/no-such-dir`.

## <N+1>. Cleanup (everyone, last, with the maintainer's OK)

1. List what you'll delete: your NAS scratch folder, your Photos folders and album, your
   Download Station tasks, your local scratch folder.
2. Ask the maintainer.
3. Delete. [A]: restore the Download Station config and schedule you changed.
4. Leave the recycle bins alone unless the maintainer says otherwise.
5. Record what you removed.
````

## Example steps

Each of these came from a real plan (scratch names generalized, steps renumbered), and each
found or proved something the unit and vdsm tests couldn't.

### Test the instructions by behavior, not by asking about them

> **0a** On `<read-only connection>`: "Which mcp-synology tools aren't available on this
> connection, and how would I turn them on?"

The server's instructions list the tools a connection withholds and the config key that
enables them. A model that received them answers with the tool names and
`permission: write`; one that didn't says the server can't do it, or guesses. Asking "what do
your instructions say?" instead invites the model to make something up, or to quote text it
has only from the plan. Questions like this showed that one client never received the
instructions at all. So each tool's description now says what that tool needs, and the
instructions keep only what no tool can say, such as which tools a connection withholds.
That's why the question is still worth asking.

### One step, one change

> **1a** `list_shares`, as the very first File Station call of a fresh connection. Quote the
> whole table and any note under it.
>
> **1b** `list_shares` again. Quote it.

Before #358, the Recycle Bin column appeared only after `delete_files` or `list_recycle_bin`
had checked a share, and then said `unknown` for every other share. A plan that deletes
something or lists a bin first never sees the missing column. Calling `list_shares` before
anything else, then again, tests the one thing that changed.

### Build on what an earlier step returned

> Setup: create `<scratch>/rb2`, upload `lone.txt` into it, `delete_files
> <scratch>/rb2/lone.txt`, then `delete_files <scratch>/rb2`.
>
> **4b** `list_recycle_bin` on your scratch share with `pattern="*.txt"`. Quote it.
>
> **4c** `restore_from_recycle_bin` only `lone.txt`, from the path 4b gave for it, with no
> destination. Quote the reply. Then `list_files <scratch>/rb2`.

The tester has to use the path the server printed, not one the plan supplies, so the step
checks that the reply is usable as the next call's input. Deleting the folder after the file
sets up the interesting case: restoring a file whose original folder is itself in the bin.

### Ask for the edge case on purpose

> **9e** `get_dir_size <scratch>`, and `get_dir_size <scratch>/no-such-dir`.
>
> **9f** `copy_files <scratch>/a.txt` into `<scratch>/missing-dir` (a folder that doesn't
> exist). Quote it.

Models rarely try a missing path on their own, so the plan has to. The first found that
`get_dir_size` of a missing path answered 0 B (#332); the second, that a copy into a missing
folder gave a generic DSM error instead of naming the folder (#371).

### Check that a read-only connection refuses well

> **8a** On `<read-only connection>`, ask, one at a time: pause a download, create an album,
> delete `<scratch>/a.txt`. Record each answer verbatim, and whether you called a tool.

The write tools aren't served, so nothing can go wrong on the NAS. What's being tested is the
model's answer: it should say the tool exists but isn't enabled here, and how to enable it,
not that the server can't do it. Asking in plain words, without naming tools, tests what the
model can work out on its own.

### Size each step to the client

> **5a** `create_download` `<a public test file of known size, about 100 MB>`. Right away,
> `list_downloads` and `get_download_info` on it. Quote the size and progress lines.
>
> **5b** About 30 s later, `get_download_info` again. Quote the size and progress lines.

Each step is one or two calls that return in seconds, so a client with a 60-second reply
limit can run it; to watch longer, add more steps like 5b rather than one step that polls.
A file of known size makes a wrong size obvious: an earlier round found that a download
whose server never sent a size showed as `0 B` (#355), and this step checks the fix. An operation that takes minutes, such as a large copy handing off
to `file_task`, goes in a section for a client without the limit.

## Answer key

One entry per step, kept apart from the plan. Write it from the code at the candidate's
commit, and name where each expectation comes from, so triage can tell a server bug from a
wrong expectation:

```markdown
- **9e** `get_dir_size` of the missing path: an error with code `not_found` naming the path,
  not a size (`filestation/metadata.py`, #332).
- **0a** Names the withheld write tools for each module and `permission: write`, and any
  module that isn't enabled, with how to add it; doesn't say the server can't do it. A model
  without the instructions guesses.
- Known issues, not to count again: #<N> (<one line>).
```
