# Using mcp-synology with Meta Muse

[Meta Muse](https://www.meta.ai/) is an agent that runs in its own VM. It has no MCP client
built in, so it connects to an MCP server by writing its own. Left to itself it usually
writes a script that starts `mcp-synology serve` for each tool call. That works, but every
call then pays a process start and a DSM login (about 25–30 s per call in our live tests),
and the model never sees what a normal MCP client keeps from the connection: the tool list
with its descriptions and parameter schemas, and the server's instructions.

Ask Muse to run the server as **one long-lived session** instead. In our live tests this
took reads from about 25–30 s to about 1–2 s after the first call, and DSM logins from two
per call to one per session. Paste this into Muse, filling in the two paths:

```text
Set up mcp-synology as one long-lived MCP session, not a process per call.

- Write a small daemon in Python with the official `mcp` client SDK. It starts
  `<venv>/bin/mcp-synology serve --config <config.yaml>` once as a stdio child, calls
  initialize once, and holds that one ClientSession. One daemon per config file.
- Give it a state directory under ~/.local/state/ (not /tmp; directory 0700). Send the
  server's stderr to server.log there. Talk to the daemon over a Unix socket in that
  directory (0600), one JSON line per request: {"tool": "...", "arguments": {...}},
  replying {"is_error": bool, "text": "..."}. Pass the server's text through unchanged.
  Allow {"calls": [...]} to run independent reads concurrently, in order.
- Pass credentials only in the child's environment (SYNOLOGY_USERNAME,
  SYNOLOGY_PASSWORD), fetched once per daemon start. Never write them to disk.
- Before taking requests, make one read-only warm-up call (for example list_shares), so
  parallel first calls don't each log in.
- After initialize, write catalog.md: the server's instructions and every tool with its
  description and parameters. Read it before using the tools, and check each request's
  arguments against the tool's inputSchema before sending it.
- Use a client timeout of 660 s: some calls wait up to 600 s before handing a long
  operation off with a handle (follow it with file_task or photo_task).
- Restart the daemon if the server exits, or if the venv's `mcp-synology --version` or
  the config file changes. Never replay a write that failed or was cut off; check with a
  read instead.
- Shut down after 30 minutes idle. On every shutdown, send the server SIGTERM (or close
  its stdin) and wait up to 6 s before killing it: it logs out of DSM on the way out.
```

Tested with Muse on its free plan (model: Muse Spark) against mcp-synology 0.10.0's
release candidates on a real NAS. Other agents that run tools from scripts benefit from the
same approach.
