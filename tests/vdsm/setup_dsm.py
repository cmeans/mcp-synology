"""DSM setup automation for virtual-dsm test instances.

Includes:
- Playwright-based wizard automation (first-boot setup, fully headless)
- Post-wizard API configuration (users, shares, permissions, test data)

Note: SYNO.Core.User, SYNO.Core.Share, and SYNO.Core.Share.Permission are
UNDOCUMENTED admin APIs. Parameters are based on community reverse engineering
and may need adjustment across DSM versions.
"""

from __future__ import annotations

import io
import logging
import time
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


def wait_for_api(base_url: str, timeout: int = DSM_BOOT_TIMEOUT) -> None:
    """Poll DSM API info endpoint until it responds.

    Retries every DSM_API_POLL_INTERVAL seconds until success or timeout.
    """
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


def complete_wizard(base_url: str, admin_user: str, admin_password: str) -> None:
    """Automate the DSM first-boot setup wizard using Playwright.

    Fills in the admin account form and clicks through all wizard pages
    (update settings, Synology account, analytics, package offers).

    Requires: playwright with chromium installed
    (uv sync --extra vdsm && uv run playwright install chromium)
    """
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

            # Step 1: Welcome — wait for wizard to fully render, then click Start
            print("    [1/6] Welcome page (waiting for wizard to load)...")
            page.wait_for_selector(".welcome-page-btn", timeout=120000)
            page.click(".welcome-page-btn")
            time.sleep(2)

            # Step 2: Account setup — fill form and click Next
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

            # Verify we advanced (check for error banner)
            error = page.query_selector(".v-tooltip-error, .error-msg")
            if error and error.is_visible():
                error_text = error.inner_text()
                msg = f"Wizard account setup failed: {error_text}"
                raise RuntimeError(msg)

            # Step 3: Update options — accept default, click Next
            print("    [3/6] Update options...")
            page.click("button:has-text('Next')")
            time.sleep(3)

            # Step 4: Synology Account — click Skip
            print("    [4/6] Synology Account (skipping)...")
            page.click("button:has-text('Skip')")
            time.sleep(3)

            # Step 5: Device Analytics — click Submit (unchecked = decline)
            print("    [5/6] Device Analytics (declining)...")
            page.click("button:has-text('Submit')")
            time.sleep(3)

            # Step 6: Synology Drive/Office install — click "No, thanks"
            print("    [6/6] Package install offer (declining)...")
            no_btn = page.query_selector("button:has-text('No, thanks')")
            if no_btn and no_btn.is_visible():
                no_btn.click()
                time.sleep(3)
            else:
                # Some DSM versions may not show this step
                logger.info("No package install prompt found — skipping")

            print("  Wizard complete!")

        finally:
            browser.close()


def login(base_url: str, username: str, password: str) -> tuple[str, str]:
    """Login to DSM and return (session_id, syno_token).

    The SynoToken is a CSRF token required by DSM 7 for admin write operations
    (SYNO.Core.User, SYNO.Core.Share, etc.). Requested via enable_syno_token=yes.
    """
    params = {
        "api": "SYNO.API.Auth",
        "version": "6",
        "method": "login",
        "account": username,
        "passwd": password,
        "format": "sid",
        "enable_syno_token": "yes",
    }
    resp = httpx.get(
        f"{base_url}/webapi/entry.cgi",
        params=params,
        timeout=30,
        verify=False,  # noqa: S501
    )
    resp.raise_for_status()
    body = resp.json()

    if not body.get("success"):
        code = body.get("error", {}).get("code", 0)
        msg = f"Login failed with error code {code}"
        raise RuntimeError(msg)

    data = body["data"]
    sid: str = data["sid"]
    syno_token: str = data.get("synotoken", "")
    logger.info("Logged in as %s (sid=%s..., synotoken=%s)", username, sid[:8], bool(syno_token))
    return sid, syno_token


def logout(base_url: str, sid: str) -> None:
    """Logout from DSM, invalidating the session."""
    params = {
        "api": "SYNO.API.Auth",
        "version": "6",
        "method": "logout",
        "_sid": sid,
    }
    try:
        resp = httpx.get(
            f"{base_url}/webapi/entry.cgi",
            params=params,
            timeout=10,
            verify=False,  # noqa: S501
        )
        resp.raise_for_status()
        logger.info("Logged out successfully")
    except Exception:
        logger.warning("Logout failed (non-critical)", exc_info=True)


def _admin_post(
    base_url: str,
    sid: str,
    syno_token: str,
    data: dict[str, str],
) -> dict[str, Any]:
    """Make an admin POST request with SynoToken CSRF header.

    DSM 7 requires the SynoToken for all admin write operations
    (SYNO.Core.User, SYNO.Core.Share, etc.).
    """
    data["_sid"] = sid
    headers: dict[str, str] = {}
    if syno_token:
        headers["X-SYNO-TOKEN"] = syno_token
    resp = httpx.post(
        f"{base_url}/webapi/entry.cgi",
        data=data,
        headers=headers,
        timeout=30,
        verify=False,  # noqa: S501
    )
    resp.raise_for_status()
    return resp.json()  # type: ignore[no-any-return]


