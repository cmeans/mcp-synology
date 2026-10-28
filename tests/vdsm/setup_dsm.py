"""DSM setup automation for virtual-dsm test instances.

Includes:
- Playwright-based wizard automation (first-boot setup, fully headless)
- Playwright-based post-wizard configuration (user creation via Control Panel)
- SSH into the DSM guest VM for shared folder creation via synoshare CLI

The undocumented SYNO.Core.* write APIs do not work on virtual-dsm
(error 105/403), so user creation is automated through the DSM web UI.
Shared folders are created by SSH-ing into the DSM guest (the QEMU VM
inside the container) and running /usr/syno/sbin/synoshare, which
registers proper DSM shared folders visible to FileStation.

DSM 7 uses a windowed desktop UI with frequent overlay masks. Clicks use
JavaScript dispatch or Playwright's force=True to bypass overlay interception.
"""

from __future__ import annotations

import json
import logging
import time
from contextlib import contextmanager
from typing import TYPE_CHECKING, Any

import httpx

from tests.vdsm.config import (
    BAKE_VERSION,
    DEFAULT_ADMIN_USER,
    DEFAULT_FRESH_PASSWORD,
    DEFAULT_FRESH_USER,
    DEFAULT_TEST_PASSWORD,
    DEFAULT_TEST_USER,
    DSM_API_POLL_INTERVAL,
    DSM_BOOT_TIMEOUT,
    DSM_TIMEZONES,
)

if TYPE_CHECKING:
    from collections.abc import Iterator

logger = logging.getLogger(__name__)

_DS_APP_ID = "SYNO.SDS.DownloadStation.Application"
_PHOTOS_APP_ID = "SYNO.Foto.AppInstance"
_SYNO_PATH = "PATH=/usr/syno/bin:/usr/syno/sbin:/usr/bin:/bin:/usr/sbin:/sbin"

# Synology Photos and the packages it depends on, installed in this order (#258).
# install_from_server doesn't pull in dependencies, and DSM 7.1 and 7.2.2 want
# different Node.js versions. On 7.3.2 and 7.4.1 Package Center's server list
# (SYNO.Core.Package.Server list, 2026-10-19) gives Photos 1.9.1-10928 needing
# Node.js_v20 and SynologyApplicationService 1.9.0, which needs Node.js_v22 (#307).
_PHOTOS_NODE_20_22 = ("Node.js_v20", "Node.js_v22", "SynologyApplicationService", "SynologyPhotos")
# Snapshot Replication, for share snapshots (the Storage module, #49). One package with
# nothing to install first, on all four versions (installed by hand on each, 2026-10-28).
# The gate is SYNO.Btrfs.Replica.Core, the one API the package adds on 7.1. Not
# SYNO.DR.Node: base DSM registers that without the package (2026-10-28).
SNAPSHOT_PACKAGE = "ReplicationService"
_SNAPSHOT_APIS = ("SYNO.Btrfs.Replica.Core",)

PHOTOS_PACKAGES: dict[str, tuple[str, ...]] = {
    "7.1": ("Node.js_v18", "SynologyApplicationService", "SynologyPhotos"),
    "7.2.2": _PHOTOS_NODE_20_22,
    "7.3.2": _PHOTOS_NODE_20_22,
    "7.4.1": _PHOTOS_NODE_20_22,
}


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _click_text(page: Any, text: str, *, timeout: int = 3) -> bool:
    """Click a visible element containing exact text via JavaScript.

    Returns True if an element was found and clicked.
    """
    clicked = page.evaluate(f"""() => {{
        const els = [...document.querySelectorAll('a, button, span, div, p, label')];
        const el = els.find(e => e.textContent.trim() === {text!r}
                                 && e.offsetParent !== null);
        if (el) {{ el.click(); return true; }}
        return false;
    }}""")
    if clicked:
        time.sleep(timeout)
    return clicked


# ---------------------------------------------------------------------------
# Boot wait
# ---------------------------------------------------------------------------


def wait_for_api(base_url: str, timeout: int = DSM_BOOT_TIMEOUT) -> None:
    """Poll DSM API info endpoint until it responds."""
    url = f"{base_url}/webapi/query.cgi?api=SYNO.API.Info&version=1&method=query&query=ALL"
    start = time.monotonic()
    deadline = start + timeout

    while time.monotonic() < deadline:
        elapsed = int(time.monotonic() - start)
        try:
            resp = httpx.get(url, timeout=10, verify=False)  # noqa: S501
            if resp.status_code == 200:
                data = resp.json()
                if data.get("success"):
                    logger.info("DSM API ready after %ds", elapsed)
                    return
        except (httpx.ConnectError, httpx.ReadTimeout, httpx.ConnectTimeout):
            pass

        logger.debug("Waiting for DSM API... (%ds)", elapsed)
        print(f"  Waiting for DSM API... ({elapsed}s)")
        time.sleep(DSM_API_POLL_INTERVAL)

    elapsed = int(time.monotonic() - start)
    msg = f"DSM API did not become ready within {elapsed}s"
    raise TimeoutError(msg)


# ---------------------------------------------------------------------------
# First-boot wizard
# ---------------------------------------------------------------------------


