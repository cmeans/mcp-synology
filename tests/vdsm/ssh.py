"""SSH-into-DSM-guest helper, shared by setup_dsm.py and conftest fixtures.

Lifted from setup_dsm.py so the same SSH plumbing can be used at
golden-image-build time (one-shot, sudo for share/index admin) and at
test-fixture time (per-test, sudo for synoindex against runtime-created
directories).
"""

from __future__ import annotations

import os
import shlex
import subprocess

SSH_OPTS = (
    "-o StrictHostKeyChecking=no -o UserKnownHostsFile=/dev/null "
    "-o PreferredAuthentications=password -o PubkeyAuthentication=no "
    "-o ConnectTimeout=10"
)


def ssh_exec(
    host: str,
    port: int,
    password: str,
    cmd: str,
    *,
    sudo: bool = True,
    timeout: int = 30,
) -> tuple[int, str]:
    """Run a command inside the DSM guest via SSH.

    Password is passed via SSHPASS env var (for sshpass) and piped to
    sudo -S via stdin when sudo=True. shlex.quote prevents shell injection
    if the password contains special characters.

    Returns (returncode, filtered_output) — the sudo lecture / password
    prompt boilerplate is stripped from stdout for cleaner test logs.
    """
    env = os.environ.copy()
    env["SSHPASS"] = password
    quoted_pw = shlex.quote(password)
    remote_cmd = f"echo {quoted_pw} | sudo -S {cmd} 2>&1" if sudo else f"{cmd} 2>&1"
    result = subprocess.run(
        f'sshpass -e ssh {SSH_OPTS} -p {port} mcpadmin@{host} "{remote_cmd}"',
        shell=True,  # noqa: S602
        capture_output=True,
        text=True,
        timeout=timeout,
        env=env,
    )
    # Strip sudo lecture and password prompt from output.
    lines = result.stdout.strip().split("\n")
    filtered = [
        ln
        for ln in lines
        if not ln.startswith("Password:")
        and "lecture" not in ln
        and not ln.strip().startswith("#")
        and "Respect the privacy" not in ln
        and "Think before you type" not in ln
        and "great power" not in ln
    ]
    return result.returncode, "\n".join(filtered).strip()
