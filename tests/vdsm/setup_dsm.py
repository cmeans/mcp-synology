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
from pathlib import Path
from typing import Any

import httpx

from tests.vdsm.config import (
    BAKE_VERSION,
    DEFAULT_ADMIN_USER,
    DEFAULT_TEST_PASSWORD,
    DEFAULT_TEST_USER,
    DSM_API_POLL_INTERVAL,
    DSM_BOOT_TIMEOUT,
)

logger = logging.getLogger(__name__)

_DS_APP_ID = "SYNO.SDS.DownloadStation.Application"

# Directory for debug screenshots on failure
_SCREENSHOT_DIR = Path(__file__).parent.parent.parent / ".vdsm" / "screenshots"


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
            "_sid": data["sid"],
        },
        headers={"X-SYNO-TOKEN": data.get("synotoken", "")},
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
    test_user: str,
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

    # Set ACL permissions for test user (requires sudo)
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
            "_sid": sid,
        }
        resp2 = httpx.get(
            f"{base_url}/webapi/entry.cgi",
            params=list_params,
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
    install_timeout_sec: int = 300,
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

    def ssh(cmd: str, *, sudo: bool = True) -> tuple[int, str]:
        return _ssh(host, ssh_port, admin_password, cmd, sudo=sudo)

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
    # Install (~3s on a warm Synology package server, 30-60s on cold cache).
    print("    Running synopkg install_from_server DownloadStation...")
    start = time.monotonic()
    rc, out = ssh(
        "/usr/syno/bin/synopkg install_from_server DownloadStation",
        sudo=True,
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
    test_user: str,
    test_password: str,
    default_destination: str = "writable",
) -> None:
    """Configure Download Station after install: default destination + permissions.

    Runs once at golden-image bake time. Uses DSM admin session to:
      1. Set DS default destination to an existing share
      2. Grant the test_user the Download Station application privilege
         (``SYNO.Core.AppPriv.Rule.set``), then check that the existing
         ``administrators`` rule survived

    Then verifies the grant from the test user's own session (``Task.list``).
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
                    "_sid": sid,
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
            print(f"    Granting {test_user} the Download Station app privilege...")
            rule = {
                "entity_type": "user",
                "entity_name": test_user,
                "app_id": _DS_APP_ID,
                "allow_ip": ["0.0.0.0"],
                "deny_ip": [],
            }
            set_data = client.get(
                "/webapi/entry.cgi",
                params={
                    "api": "SYNO.Core.AppPriv.Rule",
                    "version": "1",
                    "method": "set",
                    "rules": json.dumps([rule]),
                    "_sid": sid,
                },
            ).json()
            if not set_data.get("success"):
                code = set_data.get("error", {}).get("code", "?")
                msg = f"AppPriv.Rule.set for {test_user} failed: code {code}"
                raise RuntimeError(msg)

            list_data = client.get(
                "/webapi/entry.cgi",
                params={
                    "api": "SYNO.Core.AppPriv.Rule",
                    "version": "1",
                    "method": "list",
                    "app_id": json.dumps(_DS_APP_ID),
                    "_sid": sid,
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
            for expected in (("group", "administrators"), ("user", test_user)):
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
                    "_sid": sid,
                },
            )

    _verify_ds_access_as_user(base_url, test_user, test_password)
    print("    Download Station configured.")


def _verify_ds_access_as_user(base_url: str, username: str, password: str) -> None:
    """Fail the bake unless ``username``'s own session can list DS tasks."""
    print(f"    Verifying {username} can use Download Station...")
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
        try:
            list_data = client.get(
                "/webapi/DownloadStation/task.cgi",
                params={
                    "api": "SYNO.DownloadStation.Task",
                    "version": "1",
                    "method": "list",
                    "_sid": sid,
                },
            ).json()
        finally:
            client.get(
                "/webapi/auth.cgi",
                params={
                    "api": "SYNO.API.Auth",
                    "version": "1",
                    "method": "logout",
                    "session": "DownloadStation",
                    "_sid": sid,
                },
            )
        if not list_data.get("success"):
            code = list_data.get("error", {}).get("code", "?")
            msg = f"{username} cannot use Download Station after the grant: Task.list code {code}"
            raise RuntimeError(msg)


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
) -> dict[str, Any]:
    """Run the full post-wizard setup.

    Enables SSH via the DSM API, then SSHes into the DSM guest VM (via QEMU)
    to create the test user (synouser), install Download Station (synopkg),
    and create shared folders, test data, and permissions (synoshare).

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
    if ssh_port:
        print("\n  [1/5] Enabling SSH...")
        _enable_ssh(base_url, admin_user, admin_password)

        print("\n  [2/5] Creating test user via synouser (SSH)...")
        _create_user_via_ssh(
            ssh_host, ssh_port, admin_password, DEFAULT_TEST_USER, DEFAULT_TEST_PASSWORD
        )

        print("\n  [3/5] Installing Download Station via synopkg (SSH)...")
        _install_download_station_via_ssh(ssh_host, ssh_port, admin_password, base_url)

        print("\n  [4/5] Creating shared folders and test data via SSH...")
        _create_shared_folders_via_ssh(
            ssh_host,
            ssh_port,
            admin_password,
            DEFAULT_TEST_USER,
        )

        print("\n  [5/5] Configuring Download Station...")
        _configure_download_station_via_api(
            base_url=base_url,
            admin_user=admin_user,
            admin_password=admin_password,
            test_user=DEFAULT_TEST_USER,
            test_password=DEFAULT_TEST_PASSWORD,
            default_destination="writable",
        )
    else:
        print("\n  No SSH port — skipping all setup steps (SSH, test user, DS, shares)")

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
        "dsm_packages": ["DownloadStation"],
        "bake_version": BAKE_VERSION,
        "grants": [f"ds:{DEFAULT_TEST_USER}"],
        "test_paths": {
            "existing_share": "/testshare",
            "search_folder": "/testshare/Documents",
            "search_keyword": "Bambu",
            "writable_folder": "/writable",
        },
    }

    print("\nDSM setup complete.")
    return metadata