def complete_wizard(base_url: str, admin_user: str, admin_password: str) -> None:
    """Automate the DSM first-boot setup wizard using Playwright."""
    try:
        from playwright.sync_api import sync_playwright
    except ImportError as e:
        msg = "Playwright is required for wizard automation: uv sync --extra vdsm"
        raise ImportError(msg) from e

    print("  Automating DSM setup wizard with Playwright...")

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        page = browser.new_page(viewport={"width": 1280, "height": 900})

        try:
            page.goto(base_url, wait_until="networkidle", timeout=60000)
            time.sleep(3)

            print("    [1/6] Welcome page (waiting for wizard to load)...")
            page.wait_for_selector(".welcome-page-btn", timeout=120000)
            page.click(".welcome-page-btn")
            time.sleep(2)

            print("    [2/6] Account setup...")
            page.fill("input[name=device_name]", "VirtualDSM")
            page.fill("input[name=nas_account]", admin_user)
            passwords = [
                pw for pw in page.query_selector_all("input[name=password]") if pw.is_visible()
            ]
            if len(passwords) < 2:
                msg = "Could not find password fields in wizard"
                raise RuntimeError(msg)
            passwords[0].fill(admin_password)
            passwords[1].fill(admin_password)
            page.click("button:has-text('Next')")
            time.sleep(3)

            error = page.query_selector(".v-tooltip-error, .error-msg")
            if error and error.is_visible():
                error_text = error.inner_text()
                msg = f"Wizard account setup failed: {error_text}"
                raise RuntimeError(msg)

            print("    [3/6] Update options...")
            page.click("button:has-text('Next')")
            time.sleep(3)

            print("    [4/6] Synology Account (skipping)...")
            page.click("button:has-text('Skip')")
            time.sleep(3)

            print("    [5/6] Device Analytics (declining)...")
            page.click("button:has-text('Submit')")
            time.sleep(3)

            print("    [6/6] Package install offer (declining)...")
            no_btn = page.query_selector("button:has-text('No, thanks')")
            if no_btn and no_btn.is_visible():
                no_btn.click()
                time.sleep(3)
            else:
                logger.info("No package install prompt found — skipping")

            print("  Wizard complete!")

        finally:
            browser.close()


# ---------------------------------------------------------------------------
# Test user, shared folders and test data via SSH into DSM guest
# ---------------------------------------------------------------------------

_SYNOSHARE = "/usr/syno/sbin/synoshare"

from tests.vdsm.ssh import ssh_exec as _ssh_exec  # noqa: E402


def _enable_ssh(base_url: str, admin_user: str, admin_password: str) -> None:
    """Enable SSH service on the DSM guest via API."""
    resp = httpx.get(
        f"{base_url}/webapi/entry.cgi",
        params={
            "api": "SYNO.API.Auth",
            "version": "6",
            "method": "login",
            "account": admin_user,
            "passwd": admin_password,
            "format": "sid",
            "session": "setup",
            "enable_syno_token": "yes",
        },
        timeout=30,
        verify=False,  # noqa: S501
    )
    login_body = resp.json()
    if not login_body.get("success"):
        code = login_body.get("error", {}).get("code", 0)
        msg = f"SSH setup login failed (error {code})"
        raise RuntimeError(msg)

    data = login_body["data"]
    ssh_resp = httpx.post(
        f"{base_url}/webapi/entry.cgi",
        data={
            "api": "SYNO.Core.Terminal",
            "version": "3",
            "method": "set",
            "enable_ssh": "true",
            "ssh_port": "22",
        },
        # The session as the id cookie, never _sid: DSM misreads some session IDs as
        # _sid (#176).
        headers={"X-SYNO-TOKEN": data.get("synotoken", ""), "Cookie": f"id={data['sid']}"},
        timeout=30,
        verify=False,  # noqa: S501
    )
    ssh_body = ssh_resp.json()
    if not ssh_body.get("success"):
        code = ssh_body.get("error", {}).get("code", 0)
        msg = f"Failed to enable SSH (error {code})"
        raise RuntimeError(msg)

    print("    SSH enabled, waiting for sshd...")
    time.sleep(12)


def _ssh(
    host: str,
    port: int,
    password: str,
    cmd: str,
    *,
    sudo: bool = True,
) -> tuple[int, str]:
    """Run a command inside the DSM guest via SSH.

    Thin wrapper for the shared helper in tests/vdsm/ssh.py — kept so
    callers in this module don't have to change.
    """
    return _ssh_exec(host, port, password, cmd, sudo=sudo)


_SYNOUSER = "/usr/syno/sbin/synouser"


def _create_user_via_ssh(
    host: str,
    ssh_port: int,
    admin_password: str,
    username: str,
    password: str,
) -> None:
    """Create the local, non-admin test user with the synouser CLI.

    Idempotent: an existing user is left in place (re-bakes on a reused
    storage dir). Failure is fatal — every non-admin test depends on it.
    """
    rc, _ = _ssh(host, ssh_port, admin_password, f"{_SYNOUSER} --get {username}")
    if rc == 0:
        print(f"    Test user {username} already exists")
        return
    # synouser --add <user> <pwd> <full name> <expired 0|1> <mail> <app privilege>
    rc, out = _ssh(
        host,
        ssh_port,
        admin_password,
        f"{_SYNOUSER} --add {username} '{password}' 'MCP test user' 0 '' 0",
    )
    if rc != 0:
        msg = f"synouser --add {username} failed (rc={rc}): {out}"
        raise RuntimeError(msg)
    print(f"    Created test user: {username}")


