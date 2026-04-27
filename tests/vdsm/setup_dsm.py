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

import logging
import time
from pathlib import Path
from typing import Any

import httpx

from tests.vdsm.config import (
    DEFAULT_ADMIN_USER,
    DEFAULT_TEST_PASSWORD,
    DEFAULT_TEST_USER,
    DSM_API_POLL_INTERVAL,
    DSM_BOOT_TIMEOUT,
)

logger = logging.getLogger(__name__)

# Directory for debug screenshots on failure
_SCREENSHOT_DIR = Path(__file__).parent.parent.parent / ".vdsm" / "screenshots"


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _screenshot(page: Any, name: str) -> None:
    """Save a debug screenshot."""
    _SCREENSHOT_DIR.mkdir(parents=True, exist_ok=True)
    path = _SCREENSHOT_DIR / f"{name}.png"
    page.screenshot(path=str(path))
    print(f"    Screenshot: {path.name}")


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
# Post-wizard popups
# ---------------------------------------------------------------------------


def _dismiss_all_popups(page: Any) -> None:
    """Dismiss all overlay popups: 2FA, MFA, tour, notifications.

    DSM 7 shows persistent promotion dialogs after first login. Clicking
    through the MFA confirmation flow is unreliable — the dialog can
    reappear. Instead, we click the initial 2FA "No, thanks" then
    force-remove all promotion windows and overlay masks from the DOM.
    """
    # Step 1: Dismiss initial popups via button clicks
    for _round in range(3):
        time.sleep(1)
        if _click_text(page, "No, thanks", timeout=2):
            print("    Dismissed: 2FA promotion")
            continue
        if _click_text(page, "No, Thanks", timeout=2):
            print("    Dismissed: 2FA promotion (alt)")
            continue
        break

    # Step 2: Force-remove ALL promotion/overlay windows from the DOM.
    removed = page.evaluate("""() => {
        let count = 0;
        document.querySelectorAll(
            '.syno-promotion-app, [syno-id="promotion-app-window"]'
        ).forEach(el => { el.remove(); count++; });
        document.querySelectorAll(
            '.v-window-container-mask, .v-window-mask'
        ).forEach(el => { el.remove(); count++; });
        return count;
    }""")
    if removed:
        print(f"    Force-removed {removed} promotion/overlay elements")
    time.sleep(2)

    # Step 3: Close notification toasts
    page.evaluate("""() => {
        document.querySelectorAll('.x-tool-close, .v-close-btn')
            .forEach(btn => { if (btn.offsetParent !== null) btn.click(); });
    }""")
    time.sleep(1)


# ---------------------------------------------------------------------------
# Login
# ---------------------------------------------------------------------------


def _dsm_login(page: Any, base_url: str, username: str, password: str) -> None:
    """Login to DSM desktop via the two-step login UI."""
    page.goto(base_url, wait_until="networkidle", timeout=60000)
    time.sleep(3)

    # Step 1: Enter username and press Enter
    user_field = page.wait_for_selector("input:visible", timeout=30000)
    if user_field:
        user_field.fill(username)
    time.sleep(1)
    page.keyboard.press("Enter")
    time.sleep(3)

    # Step 2: Enter password and press Enter
    pass_field = page.wait_for_selector("input[type='password']:visible", timeout=10000)
    if pass_field:
        pass_field.fill(password)
    time.sleep(1)
    page.keyboard.press("Enter")

    # Wait for desktop to load
    time.sleep(10)
    _dismiss_all_popups(page)
    print("    Logged in to DSM desktop")
    _screenshot(page, "02-desktop")


# ---------------------------------------------------------------------------
# Control Panel navigation
# ---------------------------------------------------------------------------


def _open_control_panel(page: Any) -> None:
    """Open Control Panel from the DSM desktop."""
    _dismiss_all_popups(page)

    cp = page.query_selector("text='Control Panel'")
    if cp and cp.is_visible():
        cp.dblclick(force=True)
        time.sleep(5)
        _dismiss_all_popups(page)
        print("    Opened Control Panel")
        _screenshot(page, "03-control-panel")
        return

    print("    Warning: Could not find Control Panel shortcut")
    _screenshot(page, "03-control-panel-missing")


# ---------------------------------------------------------------------------
# User creation via Playwright
# ---------------------------------------------------------------------------


