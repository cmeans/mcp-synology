# ADR 0001 — Per-Client DSM Sessions and the Streamable HTTP Roadmap

- **Status:** Accepted (deferred-stub) — 2026-05-04
- **Issue:** [#47](https://github.com/cmeans/mcp-synology/issues/47)
- **Implementing PR:** _filled in on merge_

## Context

`docs/specs/architecture.md` documents a future per-MCP-client DSM session model: each MCP client gets its own DSM session, scoped by a `session_key` parameter threaded through `AuthManager.get_session(session_key)`. The current implementation does not support this — `get_session()` takes no key, and the session name is built once at construction time and reused for the life of the `AuthManager`.

So long as the server runs under stdio (one OS process per MCP client connection), the gap is invisible: one process = one client = one DSM session. But under the planned Streamable HTTP transport, one server process would serve many MCP clients concurrently. They would all share the same `AuthManager` and therefore the same DSM session, meaning operations from different clients would interleave on the NAS as if they were one user, and DSM session expiry from one client's idleness would log out everyone else.

The 2026-04-16 project-wide review flagged this as the single biggest spec-vs-code gap. It is not a bug — there are no Streamable HTTP users today. It is an **unmade decision** that has been documented as future. The choice was either to ship the multi-session path now, retract the spec, or formally defer with the helper restructured so the eventual change is bounded. This ADR records the deferral.

## Options Considered

### Option 1 — Ship the multi-session path now

Add `session_key` to `get_session()`, change `_build_session_name()` to derive per key, plumb the key through every tool handler down to the auth layer, decide how clients identify themselves, and stand up a session pool with lifecycle (idle GC, max-concurrent cap, per-key re-auth coordination).

**Pros:** Streamable HTTP enablement becomes a transport-config change, not a refactor. Refactor cost grows with the tool surface — doing it now is bounded; doing it later, after #48/#49/#50 add modules, costs more. Forces design clarity on session-pool questions while context is fresh.

**Cons:** Real refactor for a feature with zero current users. Maintenance tax — every future module author has to remember to thread `session_key`, and a single forgotten call site silently regresses to the shared-session model. YAGNI risk if the trigger never fires.

### Option 2 — Retract the spec

Delete the per-client-sessions section. Stop documenting a direction that isn't being built. When/if the trigger lands, re-derive the design.

**Pros:** Honest about current state. Smallest change. No code maintenance tax.

**Cons:** Throws away the design thinking that's already in the spec. The next person to address Streamable HTTP has to reconstruct from scratch. Loses an architectural signpost.

### Option 3 — Deferred stub + spec reframe (chosen)

Don't change runtime behavior. Make the private session-name helper (`_build_session_name`) accept an optional `session_key: str | None = None` parameter so the future change is a one-liner at the helper. Keep `get_session()` single-session — no public-API surface change today. Reframe the spec section from "Future" to "Planned for Streamable HTTP enablement" and link it back to this ADR. Record the question, options, decision, and revisit triggers here.

**Pros:** Spec and code agree (within the deferral framing). Helper is structurally ready — when the trigger fires, the change is to add a `session_key` parameter to `get_session`, wire a session pool keyed on it, and call `self._build_session_name(session_key=key)`. Design intent preserved. No maintenance tax (no new public API to keep stable). No behavior change.

**Cons:** Half-measure: the spec says "planned" but there is no scheduled implementation date and no acceptance criteria beyond the trigger conditions below. Future implementer still has to design the session-pool lifecycle; this ADR records that the design is deferred, not solved.

## Decision

**Option 3.** Refactor the helper, reframe the spec, leave the public surface and runtime behavior unchanged.

Concretely:

1. `AuthManager._build_session_name(session_key: str | None = None) -> str` accepts an optional key. With `session_key=None` it behaves as today (`MCPSynology_{instance_id}_{uuid8}`). With a non-None key it returns `MCPSynology_{instance_id}_{session_key}`. The constructor still calls `_build_session_name()` with no key, so every existing code path behaves identically.
2. `AuthManager.get_session()` signature unchanged. Single-session.
3. The "Future: Per-Client Sessions" section in `docs/specs/architecture.md` is renamed "Planned: Per-Client Sessions" and links back to this ADR. The Auth Manager interface example is updated to match the current `get_session()` signature, with a sentence pointing at the planned section + this ADR.
4. This ADR records the question, options, decision, and revisit triggers.

## Consequences

- **No production behavior change.** Every existing test passes unchanged. The only signature change is the addition of an optional argument with a default that preserves prior behavior.
- **Spec drift closed.** The spec no longer documents an interface the code lacks. Future readers see "planned + see ADR" instead of "future + signature in code that doesn't exist".
- **The next implementation step is bounded.** When a revisit trigger fires (see below), the work is: (a) add `session_key: str | None = None` to `get_session()`, (b) replace `self._session_name`/`self._client.sid` with a keyed pool, (c) thread the key through callers that need it, (d) decide and document the session-pool lifecycle (idle GC, max-concurrent cap, per-key re-auth, callback dispatch). The helper change is already done.
- **Per-client sessions remain unavailable today.** Any caller wanting per-client isolation under stdio (subagents asking for separate DSM sessions, multi-user wrapper scripts, isolation-in-tests) will not get it from this PR. They are out of scope for the deferred stub.

## Revisit Triggers

This ADR should be re-opened — and Option 1 (or a variant) revisited — when **any** of the following happens:

1. **Streamable HTTP transport gains a concrete implementation plan.** Either an internal decision to build it or an upstream MCP SDK release that makes it the path of least resistance.
2. **A multi-tenant deployment use case appears.** Someone (Chris, a contributor, or a downstream user) wants to run mcp-synology as a shared service where multiple MCP clients connect with different identities.
3. **Subagent isolation is requested.** A concrete need to run multiple parallel DSM sessions from one process — e.g., a Claude Code session where the main agent and a long-running search subagent should not share session expiry.
4. **A second module is added that significantly increases the tool surface.** The argument "refactor cost grows linearly with the tool surface" is empirical; once the surface roughly doubles (e.g., adding a Storage + Health module per #49 or a Notifications module), the cost-of-deferring side of the trade re-balances. At that point even without an external trigger, doing the threading proactively may be cheaper than waiting.
5. **The current single-shared-session model exhibits a real-world pain point.** E.g., session expiry from one logical caller stalling unrelated calls under genuine concurrent load, or DSM rate limits being hit because one shared session can't be parallelized.

When a revisit happens, the Option 1 work is: define the client-identity contract (transport-supplied vs caller-supplied), spec the session-pool lifecycle (max sessions, idle GC, eviction on auth failure), decide credentials policy (one set of creds shared across keys, or keyed credential lookup), and update this ADR with the new decision.

## Related

- `src/mcp_synology/core/auth.py` — `_build_session_name`, `get_session`, `logout`, `_re_authenticate`
- `docs/specs/architecture.md` — "Auth Manager" and "Planned: Per-Client Sessions (Streamable HTTP)" sections
- Issue [#47](https://github.com/cmeans/mcp-synology/issues/47) — the open question this ADR answers
- Issues [#48](https://github.com/cmeans/mcp-synology/issues/48) / [#49](https://github.com/cmeans/mcp-synology/issues/49) / [#50](https://github.com/cmeans/mcp-synology/issues/50) — feature work whose design is unblocked by this deferral being recorded
