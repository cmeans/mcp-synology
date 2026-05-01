# Changelog

## Unreleased

### Fixed

- **publish.yml: bump pinned `mcp-publisher` v1.5.0 → v1.7.6 to match the new registry OIDC audience** (#79) — the v0.5.1 release ran with `mcp-publisher v1.5.0` (the pin in `.github/actions/install-mcp-publisher/action.yml`); PyPI publish succeeded but the `publish-registry` job failed at GitHub OIDC login with `invalid audience: expected https://registry.modelcontextprotocol.io, got [mcp-registry]` (HTTP 401). Root cause: the registry deployed [`modelcontextprotocol/registry#1229`](https://github.com/modelcontextprotocol/registry/pull/1229) ("auth: bind GitHub OIDC token exchange to a per-deployment audience") in `v1.7.6` on 2026-04-30 — one day before our 2026-05-01 release. v1.5.0's `login github-oidc` flow sends audience `mcp-registry`; v1.7.6's flow sends audience `https://registry.modelcontextprotocol.io`, which is what the new registry server validates against. Bumped the action's `default` from `v1.5.0` to `v1.7.6` (and added an explanatory comment so the next bump prompt has the rationale at hand). Re-running the failed `publish-registry` job on the existing v0.5.1 tag won't pick up this fix because `actions/checkout@v6` resolves to the tag's commit; the next release tag will exercise the fix end-to-end. v0.5.1 itself is on PyPI as expected and is the install path users actually hit; the missed registry entry is purely directory metadata.
- **Keyring exception handler narrows + logs root cause** (#80) — closes #38. `core/auth.py:147-148` previously caught a bare `except Exception:` with a flat `logger.debug("Keyring not available.")`, hiding every keyring failure mode behind one generic line: locked macOS keychain (operator-actionable: unlock the keychain), `keyring.errors.NoKeyringError` on a headless host (signals a config issue), `keyring.errors.InitError`, OS-level errors on the D-Bus reach path, and genuine library bugs. Operators running `mcp-synology check -v` saw "Keyring not available." with no clue whether the keychain was locked, the backend was missing, or the library blew up. Narrowed to two typed handlers: `except KeyringError as e` (the typed-error case — covers `KeyringLocked`, `NoKeyringError`, `InitError`, `PasswordSetError`, `PasswordDeleteError`, and any other `keyring.errors.*` class) and `except OSError as e` (D-Bus reach errors, permission failures on the OS keychain DB). Both log at DEBUG with `exc_info=True` so the actual exception type, message, and traceback land in the verbose-mode output. Genuine bugs are no longer caught — they propagate up so they can be triaged. The pre-keyring D-Bus socket pre-check (`auth.py:130-131`) was bumped from DEBUG to INFO and rewritten with three concrete remediations (`mcp-synology setup` from a real desktop session, `dbus-run-session` wrapper, or `SYNOLOGY_USERNAME` / `SYNOLOGY_PASSWORD` / `SYNOLOGY_DEVICE_ID` env vars to bypass keyring entirely) — this is the most common path on Linux services launched without a desktop session, and the previous DEBUG-only log meant operators running `mcp-synology check` (without `-v`) saw a generic "no credentials" error with no breadcrumb to the root cause. Updated `tests/core/test_auth.py::_no_keyring()` fixture to raise `keyring.errors.NoKeyringError` instead of bare `Exception` so existing tests exercise the production-shaped error path. New `TestKeyringErrorHandling` (3 cases): `KeyringError` logged with `exc_info` and message text at DEBUG, `OSError` logged separately, and a defense-in-depth case proving a keyring blow-up doesn't block credential resolution from config/env. Strengthened `TestDbusSocketMissing::test_dbus_not_set_when_socket_missing_on_linux` to assert the new INFO-level remediation hint contains the socket path AND all three remediation strings. 553 unit tests pass at 96.13% coverage.
- **Background update-check task: log swallowed exceptions and bound the executor** (#81) — closes #39. Two related gaps in `server.py::SharedClientManager._bg_update_check()`. (1) The `(OSError, ValueError, KeyError)` handler used a bare `pass`, so a failed PyPI check (DNS down, malformed version string in `global.yaml`, state-file `KeyError`) silently exited the background coroutine with no breadcrumb anywhere — `SYNOLOGY_LOG_LEVEL=debug` showed nothing. Now logs at DEBUG with `exc_info=True` so the actual exception type, message, and traceback land in verbose output. Update check is best-effort, so we still don't propagate; the tool flow runs fine without an update notice. (2) The `loop.run_in_executor()` call was unbounded. The inner `urlopen(timeout=5)` covered the socket, but a thread stuck after `urlopen` returned (pathological YAML parsing on a malformed PyPI response, slow disk on `_save_global_state()`) would keep this coroutine alive forever and could delay session shutdown. Now wrapped in `asyncio.timeout(10)` (10s outer bound, generous margin over the 5s socket timeout); on timeout `_bg_update_check` catches `TimeoutError` separately, logs at DEBUG, and exits cleanly so the user-facing tool flow is never blocked. New `TestSharedClientManagerLifecycle::test_bg_update_check_timeout_logged_and_swallowed` patches `asyncio.timeout` to a 50ms window and `run_in_executor` to a 5s sleep, asserting the coroutine returns normally with the expected DEBUG log. Strengthened `test_bg_update_check_swallows_errors` to assert the new "Update check failed" DEBUG record carries `exc_info`. 554 unit tests pass at 96.13% coverage.
- **Validate version strings on `--revert <VERSION>` before they reach `pip install`** (#XX) — closes #40. `cli/version.py::_do_revert` previously fed `target_version` (and the `previous_version` loaded from `~/.local/state/mcp-synology/global.yaml`) straight into `subprocess.run(["uv", "tool", "install", "--force", f"mcp-synology=={prev}"])`. `shell=False` already neutralized command injection, but a value like `--revert latest`, `--revert 1.2`, or `--revert "1.0.0; whatever"` produced an opaque pip "Invalid requirement" error instead of an actionable CLI message — and a hand-corrupted `previous_version` field in the state file would propagate the same garbage on a no-arg `--revert`. New `_validate_version_string()` helper applies a loose PEP 440-ish regex (`^\d+\.\d+\.\d+([-.]?[a-zA-Z0-9]+)*$`) that accepts `0.5.1`, `0.5.1-rc1`, `0.5.0a1`, `1.2.3.post4`, etc. and rejects empty/whitespace-only input, missing patch segments, leading/trailing whitespace, `latest`, `v`-prefixes, shell metacharacters, and path traversal. On rejection it raises `click.ClickException` so click renders the standard `Error: ...` line and exits 1, matching the other CLI error paths and naming the expected format. Validation runs once at the chokepoint inside `_do_revert` (after `prev` is resolved from either source) so both the `--revert <VER>` and corrupt-state code paths are covered. `--auto-upgrade` doesn't need validation: its click `Choice(["enable", "disable"])` rejects anything else, and the `_do_auto_upgrade` upgrade target is a literal `mcp-synology@latest` not a user-supplied version. Twenty new tests: 8 valid versions accepted, 12 invalid inputs rejected, plus two `_do_revert` regression tests verifying that an invalid explicit `--revert <VER>` and a corrupt state-file `previous_version` both raise `ClickException` and never reach `subprocess.run`. 577 unit tests pass at 96.14% coverage.
- **Bump Pygments 2.19.2 → 2.20.0 to clear ReDoS advisory [GHSA-5239-wwwm-4pmq](https://github.com/advisories/GHSA-5239-wwwm-4pmq)** (#82) — closes Dependabot alert #3. Pygments < 2.20.0 has an inefficient regex for GUID matching that can be triggered into ReDoS by crafted input; severity Low. We pull Pygments transitively via `pytest` 9.0.3 (used by the test suite — `pytest --color` syntax-highlights traceback output), so this is a dev-dep upgrade that doesn't affect runtime. Bumped via `uv lock --upgrade-package pygments`; only `uv.lock` changed (no `pyproject.toml` constraint adjustment needed because `pytest` doesn't pin Pygments to an upper bound). 554 unit tests still pass at 96.13% coverage on the bumped lockfile; ruff/mypy clean.

## 0.5.1 (2026-05-01)

### Fixed

- **`get_file_info` and `delete_files` correctly handle multi-path inputs on real DSM 7.x** (#77) — closes #68. DSM 7.x's `SYNO.FileStation.List getinfo` and `SYNO.FileStation.Delete start` do **not** honor the documented comma-joined multi-path format, even on v2: a request with `path=/a,/b` is treated as a single literal path. For `getinfo` this surfaces as one synthetic record whose `path` field IS the literal comma-joined string (the handler's `len(files) == 1` branch then renders it as a single info card). For `delete_files` this surfaces as a successful task that actually no-ops on every input, returning `[+] Deleted N item(s)` with all paths listed but none removed. The round-1 hypothesis (pin `getinfo` to v2 to dodge a v3 quirk) was disproven by vdsm CI on DSM 7.2.2 — the comma-joined-as-single-path symptom reproduces on v2 too. Fix matches the user's documented workaround on #68: **one DSM call per input path**. Both tools now iterate `paths` and issue per-path requests, aggregating results into the same response shape callers already expect (single info card for one path, table for multiple; per-share recycle-bin messaging unchanged for delete). Trade-off is N round-trips for N paths, which is fine for typical small-N usage and trivially correct. Refactor extracted `_delete_one_path` from `delete_files` so the per-path async-task pattern (start → poll → stop in `try`/`finally`) lives in one place; `get_file_info` simply loops `client.request` since it's synchronous. Bumped `tests/conftest.py` `SYNO.FileStation.List max_version` from 2 to 3 to match DSM 7.x reality so future regression tests don't get fooled by a max-resolves-to-2 default. New `TestGetFileInfo::test_multipath_uses_per_path_serial_calls` asserts (a) N requests for N paths, (b) each request carries a single path with no commas, (c) all pinned to v2, (d) results aggregate correctly. New `TestMultiPathDelete` integration test (re-exported in `tests/vdsm/test_vdsm_integration.py`) creates two folders, deletes both in one multi-path call, verifies via `list_files` that both are actually gone — would have caught the original #68 regression before it shipped if it had existed in v0.5.0. 550 unit tests pass at 96.13% coverage.
- **`delete_files` now reports recycle-bin status correctly per share** (#73) — closes #37. Pre-fix, `modules/filestation/__init__.py:238` constructed `recycle_status: dict[str, bool] = {}` and never populated it. The closure-shared dict flowed into `delete_files`, where `operations.py:386-398` fell through to the `# Assume recycle bin by default` branch for every share, so users with `#recycle` *disabled* on a share saw `"Recycle bin is enabled on /{share} — files can be recovered..."` after a delete that had actually removed the data permanently. New lazy probe `ensure_recycle_status(client, share, recycle_status)` in `modules/filestation/helpers.py` lives off the cache: a missing share triggers `SYNO.FileStation.List` on `/{share}/#recycle` with `limit=0`. Success → `True`, DSM 408 → `False`, 105 (permission denied) or any other DSM error → `True` + WARNING log (preserves prior optimistic-default behavior so messaging stays consistent and the operator sees the diagnostic). Cached in-place so repeated deletes against the same share don't re-probe. Wired into `delete_files` and `list_recycle_bin` (the two correctness-sensitive paths). `list_shares` left alone — it renders whatever's already cached, kept cheap. **Self-correct on observation**: when `list_recycle_bin` sees DSM behavior contradicting the cached value (cached `True` but the actual list returns 408 → flip to `False`; or vice-versa on an unexpected success), the helper updates the cache in place and logs at INFO so subsequent `delete_files` calls in the same session see the corrected state without waiting for re-auth invalidation. **Invalidate-on-reauth**: new `AuthManager.add_on_reauth_callback` API + dispatch loop in `_re_authenticate`; new `SharedClientManager.subscribe_on_reauth` proxy that queues callbacks before the AuthManager is lazily created and flushes them on first `get_client`. Filestation `register()` subscribes `recycle_status.clear` so admin-side toggles to `#recycle` between sessions are picked up after the next session-error-driven re-auth. Eighteen new tests (9 helper + 3 auth + 3 server + 1 delete-files lazy-probe + 2 list_recycle_bin self-correct); 546 passing total at 96.10% coverage. Persistence across server restarts (binding the closure dict to `ServerState.recycle_bin_status`) is intentionally out of scope — that field exists on the model but `load_state`/`save_state` aren't currently wired up at all; treating that as a separate follow-up since plumbing it touches a much larger surface than #37 alone needs. vdsm integration test (verifying real DSM recycle-on/recycle-off shares produce correct messaging end-to-end) is also a follow-up — `synoshare --setopt` recycle-toggle reliability on DSM 7.2.x is unproven (PR #23 reverted a similar setopt for share creation), so unit tests carry the regression load until that's verified.

### Added

- **README: per-installer and per-OS download-breakdown badges** (#72) — adds two new badge groups to `README.md` mirroring the layout `cmeans/mcp-clipboard` adopted after the upstream `cmeans/pypi-winnow-downloads` service grew its installer/OS endpoints. Group 1 (six badges): `pip`, `pipenv`, `pipx`, `uv`, `poetry`, `pdm` 30d non-CI download counts via `installer-{installer}-30d-non-ci.json` endpoints. Group 2 (three badges): `linux`, `macos`, `windows` 30d non-CI download counts via `os-{os}-30d-non-ci.json` endpoints. All nine new badges link to [`cmeans/pypi-winnow-downloads`](https://github.com/cmeans/pypi-winnow-downloads) (the dogfooded service) rather than to PyPI itself, consistent with the existing aggregate Downloads badge from #62 — keeps the "powered by" attribution implicit and gives a curious reader a single click into the data source. Verified the endpoints are live for `mcp-synology` (e.g. `installer-pip-30d-non-ci.json` returns the expected schemaVersion-1 payload). No code or test changes — README-only.

### Fixed

- **`mcp-synology setup` now writes the config file atomically** (#71) — closes #70. Cascades the atomic-write helper introduced in PR #69 (`core/fs.py::atomic_write_text`) into `cli/setup.py:178-183`, the last user-visible non-atomic write site in the project. Previously the interactive setup flow persisted the user-edited config via `config_path.write_text(header + raw_yaml, encoding="utf-8")` — same torn-write window as the runtime state files PR #69 fixed: a Ctrl+C, OOM, or power loss between the file truncate and the final write would leave a zero-byte or half-written `<instance_id>.yaml` that the next `mcp-synology check` / `serve` invocation would fail to parse. The window is small (single write of a small YAML payload) and the workflow is interactive, so the practical risk is much lower than the runtime-state case — but the helper was already sitting there ready to use, and closing this site means there are no remaining `path.write_text` calls in the project that persist user/runtime data. Drops the now-redundant `_CONFIG_DIR.mkdir(parents=True, exist_ok=True)` since `atomic_write_text` already creates parent directories. New `TestSetupAtomicConfigWrite::test_setup_writes_config_atomically_with_no_tmp_sibling` regression test in `tests/core/test_cli_setup.py` runs the full interactive setup flow against a not-pre-created `_CONFIG_DIR`, then asserts (a) the dir was auto-created (proves the helper ran), (b) no `.tmp` sibling lingers in the dir after a successful write, and (c) the resulting YAML starts with the generated header and contains the entered host.
- **State file writes are now atomic** (#69) — closes #36. `core/state.py:save_state()` and `cli/version.py:_save_global_state()` previously called `path.write_text(...)`, which on POSIX is *not* atomic — a process kill (Claude Desktop quit, OOM, power loss) between the file truncate and the final write produces a zero-byte or half-written `state.yaml`/`global.yaml`. The next startup either fails to load it or silently falls back to default state, losing persistent state (device tokens, API version cache, update-check timestamps) and looping on re-authentication or spamming PyPI. New `core/fs.py` houses the shared `atomic_write_text(path, content, *, encoding="utf-8")` helper: it writes to a sibling `.tmp` file then `Path.replace()`s it onto the target. `os.replace` (which `Path.replace` maps to) is atomic on both POSIX and Windows, so a reader either sees the previous contents or the new contents — never a torn write. The helper also creates parent directories and best-effort cleans up the `.tmp` on rename failure (`FileNotFoundError` swallowed; other `OSError`s during cleanup logged at WARNING and the original exception is re-raised). Both `save_state` and `_save_global_state` now route through it; the per-call-site `path.parent.mkdir(parents=True, exist_ok=True)` is gone since the helper does that. Seven `TestAtomicWriteText` cases in `tests/core/test_fs.py` cover the happy path (writes content, creates missing parent dirs, overwrites existing file, no `.tmp` left behind on success, custom encoding) plus two `Path.replace`-patched cases that verify (a) the original `OSError` propagates, the `.tmp` sibling is removed, and no partial file lands at the target, and (b) an existing target file is preserved untouched when the rename fails.
- **vdsm: refresh DSM search index for both setup-time and runtime-created test data** (#67) — fixes the `tests/test_integration.py::TestSearch::test_search_keyword_finds_directory` flake on vdsm CI. DSM Universal Search doesn't crawl non-indexed shares promptly on a freshly-booted vdsm, so search calls returned `0 results found` for several minutes after the data was created — well past the test's 65-second retry budget. Two-part fix: (1) `tests/vdsm/setup_dsm.py` calls `synoindex -A -d` for `/testshare/Documents` and `/testshare/Media` at golden-image build time, registering the static test data with DSM's search index. (2) New `refresh_search_index` async fixture in `tests/test_integration.py` (no-op default for real-NAS runs) and a vdsm override in `tests/vdsm/conftest.py` that invokes `synoindex -A -d` via SSH for the runtime-created `Bambu Studio` subdirectory. Without (2), the round-1 partial fix shipped via this PR's earlier head improved the test from 6-attempt fail to 4-attempt success — still flaky. With (2), search registers the new path synchronously and the test passes on attempt 1. SSH plumbing was lifted from `setup_dsm.py` into a new shared `tests/vdsm/ssh.py` module so both image-build-time and test-fixture-time SSH go through the same helper. Best-effort: a non-zero `synoindex` return logs a warning and falls back to the existing retry loop. Modifying `setup_dsm.py` invalidates the vdsm-workflow golden-image cache key, so the next CI run rebuilds the image with the fix baked in.
- **Auth ignores empty / whitespace-only credentials at every read site** (#66) — closes #35. `_resolve_credentials()` in `core/auth.py` previously trusted that env / config / keyring values, if present, contained real credentials. Empty strings already fell through (Python truthiness handled them), but **whitespace-only** values like `auth: {username: "   "}` mid-edit slipped through and reached `login()` as `('   ', '\t', None)`, surfacing as a generic DSM 400 that pointed neither at the config nor at the empty values. New `_present_or_none()` helper returns the value unchanged when it has any non-whitespace content, otherwise `None`. Applied at all nine read sites (3 env vars × 3 storage tiers — env / plaintext config / keyring). Meaningful padding is preserved (e.g. a real password `"  pwd  "` keeps its spaces); only purely empty/whitespace-only inputs are filtered. Six regression tests cover whitespace at each strategy level (env, config, keyring), the empty-string regression, the whitespace-doesn't-shadow-valid-keyring case, and the preserve-internal-padding guarantee.
- **CLI catches `pydantic.ValidationError` and emits a clean `Error:` line** (#65) — closes #34. The four `load_config()` call sites (`cli/check.py`, `cli/main.py` for `serve`, `cli/setup.py` discovery, `cli/setup.py:_setup_with_config` for `-c`) previously caught only `(FileNotFoundError, ValueError, yaml.YAMLError)`. Pydantic's `ValidationError` IS a `ValueError` subclass, so it WAS caught — but `str(e)` rendered Pydantic's raw multi-line traceback-style block, defeating the clean-error pattern PR #26 established for malformed YAML. New helper `format_validation_error()` in `core/config.py` renders `exc.errors()` as a one-line header (`Configuration validation failed (N error(s)):`) plus per-error `<dotted.location>: <message> (got <input>)` lines. The catch order at each site is now `except ValidationError` BEFORE `except (FileNotFoundError, ValueError, yaml.YAMLError)` since pydantic's class subclasses ValueError. Four regression tests added (one per call site) reproducing the gap by triggering AppConfig's strict-top-level `extra="forbid"` with an unknown field, asserting `Error: Configuration validation failed` lands and no `Traceback` leaks.
- **`search_files` MCP tool exposes `mtime_from` / `mtime_to`** (#64) — closes #33. The underlying `search_files()` handler in `modules/filestation/search.py` accepted both parameters and forwarded them to DSM, but the FastMCP tool registration in `modules/filestation/__init__.py:tool_search_files` omitted them entirely — the documented modification-date filter was inaccessible from MCP clients. The parameters now appear on the tool surface and the description names ISO-8601 / `YYYY-MM-DD` / Unix-epoch as accepted formats. While fixing the surface gap, also added a `parse_mtime()` helper in `modules/filestation/helpers.py` so the documented format actually round-trips: ISO-8601 datetimes (with or without offset), bare calendar dates, and numeric epoch strings now all convert to the integer epoch seconds DSM's `SYNO.FileStation.Search` API expects. Naive datetimes are treated as UTC for stable cross-host behavior. New tests: 8 `TestParseMtime` cases in `tests/modules/filestation/test_helpers.py` plus a `test_search_with_mtime_filter` round-trip test that asserts the converted epoch lands in the DSM `start` request via respx (508 tests pass total at 96.14% coverage).
- **Auto-CHANGELOG workflow now inserts `### Changed` in Keep-a-Changelog order** (#63) — the workflow's inline-Python composer previously inserted a newly-created `### Changed` block at `unreleased_idx + 1`, which placed it ABOVE any existing `### Added` (or other earlier-sorting subsection). Per Keep a Changelog v1.1.0 the canonical order is Added → Changed → Deprecated → Removed → Fixed → Security; the bug surfaces when `## Unreleased` already contains `### Added` (or any non-Changed subsection) and the next Dependabot PR creates a fresh `### Changed`. Currently dormant on this repo because `## Unreleased` happens to have both subsections; would manifest after the next release ships and a feature PR adds `### Added` to the fresh Unreleased section before any Dependabot bump. Fix: walk forward from `## Unreleased` looking for the first `###` subsection that should sort AFTER `### Changed` (Deprecated / Removed / Fixed / Security) OR the next `## ` release heading; insert immediately before whichever comes first. Default insertion point is the end of the Unreleased section. Smoke-tested locally against five CHANGELOG arrangements (empty Unreleased, Added-only, existing Changed, Added+Fixed, Fixed-only) — all produce KaC-ordered output. Surfaced by [`cmeans/pypi-winnow-downloads#26`](https://github.com/cmeans/pypi-winnow-downloads/issues/26) during QA review of the cascade PR there.

### Changed

- **Dogfood download badge: swap shields.io built-in for `pypi-badges.intfar.com`** (#62) — replaces `https://img.shields.io/pypi/dm/mcp-synology` with `https://img.shields.io/endpoint?url=https%3A%2F%2Fpypi-badges.intfar.com%2Fmcp-synology%2Fdownloads-30d-non-ci.json` in `README.md` line 16. Badge endpoint is the dogfooded service from [`cmeans/pypi-winnow-downloads`](https://github.com/cmeans/pypi-winnow-downloads), which winnows out CI traffic from the BigQuery PyPI download data. Badge link target updated to the upstream repo so the "powered by" attribution is implicit (no acknowledgements-section bloat). Resulting label reads "pip*/uv/poetry/pdm (30d)" instead of the generic "Downloads" — accurately reflecting the non-CI scope.

### Fixed

- **Auto-CHANGELOG workflow now records correct versions on grouped Dependabot PRs** (#60) — bumps `dependabot/fetch-metadata` from `v2.5.0` (SHA `21025c70…`) to `v3.1.0` (SHA `25dd0e34…`). Surfaced by live PR #59 (github-actions group, 8 updates), which produced an entry with empty version arrows: `actions/checkout →, astral-sh/setup-uv →, ...`. Root cause: `fetch-metadata@v2.5.0` returns empty-string `prevVersion`/`newVersion` for every package in a grouped update, so the `.get(key, '?')` fallback in the workflow's inline Python didn't trigger (the keys were present, just empty). [Upstream PR #632](https://github.com/dependabot/fetch-metadata/pull/632) (shipped in v3.0.0, refined in v3.1.0) added body-metadata parsing for multi-dependency PRs, which is exactly the fix for this gap. SHA pin updated; no inline-Python changes needed. v3 also requires Node.js 24, which addresses the deprecation warning the v2 line was emitting on every run.

### Added

- **Dependabot config: weekly grouped updates for pip + github-actions** (#57) — adds `.github/dependabot.yml` covering Python deps (pyproject.toml + uv.lock) and GitHub Actions referenced from `.github/workflows/*.yml`. Schedule is weekly Monday 06:00 America/Chicago, single grouped PR per ecosystem, labels `dependencies` + `python` / `github-actions`, commit prefix `chore(deps)`. No `docker` ecosystem because mcp-synology has no Dockerfile. Pattern ported from `cmeans/pypi-winnow-downloads#21`.
- **Community-health files: `CONTRIBUTING.md`, `CODE_OF_CONDUCT.md`, `SECURITY.md`** (#56) — adds the standard GitHub community health files. `CONTRIBUTING.md` documents the inbound = outbound Apache-2.0 licensing rule, the no-bounties policy, the dev workflow (`uv sync`, `pytest`, `ruff`, `mypy`), the test-mirror file convention, the integration-test setup pointer, and the QA-label flow (`Awaiting CI` → `Ready for QA` → `QA Approved`). `CODE_OF_CONDUCT.md` adopts Contributor Covenant 2.1 and routes private reports through GitHub Private Security Advisories with a `Conduct` title prefix. `SECURITY.md` declares 0.5.x as the supported line and enumerates in-scope areas (auth/credential handling, session-token leakage, File Station path validation, argument injection in the DSM client, permission-tier gating, async background-task cleanup, config-loading invariants, supply-chain/registry-publish integrity, GHA injection patterns) and out-of-scope areas (upstream-dependency CVEs, DSM itself, compromised-host scenarios). Pattern ported from `cmeans/pypi-winnow-downloads#20`. Also aligns the now-merged `.github/PULL_REQUEST_TEMPLATE.md` (from #58) with CI: adds `scripts/` to the ruff and mypy checklist lines, tightens the pytest path placeholder to `tests/modules/<area>/test_<file>.py`, and matches the `## CHANGELOG` checkbox wording with the CONTRIBUTING.md PR-body example.
- **Dependabot PR hygiene: `.github/PULL_REQUEST_TEMPLATE.md` + auto-CHANGELOG workflow** (#58) — addresses the QA gap surfaced by the first Dependabot PR (#55) where the Dependabot-generated body had no `## QA` section and the auto-bump didn't produce the unconditional CHANGELOG entry that `CLAUDE.md` § "Adding a CHANGELOG entry on every PR" requires. Two artifacts: (1) `PULL_REQUEST_TEMPLATE.md` providing a `## Summary` / `## Test plan` / `## CHANGELOG` scaffold for human-authored PRs (Dependabot bypasses templates). (2) `.github/workflows/dependabot-changelog.yml` running on `pull_request_target` for `dependabot[bot]`-authored PRs only — mints a token via `actions/create-github-app-token` (SHA-pinned to v3.1.1) so pushes attribute to `cmeans-claude-dev[bot]` and re-fire the required `pull_request` checks (lint / typecheck / test 3.11/3.12/3.13 / version-sync), enumerates the bump set via `dependabot/fetch-metadata` (SHA-pinned to v2.5.0), prefers the named `dependency-group` output and falls back to `package-ecosystem`, and pushes a follow-up commit (`chore(changelog): record dep bumps from #N`) back to the Dependabot branch. Required repo secrets: `BOT_APP_ID`, `BOT_APP_PRIVATE_KEY`. Loop guard skips when the last commit author is `cmeans-claude-dev[bot]`; idempotency check skips when `(#N)` is already present in `CHANGELOG.md` so `@dependabot recreate`/`rebase` doesn't double-write and a hand-prepended human entry (e.g., the CVE callouts in PR #55) is preserved.

### Changed

- **Bump github-actions group: actions/checkout 4→6, astral-sh/setup-uv 5→7, actions/setup-python 5→6, codecov/codecov-action 4→6, actions/upload-artifact 4→7, actions/download-artifact 4→8, actions/cache 4→5** (#61)
- **Bump uv group: pytest 9.0.2→9.0.3, cryptography 46.0.5→46.0.7, python-multipart 0.0.22→0.0.26, requests 2.32.5→2.33.0** (#55) — first Dependabot-authored PR on the repo. Direct dev dep: `pytest`. Transitive bumps (no `pyproject.toml` constraints widened): `cryptography` via `pyjwt` + `secretstorage`, `python-multipart` via `mcp`, `requests` via `docker` (vdsm extra only). Picks up [CVE-2026-39892](https://github.com/pyca/cryptography/security/advisories) (cryptography buffer overflow; 46.0.7 wheels also ship OpenSSL 3.5.6) and [CVE-2025-71176](https://github.com/pytest-dev/pytest/security/advisories) (pytest insecure tmpdir). `requests` 2.33.0 drops Python 3.9 support — irrelevant for this project (`requires-python = ">=3.11"`). All 499 unit tests pass at 96.04% coverage on the bumped lockfile.

### Fixed

- **Harden `pr-labels-ci.yml` against shell injection via fork-PR branch names** (#53) — closes #52. Cascades the `env:` pattern from `cmeans/mcp-clipboard#88` into this repo's `.github/workflows/pr-labels-ci.yml`. Both the `on-ci-pass` and `on-ci-fail` jobs previously inlined `${{ github.event.workflow_run.head_branch }}` directly inside `run:` blocks. `head_branch` is contributor-controlled on fork PRs and git refnames allow shell metacharacters (`$`, backtick, `;`, `&`, `|`, etc.), so a malicious fork branch name would render as directly-executed shell once the expression was substituted. `REPO`, `RUN_ID`, and `HEAD_BRANCH` now come through step-level `env:` blocks and the shell references them as `$REPO` / `$RUN_ID` / `$HEAD_BRANCH`. Also avoids the latent parser trap documented in `cmeans/yt-dont-recommend#28`: GHA substitutes `${{ ... }}` inside `run:` blocks before the shell sees them *including within shell comments*, and the queue-time parser rejects an empty expression on `workflow_dispatch` — the explanatory comments therefore describe the concept ("not a direct GHA expression") rather than showing the literal sequence. Verified locally that `yaml.safe_load` parses cleanly and that no `${{ ... }}` substitution survives inside any `run:` body.

### Changed

- **Task-completion error envelopes now route through `error_from_code()` for specific codes** (#30) — addresses F18 from the PR #9 self-audit. When a background CopyMove or Delete task completes with an `error` dict, `operations.py:256` (copy/move) and `operations.py:365` (delete) now pass the DSM error code through `error_from_code(err_code, "SYNO.FileStation.CopyMove" | "SYNO.FileStation.Delete")` to build the envelope, matching the synchronous error paths elsewhere in the module. Envelope `code` becomes the mapped value (e.g. `408` → `not_found`, `414` → `already_exists`, `416` → `disk_full`, `1100` → `filestation_error`, `105` → `permission_denied`); envelope `retryable` inherits from the mapped exception (e.g. `disk_full` is `retryable=true`); envelope `suggestion` comes from the per-code mapping when one exists and falls back to the previous generic suggestion otherwise. Unknown/unmapped codes still produce the `dsm_error` envelope. **Behavior change** — callers previously catching `dsm_error` on these two paths will now receive the more specific code. Tests `test_copy_task_completes_with_error` and `test_delete_task_completes_with_error` updated to assert `filestation_error` on code 1100 (plus the per-code suggestion text); four new cases cover 408→not_found, 416→disk_full+retryable, 105→permission_denied, and the unknown-code fallback.
- **Structured error envelopes now include `param`/`value` on five more call sites** (#29) — addresses F17 from the PR #9 self-audit. Five `error_response()` call sites in `modules/filestation/*.py` previously emitted envelopes without the `param`/`value` fields that smart clients could dispatch on: `metadata.py:76` (`get_file_info` multi-path empty-result → `param="paths"`, `value=<paths>`), `metadata.py:222` (`get_dir_size` timeout → `param="timeout"`, `value=<timeout>`), `operations.py:245` (copy/move timeout), `operations.py:353` (delete timeout), and `transfer.py:209` (download local-write `OSError` → `param="dest_folder"`, `value=<dest_folder>`). Existing `message` text is unchanged. Regression assertions added to `test_empty_files_list_returns_not_found`, `test_dir_size_timeout`, `test_copy_timeout`, `test_delete_timeout`, and `test_download_write_permission_error`.

### Added

- **Auto-publish to MCP registry on release** (#27) — adds a `publish-registry` job to `.github/workflows/publish.yml` that runs after `publish-pypi` and publishes `server.json` to `registry.modelcontextprotocol.io` via [`mcp-publisher`](https://github.com/modelcontextprotocol/registry). Uses GitHub OIDC authentication (`permissions: id-token: write`) so no long-lived registry token is needed. The step is idempotent — if the tag's workflow is re-run after a successful registry publish, the duplicate-version error (registry requires every version string to be unique per [its versioning docs](https://github.com/modelcontextprotocol/registry/blob/main/docs/modelcontextprotocol-io/versioning.mdx)) is caught and downgraded to a `::warning::` rather than failing the release. Also adds a `validate-server-json` job to `.github/workflows/ci.yml` that runs `mcp-publisher validate server.json` on every PR so schema breakage is caught before a tag push, complementing the existing `version-sync` check which enforces alignment with `pyproject.toml`.
- **vdsm CI workflow** (#24) — adds `.github/workflows/vdsm.yml` that runs the 47 vdsm integration tests on every PR using GitHub Actions' `ubuntu-24.04` runner with `/dev/kvm` access. Golden image is cached via `actions/cache@v4`, keyed on DSM version + hash of the setup scripts (`scripts/vdsm_setup.py`, `tests/vdsm/setup_dsm.py`, `tests/vdsm/config.py`, `tests/vdsm/golden_image.py`, `tests/vdsm/container.py`). Cache miss path invokes `scripts/vdsm_setup.py --yes` to build a fresh golden image. The new workflow is independent from `ci.yml` so a vdsm flake never blocks unit-test merges, and starts with `continue-on-error: true` until it has a track record of stability. Also adds a `--yes/-y` flag to `vdsm_setup.py` for non-interactive CI use.

### Fixed

- **Malformed YAML config now produces a clean error message** (#26) — `serve`, `check`, and `setup` commands previously dumped a raw `yaml.ScannerError`/`yaml.ParserError` traceback when a user had a typo in their config. All four `load_config()` call sites (`check.py:24`, `main.py:112`, `setup.py:189` for the `-c` path, `setup.py:47` for the discovery path) now catch `yaml.YAMLError` alongside `FileNotFoundError`/`ValueError` and emit the standard red `Error: ...` line before exiting 1. Four new regression tests cover the malformed-YAML path for each command, including the discovery path when `setup` is invoked without `-c`.
- **Remove `synoshare --setopt` recycle bin enablement** (#24) — reverts the recycle bin enablement added in #23. DSM 7.2.2's `synoshare` CLI has no `--setopt` subcommand, so the call always fails (only hidden locally because the pre-existing golden image was built before the change). The revert is safe because `list_recycle_bin` in production code already handles a disabled recycle bin gracefully (returns a friendly "not enabled" message), which is exactly the path `test_02_list_recycle_bin` exercises. `TestRecycleBin` docstring updated to reflect that the test is tolerant of both states.
- **vdsm 47/47: fix all 5 remaining virtual-dsm test failures** (#23) — fixes the 5 vdsm-specific test failures identified in the #22 handoff. Production code improvements: `get_dir_size` now handles DSM error 599 (task completed before status poll) gracefully instead of crashing, returning a best-effort result; `list_recycle_bin` catches error 408 on the `#recycle` path and returns a friendly "recycle bin not enabled" message instead of raising. Test fixes: `test_get_system_info` makes Temperature assertion conditional on non-virtual hardware; `test_search_keyword_finds_directory` creates a "Bambu Studio" directory via the API (DSM search matches names, not content) and searches from the share root with retries; `test_utilization_before_and_during_load` tolerates DirSize failure since it's only a load generator. Setup fix: `setup_dsm.py` enables recycle bin via `synoshare --setopt` after share creation. New unit test for the error 599 path.

- **vdsm full automation: SSH + synoshare for shared folders** (#22) — completes the vdsm golden image setup by SSH-ing into the DSM guest VM (not the QEMU host container) to run `/usr/syno/sbin/synoshare --add` for proper DSM shared folder registration. Exposes SSH port 22, enables SSH via `SYNO.Core.Terminal` API, creates test data without sudo. Fixes FileStation API error 119 by adding `session` parameter to login. 42/47 vdsm tests pass; 5 remaining failures are virtual-dsm behavioral differences (no temp sensor, background task timing, search indexing, recycle bin config).
- **vdsm test infrastructure fixes** (#21) — fixes conftest `instance_id` validation (dots → hyphens), adds admin credentials from golden image metadata, rewrites `setup_dsm.py` with Playwright-based user creation (ExtJS-compatible `type()` input, DOM-based popup removal, wizard step navigation), adds `container_id` property, switches to stronger test password for DSM password policy. Podman KVM passthrough works; 21/47 vdsm tests pass on bare DSM without storage volume.
- **GitHub Sponsors funding configuration** (#20) — adds `.github/FUNDING.yml` to enable the Sponsor button on the repository
- **Test coverage Phase 3 + Phase 4 of #14** (#19) — closes #14. Total coverage 93% → 96%, with Phase 4's `--cov-fail-under=95` guardrail enforced in `pyproject.toml` so future regressions fail CI. Three more files at 100% (`server.py` 57% → 99% — one defensive `if self._client is None` branch unreachable; `core/auth.py` 90% → 100%; `modules/__init__.py` 96% → 100%; `core/formatting.py` 97% → 99%). Test count 457 → 487 (+30 cases). New `TestSharedClientManagerLifecycle` (15 cases) directly tests the lazy `get_client` init, `with_update_notice` clearing logic, signal handler installation including SIGTERM closure invocation, `_cleanup_session` with both running-loop and no-loop paths, and `_bg_update_check` with newer-version, no-update, and error-swallowing scenarios. New `TestPlatformLabel`, `TestCreateServerInstructionPaths` cover the `_platform_label` Darwin/Linux/Windows branches and the `instructions_file` / `custom_instructions` template paths. New `TestDbusSocketMissing`, `TestLoginErrorPaths`, `TestLogout` close the remaining gaps in `core/auth.py` (D-Bus socket-not-found branch, non-2FA SynologyError propagation, "no sid" AuthenticationError, and the three logout paths). No production code touched.
- **Test coverage Phase 2 of #14** (#17) — total coverage 85% → 93%. `cli/version.py` 27% → 100% and `cli/setup.py` 63% → 100%, the two largest gaps remaining after Phase 1. Test count 392 → 457 (+65 cases) across two new test files: `tests/core/test_cli_version.py` (40 cases covering `_get_current_version`/`_get_latest_pypi_version`/`_version_tuple`/`_detect_installer`/`_load_global_state`/`_save_global_state`/`_check_for_update`/`_do_auto_upgrade`/`_do_revert`, with `urlopen` and `subprocess.run` mocked at the boundary), and `tests/core/test_cli_setup.py` (25 cases covering the async helpers `_attempt_login`/`_connect_and_login`/`_setup_login` including the 2FA bootstrap path with device-token storage, plus `_setup_credential_flow` error paths, the `setup` command's discovered-config valid-and-invalid branches, the `_setup_interactive` validation-failure exit, and the `_emit_claude_desktop_snippet` Linux DBUS fallback). No production code touched.
- **Test coverage Phase 1 of #14** (#16) — total coverage 81% → 85%. Five files brought to 100%: `cli/check.py` (51%), `cli/main.py` (56%), `cli/logging_.py` (78%), `modules/system/__init__.py` (23%), `modules/filestation/__init__.py` (70%). Test count 336 → 392 (+56 cases). New test classes in `tests/core/test_cli.py` cover the `_check_login` async path, every top-level option in the `main` group (`--check-update`, `--auto-upgrade`, `--revert`, version-change tracking, auto-upgrade trigger), and the early/configured logging setup. Two new test files (`tests/modules/{system,filestation}/test_register.py`) exercise module registration closure bodies via `server._tool_manager._tools[name].fn` extraction with sentinel `AsyncMock` return values, walking the tool body lines that the prior `assert server is not None` style left uncovered. No production code touched.
- **`CLAUDE.md` documents the per-PR CHANGELOG convention** (#16) — adds an "Adding a CHANGELOG entry on every PR" section under "Common Tasks" specifying that every PR updates `## Unreleased` in `CHANGELOG.md` using strict Keep a Changelog categories (`### Added`, `### Changed`, `### Fixed`). Updates the "Bumping the version for a release" steps to rename `## Unreleased` to `## <version> (<date>)` and add a fresh empty `## Unreleased` section, plus notes that the `publish.yml` `github-release` awk extractor (`## <version>( |\()`) walks past `## Unreleased` harmlessly during tag-push releases.

### Changed

- **`pyproject.toml` is now the single source of truth for the project version** (#15) — closes #11. Adds `scripts/sync-server-json.py` (stdlib only, uses `tomllib`) which propagates `[project].version` from `pyproject.toml` into `server.json`'s two version fields (top-level and `packages[0].version`). New `version-sync` CI job runs the script with `--check` and fails any PR where `server.json` has drifted from `pyproject.toml`. CI's `lint` and `typecheck` jobs were extended to cover `scripts/` (a pre-existing gap, dormant until this PR introduced the first `.py` file in that directory). Release flow documented in `CLAUDE.md`: bump `pyproject.toml`, run the sync script, run `uv lock`, update `CHANGELOG.md`, commit. Never edit `server.json`'s version fields by hand.

### Fixed

- **`publish.yml` `github-release` job is now idempotent** (#13) — closes #12. The release-creation step previously failed with HTTP 422 if a Release for the tag already existed (e.g., hand-crafted ahead of the workflow run). It now reads notes from `CHANGELOG.md` via `awk` extraction (skipping the `## <version>` heading, capturing up to the next `## `) and uses `gh release view` → `gh release edit` if a Release exists, `gh release create` otherwise. Falls back to `--generate-notes` with a `::warning::` annotation if `CHANGELOG.md` has no matching entry. Hardened against shell injection by passing values via `env:` instead of `${{ }}` interpolation.

## 0.5.0 (2026-04-10)

### Changed

- **Error responses are now structured JSON envelopes with `isError=true`** (#9)
  - Tool errors previously returned human-readable strings like `[!] List files failed: ...`. They now raise `ToolError` with a JSON envelope:
    ```json
    {
      "status": "error",
      "error": {
        "code": "not_found",
        "message": "List files failed (DSM error 408): No such file or directory",
        "retryable": false,
        "suggestion": "Use list_files or search_files to find the correct path.",
        "help_url": "https://github.com/cmeans/mcp-synology/blob/main/docs/error-codes.md#not_found"
      }
    }
    ```
  - The MCP SDK wraps this in a `CallToolResult` with `isError=true`, which is the correct protocol signal for tool failures. Clients that only display text content see the JSON directly; clients that key off `isError` now get proper failure signaling.
  - All 13 possible `code` values are documented in [`docs/error-codes.md`](docs/error-codes.md), with per-code sections covering symptoms, causes, retryability, and concrete fixes.
  - This is a client-visible behavior change. Any client that was pattern-matching the old `[!] ... failed:` text format will need to update — parse the JSON envelope instead, or key off `isError` at the MCP protocol level.

### Added

- **`ErrorCode(StrEnum)` in `core/errors.py`** — single source of truth for every code the server can emit. `error_response(code: ErrorCode)` is typed so call-site typos become mypy errors rather than silent envelopes with missing `help_url`.
- **`docs/error-codes.md`** — 12-section reference covering every surfaceable `ErrorCode` member. Each section has root causes, fix steps with specific DSM control-panel paths, and explicit retryability statements. `session_expired` is intentionally omitted (auto-retried by the core client; never surfaced to users).
- **Multi-invariant drift test** (`tests/core/test_help_urls.py`) — enforces that `ErrorCode` ↔ `HELP_URLS` registry ↔ `docs/error-codes.md` anchors stay in sync in all directions. Adding a new code without its doc section, or renaming a section without updating the registry, fails CI.
- **`errno.ENOSPC` detection** in `download_file` OSError fallback — replaces locale-dependent substring matching on error text, so local disk-full is correctly reported as `disk_full`/`retryable=True` regardless of OS language or DSM version.
- **Unit test coverage** for `modules/system/info.py` and `modules/system/utilization.py` — both modules previously had no unit tests (13% coverage), now at 99–100%.

### Fixed

- **`unavailable` `retryable` semantic is now consistent across modules** — `system/utilization.py` previously reported `retryable=False` while `system/info.py` reported `retryable=True` for the same condition ("API responded but returned no data"). Both now use `retryable=True` with an inline comment explaining the transient-condition rationale.
- **`download_file` disk-full is now reported with the same code in both detection paths** — the pre-flight branch (via `shutil.disk_usage`) and the OSError fallback previously disagreed: pre-flight emitted `disk_full`/retryable=True, fallback emitted `filesystem_error`/retryable=False despite a "Free space on the local disk" suggestion. Both now emit `disk_full`/retryable=True when disk-full is the actual cause.
- **`error_response()` is safe against non-JSON-serializable `value` arguments** — `json.dumps(..., default=str)` prevents a future caller passing `bytes` or a custom object from crashing the error handler mid-envelope.

## 0.4.1 (2026-04-07)

### Fixed

- **Claude Desktop config** — setup snippet now uses `uvx mcp-synology` instead of bare command, which failed with ENOENT on systems where `~/.local/bin` isn't in Claude Desktop's PATH
- **Migration script** — now auto-updates `claude_desktop_config.json` (detects and rewrites old synology-mcp entries), creates `.json.bak` backup before writing, preserves extra args, handles `--config=value` equals syntax
- **README** — added migration section near top for users upgrading from synology-mcp, standardized all examples on `uvx`

### Added

- **MCP registry files** — `glama.json` for Glama.ai directory, `server.json` for official MCP Registry
- **PyPI ownership verification** — `mcp-name` comment in README for official registry validation
- **GitHub issue templates** — bug report, feature request, platform test report

## 0.4.0 (2026-04-05)

### Breaking Changes

- **Package renamed** — `synology-mcp` → `mcp-synology` (distribution, CLI command, config paths, state paths, keyring service)
- **Python import renamed** — `synology_mcp` → `mcp_synology`
- **Config directory** — `~/.config/synology-mcp/` → `~/.config/mcp-synology/`
- **State directory** — `~/.local/state/synology-mcp/` → `~/.local/state/mcp-synology/`
- **Keyring service** — `synology-mcp/{instance_id}` → `mcp-synology/{instance_id}` (re-run `mcp-synology setup`)
- **DSM session/device name** — `SynologyMCP` → `MCPSynology`
- **License** — MIT → Apache 2.0

### Features

- **File transfer tools** — 2 new tools for uploading and downloading files:
  - `upload_file` — upload local files to NAS with overwrite control, custom filenames, and progress reporting (WRITE tier)
  - `download_file` — download NAS files to local disk with pre-flight disk space check, streaming writes, partial file cleanup on failure, and progress reporting (READ tier)
  - Large file warnings when transfers exceed 1 GB
- **Project icons** — light/dark SVGs, PNGs (16–256px), and favicon.ico exposed via MCP `icons` parameter
- **TestPyPI workflow** — dedicated `test-publish.yml` for manual dispatch; `publish.yml` simplified to tag-only PyPI publishing
- **Virtual DSM test framework** — container-based integration testing with golden image save/restore, Playwright-based DSM wizard automation, and Podman/Docker auto-detection (`tests/vdsm/`)

### Migration

A migration script handles config, state, and keyring automatically:

```bash
uv tool install mcp-synology
python scripts/migrate-from-synology-mcp.py          # dry run — preview changes
python scripts/migrate-from-synology-mcp.py --apply  # apply changes
```

Then update Claude Desktop config: change `"command"` from `"synology-mcp"` to `"mcp-synology"`.

## 0.3.1 (2026-03-18)

### Features

- **System monitoring module** — 2 new read-only tools:
  - `get_system_info` — model, firmware, CPU specs, RAM, temperature, uptime (works for all users via `SYNO.DSM.Info`, supplemented by `SYNO.Core.System` for admin users)
  - `get_resource_usage` — live CPU load, memory usage, disk I/O per drive, network throughput per interface (requires admin account via `SYNO.Core.System.Utilization`)

### Bug Fixes

- **Orphaned background tasks** — Search, DirSize, CopyMove, and Delete operations now use `try/finally` to ensure tasks are always stopped. Previously, errors during polling could skip cleanup, leaving `synoscgi` processes consuming CPU indefinitely on the NAS
- **Cleanup failures logged** — replaced silent `contextlib.suppress` with warning-level log messages
- **Always use GET** — removed POST logic entirely. DSM 7.1 reports `requestFormat=JSON` on all FileStation APIs even at v2, causing silent failures with POST

### Code Quality

- Removed unused `noqa` directives
- `datetime.now(tz=UTC)` instead of naive `datetime.now()`
- `Self` return type for `__aenter__`
- `int | float` simplified to `float` in type hints
- `list.extend` with generators instead of append loops
- Store `asyncio.create_task` references to prevent GC
- Move inline imports (`asyncio`, `time`) to module top level
- Initialize task attributes in `__init__`

### Documentation

- README restructured: modules listed separately from features, env-var mode shows Claude Desktop config, 2FA token expiry clarified, config hierarchy shown in YAML examples, custom instructions use cases expanded
- DEVELOPMENT.md extracted from README: build commands, integration test setup, design docs
- CLAUDE.md updated: v0.3.x status, GET-only rule, version pinning, search gotchas, background task cleanup pattern
- Integration tests expanded to 37 (system info, resource usage with admin fixture, utilization under load)

## 0.3.0 (2026-03-18)

Major refactor: CLI split, module registration system, DSM API fixes, integration tests.

### Breaking Changes

- **CLI is now a package** — `src/mcp_synology/cli.py` split into `cli/` package with 6 submodules (main, setup, check, version, logging_). Backward-compatible re-exports via `cli/__init__.py`

### Bug Fixes

- **Always use GET for DSM API calls** — DSM 7.1 reports `requestFormat=JSON` on all FileStation APIs even at v2, causing silent failures with POST. All requests now use GET exclusively
- **Pin CopyMove, Delete, Search to v2** — v3 JSON request format incompatible with our comma-separated path encoding
- **Search finds directories** — always send `filetype=all` (DSM defaults to `"file"`, excluding directories from results)
- **Search wildcard wrapping** — bare keywords auto-wrapped with `*...*` (e.g., `"Bambu"` → `"*Bambu*"`) so substring matching works
- **Search poll retry** — don't trust `finished=True` with 0 results until 3+ polls, preventing false positives on non-indexed shares
- **Orphaned background task cleanup** — all async tasks (Search, DirSize, CopyMove, Delete) now use `try/finally` to ensure stop/clean is called. Previously, errors during polling would skip cleanup, leaving orphaned `synoscgi` processes consuming CPU indefinitely
- **Cleanup failures logged** — replaced silent `contextlib.suppress` with warning-level log messages on stop/clean failure
- **Copy/move error detection** — check `error` field in status response, not just `finished` flag. Added error codes 1000-1002 for copy/move failures
- **Error 600 mapped** — search folder access denied now returns actionable message

### Features

- **Generic module registration** — `RegisterContext` + `SharedClientManager` pattern replaces 400-line monolithic `_register_filestation()`. New modules just define `register(ctx)` functions
- **MCP tool annotations** — `readOnlyHint`, `destructiveHint`, `idempotentHint` from mcp.types, with `default_annotations()` helper
- **Multi-NAS server identity** — server name includes `display_name` (e.g., `synology-nas01`). Template variables `{display_name}`, `{instance_id}`, `{host}`, `{port}` in instruction files
- **Custom instructions** — `custom_instructions` config field (prepended to built-in instructions) and `instructions_file` (full replacement) for non-clone installs
- **Integration test suite** — 32 tests against real NAS: connection, listing, search, metadata, copy/move/rename/delete lifecycle, recycle bin, error handling
- **Configurable test paths** — `test_paths` in `integration_config.yaml` for NAS-specific folders

### Documentation

- CLAUDE.md updated: v0.3.0 status, GET-only rule, version pinning, search gotchas, background task cleanup pattern, integration test setup
- README: multi-NAS setup with aliases, custom instructions, Linux DBUS note
- Config spec: `alias`, `custom_instructions`, `instructions_file` fields
- Power-user example: alias and instruction configuration

## 0.2.2 (2026-03-17)

Code quality fixes from second external review.

### Bug Fixes

- **No more `assert` in production** — replaced 8 bare asserts in server.py and cli.py with explicit `if`/`raise RuntimeError` checks that survive `python -O`
- **Renamed builtin-shadowing exceptions** — `PermissionError` → `SynologyPermissionError`, `FileExistsError` → `SynologyFileExistsError` to prevent confusion with Python builtins
- **Removed fragile is-directory heuristic** — copy/move/delete output no longer guesses file vs folder icons; plain names until type is known
- **Session cleanup on shutdown** — `atexit` handler and SIGTERM/SIGINT signal handlers call `AuthManager.logout()` to free DSM sessions
- **Search truncation notice** — when results exceed limit, output now shows "(showing 500 of 1,234 — increase limit to see more)"
- **Removed dead `poll_async_task` helper** — unused generic polling function removed from helpers.py

### Documentation

- README install updated to `uv tool install mcp-synology` (PyPI) instead of git URL

## 0.2.1 (2026-03-18)

### Bug Fixes

- **Sort by modified date** — map common field names (modified, date, created) to DSM API fields (mtime, crtime, etc.)
- **Narrow exception handling** — replaced all broad `except Exception` with specific types across cli.py
- **Typed lazy state** — server init state is now a dataclass instead of untyped dict
- **Publish workflow runs tests** — broken code can no longer publish to PyPI
- **Async fixture type hint** — proper `AsyncGenerator` annotation
- **Docs accuracy** — D-Bus wording, README install section title

## 0.2.0 (2026-03-18)

Quality and correctness fixes from critical code review and live testing.

### Bug Fixes

- **Update check no longer blocks first tool call** — PyPI check runs in background thread via asyncio, tool response returns immediately
- **Deduplicated login flows** — extracted shared `_attempt_login()`, eliminating ~100 lines of duplicate 2FA handling code
- **Instance ID accepts uppercase** — `MyNAS` silently becomes `mynas` instead of erroring about invalid characters
- **Search timeout accurate** — uses `time.monotonic()` instead of counting sleep intervals, which excluded request duration
- **Search pattern fix** — `*.mkv` correctly uses DSM extension filter instead of broken pattern parameter
- **Pagination correct with hidden #recycle** — changed default to show `#recycle` (avoids offset math bugs); users can still hide via config
- **Auth error 402 correctly identified** — Auth-specific error code map prevents FileStation "System too busy" misidentification
- **Session parameter removed from login** — was causing 402 errors on some DSM configurations
- **D-Bus socket not found now logged** — was silently failing; helps diagnose keyring issues on Linux
- **Directory detection improved** — better heuristic in copy/move/delete output formatting

### Features

- **MCP tool annotations** — all 12 tools annotated with readOnlyHint, destructiveHint, idempotentHint
- **Version management** — `--check-update`, `--auto-upgrade enable|disable`, `--revert [VERSION]`
- **In-session update notices** — first tool response in Claude Desktop shows notice if newer version on PyPI
- **`check_for_updates` config flag** — set to false to silence update notices
- **Instruction files** — MCP server instructions moved to external `.md` file for easier maintenance
- **Publish workflow** — GitHub Actions publishes to PyPI on tag push, TestPyPI on manual dispatch
- **Auth error codes 400-410** — full Auth API error map with actionable messages
- **File Station error codes 900, 1100, 1101** — filesystem permission denied, unsupported target

### Documentation

- README rewritten with `uv tool install` Quick Start (not git clone)
- Updates section documenting version management
- Credentials doc expanded with 2FA device tokens, platform table, Linux D-Bus

## 0.1.0 (2026-03-17)

Initial release.

### Features

- **File Station module** — 12 tools for managing files on Synology NAS:
  - READ: list_shares, list_files, list_recycle_bin, search_files, get_file_info, get_dir_size
  - WRITE: create_folder, rename, copy_files, move_files, delete_files, restore_from_recycle_bin
- **Interactive setup** — `mcp-synology setup` creates config, stores credentials, handles 2FA, emits Claude Desktop snippet
- **2FA support** — auto-detected device token bootstrap with silent re-authentication
- **Secure credentials** — OS keyring integration (macOS Keychain, Windows Credential Manager, Linux GNOME Keyring / KWallet)
- **Linux D-Bus auto-detection** — keyring works from Claude Desktop without manual env var configuration
- **Multi-NAS** — separate configs, credentials, and state per instance via `instance_id`
- **Env-var-only mode** — `SYNOLOGY_HOST` without a config file synthesizes a default config
- **Permission tiers** — READ or WRITE per module, enforced at tool registration
- **Configurable timeouts** — per-operation overrides for search, copy/move, delete, dir size
- **Debug logging** — passwords masked, only relevant APIs logged, `--verbose` flag

### Configuration

- `check_for_updates` — disable PyPI update checks (default: true)
- `alias` — friendly display name for the NAS instance
- `instance_id` — arbitrary identifier that keys credentials, state, and config files
- Per-operation timeouts: `search_timeout`, `copy_move_timeout`, `delete_timeout`, `dir_size_timeout`, `search_poll_interval`

### Tested against

- Synology DS1618+ running DSM 7.1.1-42962 Update 6
- All 12 File Station tools verified via Claude Desktop
- 2FA login with device token re-authentication
- 243 automated tests, 84% code coverage