def _create_shared_folders_via_ssh(
    host: str,
    ssh_port: int,
    admin_password: str,
    test_users: tuple[str, ...],
) -> None:
    """Create shared folders and test data via SSH into the DSM guest.

    Uses /usr/syno/sbin/synoshare to register proper DSM shared folders
    on the auto-created Volume 1. Files created this way are visible to
    FileStation and all DSM APIs.
    """

    def ssh(cmd: str, *, sudo: bool = True) -> tuple[int, str]:
        return _ssh(host, ssh_port, admin_password, cmd, sudo=sudo)

    # Create shared folders with synoshare (requires sudo).
    # Share creation is the core purpose of this function — failure is fatal.
    for name, desc in [("testshare", "MCP test"), ("writable", "MCP writable")]:
        rc, out = ssh(f"{_SYNOSHARE} --add {name} '{desc}' /volume1/{name} '' '' '' 0 0")
        if rc == 0:
            print(f"    Created share: {name}")
        else:
            msg = f"synoshare --add {name} failed (rc={rc}): {out}"
            raise RuntimeError(msg)

    # Note: DSM 7.2.2's `synoshare` CLI has no supported command to toggle the
    # per-share recycle bin. Shares created via `synoshare --add` have recycle
    # bin disabled. This is fine — `list_recycle_bin` in production code
    # handles the disabled case gracefully (returns a friendly
    # "Recycle bin is not enabled" message), and `test_02_list_recycle_bin`
    # exercises exactly that path.

    # Verify shares registered
    rc, out = ssh(f"{_SYNOSHARE} --enum ALL")
    print(f"    Shares: {out}")

    # Make shares world-writable so test data can be created without sudo
    ssh("chmod -R 777 /volume1/testshare /volume1/writable")

    # Create test directories and data (no sudo — dirs are 777)
    # Note: DSM search matches file/directory *names*, not content.
    # "Bambu Studio" directory ensures the search_keyword "Bambu" finds a result.
    ssh(
        "mkdir -p /volume1/testshare/Documents"
        " '/volume1/testshare/Documents/Bambu Studio'"
        " /volume1/testshare/Media",
        sudo=False,
    )
    ssh(
        "echo 'This is a sample report for MCP integration testing.'"
        " > /volume1/testshare/Documents/report.txt",
        sudo=False,
    )
    ssh(
        "echo 'Bambu Lab X1C 3D printer configuration notes.'"
        " > '/volume1/testshare/Documents/Bambu Studio/config.txt'",
        sudo=False,
    )
    ssh("printf '\\x1a\\x45\\xdf\\xa3' > /volume1/testshare/Media/sample.mkv", sudo=False)

    rc, out = ssh("ls -la /volume1/testshare/Documents/", sudo=False)
    print(f"    Test data:\n{out}")

    # Force DSM's file indexer to register the test data immediately.
    # Without this, search tests against /testshare/Documents/Bambu Studio
    # are flaky on freshly-booted vdsm — DSM Universal Search doesn't crawl
    # non-indexed shares promptly, so search_files returns "0 results found"
    # for up to several minutes after the data is created. Calling
    # `synoindex -A -d <path>` adds the directory subtree to the search
    # index immediately. Best-effort: if synoindex isn't available on this
    # DSM build, log a warning and continue — the failure mode is just the
    # pre-existing search-flake, not a setup blocker.
    rc, out = ssh("/usr/syno/bin/synoindex -A -d /volume1/testshare/Documents")
    if rc == 0:
        print("    Search index: refreshed via synoindex -A -d /testshare/Documents")
    else:
        print(f"    Warning: synoindex -A -d returned rc={rc}: {out}")
    rc, out = ssh("/usr/syno/bin/synoindex -A -d /volume1/testshare/Media")
    if rc == 0:
        print("    Search index: refreshed via synoindex -A -d /testshare/Media")
    else:
        print(f"    Warning: synoindex -A -d /testshare/Media returned rc={rc}: {out}")

    # Set ACL permissions for the test users (requires sudo)
    for test_user in test_users:
        for name in ["testshare", "writable"]:
            rc, out = ssh(f"{_SYNOSHARE} --setuser {name} RW + {test_user}")
            if rc == 0:
                print(f"    Permissions set on {name} for {test_user}")
            else:
                print(f"    Warning: setuser {name} rc={rc}: {out}")


# ---------------------------------------------------------------------------
# Verification
# ---------------------------------------------------------------------------


def _verify_setup_via_api(base_url: str, username: str, password: str) -> bool:
    """Verify setup by logging in via API and listing shares."""
    params = {
        "api": "SYNO.API.Auth",
        "version": "6",
        "method": "login",
        "account": username,
        "passwd": password,
        "format": "sid",
        "session": "FileStation",
    }
    try:
        resp = httpx.get(
            f"{base_url}/webapi/entry.cgi",
            params=params,
            timeout=30,
            verify=False,  # noqa: S501
        )
        body = resp.json()
        if not body.get("success"):
            print(f"    Verify: login as {username} failed")
            return False

        sid = body["data"]["sid"]

        list_params = {
            "api": "SYNO.FileStation.List",
            "version": "2",
            "method": "list_share",
        }
        resp2 = httpx.get(
            f"{base_url}/webapi/entry.cgi",
            params=list_params,
            headers={"Cookie": f"id={sid}"},  # not _sid (#176)
            timeout=30,
            verify=False,  # noqa: S501
        )
        body2 = resp2.json()
        if body2.get("success"):
            shares = body2.get("data", {}).get("shares", [])
            share_names = [s.get("name", "") for s in shares]
            print(f"    Verify: shares visible to {username}: {share_names}")
            return bool(share_names)
        else:
            code = body2.get("error", {}).get("code", 0)
            print(f"    Verify: list_share failed (code {code})")
            return False
    except Exception:
        logger.warning("Verification failed", exc_info=True)
        return False


# ---------------------------------------------------------------------------
# Download Station install via synopkg (SSH)
# ---------------------------------------------------------------------------