def create_user(
    base_url: str, sid: str, username: str, password: str, *, syno_token: str = ""
) -> None:
    """Create a local DSM user.

    Uses the undocumented SYNO.Core.User API. If it fails, prints manual
    instructions and continues.
    """
    try:
        body = _admin_post(
            base_url,
            sid,
            syno_token,
            {
                "api": "SYNO.Core.User",
                "method": "create",
                "version": "1",
                "name": username,
                "password": password,
                "description": "MCP integration test user",
            },
        )

        if body.get("success"):
            logger.info("Created user: %s", username)
            print(f"  Created user: {username}")
        else:
            code = body.get("error", {}).get("code", 0)
            logger.warning("Create user API returned error code %d", code)
            print(f"  Warning: Create user returned error code {code}")
            _print_manual_user_instructions(username, password)
    except Exception:
        logger.warning("Create user API call failed", exc_info=True)
        _print_manual_user_instructions(username, password)


def _print_manual_user_instructions(username: str, password: str) -> None:
    """Print instructions for manual user creation via web UI."""
    print("\n  Manual step needed — create user via DSM web UI:")
    print("    Control Panel > User & Group > Create")
    print(f"    Username: {username}")
    print(f"    Password: {password}")
    print()


def create_shared_folder(
    base_url: str, sid: str, name: str, vol_path: str = "/volume1", *, syno_token: str = ""
) -> None:
    """Create a shared folder on the NAS.

    Uses the undocumented SYNO.Core.Share API. If it fails, prints manual
    instructions and continues.
    """
    try:
        body = _admin_post(
            base_url,
            sid,
            syno_token,
            {
                "api": "SYNO.Core.Share",
                "method": "create",
                "version": "1",
                "name": name,
                "vol_path": vol_path,
                "desc": f"MCP test share: {name}",
            },
        )

        if body.get("success"):
            logger.info("Created shared folder: %s", name)
            print(f"  Created shared folder: {name}")
        else:
            code = body.get("error", {}).get("code", 0)
            logger.warning("Create share API returned error code %d for '%s'", code, name)
            print(f"  Warning: Create share '{name}' returned error code {code}")
            _print_manual_share_instructions(name)
    except Exception:
        logger.warning("Create share API call failed for '%s'", name, exc_info=True)
        _print_manual_share_instructions(name)


def _print_manual_share_instructions(name: str) -> None:
    """Print instructions for manual shared folder creation via web UI."""
    print("\n  Manual step needed — create shared folder via DSM web UI:")
    print("    Control Panel > Shared Folder > Create")
    print(f"    Name: {name}")
    print("    Location: Volume 1")
    print()


def set_share_permissions(
    base_url: str, sid: str, share_name: str, username: str, *, syno_token: str = ""
) -> None:
    """Grant read/write access to a user on a shared folder.

    Uses the undocumented SYNO.Core.Share.Permission API. If it fails,
    prints manual instructions and continues.
    """
    # The permission payload format varies across DSM versions. This is a
    # best-effort attempt based on community reverse engineering.
    try:
        body = _admin_post(
            base_url,
            sid,
            syno_token,
            {
                "api": "SYNO.Core.Share.Permission",
                "method": "set",
                "version": "1",
                "name": share_name,
                "user_group_type": "local_user",
                "permissions": f'{{"users":[{{"name":"{username}","is_writable":true}}]}}',
            },
        )

        if body.get("success"):
            logger.info("Set permissions on /%s for %s", share_name, username)
            print(f"  Set permissions on /{share_name} for {username}")
        else:
            code = body.get("error", {}).get("code", 0)
            logger.warning(
                "Set permissions API returned error code %d for '%s'",
                code,
                share_name,
            )
            print(f"  Warning: Set permissions on '{share_name}' returned error code {code}")
            _print_manual_permission_instructions(share_name, username)
    except Exception:
        logger.warning(
            "Set permissions API call failed for '%s'",
            share_name,
            exc_info=True,
        )
        _print_manual_permission_instructions(share_name, username)


def _print_manual_permission_instructions(share_name: str, username: str) -> None:
    """Print instructions for manual permission setting via web UI."""
    print("\n  Manual step needed — set permissions via DSM web UI:")
    print(f"    Control Panel > Shared Folder > Select '{share_name}' > Edit")
    print(f"    Permissions tab > Local users > {username} > Read/Write")
    print()