def _create_user_via_ui(page: Any, username: str, password: str) -> None:
    """Create a local user via Control Panel > User & Group wizard.

    Uses Playwright's type() for form fields (not fill()) because DSM's
    ExtJS framework requires keystroke events for internal validation.
    The wizard buttons are <button class="x-btn-text"> elements.
    """
    print("    Creating user via web UI...")

    _click_text(page, "User & Group", timeout=3)
    _screenshot(page, "04-user-group")

    # Click Create in the toolbar
    page.locator("span:has-text('Create')").first.click(force=True)
    time.sleep(3)
    _screenshot(page, "05-user-wizard")

    # Fill form fields using type() for proper ExtJS event dispatch
    wizard = page.locator(".sds-wizard-window")

    name_input = wizard.locator("input[aria-label='Name'], input[name='name']").first
    name_input.click(force=True)
    name_input.press("Control+a")
    name_input.type(username, delay=50)
    time.sleep(0.5)

    pwd_field = wizard.locator("input[type='password']").first
    pwd_field.click(force=True)
    pwd_field.type(password, delay=30)
    time.sleep(0.5)

    confirm_field = wizard.locator("input[type='password']").nth(1)
    confirm_field.click(force=True)
    confirm_field.type(password, delay=30)
    time.sleep(0.5)

    _screenshot(page, "06-user-filled")

    # Click through wizard steps
    for step in range(10):
        time.sleep(2)
        _screenshot(page, f"06-user-step-{step}")

        # Re-acquire wizard locator each step (class may change on final page)
        wiz = page.locator(".sds-wizard-window, .x-window.active-win")

        # Check for Apply/Done (final step buttons)
        for final_text in ["Apply", "Done"]:
            final_btn = wiz.locator(f"button:has-text('{final_text}')")
            if final_btn.count() > 0 and final_btn.first.is_visible():
                final_btn.first.click(force=True)
                print(f"    User wizard: {final_text}")
                time.sleep(5)
                step = 99  # noqa: PLW2901
                break
        if step == 99:
            break

        # Click Next
        nxt = wiz.locator("button:has-text('Next')")
        if nxt.count() > 0 and nxt.first.is_visible():
            nxt.first.click(force=True)
            print(f"    User wizard: Next (step {step})")
            continue

        print(f"    User wizard: no buttons at step {step}")
        break

    time.sleep(3)
    _screenshot(page, "07-user-created")
    print(f"    User '{username}' creation completed")


# ---------------------------------------------------------------------------
# Shared folders and test data via SSH into DSM guest
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

    Uses Playwright for user creation (requires web UI), then SSH into the
    DSM guest VM (via QEMU) for shared folder creation with synoshare CLI,
    test data, and permissions.

    Returns metadata dict for the golden image sidecar.
    """
    try:
        from playwright.sync_api import sync_playwright
    except ImportError as e:
        msg = "Playwright is required: uv sync --extra vdsm"
        raise ImportError(msg) from e

    print("\nConfiguring DSM for integration testing...")

    # 1. Create test user via web UI (Control Panel wizard)
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        page = browser.new_page(viewport={"width": 1280, "height": 900})

        try:
            print("\n  [1/4] Logging in to DSM...")
            _dsm_login(page, base_url, admin_user, admin_password)

            print("\n  [2/4] Creating test user...")
            _open_control_panel(page)
            _create_user_via_ui(page, DEFAULT_TEST_USER, DEFAULT_TEST_PASSWORD)

        except Exception:
            _screenshot(page, "error-state")
            raise
        finally:
            browser.close()

    # 2. Enable SSH and create shared folders via synoshare in the DSM guest
    if ssh_port:
        print("\n  [3/4] Enabling SSH...")
        _enable_ssh(base_url, admin_user, admin_password)

        print("\n  [4/4] Creating shared folders and test data via SSH...")
        _create_shared_folders_via_ssh(
            ssh_host,
            ssh_port,
            admin_password,
            DEFAULT_TEST_USER,
        )
    else:
        print("\n  [3/4] No SSH port — skipping share creation")

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
        "test_paths": {
            "existing_share": "/testshare",
            "search_folder": "/testshare/Documents",
            "search_keyword": "Bambu",
            "writable_folder": "/writable",
        },
    }

    print("\nDSM setup complete.")
    return metadata