def _install_download_station_via_ssh(
    host: str,
    ssh_port: int,
    admin_password: str,
    base_url: str,
    *,
    install_timeout_sec: int = 900,
) -> None:
    """Install Download Station via synopkg over SSH.

    Replaces the original Playwright/Package-Center UI flow, which proved
    unautomatable against Synology's heavily customized DSM 7 Package
    Center (Ext 3.4.1 + syno-ux checkbox = no synthetic-click path
    dismisses the ToS modal). SSH bypasses the UI entirely and reuses
    the `_ssh` helper proven for share creation.

    Three-step start:
      1. `synopkg install_from_server` to install (~3s)
      2. `synopkg start` is a no-op on vDSM — systemd-unit lookup fails
         with status_code 263 even though install succeeded
      3. `/var/packages/DownloadStation/scripts/start-stop-status start`
         runs the package's own SysV-style start script which actually
         starts the DS services (synopkg/synosystemd is the misleading
         layer; the package internals work fine)

    Gate is the DSM API cache (SYNO.API.Info query for
    SYNO.DownloadStation.Task), NOT `synopkg status` — the latter keeps
    reporting stopped on vDSM regardless of actual service state.

    Idempotent on cached golden images: returns early if the API is
    already registered.
    """

    def ssh(cmd: str, *, sudo: bool = True, timeout: int = 30) -> tuple[int, str]:
        return _ssh_exec(host, ssh_port, admin_password, cmd, sudo=sudo, timeout=timeout)

    # All DS APIs the downstream bake steps + the vdsm tests touch.
    # Different DS APIs register at different times after start —
    # Task tends to appear first, Info (used by setconfig at [6/6]) lags.
    # Gating on the full set avoids the "Task registered but Info isn't"
    # race that bit CI on 43a2526.
    required_apis = (
        "SYNO.DownloadStation.Task",
        "SYNO.DownloadStation.Info",
        "SYNO.DownloadStation.Statistic",
        "SYNO.DownloadStation.Schedule",
    )

    def missing_ds_apis() -> list[str]:
        """Return the subset of required DS APIs NOT yet in the API cache."""
        try:
            resp = httpx.get(
                f"{base_url}/webapi/query.cgi",
                params={
                    "api": "SYNO.API.Info",
                    "version": "1",
                    "method": "query",
                    "query": ",".join(required_apis),
                },
                timeout=10,
                verify=False,  # noqa: S501
            )
            data = resp.json().get("data", {})
            return [api for api in required_apis if not data.get(api)]
        except (httpx.HTTPError, ValueError):
            return list(required_apis)

    # Idempotency: a cached golden image rebuilt against an already-DS
    # image will have all APIs registered already.
    if not missing_ds_apis():
        print("    Download Station already running (all DS APIs in cache)")
        return
    # Install (~3s on a warm Synology package server, 30-60s on cold cache, and
    # minutes when Synology's download servers are slow; ssh_exec's default is 30 s).
    print("    Running synopkg install_from_server DownloadStation...")
    start = time.monotonic()
    rc, out = ssh(
        "/usr/syno/bin/synopkg install_from_server DownloadStation",
        sudo=True,
        timeout=install_timeout_sec,
    )
    elapsed = int(time.monotonic() - start)
    if rc != 0:
        msg = f"synopkg install_from_server failed after {elapsed}s (rc={rc}): {out.strip()}"
        raise RuntimeError(msg)
    print(f"    synopkg install completed in {elapsed}s")
    # Verify install via the package list (cheap sanity check).
    rc, out = ssh("/usr/syno/bin/synopkg list --name DownloadStation", sudo=False)
    if "DownloadStation" not in out:
        msg = f"DownloadStation not in package list after install: {out.strip()[:200]}"
        raise RuntimeError(msg)
    # Run synopkg start (typically a no-op on vDSM but harmless on real DSM)
    # then the package's own start script which actually starts the DS
    # services on vDSM. start-stop-status emits benign warnings about
    # python2 (eMule plugin only) and a missing amule statistics file —
    # both are unrelated to the core download/list/edit task surface.
    ssh("/usr/syno/bin/synopkg start DownloadStation", sudo=True)
    rc, out = ssh(
        "/var/packages/DownloadStation/scripts/start-stop-status start 2>&1",
        sudo=True,
    )
    print(f"    start-stop-status start: rc={rc}")
    if "active" not in out.lower() and "started" not in out.lower():
        # No "active" marker — likely failed to start. Surface the output
        # so we can debug; don't silently swallow.
        print(f"    start-stop-status output:\n{out.strip()[:600]}")
    # Poll the API cache for ALL required DS APIs — gating on Task alone
    # is insufficient because Info (used by setconfig at [6/6]) registers
    # on a different timeline. synopkg status keeps reporting stopped on
    # vDSM regardless of actual service state, so it's not a useful gate.
    deadline = time.monotonic() + 120
    missing: list[str] = list(required_apis)
    while time.monotonic() < deadline:
        missing = missing_ds_apis()
        if not missing:
            print(f"    All {len(required_apis)} DS APIs registered in DSM API cache.")
            break
        time.sleep(3)
    else:
        msg = (
            f"DS APIs still missing from cache after 120s: {missing}. "
            "Package services may not have started — check "
            "/var/log/synopkg.log on the NAS."
        )
        raise RuntimeError(msg)
    print("    Download Station installed, started, and API-verified.")


# ---------------------------------------------------------------------------
# Download Station configuration via API
# ---------------------------------------------------------------------------


def _configure_download_station_via_api(
    base_url: str,
    admin_user: str,
    admin_password: str,
    test_users: tuple[tuple[str, str], ...],
    default_destination: str = "writable",
) -> None:
    """Configure Download Station after install: default destination + permissions.

    Runs once at golden-image bake time. Uses DSM admin session to:
      1. Set DS default destination to an existing share
      2. Grant each test user the Download Station application privilege
         (``SYNO.Core.AppPriv.Rule.set``), then check that the existing
         ``administrators`` rule survived

    Then verifies each grant from that user's own session (``Task.list``).
    Any failure fails the bake: an image without the grant would only show up
    later as a 105 on every non-admin DS test.
    """
    print("    Configuring Download Station (admin session)...")

    with httpx.Client(base_url=base_url, timeout=30, verify=False) as client:  # noqa: S501
        # Admin login
        login_resp = client.get(
            "/webapi/auth.cgi",
            params={
                "api": "SYNO.API.Auth",
                "version": "3",
                "method": "login",
                "account": admin_user,
                "passwd": admin_password,
                "session": "DownloadStation",
                "format": "cookie",
            },
        )
        login_data = login_resp.json()
        if not login_data.get("success"):
            msg = f"Admin login failed: {login_data.get('error')}"
            raise RuntimeError(msg)
        sid = login_data["data"]["sid"]
        client.headers["Cookie"] = f"id={sid}"  # never _sid (#176)

        try:
            # 1. Set DS default destination
            # DSM 7.2.2 DS Info API only dispatches via /webapi/DownloadStation/info.cgi,
            # NOT the generic /webapi/entry.cgi (which returns error 102 for ALL
            # DS API calls). The method name is `setserverconfig`, NOT the
            # `setconfig` published in some older DSM API docs — `setconfig`
            # returns error 103 (method does not exist). Discovered by probing
            # local vdsm 7.2.2 with both endpoints x [setconfig/setserverconfig/
            # set/config/update] x [v1/v2]; only setserverconfig at
            # info.cgi succeeded.
            print(f"    Setting DS default_destination = {default_destination}")
            setconfig_resp = client.get(
                "/webapi/DownloadStation/info.cgi",
                params={
                    "api": "SYNO.DownloadStation.Info",
                    "version": "1",
                    "method": "setserverconfig",
                    "default_destination": default_destination,
                },
            )
            setconfig_data = setconfig_resp.json()
            if not setconfig_data.get("success"):
                err = setconfig_data.get("error", {}).get("code", "?")
                msg = f"DS setserverconfig failed: code {err}"
                raise RuntimeError(msg)

            # 2. Grant test_user the DS application privilege. Rule.set merges
            # with the existing rules (verified: a set carrying only this user's
            # rule leaves the administrators group rule in place), so a single
            # set is enough. Rule.list wants a JSON-quoted app_id.
            names = [user for user, _ in test_users]
            print(f"    Granting {', '.join(names)} the Download Station app privilege...")
            rules = [
                {
                    "entity_type": "user",
                    "entity_name": user,
                    "app_id": _DS_APP_ID,
                    "allow_ip": ["0.0.0.0"],
                    "deny_ip": [],
                }
                for user in names
            ]
            set_data = client.get(
                "/webapi/entry.cgi",
                params={
                    "api": "SYNO.Core.AppPriv.Rule",
                    "version": "1",
                    "method": "set",
                    "rules": json.dumps(rules),
                },
            ).json()
            if not set_data.get("success"):
                code = set_data.get("error", {}).get("code", "?")
                msg = f"AppPriv.Rule.set for {', '.join(names)} failed: code {code}"
                raise RuntimeError(msg)

            list_data = client.get(
                "/webapi/entry.cgi",
                params={
                    "api": "SYNO.Core.AppPriv.Rule",
                    "version": "1",
                    "method": "list",
                    "app_id": json.dumps(_DS_APP_ID),
                },
            ).json()
            if not list_data.get("success"):
                code = list_data.get("error", {}).get("code", "?")
                msg = f"AppPriv.Rule.list failed: code {code}"
                raise RuntimeError(msg)
            entities = {
                (r.get("entity_type"), r.get("entity_name"))
                for r in list_data.get("data", {}).get("rules", [])
            }
            for expected in (("group", "administrators"), *(("user", n) for n in names)):
                if expected not in entities:
                    msg = (
                        f"Download Station app privilege rules lack {expected[0]} "
                        f"{expected[1]!r} after Rule.set: {sorted(entities)}"
                    )
                    raise RuntimeError(msg)
        finally:
            # Logout regardless of success
            client.get(
                "/webapi/auth.cgi",
                params={
                    "api": "SYNO.API.Auth",
                    "version": "1",
                    "method": "logout",
                    "session": "DownloadStation",
                },
            )

    for user, password in test_users:
        _verify_ds_access_as_user(base_url, user, password)
    print("    Download Station configured.")


