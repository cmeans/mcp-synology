# Synology MCP Server — Architecture Decisions

> **Version:** 0.3 | **Updated:** 2026-03-16T17:30Z — Added auth architecture (strategy chain, credential storage, session lifecycle, bootstrap CLI). Clarified MCP SDK choice.

## Stack

- **Language:** Python
- **MCP SDK:** `mcp.server.mcpserver.MCPServer` from the official Python MCP SDK (`mcp` package, 2.x; `mcp.server.fastmcp.FastMCP` in 1.x) — NOT the standalone `fastmcp` package by PrefectHQ, which has diverged into a larger framework
- **HTTP client:** httpx (async)
- **Distribution:** uvx (from GitHub), Docker as secondary option
- **Project/dependency management:** uv
- **Config format:** YAML (PyYAML, always a SafeLoader: `safe_load`, or `parse_config_text` for config files)
- **Credential storage:** keyring (OS-native: macOS Keychain, Windows Credential Manager, Linux Secret Service)
- **Validation:** Pydantic / dataclasses with type hints throughout

### Why Python

- Developer (Chris) is proficient in Python and can effectively review AI-generated code for correctness and idiom
- Existing MCP project (clipboard-mcp) is Python — familiar patterns for FastMCP (now `MCPServer`) tool registration, pyproject.toml, uvx distribution
- Python MCP SDK is mature: FastMCP (now `MCPServer`) decorator-based APIs, async support, Pydantic validation, solid Streamable HTTP transport story
- No meaningful deployment disadvantage vs TypeScript for the target audience (Synology NAS users are technical)

---

## Layered Architecture

### Layer 1 — Core (`mcp_synology/core/`)

Shared infrastructure used by all modules.

#### DSM API Client (`core/client.py`)

A thin async HTTP client that knows DSM request/response conventions but nothing about specific APIs (File Station, Download Station, etc.).