def upload_test_data(base_url: str, sid: str) -> None:
    """Upload seed files for search and listing tests.

    Creates small test files in /testshare for integration tests to validate
    against. Uses SYNO.FileStation.Upload with multipart POST.
    """
    test_files: list[tuple[str, str, bytes]] = [
        (
            "/testshare/Documents",
            "report.txt",
            b"This is a sample report for MCP integration testing.\n",
        ),
        (
            "/testshare/Documents",
            "search_target.txt",
            b"Bambu Lab X1C 3D printer configuration notes.\n",
        ),
        (
            "/testshare/Media",
            "sample.mkv",
            b"\x1a\x45\xdf\xa3",  # Minimal MKV/WebM magic bytes
        ),
    ]

    for dest_folder, filename, content in test_files:
        _upload_file(base_url, sid, dest_folder, filename, content)


def _upload_file(
    base_url: str,
    sid: str,
    dest_folder: str,
    filename: str,
    content: bytes,
) -> None:
    """Upload a single file via SYNO.FileStation.Upload.

    SID is passed as a query parameter. Form data includes api/version/method/
    path/overwrite/create_parents. File is sent as multipart "file" field.
    """
    url = f"{base_url}/webapi/entry.cgi"
    query_params = {"_sid": sid}
    form_data = {
        "api": "SYNO.FileStation.Upload",
        "version": "2",
        "method": "upload",
        "path": dest_folder,
        "overwrite": "true",
        "create_parents": "true",
    }
    file_obj = io.BytesIO(content)

    try:
        resp = httpx.post(
            url,
            params=query_params,
            data=form_data,
            files={"file": (filename, file_obj, "application/octet-stream")},
            timeout=60,
            verify=False,  # noqa: S501
        )
        resp.raise_for_status()
        body = resp.json()

        if body.get("success"):
            logger.info("Uploaded %s/%s (%d bytes)", dest_folder, filename, len(content))
            print(f"  Uploaded {dest_folder}/{filename}")
        else:
            code = body.get("error", {}).get("code", 0)
            logger.warning(
                "Upload failed for %s/%s with error code %d",
                dest_folder,
                filename,
                code,
            )
            print(f"  Warning: Upload {dest_folder}/{filename} failed (code {code})")
    except Exception:
        logger.warning(
            "Upload failed for %s/%s",
            dest_folder,
            filename,
            exc_info=True,
        )
        print(f"  Warning: Upload {dest_folder}/{filename} failed")


def _verify_setup(base_url: str, sid: str) -> bool:
    """Verify setup by listing shares via SYNO.FileStation.List."""
    params = {
        "api": "SYNO.FileStation.List",
        "version": "2",
        "method": "list_share",
        "_sid": sid,
    }
    try:
        resp = httpx.get(
            f"{base_url}/webapi/entry.cgi",
            params=params,
            timeout=30,
            verify=False,  # noqa: S501
        )
        resp.raise_for_status()
        body = resp.json()

        if body.get("success"):
            shares = body.get("data", {}).get("shares", [])
            share_names = [s.get("name", "") for s in shares]
            logger.info("Shares found: %s", share_names)
            print(f"  Shares visible: {share_names}")
            return True
        else:
            code = body.get("error", {}).get("code", 0)
            logger.warning("List shares failed with error code %d", code)
            return False
    except Exception:
        logger.warning("List shares verification failed", exc_info=True)
        return False


def setup_dsm_for_testing(
    base_url: str,
    admin_password: str,
    *,
    admin_user: str = DEFAULT_ADMIN_USER,
) -> dict[str, Any]:
    """Run the full post-wizard setup. Returns metadata dict.

    Steps:
    1. Login as admin
    2. Create test user
    3. Create shared folders: "testshare", "writable"
    4. Set permissions on shares for test user
    5. Upload test data to testshare
    6. Verify setup (list_shares check)
    7. Logout
    8. Return metadata dict with credentials and test_paths
    """
    print("\nConfiguring DSM for integration testing...")

    # 1. Login as admin (with SynoToken for CSRF protection)
    print("\n[1/7] Logging in as admin...")
    sid, syno_token = login(base_url, admin_user, admin_password)

    try:
        # 2. Create test user
        print("\n[2/7] Creating test user...")
        create_user(
            base_url,
            sid,
            DEFAULT_TEST_USER,
            DEFAULT_TEST_PASSWORD,
            syno_token=syno_token,
        )

        # 3. Create shared folders
        print("\n[3/7] Creating shared folders...")
        create_shared_folder(base_url, sid, "testshare", syno_token=syno_token)
        create_shared_folder(base_url, sid, "writable", syno_token=syno_token)

        # 4. Set permissions
        print("\n[4/7] Setting share permissions...")
        set_share_permissions(
            base_url,
            sid,
            "testshare",
            DEFAULT_TEST_USER,
            syno_token=syno_token,
        )
        set_share_permissions(
            base_url,
            sid,
            "writable",
            DEFAULT_TEST_USER,
            syno_token=syno_token,
        )

        # 5. Upload test data
        print("\n[5/7] Uploading test data...")
        upload_test_data(base_url, sid)

        # 6. Verify
        print("\n[6/7] Verifying setup...")
        _verify_setup(base_url, sid)

    finally:
        # 7. Logout
        print("\n[7/7] Logging out...")
        logout(base_url, sid)

    # 8. Build metadata
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