@contextmanager
def _user_session(
    base_url: str, username: str, password: str
) -> Iterator[tuple[httpx.Client, str]]:
    """Log ``username`` in for one bake step; yield the HTTP client and sid."""
    with httpx.Client(base_url=base_url, timeout=30, verify=False) as client:  # noqa: S501
        login_data = client.get(
            "/webapi/auth.cgi",
            params={
                "api": "SYNO.API.Auth",
                "version": "3",
                "method": "login",
                "account": username,
                "passwd": password,
                "session": "DownloadStation",
                "format": "sid",
            },
        ).json()
        if not login_data.get("success"):
            msg = f"{username} login failed: {login_data.get('error')}"
            raise RuntimeError(msg)
        sid = login_data["data"]["sid"]
        # Every call of this client carries the session as the id cookie, never as
        # _sid: DSM misreads some session IDs as _sid and answers 119 (#176).
        client.headers["Cookie"] = f"id={sid}"
        try:
            yield client, sid
        finally:
            client.get(
                "/webapi/auth.cgi",
                params={
                    "api": "SYNO.API.Auth",
                    "version": "1",
                    "method": "logout",
                    "session": "DownloadStation",
                },
            )


def _ds_info(client: httpx.Client, sid: str, method: str, **params: str) -> dict[str, Any]:
    """Call ``SYNO.DownloadStation.Info`` v1 and raise unless it succeeds."""
    data = client.get(
        "/webapi/DownloadStation/info.cgi",
        params={
            "api": "SYNO.DownloadStation.Info",
            "version": "1",
            "method": method,
            **params,
        },
    ).json()
    if not data.get("success"):
        code = data.get("error", {}).get("code", "?")
        msg = f"DS Info.{method} failed: code {code}"
        raise RuntimeError(msg)
    result: dict[str, Any] = data.get("data", {})
    return result


def _verify_ds_access_as_user(base_url: str, username: str, password: str) -> None:
    """Fail the bake unless ``username``'s own session can list DS tasks."""
    print(f"    Verifying {username} can use Download Station...")
    with _user_session(base_url, username, password) as (client, sid):
        list_data = client.get(
            "/webapi/DownloadStation/task.cgi",
            params={
                "api": "SYNO.DownloadStation.Task",
                "version": "1",
                "method": "list",
            },
        ).json()
    if not list_data.get("success"):
        code = list_data.get("error", {}).get("code", "?")
        msg = f"{username} cannot use Download Station after the grant: Task.list code {code}"
        raise RuntimeError(msg)


def _set_own_ds_default_destination(
    base_url: str, username: str, password: str, destination: str
) -> None:
    """Set ``username``'s own DS default destination, as a first DS visit would.

    DS never starts a non-manager's tasks until that user has a default
    destination (DS 3.9 and 4.1, #124); DSM applies it per user. Fails the
    bake unless the user's own ``getconfig`` then reports it.
    """
    print(f"    Setting {username}'s own DS default destination = {destination}")
    with _user_session(base_url, username, password) as (client, sid):
        _ds_info(client, sid, "setserverconfig", default_destination=destination)
        got = _ds_info(client, sid, "getconfig").get("default_destination")
    if got != destination:
        msg = f"{username}'s DS default destination is {got!r} after setting {destination!r}"
        raise RuntimeError(msg)


def _verify_no_ds_default_destination(base_url: str, username: str, password: str) -> None:
    """Fail the bake unless ``username`` still has no DS default destination."""
    with _user_session(base_url, username, password) as (client, sid):
        got = _ds_info(client, sid, "getconfig").get("default_destination")
    if got:
        msg = f"{username} should have no DS default destination, has {got!r}"
        raise RuntimeError(msg)


# ---------------------------------------------------------------------------
# Synology Photos (#258)
# ---------------------------------------------------------------------------

# The Photos APIs the bake and the Photos tests need registered before going on.
_PHOTOS_APIS = (
    "SYNO.Foto.UserInfo",
    "SYNO.Foto.Browse.Item",
    "SYNO.Foto.Browse.Folder",
    "SYNO.Foto.Setting.TeamSpace",
    "SYNO.FotoTeam.Browse.Folder",
)