**Responsibilities:**
- Build request URLs from API name + version + method
- Inject session ID into requests (from auth manager)
- Send each request the way its module's spec chooses (transport is chosen per call, maintainer 2026-10-17). File Station and Download Station send GET, with two scoped exceptions sent as form POSTs: Download Station URI `Task.create` (#123) and `SYNO.API.Auth` `login` (#235), which keeps the password, OTP code and device token out of URLs and so out of DSM and proxy access logs. The Photos module (`photos-module-spec.md`) sends its JSON calls as form POSTs
- Parse the standard `{success, data, error}` response envelope
- Map error codes to typed Python exceptions (common codes shared across all APIs, plus API-specific codes)
- Call `SYNO.API.Info` with `query=ALL` at startup; cache the result as a map of API name → `{path, minVersion, maxVersion}`
- Auto-negotiate API versions: use the highest version supported by the target NAS
- Choose a connection per call (#222). DSM reloads its nginx whenever its web configuration changes (a package starts, stops or updates; a setting or certificate changes): on DSM 7.1, twice in the 13 s after Download Station starts. A request sent on a pooled keep-alive connection at the moment a reload closes it fails with `httpx.RemoteProtocolError` ("Server disconnected without sending a response"), and httpx resends only connection-setup failures. So **reads** (method `get*`, `list*`, `count*`, or exactly `query`, `status`, `me` or `info`) use the pooled connection and are resent once on a fresh connection after a dropped connection (`RemoteProtocolError`, `ReadError`, `WriteError`); **everything else** (writes, uploads, URI and file creates, downloads) uses a client that never reuses a connection, so it can't be caught by a reload and is never resent. A method not on the read list counts as a write, so a wrong guess costs a connection, never a repeated write. Both clients share one cookie jar. Measured on vdsm 7.1 with reloads forced about every 1.5 s during back-to-back reads: 5 drops in 8,968 calls before, 0 in 8,082 after (writes weren't timed under reloads; they're covered by design, since their connection is never reused).

**What it does NOT do:**
- Know anything about specific Synology APIs
- Handle authentication (delegated to auth manager)
- Format responses for LLM consumption (delegated to modules + shared formatters)

#### Auth Manager (`core/auth.py`)

Owns session lifecycle and credential retrieval. See [Auth Architecture](#auth-architecture) for the full design.

**Responsibilities:**
- Retrieve credentials from the storage hierarchy (env vars → config values → keyring)
- Login, logout, session refresh via `SYNO.API.Auth` (v6 recommended)
- Device token flow for 2FA-enabled accounts
- Provide valid session ID to the DSM client on demand
- Transparent re-authentication on session errors
- Manage DSM session naming for coexistence with other integrations

**Key interface:**

```python
class AuthManager:
    async def get_session(self) -> str:
        """Get a valid DSM session ID, logging in if needed."""
        ...
```

The current signature is single-session, matching the stdio transport's one-process-per-client model. A `session_key: str | None = None` parameter is reserved for the future per-client session model under Streamable HTTP — see [Planned: Per-Client Sessions](#planned-per-client-sessions-streamable-http) below and [ADR 0001](../adr/0001-per-client-dsm-sessions.md) for the deferral decision and revisit triggers.

#### Config System (`core/config.py`)

**Responsibilities:**
- Load YAML config file (connection details, module enable/disable, per-module permission tiers)
- Support env var overrides for sensitive values (credentials, tokens)
- Validate config at startup with clear error messages

#### Response Formatters (`core/formatting.py`)

Shared formatters that produce consistent output for LLM consumption.

```python
format_table(headers, rows, title=None) -> str      # File listings, task lists, share listings
format_key_value(pairs, title=None) -> str           # System info, single-item details, status checks
format_status(message, success=True) -> str          # Operation confirmations (move, copy, delete)
format_tree(nodes, title=None) -> str                # Directory trees, nested folder views
format_error(operation, error, suggestion=None) -> str  # User-friendly error messages
```

Modules use these by default. They can compose them (e.g., a file move returns `format_status` + `format_table` for the destination listing). Modules may extend with custom formatting for truly novel output, but the shared formatters are the path of least resistance.

**Rationale:** Consistent formatting means the LLM learns response patterns quickly and is less prone to parsing errors. Doesn't preclude the LLM or downstream tools from reformatting further.

### Layer 2 — Modules (`mcp_synology/modules/`)

Each module is a Python package (directory with `__init__.py`) corresponding to a DSM service.

#### Module Metadata

Every module declares static metadata as a typed `ModuleInfo` dataclass:

```python
@dataclass
class ApiRequirement:
    api_name: str
    min_version: int = 1
    max_version: int | None = None
    optional: bool = False  # Module works without it but with reduced functionality

@dataclass
class ToolInfo:
    name: str
    description: str
    permission_tier: PermissionTier = PermissionTier.READ

@dataclass
class ModuleInfo:
    name: str
    description: str
    required_apis: list[ApiRequirement]
    tools: list[ToolInfo]
```

**Purpose of declaring tools in metadata:**
- Server can filter by permission tier BEFORE calling `register()` — tools below the allowed tier are never registered and are invisible to the LLM
- Enables a future "list available modules and their tools" capability
- Server validates API requirements against SYNO.API.Info cache at startup (fail fast if NAS doesn't support required APIs)

#### Module Registration

```python
async def register(server, client, allowed_tools: set[str]):
    """Register MCP tools. Only tools in allowed_tools will be registered."""
    ...
```

The server calls `register()` for each enabled module, passing only the tool names that passed permission filtering. The module registers `MCPServer` tool handlers for those tools.

#### Version Dispatch Pattern

For handling different Synology API versions within a module:

```python
class VersionedHandler:
    def version(self, min_ver: int, max_ver: int | None = None):
        """Register a handler for a version range."""
        ...

    async def __call__(self, negotiated_version: int, **kwargs):
        """Dispatch to the best matching handler."""
        ...
```

**Key design principle:** Different version handlers produce the SAME internal data model. Version dispatch handles differences in *how you talk to the API*; the module's data model is version-independent.

- Single-version support is the easy path (most module authors start here)
- Multi-version is opt-in per handler for broader NAS compatibility
- The lower you go in version support, the wider the installed base you can serve

### Layer 3 — Server (`mcp_synology/server.py`)

Top-level MCP server entry point.

**Startup sequence:**
1. Load and validate YAML config
2. Initialize DSM client → connect → call SYNO.API.Info to cache available APIs
3. Initialize auth manager → authenticate
4. For each module enabled in config:
   a. Import module, read `MODULE_INFO`
   b. Validate `required_apis` against API info cache (warn or skip if unsupported)
   c. Filter `tools` by configured permission tier → produce `allowed_tools` set
   d. Call `module.register(server, client, allowed_tools)`
5. Start the `MCPServer` transport (stdio initially; Streamable HTTP later)

---

## Module Discovery

**Approach:** Explicit, via YAML config.

Users install the full package but only enable modules they need. Modules not listed or disabled in config are never loaded.

**Rationale:**
- Reduces LLM prompt context — every registered tool adds to what the LLM sees; no reason to pollute with tools the user doesn't need
- MCP servers over stdio are spawned as child processes; the tool list is fixed at startup; no dynamic load/unload during a session (restart required for changes)
- Predictable — user knows exactly what's active
- Simple — no scanning, no plugin directories, no entry points discovery

---

## Permission Tiers

```python
class PermissionTier(Enum):
    READ = "read"      # Observability: list, search, get info, status
    WRITE = "write"    # Mutation: move, copy, delete, create, rename
    ADMIN = "admin"    # System-level: user management, service control (future)
```

- **Default tier is READ** — safe out of the box
- Write and admin require explicit opt-in in config
- Tier is configured per-module
- Enforcement: tools above the configured tier are never registered with the MCP server; the LLM cannot see or invoke them
- **Scope: tiers guard the NAS only.** A tool's tier reflects what it does on the NAS. `download_file` is READ because it only reads the NAS, even though it writes a file on the machine running the server. Effects on that machine are described by MCP tool annotations instead (`download_file`: `readOnlyHint=false`, `destructiveHint=true`), so the client can confirm them, and by the tool's own safeguards (`download_file` writes only inside `dest_folder`).

---

## NAS Scope

**Current:** Single NAS per server instance.

Users with multiple NAS devices configure multiple MCP server entries in their client (e.g., Claude Desktop), each with its own config file:

```yaml
# Claude Desktop config
"synology-primary":
    command: uvx
    args: [mcp-synology, --config, ~/.config/mcp-synology/primary.yaml]
"synology-backup":
    command: uvx
    args: [mcp-synology, --config, ~/.config/mcp-synology/backup.yaml]
```

The LLM distinguishes them by server name. Each instance validates independently against its target NAS.

**Roadmapped:** Multi-NAS support (single server instance managing multiple connections). Deferred because it adds complexity to every layer — tool parameter disambiguation, per-NAS permission policies, multiple auth sessions, error attribution. The single-instance approach has a clean workaround for now.

---

## Auth Architecture

### DSM API Auth Model

Synology DSM uses session-based authentication via `SYNO.API.Auth` (versions 3–7, v6 recommended). There are no personal access tokens or API keys for general DSM API use. Every session starts with username + password and returns a `sid` (session ID), optionally a `did` (device ID), and a `synotoken` (CSRF token).

### Auth Strategy Chain

The auth manager auto-detects the appropriate authentication path rather than requiring the user to declare their 2FA status. On each login attempt, it reacts to the DSM API's response:

1. **If device_id is available** → attempt login with account + passwd + device_name + device_id (Path B: 2FA with remembered device). This is the steady-state for 2FA users.
2. **If no device_id and login succeeds** → account has no 2FA (Path A: simple login). Done.
3. **If no device_id and DSM returns error 403** → 2FA is required but no device token exists. Server cannot proceed; the user must run the setup CLI to bootstrap. See [Setup / Bootstrap Flow](#setup--bootstrap-flow).
4. **If a device_id was sent and DSM still returns error 403** → DSM rejected the stored device token (for example, the trusted device was removed in DSM). The error says so, rather than reporting that no token exists, and the user re-runs the setup CLI to sign in with a new 2FA code, after removing a `device_id` set in the environment or config file, which would override the keyring (the refusal's suggestion names it).

### Rejected Auth Approaches

- **TOTP secret storage:** Storing the TOTP seed so the server can generate OTP codes itself. Rejected because it turns the config/keyring into a skeleton key that fully bypasses 2FA, undermining the security posture we're differentiating on.
- **Raw OTP at runtime (no device token):** Requiring the user to provide a fresh OTP code every time the server starts or re-authenticates. Rejected because MCP servers over stdio are spawned as child processes with no interactive input channel. Sessions also timeout during idle periods, which would require interrupting the user for a new OTP code mid-conversation. Unusable for unattended operation.

### Credential Storage

Three secrets need to persist: username, password, and (after 2FA bootstrap) device_id.

**Storage hierarchy** (checked in order; each of the three values is resolved separately, and the first source that has it wins):

1. **Environment variables** (explicit override) — `SYNOLOGY_USERNAME`, `SYNOLOGY_PASSWORD`, `SYNOLOGY_DEVICE_ID`. Covers headless environments, Docker deployments, and CI where no keyring daemon is available.
2. **YAML config values** — directly in the config file. A username here is fine: it isn't a secret, and a config username with the password in the keyring is the recommended setup. A password (or device_id) here is stored in plaintext on disk; the server warns about a config password at startup (#167).
3. **OS keyring** (the default) — via the Python `keyring` library. Delegates to macOS Keychain, Windows Credential Manager, or Linux Secret Service (GNOME Keyring / KWallet). The setup CLI writes credentials here by default. This is the secure-by-default path; the explicit sources above override it.

All three secrets (including device_id) are stored in the keyring when available. The keyring library auto-detects the appropriate backend; on headless systems without a keyring daemon, the setup CLI says so, still writes the config (it holds no secret), and lists the variables to set, without logging in. It never prints a device token: a 2FA account there gets one from a machine with a keyring, as `SYNOLOGY_DEVICE_ID` (#481).

**Service name convention for keyring:**

```
Service: mcp-synology/{instance_id}
Accounts: username, password, device_id, host
```

**An entry is tied to its host (#536; maintainer, 2026-11-04) and port (#562; maintainer, 2026-11-05).** The record is `core.config.comparable_target`, `host:port` with the port in use, so the default compares equal with it written out; a record of this host with no port counts as no record. It's found by instance ID alone, and a derived ID is only the host's first label, so `nas.home` and `nas.office` share one. So `setup` saves `host` (in the form `core.config.comparable_target` gives, `host:port` with the host as `comparable_host` writes it, so `NAS.home` or an IDN's `xn--` form isn't another host) beside the password, in the keyring rather than a file found through `HOME`, which the same `env` block can change. Whenever `resolve_credentials` would use a username or password from the keyring, it compares the recorded host with `connection.host`, and on a mismatch raises before any credential is sent, with advice to run `setup`. (`SYNO.API.Info`, which carries none, still goes first, so a host that's down gets a connection error instead.) A device token alone, beside credentials from elsewhere, doesn't refuse: that login carries none of the keyring's secrets, so the token is left out, with a warning (once per config), and a 2FA account gets DSM's own error and its advice (maintainer, 2026-11-05). That path only reads: with no record, the token stays out too and nothing is recorded, since a login using none of the entry's password shouldn't settle whose it is. A warning would come too late, and there's no variable to skip this, since the same `env` block could set it. The error's text doesn't name the recorded host, since it reaches the model, and an `env` block's author would learn the real NAS's name from the reply; `check` adds it, for someone at their own terminal (`CredentialHostError.recorded_host`). A keyring that can't answer about the host refuses too, with advice to unlock it.

An entry from before this has no record. It takes the config's host on first use only when a login uses the entry's password and the config was named with `--config` by a full path, with none of `SYNOLOGY_HOST`, `SYNOLOGY_PORT` and `SYNOLOGY_INSTANCE_ID` set (even to the same value). `AuthManager.login` writes it once DSM returns a session (pre-QA on #559): written before the login, a host that's down, or another NAS refusing the password, pinned the entry and refused its real config. DSM asking for a second factor doesn't record it, since nothing captured shows it asks only once the password is right, and a false record pins the wrong host; `setup`, which those accounts run next, records it. A write that fails then warns, and the next login tries again. A username from the keyring with a password from elsewhere is refused on a mismatch but doesn't record. `MCP_SYNOLOGY_CONFIG`, the default location, a literal `~/...` in a client's args and a relative path don't count (maintainer, 2026-11-05, narrowing the design's item 4; relative paths from QA on #559): an `env` block can set the first, and supply the default location and `~` through `HOME`, since no shell expands `~` in an MCP client's args; a relative path is whatever the client's working folder holds. Otherwise it refuses, and the error doesn't list these conditions (the reply reaches the model, which may be able to edit a client's config). Every message that offers `check` as a way to record gets it from `core.auth.check_route`, so none names a route that can't work: it needs an entry with no record that holds a password, with the config's `auth.password` removed first if it has one, and keeps a 2FA device token. It asks first, default No, and sends nothing with no one at a terminal to answer (maintainer, 2026-11-05: `check` on a full path otherwise sent the password with none of this text shown). It's offered only to someone who knows whose credentials these are (`how_to_record`: "If you know these are the credentials for `<host>`…", not "if this is the right NAS", which `nas.office` is for its own config while the entry holds `nas.home`'s password; QA round 2 on #559). Otherwise the advice is the NAS's own `instance_id` and `setup`, which asks for the password and leaves the entry alone (`own_instance_id_clause`), since `setup` on the same entry would replace another NAS's credentials and token. An entry recorded for another host separates an old address (`setup`, replacing it) from another NAS (its own `instance_id`). The maintainer chose this text on 2026-11-05, after two QA rounds on it. The refusal's tool reply names no recording command, only `setup` and this document: a model can't know whose credentials these are. The older unreachable, busy and refused-login replies still name `check --config <file>`, which asks someone at a terminal before it records, so they stay (maintainer, 2026-11-05). `CredentialHostError.terminal_suggestion` carries the full text for `check`. With a host or connection variable set, `setup` is run with the same variable, since at a terminal it would otherwise store for the file's host or instance, or not connect. `check` says when it recorded the host, reading it back first, and when its login left the entry unrecorded. Entries moved by `scripts/migrate-from-synology-mcp.py` are in this case. Credentials from the environment or a config file need no record, and don't make one; a device token read beside them needs one (above). One host per entry: a NAS reached by two addresses uses a second config with its own `instance_id`.

`setup` writes the entry only after DSM accepts the login, new 2FA device token included, and before anything else can fail (#454): `_attempt_login` writes nothing, and `_store_keyring` runs right after it succeeds. So a mistyped password, a wrong code, an unreachable NAS or a reply with no session ID leaves the entry as it was, and `setup` exits 1. The hostname only suggests an alias, so a failed read costs that and nothing else. With no new token, a stored one stays for the same username and goes for another. A new token that can't be stored ends `setup` with exit 1, since the server can't log in to that account without it. With no keyring at all it doesn't log in, since there's nowhere to keep the result.

`setup` asks before replacing an entry recorded for another host or port, or with no record, and before overwriting a config file, ahead of the credential prompts, so declining writes nothing. A keyring that fails on that first read stops there with nothing changed, since it may work again by the write, and replace another host's entry unasked; only no keyring at all carries on, to say so when writing and name the variables to set. When the host changes, it deletes the old device token, password and username first, then writes the new host, then the username and password, so a stop partway leaves nothing to send to the wrong host, and the message says what was already removed. The old device token would otherwise be sent to this one.

### Runtime State

There's no per-instance state file: `DsmClient` keeps the `SYNO.API.Info` cache in memory, read when the first call builds the client, and versions are computed from it per call. The update check's `~/.local/state/mcp-synology/global.yaml` is the one state file (#538). The config stays read-only from the server's perspective.

### Session Lifecycle

#### DSM Session Naming

DSM supports multiple concurrent named sessions per account via the `session` parameter on `SYNO.API.Auth`. Different station APIs use different session names (e.g., `FileStation`, `DownloadStation`). Sessions with different names coexist without conflict; sessions with the *same* name on the same account will displace each other (error 107).

**Session name format:** `MCPSynology_{instance_id}_{unique_id}`

- `instance_id`: from the config file, differentiates multiple NAS targets
- `unique_id`: generated at process startup (short UUID), prevents collision with stale sessions from previous runs that didn't cleanly logout

This ensures the MCP server doesn't kick out DSM web UI sessions, Home Assistant, or other integrations using the same account with their own session names.

#### Logout on Shutdown

`serve` logs out of DSM when its run ends: on stdin EOF, and on SIGTERM or SIGINT.
- **Where it runs:** the logout is in the server's `lifespan` teardown (`SharedClientManager.aclose`), while the event loop and the HTTP client are still alive.
- **Limits:** it's shielded from cancellation and bounded by `LOGOUT_TIMEOUT` (5 s). Any failure is logged as a WARNING, including a DSM error reply, which `AuthManager.logout(raise_errors=True)` passes up, and a timeout. The session then stays open until DSM's own session timeout.
- **No re-login on `SYNO.API.Auth`:** a session error (106/107/119) on login or logout doesn't trigger the client's re-login and retry. Otherwise a logout that met an expired session would log in again just to log out, and could leave that new session open if the 5 s limit cut in.
- **Signals:** SIGTERM and SIGINT raise `SystemExit`, so the loop unwinds through that teardown. `serve` then leaves with `os._exit`, because the stdio reader thread is still blocked on stdin and joining it at interpreter exit would hang.
- **Why not atexit (#402):** the logout used to run at atexit. By then asyncio can't resolve a host ("cannot schedule new futures after interpreter shutdown"), so it never completed, and every exit left its session open.

#### Transparent Re-authentication

When a module makes an API call and the DSM client receives a session error, re-auth happens transparently:

1. DSM client receives error 106 (timeout), 107 (duplicate login), or 119 (invalid session)
2. Client takes its re-login lock (an `asyncio.Lock`), so one re-login runs at a time. If the session was replaced after the failed request was sent (another request's re-login already ran), it skips the login and retries with the new session: parallel calls that meet the same expired session log in once, not once each (#359). If that re-login fails, the requests that were waiting for it get its error rather than each trying again; a later request tries afresh
3. Otherwise the auth manager logs in again using stored credentials + device_id. The expired session stays in place until the new one exists (cleared first, every request sent during the login would go without a session and queue its own re-login), and the login itself is sent without it
4. On success → original request is retried with the new session ID; module never sees the error
5. On failure → the login's own error propagates to the module (#448): a changed password, a disabled account or a 2FA prompt says so, with its suggestion, rather than reading as the session error that led to the login. A proxy's answer to the login (an HTTP error status, a page that isn't DSM's) is raised as it is (#321)

**First login:** the server logs in lazily, on the first tool call. Parallel first calls wait for one client and one login (`SharedClientManager`'s init lock, #359); if it fails, the calls that were waiting get its error, and a later call tries again.

**Refused logins (#447):** when DSM refuses the credentials (400 wrong account or password, 401 disabled account, 403 2FA required or the device token rejected, 404 2FA code failed, 406 2FA enforced), the refusal is remembered and every later login with the same credentials is answered with it without sending the login, the first login and the re-login after a session error alike. (After a refused re-login, a tool call still sends its request with the expired session, gets 119 and is answered with the refusal; after a refused first login, nothing is sent at all.) DSM's Auto Block (on by default: 10 failed logins within 5 minutes) would otherwise block the computer's IP address, for the user's browser and every other client too, after about ten tool calls. The refusal holds until the resolved credentials change (`mcp-synology setup` writes new ones to the keyring while the server runs; a changed config or environment needs a restart anyway) or the server restarts. The suggestion names the fix for the credential the code concerns (`refusal_advice`): for 400 the password (`setup` when it's in the keyring, else where it's set, then a restart) and, if the username is wrong, where it comes from; for 403, 404 and 406 the device token (`setup`, unless a `device_id` in the environment or config file overrides the keyring, or `keyring` found no backend: then a token from a machine with a keyring, in `SYNOLOGY_DEVICE_ID`); for 401 none, since only an administrator can enable the account. `mcp-synology check` prints DSM's own error (the refusal's `__cause__`) with advice for that run (`refusal_advice(..., cli=True)`: by the code and the credential's source, without the server-only sentences), since the refusal's text is about the running server. Advice that names `setup` or `check` gives `--config <your config file>`, since a config written by `setup` is `<instance_id>.yaml`, which neither finds on its own. A failed re-login keeps the expired session in place (#359), so the shutdown logout may send it: DSM's session error there (106/107/119) means it's already closed, logged at DEBUG, not warned. Only a digest of the credentials is kept. A transport error isn't remembered, and neither are DSM's other login codes (402 and 407–410), which `core/errors.py` doesn't map to `AuthenticationError`; what each means and whether to remember it is #460.

**Retry limit:** Exactly one re-auth attempt per request. If the retry also fails, the error propagates. This prevents infinite loops when something is fundamentally wrong (e.g., password changed, account disabled).

**Error 107 special handling:** Re-auth succeeds, but a warning is logged recommending the user create a dedicated DSM service account for the MCP server. Two applications sharing an account + session name will steal sessions from each other.

**Error 105 (permission denied):** Not a session issue — this is an authorization failure. Propagated directly, no re-auth attempted.

**Keepalive strategy:** Lazy re-auth (let sessions expire, re-authenticate on next request) rather than proactive keepalive pings. Simpler, no unnecessary traffic, and re-auth is fast (single HTTP call).

#### Planned: Per-Client Sessions (Streamable HTTP)

Under stdio transport, one process = one MCP session = one DSM session shared across all chats. This is fine — DSM sessions handle concurrent API calls.

Under Streamable HTTP (planned, not yet implemented), multiple MCP clients would connect to one server, each with its own MCP session and `session_id`. The auth manager would scope DSM sessions per client via a `session_key` parameter on `get_session()`:

```python
# Stdio (current): single shared session
sid = await auth_manager.get_session()

# Streamable HTTP (planned): per-MCP-client session
sid = await auth_manager.get_session(session_key=ctx.session_id)
```

The DSM session name would become `MCPSynology_{instance_id}_{mcp_session_id}`, giving each connecting client its own isolated DSM session. The session-name helper (`AuthManager._build_session_name`) already accepts an optional `session_key` argument so the future change is bounded; the public `get_session()` signature, the session pool, and the lifecycle (login/logout/GC) per key are deferred until a transport or multi-tenant trigger lands.

This is a deliberate deferral, not an oversight. See [ADR 0001 — Per-Client DSM Sessions](../adr/0001-per-client-dsm-sessions.md) for the question, options considered, decision, and the trigger conditions that would re-open it.

### Setup / Bootstrap Flow

A separate CLI command handles interactive first-run setup, including the 2FA device token exchange:

```
$ mcp-synology setup --config ~/.config/mcp-synology/primary.yaml
```

**Setup sequence:**
1. Read config file for NAS connection details (host, port, HTTPS)
2. Ask first if the keyring entry isn't this host's (#536)
3. Prompt for username and password interactively
4. Attempt login to the NAS
5. If error 403 (2FA required): prompt for OTP code from the user's authenticator app, then login with otp_code + `enable_device_token=yes` + `device_name=MCPSynology`
6. Only then store the username, password, host and any returned device_id in the keyring (#454)
7. Log out the setup session

**Design properties:**
- **Idempotent:** running setup again updates stored credentials. Handles password rotation and device token refresh.
- **Handles both 2FA and non-2FA:** skips OTP prompt if the NAS doesn't require it
- **Validates before storing:** the login that's stored is the one DSM just accepted
- **Degrades gracefully:** if keyring is unavailable, `setup` still writes the config and lists the variables to set

**Additional CLI commands:**

- `mcp-synology serve` — normal MCP server mode (launched by Claude Desktop)
- `mcp-synology setup` — interactive credential setup and 2FA bootstrap
- `mcp-synology check` — validate the config, as `serve` does, then check the stored credentials can log in (does not start the server). A NAS it can't reach (connection refused, DNS, timeout) or whose TLS certificate fails verification is reported as one red error line and exit 1, never a traceback; `setup` uses the same handling (#191)

**Documentation flow:** install → create config with host/port → run `mcp-synology setup` → add server entry to Claude Desktop config → restart Claude Desktop.

#### Future: In-Chat Bootstrap via MCP Elicitation

The MCP specification is evolving to support "elicitation" — servers requesting input from the user through the client UI. Once this is reliably supported in major MCP clients (Claude Desktop in particular), the setup flow could be performed in-chat: the server detects missing credentials, asks for username/password/OTP through the conversation UI, completes the device token exchange, and stores credentials — no CLI step required. This is tracked as a planned enhancement pending MCP spec/client maturity.

### User Account Recommendations

Documentation should strongly recommend:

- **Create a dedicated DSM user account** for the MCP server (e.g., `mcp-service`). Avoids session conflicts with the user's own DSM web UI sessions and other integrations.
- **Grant minimum necessary permissions** to this account. Only the shared folders and services the MCP modules need access to.
- **Enable 2FA on the service account** and use the device token flow. The MCP server supports it, and it's the right security posture.

---

## Logging

### Approach

Every module uses `logging.getLogger(__name__)` so log output includes the full module path (e.g., `mcp_synology.core.client`, `mcp_synology.core.auth`). This makes it easy to trace log messages to their source file during debugging.

### Log Level Guidelines

- **DEBUG** — detailed operational trace for troubleshooting: every DSM API request/response (with passwords masked), credential resolution steps (which source provided credentials), config discovery and loading, version negotiation results, API cache contents, session lifecycle events, module registration decisions. This is the level to use when something isn't working.
- **INFO** — significant lifecycle events only: successful authentication, re-authentication attempts, the config source used (`Config: …`, #511), config notes (a module disabled).
- **WARNING** — configuration issues that may indicate problems: HTTPS disabled (#374), SSL verification off, a plaintext password, an env var overriding the config file's host, port, HTTPS or instance ID, unknown module names and settings, deprecated settings. The security ones (all but the last two) are shown at any log level (#533).
- **ERROR** — failures that prevent operation (logged via exceptions, not typically via `logger.error` directly).

### Configuration

Log level is set via:
1. Config file: `logging.level: debug` (default: `info`)
2. Environment variable: `SYNOLOGY_LOG_LEVEL=debug` (overrides config)

Logs go to stderr by default. Optionally `logging.file` adds a file (useful when launched by Claude Desktop where stderr may not be visible). What's logged while the config loads is replayed into it once it's open, so the security warnings reach it at any log level (#511 PR 3).

---

## Open Design Topics

- **File Station module:** Concrete tool definitions, parameters, response shapes. Motivated by the weekly file-moving workflow.
- **Config schema:** Full YAML structure with examples. Auth section should cover the credential storage hierarchy and instance_id convention.
- **Project scaffolding:** Repo structure, pyproject.toml, CI, testing strategy.
