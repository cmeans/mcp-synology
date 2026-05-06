# Synology MCP Server — Architecture Decisions

> **Version:** 0.3 | **Updated:** 2026-03-16T17:30Z — Added auth architecture (strategy chain, credential storage, session lifecycle, bootstrap CLI). Clarified MCP SDK choice.

## Stack

- **Language:** Python
- **MCP SDK:** `mcp.server.fastmcp` from the official Python MCP SDK (`mcp` package) — NOT the standalone `fastmcp` package by PrefectHQ, which has diverged into a larger framework
- **HTTP client:** httpx (async)
- **Distribution:** uvx (from GitHub), Docker as secondary option
- **Project/dependency management:** uv
- **Config format:** YAML (PyYAML, always use `safe_load`)
- **Credential storage:** keyring (OS-native: macOS Keychain, Windows Credential Manager, Linux Secret Service)
- **Validation:** Pydantic / dataclasses with type hints throughout

### Why Python

- Developer (Chris) is proficient in Python and can effectively review AI-generated code for correctness and idiom
- Existing MCP project (clipboard-mcp) is Python — familiar patterns for FastMCP tool registration, pyproject.toml, uvx distribution
- Python MCP SDK is mature: FastMCP decorator-based APIs, async support, Pydantic validation, solid Streamable HTTP transport story
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
- Parse the standard `{success, data, error}` response envelope
- Map error codes to typed Python exceptions (common codes shared across all APIs, plus API-specific codes)
- Call `SYNO.API.Info` with `query=ALL` at startup; cache the result as a map of API name → `{path, minVersion, maxVersion}`
- Auto-negotiate API versions: use the highest version supported by the target NAS

**What it does NOT do:**
- Know anything about specific Synology APIs
- Handle authentication (delegated to auth manager)
- Format responses for LLM consumption (delegated to modules + shared formatters)

#### Auth Manager (`core/auth.py`)