def _missing_apis(base_url: str, apis: tuple[str, ...]) -> list[str]:
    """Return the subset of ``apis`` not yet in DSM's API cache."""
    try:
        resp = httpx.get(
            f"{base_url}/webapi/query.cgi",
            params={
                "api": "SYNO.API.Info",
                "version": "1",
                "method": "query",
                "query": ",".join(apis),
            },
            timeout=10,
            verify=False,  # noqa: S501
        )
        data = resp.json().get("data", {})
        return [api for api in apis if not data.get(api)]
    except (httpx.HTTPError, ValueError):
        return list(apis)


def _install_photos_via_ssh(
    host: str,
    ssh_port: int,
    admin_password: str,
    base_url: str,
    packages: tuple[str, ...],
    *,
    register_timeout_sec: int = 300,
) -> None:
    """Install Synology Photos and its dependencies with synopkg over SSH.

    As for Download Station, the gate is the API cache, not ``synopkg status``.
    Idempotent: returns at once if the Photos APIs are already registered.
    """

    def ssh(cmd: str, *, sudo: bool = True, timeout: int = 30) -> tuple[int, str]:
        return _ssh_exec(host, ssh_port, admin_password, cmd, sudo=sudo, timeout=timeout)

    if not _missing_apis(base_url, _PHOTOS_APIS):
        print("    Synology Photos already running (all Photos APIs in cache)")
        return
    for pkg in packages:
        start = time.monotonic()
        # SynologyApplicationService's installer fails (rc 1, no output) under sudo's
        # reset PATH, and Photos takes minutes. The leading echo keeps synopkg's
        # first output line: ssh_exec drops the line sudo's prompt is printed on.
        rc, out = ssh(
            f"sh -c 'echo; env {_SYNO_PATH} /usr/syno/bin/synopkg install_from_server {pkg}'",
            timeout=900,
        )
        elapsed = int(time.monotonic() - start)
        if rc != 0:
            msg = f"synopkg install_from_server {pkg} failed after {elapsed}s (rc={rc}): {out}"
            raise RuntimeError(msg)
        print(f"    Installed {pkg} in {elapsed}s")
    rc, out = ssh("/usr/syno/bin/synopkg list --name", sudo=False)
    absent = [pkg for pkg in packages if pkg not in out.split()]
    if absent:
        msg = f"Not in the package list after install: {absent} (list: {out.strip()[:300]})"
        raise RuntimeError(msg)
    deadline = time.monotonic() + register_timeout_sec
    missing = list(_PHOTOS_APIS)
    while time.monotonic() < deadline:
        missing = _missing_apis(base_url, _PHOTOS_APIS)
        if not missing:
            break
        time.sleep(3)
    else:
        msg = (
            f"Photos APIs still missing from the cache after {register_timeout_sec}s: "
            f"{missing}. Check /var/log/synopkg.log on the NAS."
        )
        raise RuntimeError(msg)
    print("    Synology Photos installed and API-verified.")


def _install_snapshot_replication_via_ssh(
    host: str,
    ssh_port: int,
    admin_password: str,
    base_url: str,
    *,
    register_timeout_sec: int = 300,
) -> None:
    """Install Snapshot Replication with synopkg over SSH (share snapshots, #49).

    As for Photos, the gate is the API cache. Idempotent: returns at once if its APIs
    are already registered.
    """
    if not _missing_apis(base_url, _SNAPSHOT_APIS):
        print("    Snapshot Replication already installed (its APIs are in the cache)")
        return
    start = time.monotonic()
    rc, out = _ssh_exec(
        host,
        ssh_port,
        admin_password,
        f"sh -c 'echo; env {_SYNO_PATH} /usr/syno/bin/synopkg install_from_server "
        f"{SNAPSHOT_PACKAGE}'",
        sudo=True,
        timeout=900,
    )
    elapsed = int(time.monotonic() - start)
    if rc != 0:
        msg = (
            f"synopkg install_from_server {SNAPSHOT_PACKAGE} failed after {elapsed}s "
            f"(rc={rc}): {out}"
        )
        raise RuntimeError(msg)
    print(f"    Installed {SNAPSHOT_PACKAGE} in {elapsed}s")
    deadline = time.monotonic() + register_timeout_sec
    while _missing_apis(base_url, _SNAPSHOT_APIS):
        if time.monotonic() > deadline:
            msg = (
                f"Snapshot Replication's APIs still missing after {register_timeout_sec}s. "
                "Check /var/log/synopkg.log on the NAS."
            )
            raise RuntimeError(msg)
        time.sleep(3)
    print("    Snapshot Replication installed and API-verified.")


def _photos_call(
    client: httpx.Client,
    sid: str,
    api: str,
    method: str,
    version: int = 1,
    **params: Any,
) -> dict[str, Any]:
    """Call a Photos API as a form POST with JSON-encoded values; raise unless it succeeds.

    ``api``/``method``/``version`` go in the body: with them only in the query string
    and an empty body, DSM answers 101 (#258).
    """
    body = {"api": api, "method": method, "version": str(version)}
    body.update({k: json.dumps(v) for k, v in params.items()})
    data = client.post("/webapi/entry.cgi", data=body).json()
    if not data.get("success"):
        msg = f"{api}.{method} failed: {data.get('error')}"
        raise RuntimeError(msg)
    result: Any = data.get("data", {})
    return result if isinstance(result, dict) else {"list": result}


def _core_call(
    client: httpx.Client, sid: str, api: str, method: str, version: int = 1, **params: str
) -> dict[str, Any]:
    """Call a ``SYNO.Core.*`` API by GET (as the rest of the bake); raise unless it succeeds."""
    data = client.get(
        "/webapi/entry.cgi",
        params={"api": api, "method": method, "version": str(version), **params},
    ).json()
    if not data.get("success"):
        msg = f"{api}.{method} failed: {data.get('error')}"
        raise RuntimeError(msg)
    result: dict[str, Any] = data.get("data", {})
    return result


