# Security Policy

## Supported versions

mcp-synology is currently on the 0.5.x line. Fixes for security issues
are applied to the latest published version only. Users of earlier
versions should upgrade.

| Version | Supported         |
| ------- | ----------------- |
| 0.5.x   | ✅ security fixes |
| < 0.5   | ❌ upgrade        |

## Reporting an issue

**Please do not file a public GitHub issue for security problems.**

The only supported channel is a **GitHub Private Security Advisory**.
To open one:

1. Go to <https://github.com/cmeans/mcp-synology/security/advisories/new>.
2. Fill in a description, steps to reproduce, and the affected
   version.
3. Submit as a draft advisory. Only the maintainer will see it.

This creates a private thread where the report, any proof-of-concept,
the fix, and disclosure timing can be discussed without exposing the
issue publicly. The private vulnerability reporting feature is
enabled on this repository.

If you cannot use GitHub Private Security Advisories for some reason,
please open a **public** issue titled simply "Security contact
request" — no details — and the maintainer will reach out to arrange
a private channel.

## Please include

- A description of the issue and its impact.
- Steps to reproduce (or a proof-of-concept).
- The version of mcp-synology affected (output of `mcp-synology
  --version`).
- Your operating system, Python version, DSM version, and NAS model
  (auth and File Station behavior is DSM-version-dependent — DSM
  6.x, 7.0, 7.1, and 7.2 each have known quirks).
- Whether the issue is reproducible against a clean
  `pip install mcp-synology`, against the vdsm test harness in
  `tests/vdsm/`, or only against a specific NAS configuration.

## What to expect

- **Acknowledgment** after the maintainer sees the report. Response
  times vary — this is a one-person project.
- **Coordinated fix timeline.** mcp-synology is maintained by one
  person, not a security team. Please be patient.
- **Credit in the release notes** if you'd like it. Anonymous
  disclosure is also fine.
- **No monetary reward.** mcp-synology does not operate a bug bounty
  program. Reports are voluntary contributions to project safety.

## Scope

**In scope**

- Credential-handling issues in the auth manager
  (`src/mcp_synology/core/auth.py`) — OS keyring storage, env-var
  fallback, plaintext-config-file last-resort path, the
  `MCPSynology_{instance_id}_{unique_id}` session-name format, and
  the lazy-keepalive design.
- Session-error retry logic in the DSM client
  (`src/mcp_synology/core/client.py` + `src/mcp_synology/core/errors.py`)
  — DSM error codes 106 / 107 / 119 trigger transparent re-auth and
  exactly-one retry. The codes and the `is_session_error` helper live
  in `errors.py`; the retry path lives in `client.py`
  (`_SESSION_ERROR_CODES` set plus the call sites that consult it).
  Bypasses, missed retry sites, leaked credentials during re-auth, or
  re-auth-on-105 (permission-denied) regressions would be in scope.
- DSM session token leakage — passwords are masked in DEBUG logs;
  regressions where a session token, password, or 2FA OTP appears in
  log output, error messages, or persisted state would be in scope.
- Path-traversal or share-validation issues in File Station tool
  handlers (`src/mcp_synology/modules/filestation/`) — paths are
  normalized and the first component is validated against the cached
  share list; bypasses that let a tool reach outside a share would
  be in scope.
- Argument-injection or unsafe parameter encoding in
  `src/mcp_synology/core/client.py` — comma/backslash escaping in
  multi-path params, query-string construction for DSM API calls.
- MCP tool exposure issues — tools that should be gated by the
  permission tier (READ vs WRITE) but are unconditionally registered,
  or write tools registered without explicit user opt-in.
- Background-task lifecycle bugs in the four async DSM tasks (Search,
  DirSize, CopyMove, Delete) that could leak session resources or
  leave orphan tasks consuming CPU on the NAS — `try/finally` cleanup
  must always call stop/clean.
- Config-loading issues — strict top-level validation, lenient
  module-settings validation, env-var override precedence, and the
  read-only-from-server-perspective invariant. Anything that lets a
  malformed config escalate beyond a clean error message is in scope.
- Supply-chain or packaging issues affecting published wheels or
  sdists on PyPI (trusted publishing, sdist contents, lockfile
  integrity).
- MCP-registry publish workflow integrity (`.github/workflows/publish.yml`
  → `publish-registry` job) — OIDC-only auth, idempotent re-publish
  behavior on duplicate-version errors.
- GitHub Actions workflow injection — particularly any new use of
  `${{ github.event.* }}` expressions inside `run:` blocks where a
  contributor-controlled value (branch name, PR title, comment body)
  could become directly-executed shell. The pattern documented in
  `.github/workflows/pr-labels-ci.yml` (route through step-level
  `env:` and reference as `$VAR`) is the project standard.

**Out of scope**

- Vulnerabilities in dependencies (`mcp`, `httpx`, `keyring`,
  `pydantic`, `PyYAML`, `click`) — please report those upstream to
  the affected project.
- Vulnerabilities in DSM itself — report those to Synology PSIRT at
  <https://www.synology.com/security/advisory>. mcp-synology is a
  client; we patch around DSM bugs but don't fix DSM.
- Attacks that require an adversary to already have shell access on
  the host where mcp-synology is running, write access to the user's
  config file, write access to the OS keyring, or NAS admin
  credentials — that's a compromised host or a compromised NAS, not
  a project-specific issue.
- DSM API rate-limit / lockout surprises from misconfigured polling
  intervals — that's documentation territory, not a security issue.
- Issues with Claude Desktop, Claude Code, or any other MCP host —
  please report to the affected host project.

## Historical issues

Security-relevant findings are tracked in the GitHub issue tracker
under the `security` label. See also the [`LICENSE`](LICENSE) file
for Apache-2.0 warranty disclaimers.