Owns session lifecycle and credential retrieval. See [Auth Architecture](#auth-architecture) for the full design.

**Responsibilities:**
- Retrieve credentials from the storage hierarchy (keyring → env vars → config values)
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

The server calls `register()` for each enabled module, passing only the tool names that passed permission filtering. The module registers FastMCP tool handlers for those tools.

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
5. Start FastMCP transport (stdio initially; Streamable HTTP later)

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

### Rejected Auth Approaches

- **TOTP secret storage:** Storing the TOTP seed so the server can generate OTP codes itself. Rejected because it turns the config/keyring into a skeleton key that fully bypasses 2FA, undermining the security posture we're differentiating on.
- **Raw OTP at runtime (no device token):** Requiring the user to provide a fresh OTP code every time the server starts or re-authenticates. Rejected because MCP servers over stdio are spawned as child processes with no interactive input channel. Sessions also timeout during idle periods, which would require interrupting the user for a new OTP code mid-conversation. Unusable for unattended operation.

### Credential Storage

Three secrets need to persist: username, password, and (after 2FA bootstrap) device_id.

**Storage hierarchy** (checked in order):

1. **OS keyring** (preferred) — via the Python `keyring` library. Delegates to macOS Keychain, Windows Credential Manager, or Linux Secret Service (GNOME Keyring / KWallet). The setup CLI writes credentials here by default. This is the secure-by-default path.
2. **Environment variables** (fallback) — `SYNOLOGY_USERNAME`, `SYNOLOGY_PASSWORD`, `SYNOLOGY_DEVICE_ID`. Covers headless environments, Docker deployments, and CI where no keyring daemon is available.
3. **YAML config values** (last resort) — directly in the config file. Documented with a clear warning that this stores credentials in plaintext on disk. For quick testing only, not recommended for real use.

All three secrets (including device_id) are stored in the keyring when available. The keyring library auto-detects the appropriate backend; on headless systems without a keyring daemon, the setup CLI warns the user and falls back to env var guidance.

**Service name convention for keyring:**

```
Service: mcp-synology/{instance_id}
Accounts: username, password, device_id
```

### Separate State File

Non-sensitive server-managed runtime state is stored in a separate state file (not in the YAML config):

- **Location:** `~/.local/state/mcp-synology/{instance_id}/state.yaml` (or alongside the config file)
- **Contents:** SYNO.API.Info cache (API name → path/version map), last successful connection timestamp, negotiated API versions
- **Not stored here:** credentials, device tokens, session IDs (these go in the keyring or are ephemeral)

**Rationale:** Config files are user-managed input; state files are server-managed output. The config remains read-only from the server's perspective. No surprising mutations to user-edited files.

### Session Lifecycle

#### DSM Session Naming

DSM supports multiple concurrent named sessions per account via the `session` parameter on `SYNO.API.Auth`. Different station APIs use different session names (e.g., `FileStation`, `DownloadStation`). Sessions with different names coexist without conflict; sessions with the *same* name on the same account will displace each other (error 107).

**Session name format:** `MCPSynology_{instance_id}_{unique_id}`

- `instance_id`: from the config file, differentiates multiple NAS targets
- `unique_id`: generated at process startup (short UUID), prevents collision with stale sessions from previous runs that didn't cleanly logout

This ensures the MCP server doesn't kick out DSM web UI sessions, Home Assistant, or other integrations using the same account with their own session names.

#### Transparent Re-authentication

When a module makes an API call and the DSM client receives a session error, re-auth happens transparently:

1. DSM client receives error 106 (timeout), 107 (duplicate login), or 119 (invalid session)
2. Client requests re-authentication from the auth manager
3. Auth manager acquires an `asyncio.Lock` (prevents concurrent re-auth from multiple in-flight requests)
4. Auth manager logs in again using stored credentials + device_id
5. On success → original request is retried with the new session ID; module never sees the error
6. On failure → typed exception propagated to the module

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
2. Prompt for username and password interactively (or read from env vars if pre-set)
3. Attempt login to the NAS
4. If error 403 (2FA required): prompt for OTP code from the user's authenticator app
5. Login with otp_code + `enable_device_token=yes` + `device_name=MCPSynology`
6. Store username, password, and returned device_id in the keyring (or fallback storage)
7. Perform a validation login using the stored credentials (mimicking server startup)
8. Confirm success; log out the setup session

**Design properties:**
- **Idempotent:** running setup again updates stored credentials. Handles password rotation and device token refresh.
- **Handles both 2FA and non-2FA:** skips OTP prompt if the NAS doesn't require it
- **Validates before confirming:** catches misconfiguration before the user tries to start the server
- **Degrades gracefully:** if keyring is unavailable, falls back to env var guidance with clear instructions

**Additional CLI commands:**

- `mcp-synology serve` — normal MCP server mode (launched by Claude Desktop)
- `mcp-synology setup` — interactive credential setup and 2FA bootstrap
- `mcp-synology check` — validate stored credentials can authenticate (for debugging, does not start the server)

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
- **INFO** — significant lifecycle events only: successful authentication, re-authentication attempts, security-relevant config notes (HTTPS disabled, SSL verification off, module disabled).
- **WARNING** — configuration issues that may indicate problems: unknown module names, deprecated settings.
- **ERROR** — failures that prevent operation (logged via exceptions, not typically via `logger.error` directly).

### Configuration

Log level is set via:
1. Config file: `logging.level: debug` (default: `info`)
2. Environment variable: `SYNOLOGY_LOG_LEVEL=debug` (overrides config)

Logs go to stderr by default. Optionally `logging.file` can redirect to a file (useful when launched by Claude Desktop where stderr may not be visible).

---

## Open Design Topics

- **File Station module:** Concrete tool definitions, parameters, response shapes. Motivated by the weekly file-moving workflow.
- **Config schema:** Full YAML structure with examples. Auth section should cover the credential storage hierarchy and instance_id convention.
- **Project scaffolding:** Repo structure, pyproject.toml, CI, testing strategy.