def _wait_for(check: Any, what: str, *, timeout_sec: int = 60) -> None:
    """Poll ``check()`` until it's true; fail the bake after ``timeout_sec``."""
    deadline = time.monotonic() + timeout_sec
    while time.monotonic() < deadline:
        if check():
            return
        time.sleep(3)
    msg = f"Timed out after {timeout_sec}s waiting for {what}"
    raise RuntimeError(msg)


def _set_timezone_via_api(base_url: str, admin_user: str, admin_password: str, zone: str) -> None:
    """Set DSM's timezone to ``zone`` (a ``SYNO.Core.Region.NTP listzone`` value).

    ``set`` v1 answers 5701 unless it also gets ``enable_ntp`` and ``server``, so they
    are sent back as ``get`` reported them (v2 answers 5701 even with them). The zone
    takes effect at once (``/etc/localtime``, and Synology Photos' times for files
    indexed after it).
    """
    with _user_session(base_url, admin_user, admin_password) as (client, sid):
        zones = _core_call(client, sid, "SYNO.Core.Region.NTP", "listzone")
        names = {z.get("value") for z in zones.get("zonedata", [])}
        if zone not in names:
            msg = f"DSM has no timezone {zone!r}; listzone offers {sorted(names)}"
            raise RuntimeError(msg)
        ntp = _core_call(client, sid, "SYNO.Core.Region.NTP", "get")
        _core_call(
            client,
            sid,
            "SYNO.Core.Region.NTP",
            "set",
            timezone=zone,
            enable_ntp=str(ntp["enable_ntp"]),
            server=str(ntp["server"]),
        )
        now = _core_call(client, sid, "SYNO.Core.Region.NTP", "get").get("timezone")
        if now != zone:
            msg = f"Setting the timezone to {zone} left it at {now}"
            raise RuntimeError(msg)
    print(f"    Timezone set to {zone} (was {ntp.get('timezone')})")


def _configure_photos_via_api(
    base_url: str, admin_user: str, admin_password: str, photos_user: str
) -> None:
    """Make Synology Photos usable by ``photos_user``, with both its spaces.

    1. Turn on the user home service (with its recycle bin): Photos keeps each
       user's personal space in ``homes/<user>/Photos`` and answers 602 without it.
    2. Grant ``photos_user`` the Photos application privilege (otherwise 160).
    3. Enable Shared Space and give ``photos_user`` the ``management`` role there
       (otherwise 801), so the Photos tests can write to it.

    Every other user keeps the defaults: ``mcpfresh`` has no Photos privilege.
    """
    print("    Configuring Synology Photos (admin session)...")
    with _user_session(base_url, admin_user, admin_password) as (client, sid):
        _core_call(
            client,
            sid,
            "SYNO.Core.User.Home",
            "set",
            enable="true",
            location=json.dumps("/volume1"),
            enable_recycle_bin="true",
        )
        _wait_for(
            lambda: _core_call(client, sid, "SYNO.Core.User.Home", "get").get("enable"),
            "the user home service",
        )
        print("    User home service enabled (with recycle bin)")

        rules = [
            {
                "entity_type": "user",
                "entity_name": photos_user,
                "app_id": _PHOTOS_APP_ID,
                "allow_ip": ["0.0.0.0"],
                "deny_ip": [],
            }
        ]
        _core_call(client, sid, "SYNO.Core.AppPriv.Rule", "set", rules=json.dumps(rules))
        listed = _core_call(
            client, sid, "SYNO.Core.AppPriv.Rule", "list", app_id=json.dumps(_PHOTOS_APP_ID)
        )
        entities = {(r.get("entity_type"), r.get("entity_name")) for r in listed.get("rules", [])}
        for expected in (("group", "administrators"), ("user", photos_user)):
            if expected not in entities:
                msg = f"Photos app privilege rules lack {expected}: {sorted(entities)}"
                raise RuntimeError(msg)
        print(f"    Granted {photos_user} the Synology Photos app privilege")

        # set_enable takes "enabled" (with "enable" it answers 120, reason required).
        _photos_call(client, sid, "SYNO.Foto.Setting.TeamSpace", "set_enable", enabled=True)
        _wait_for(
            lambda: _photos_call(client, sid, "SYNO.Foto.Setting.TeamSpace", "get").get("enabled"),
            "Shared Space to turn on",
        )
        users = _photos_call(client, sid, "SYNO.Foto.Sharing.Misc", "list_user_group")
        uid = next(
            (
                u["id"]
                for u in users.get("list", [])
                if u.get("name") == photos_user and u.get("type", "user") == "user"
            ),
            None,
        )
        if uid is None:
            msg = f"{photos_user} not in Photos' user list: {users}"
            raise RuntimeError(msg)
        grant = {
            "action": "update",
            "type": "user",
            "id": uid,
            "permission": "management",
            "auto_backup": False,
        }
        _photos_call(client, sid, "SYNO.Foto.Setting.TeamSpace", "update_permission", list=[grant])
        perms = _photos_call(client, sid, "SYNO.Foto.Setting.TeamSpace", "list_permission")
        if not any(
            p.get("type") == "user" and p.get("id") == uid and p.get("permission") == "management"
            for p in perms.get("list", [])
        ):
            msg = f"{photos_user} lacks the Shared Space management role: {perms}"
            raise RuntimeError(msg)
        print(f"    Shared Space enabled; {photos_user} has the management role")


def _verify_photos_access(
    base_url: str, photos_user: str, photos_password: str, fresh_user: str, fresh_password: str
) -> None:
    """Fail the bake unless ``photos_user`` reaches both spaces and ``fresh_user`` gets 160."""
    print(f"    Verifying {photos_user} can use Synology Photos...")
    with _user_session(base_url, photos_user, photos_password) as (client, sid):
        _photos_call(client, sid, "SYNO.Foto.UserInfo", "me")
        root = _photos_call(client, sid, "SYNO.Foto.Browse.Folder", "get", 2, id=0)
        team = _photos_call(client, sid, "SYNO.FotoTeam.Browse.Folder", "get", 2, id=1)
    print(
        f"    Personal root folder id {root['folder']['id']}, "
        f"Shared Space root folder id {team['folder']['id']}"
    )
    with _user_session(base_url, fresh_user, fresh_password) as (client, sid):
        try:
            _photos_call(client, sid, "SYNO.Foto.UserInfo", "me")
        except RuntimeError as e:
            if "160" not in str(e):
                raise
        else:
            msg = f"{fresh_user} can use Photos, but should have no Photos privilege"
            raise RuntimeError(msg)
    print(f"    {fresh_user} has no Photos privilege (160), as intended")


# ---------------------------------------------------------------------------
# Main setup entry point
# ---------------------------------------------------------------------------


def setup_dsm_for_testing(
    base_url: str,
    admin_password: str,
    *,
    admin_user: str = DEFAULT_ADMIN_USER,
    ssh_host: str = "localhost",
    ssh_port: int = 0,
    dsm_version: str,
) -> dict[str, Any]:
    """Run the full post-wizard setup.

    Sets the timezone (DSM_TIMEZONES) and enables SSH via the DSM API, then
    SSHes into the DSM guest VM (via QEMU) to create the test user (synouser),
    install Download Station, Synology Photos and Snapshot Replication (synopkg), and
    create shared folders, test data, and permissions (synoshare).

    Returns metadata dict for the golden image sidecar.
    """
    print("\nConfiguring DSM for integration testing...")

    # Enable SSH, create the test user, install Download Station, create
    # shared folders, configure DS — all via the SSH/CLI/API surface. The
    # test user used to be created through the Control Panel UI, but on
    # DSM 7.1 the desktop's Control Panel icon never opens the window under
    # Playwright (tooltip only, deterministic across retries), and the
    # Package Center UI proved unautomatable over Ext 3.4.1's component model
    # (see _install_download_station_via_ssh docstring).
    if not ssh_port:
        # Every setup step runs over SSH; baking without it would save an
        # image stamped bake_version/grants that has none of them.
        msg = "No SSH port for the DSM guest; cannot bake a golden image."
        raise RuntimeError(msg)
    photos_packages = PHOTOS_PACKAGES.get(dsm_version)
    if photos_packages is None:
        msg = f"No Synology Photos package list for DSM {dsm_version} (PHOTOS_PACKAGES)."
        raise RuntimeError(msg)
    timezone = DSM_TIMEZONES.get(dsm_version)
    if timezone is None:
        msg = f"No timezone for DSM {dsm_version} (DSM_TIMEZONES)."
        raise RuntimeError(msg)

    # First, so every file and Photos item the bake creates gets times in this zone.
    print(f"\n  [1/9] Setting the timezone to {timezone}...")
    _set_timezone_via_api(base_url, admin_user, admin_password, timezone)

    print("\n  [2/9] Enabling SSH...")
    _enable_ssh(base_url, admin_user, admin_password)

    test_users = (
        (DEFAULT_TEST_USER, DEFAULT_TEST_PASSWORD),
        (DEFAULT_FRESH_USER, DEFAULT_FRESH_PASSWORD),
    )
    print("\n  [3/9] Creating test users via synouser (SSH)...")
    for user, password in test_users:
        _create_user_via_ssh(ssh_host, ssh_port, admin_password, user, password)

    print("\n  [4/9] Installing Download Station via synopkg (SSH)...")
    _install_download_station_via_ssh(ssh_host, ssh_port, admin_password, base_url)

    print("\n  [5/9] Installing Synology Photos via synopkg (SSH)...")
    _install_photos_via_ssh(ssh_host, ssh_port, admin_password, base_url, photos_packages)

    print("\n  [6/9] Installing Snapshot Replication via synopkg (SSH)...")
    _install_snapshot_replication_via_ssh(ssh_host, ssh_port, admin_password, base_url)

    print("\n  [7/9] Creating shared folders and test data via SSH...")
    _create_shared_folders_via_ssh(
        ssh_host,
        ssh_port,
        admin_password,
        tuple(user for user, _ in test_users),
    )

    print("\n  [8/9] Configuring Download Station...")
    _configure_download_station_via_api(
        base_url=base_url,
        admin_user=admin_user,
        admin_password=admin_password,
        test_users=test_users,
        default_destination="writable",
    )
    _set_own_ds_default_destination(base_url, DEFAULT_TEST_USER, DEFAULT_TEST_PASSWORD, "writable")
    _verify_no_ds_default_destination(base_url, DEFAULT_FRESH_USER, DEFAULT_FRESH_PASSWORD)

    print("\n  [9/9] Configuring Synology Photos...")
    _configure_photos_via_api(base_url, admin_user, admin_password, DEFAULT_TEST_USER)
    _verify_photos_access(
        base_url,
        DEFAULT_TEST_USER,
        DEFAULT_TEST_PASSWORD,
        DEFAULT_FRESH_USER,
        DEFAULT_FRESH_PASSWORD,
    )

    # Verify — check both admin and test user can see shares
    print("\n  Verifying setup...")
    if not _verify_setup_via_api(base_url, admin_user, admin_password):
        logger.warning("Verification failed for admin user — golden image may be incomplete")
    if not _verify_setup_via_api(base_url, DEFAULT_TEST_USER, DEFAULT_TEST_PASSWORD):
        logger.warning("Verification failed for test user — ACL permissions may not be set")

    # Build metadata
    metadata: dict[str, Any] = {
        "dsm_url": base_url,
        "admin_user": admin_user,
        "admin_password": admin_password,
        "test_user": DEFAULT_TEST_USER,
        "test_password": DEFAULT_TEST_PASSWORD,
        "dsm_packages": ["DownloadStation", *photos_packages, SNAPSHOT_PACKAGE],
        "bake_version": BAKE_VERSION,
        "timezone": timezone,
        "fresh_user": DEFAULT_FRESH_USER,
        "fresh_password": DEFAULT_FRESH_PASSWORD,
        "grants": [
            f"ds:{DEFAULT_TEST_USER}",
            f"ds:{DEFAULT_FRESH_USER}",
            f"photos:{DEFAULT_TEST_USER}",
            f"photos-shared-space:{DEFAULT_TEST_USER}:management",
        ],
        "ds_default_destination": {DEFAULT_TEST_USER: "writable"},
        "test_paths": {
            "existing_share": "/testshare",
            "search_folder": "/testshare/Documents",
            "search_keyword": "Bambu",
            "writable_folder": "/writable",
        },
    }

    print("\nDSM setup complete.")
    return metadata
