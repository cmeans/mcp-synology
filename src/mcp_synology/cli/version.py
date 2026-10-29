"""Version checking, PyPI queries, auto-upgrade, revert, global state."""

from __future__ import annotations

import contextlib
import json as json_mod
import logging
import re
import subprocess
import sys
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any
from urllib.request import Request, urlopen

if TYPE_CHECKING:
    from collections.abc import Iterator

import click
import yaml

from mcp_synology import __version__

logger = logging.getLogger(__name__)

_PYPI_PACKAGE = "mcp-synology"
_GITHUB_REPO = "cmeans/mcp-synology"
_VERSION_CHECK_INTERVAL = 86400  # 24 hours
# A version is announced to the user at most once a week, a security release every
# session until upgraded (#314). The release's CHANGELOG section starts with this.
_ANNOUNCE_COOLDOWN = 7 * 86400
_SECURITY_MARKER = "**Security release:**"

# Loose PEP 440-ish: requires X.Y.Z and optionally trailing pre/post/dev/local
# segments separated by `-` or `.` (e.g. `0.5.1`, `0.5.1-rc1`, `1.2.3.post4`).
# Tight enough to reject `latest`, `1.2`, whitespace, or shell metacharacters
# before the value reaches `pip install mcp-synology==<value>`.
# Each part can match only one way, so a long non-matching value fails in linear
# time: an optional suffix glued to the patch number must start with a letter
# (`0.6.0rc1`), and every later segment starts with its separator. The old
# `([-.]?[a-zA-Z0-9]+)*` could split a run of letters or digits exponentially many
# ways (ReDoS, CodeQL py/redos, #206). Matched with fullmatch, since `$` would also
# accept a trailing newline.
_VERSION_RE = re.compile(r"\d+\.\d+\.\d+(?:[a-zA-Z][a-zA-Z0-9]*)?(?:[-.][a-zA-Z0-9]+)*")


def _validate_version_string(value: str) -> None:
    """Reject malformed version strings before they flow into a pip command.

    Raises click.ClickException so click renders the standard `Error: ...`
    line and exits 1 — same shape as the other CLI error paths.
    """
    if not isinstance(value, str) or not _VERSION_RE.fullmatch(value):
        raise click.ClickException(
            f"Invalid version string: {value!r}. "
            "Expected MAJOR.MINOR.PATCH with optional pre/post suffix "
            "(e.g. 0.5.1, 0.5.1-rc1, 1.2.3.post4)."
        )


def _get_current_version() -> str:
    """Return the running version."""
    try:
        from importlib.metadata import PackageNotFoundError
        from importlib.metadata import version as _pkg_version

        return _pkg_version(_PYPI_PACKAGE)
    except (PackageNotFoundError, ImportError):
        return __version__


def _get_latest_pypi_version() -> str | None:
    """Fetch the latest version from PyPI. Returns None on any failure."""
    try:
        req = Request(
            f"https://pypi.org/pypi/{_PYPI_PACKAGE}/json",
            headers={"User-Agent": f"{_PYPI_PACKAGE}/{_get_current_version()}"},
        )
        with urlopen(req, timeout=5) as resp:
            data = json_mod.loads(resp.read())
        result: str = data["info"]["version"]
        return result
    except (OSError, KeyError, ValueError):
        return None


# The PEP 440 forms this project publishes: X.Y.Z with an optional pre-release (a, b,
# rc), post-release and dev segment (`0.10.0`, `0.10.0rc11`, `0.10.0.post1`, `0.11.0.dev3`).
_PEP440_RE = re.compile(
    r"(\d+)\.(\d+)\.(\d+)"
    r"(?:[-_.]?(a|b|rc)[-_.]?(\d*))?"
    r"(?:[-_.]?post[-_.]?(\d*))?"
    r"(?:[-_.]?dev[-_.]?(\d*))?"
)
_PRE_RANK = {"a": 0, "b": 1, "rc": 2}


def _version_tuple(v: str) -> tuple[int, ...] | None:
    """A sort key for a PEP 440 version string, so `0.10.0rc11 < 0.10.0 < 0.10.0.post1`.

    Covers the forms above; returns None for anything else, so callers must handle it
    rather than have a `(0,)` sentinel silently compare less than every real version.
    Before #411 this split on dots and failed on any pre-release (`0.10.0rc9`), so an rc
    install never heard of the final release.
    """
    m = _PEP440_RE.fullmatch(v.strip()) if isinstance(v, str) else None
    if m is None:
        logger.debug("_version_tuple: failed to parse %r", v)
        return None
    major, minor, patch, pre, pre_n, post_n, dev_n = m.groups()
    # A plain release sorts after its pre-releases; a dev release of X.Y.Z with no
    # pre-release sorts before X.Y.Za1.
    if pre:
        pre_rank = _PRE_RANK[pre]
    elif dev_n is not None and post_n is None:
        pre_rank = -1
    else:
        pre_rank = 3
    return (
        int(major),
        int(minor),
        int(patch),
        pre_rank,
        int(pre_n or 0),
        -1 if post_n is None else int(post_n or 0),
        1 if dev_n is None else 0,
        int(dev_n or 0),
    )


def _is_newer(latest: str | None, current: str) -> bool:
    """Whether ``latest`` is a later version than ``current`` (False if either won't parse)."""
    if not latest:
        return False
    latest_t, current_t = _version_tuple(latest), _version_tuple(current)
    return latest_t is not None and current_t is not None and latest_t > current_t


def _detect_installer() -> str | None:
    """How this copy was installed: ``uv`` (uv tool), ``uvx``, ``pipx`` or None.

    The running interpreter's prefix tells it most reliably (an MCP client may launch
    the server by full path, so the ``mcp-synology`` on PATH can be another copy).
    PATH is asked only when this isn't a virtual environment at all, so a plain venv
    isn't reported as the uv tool copy that happens to be on PATH.
    """
    import shutil

    paths = [sys.prefix]
    if sys.prefix == sys.base_prefix:
        paths.append(shutil.which(_PYPI_PACKAGE) or "")
    for path in paths:
        norm = path.replace("\\", "/").lower()
        # uvx runs from uv's cache: ~/.cache/uv/archive-v0/… (Linux, macOS) or
        # …/AppData/Local/uv/cache/archive-v0/… (Windows).
        if re.search(r"/uv/(?:cache/)?archive-v\d", norm) or "/.cache/uv/" in norm:
            return "uvx"
        if "/uv/tools/" in norm:
            return "uv"
        if "pipx" in norm:
            return "pipx"
    return None


def _upgrade_hint() -> str:
    """The command that upgrades this install, for the update notice (#314)."""
    installer = _detect_installer()
    if installer == "uv":
        return f"run `uv tool upgrade {_PYPI_PACKAGE}`, then restart your MCP client"
    if installer == "pipx":
        return f"run `pipx upgrade {_PYPI_PACKAGE}`, then restart your MCP client"
    if installer == "uvx":
        # uvx re-resolves once its cached copy of PyPI's index is stale (PyPI sends
        # max-age=600), so a restart usually upgrades it; the cache clean makes sure.
        return (
            "restart your MCP client (uvx checks PyPI again once its cached index is over 10 "
            "minutes old); if it still runs the old version, quit the client, run "
            f"`uv cache clean {_PYPI_PACKAGE}`, then start it again"
        )
    if _uv_managed_env():
        return (
            f"run `uv pip install -U {_PYPI_PACKAGE} --python {sys.executable}`, "
            "then restart your MCP client"
        )
    return f"run `{sys.executable} -m pip install -U {_PYPI_PACKAGE}`, then restart your MCP client"


def _uv_managed_env() -> bool:
    """Whether this venv was made by uv (``uv venv``/``uv sync``), which installs no pip."""
    try:
        cfg = (Path(sys.prefix) / "pyvenv.cfg").read_text(encoding="utf-8")
    except OSError:
        return False
    return any(line.split("=")[0].strip() == "uv" for line in cfg.splitlines())


def _release_url(version: str) -> str:
    return f"https://github.com/{_GITHUB_REPO}/releases/tag/v{version}"


def _is_security_release(version: str) -> bool | None:
    """Whether the GitHub release for ``version`` is a security release (#314).

    A security release's CHANGELOG section starts with ``**Security release:**``, and
    ``publish.yml`` builds the release body from that section. None when GitHub can't be
    reached or has no such release; the caller treats that as an ordinary update.
    """
    try:
        req = Request(
            f"https://api.github.com/repos/{_GITHUB_REPO}/releases/tags/v{version}",
            headers={
                "User-Agent": f"{_PYPI_PACKAGE}/{_get_current_version()}",
                "Accept": "application/vnd.github+json",
            },
        )
        # 3 s, so PyPI (5 s) and GitHub fit the server's 10 s bound on the whole check.
        with urlopen(req, timeout=3) as resp:
            body = json_mod.loads(resp.read()).get("body") or ""
    except (OSError, ValueError, AttributeError):
        return None
    return _SECURITY_MARKER in body


def _install_key() -> str:
    """Which install is running: the interpreter's environment (#314).

    Announcements are recorded per install, not per machine: configs may run different
    installs at different versions, and announcing to one mustn't silence another. The
    connections that share an install (one uv tool copy serving three NASes) share one
    weekly announcement.
    """
    return sys.prefix


def _announced(state: dict[str, Any]) -> dict[str, Any]:
    """This install's announcement record; empty when missing or not what's expected."""
    announced = state.get("announced")
    record = announced.get(_install_key()) if isinstance(announced, dict) else None
    return record if isinstance(record, dict) else {}


def _should_announce(state: dict[str, Any], latest: str, *, now: float | None = None) -> bool:
    """Whether to announce ``latest`` now: a security release always, others once a week.

    ``announced`` in the global state records, per install, the version last announced
    and when; a newer version resets it.
    """
    if state.get("latest_is_security") and state.get("latest_known_version") == latest:
        return True
    record = _announced(state)
    if record.get("version") != latest:
        return True
    try:
        last = datetime.fromisoformat(str(record.get("at"))).timestamp()
    except (ValueError, TypeError):
        return True
    now = datetime.now(tz=UTC).timestamp() if now is None else now
    return now - last >= _ANNOUNCE_COOLDOWN


def _mark_announced(state: dict[str, Any], latest: str) -> None:
    announced = state.get("announced")
    if not isinstance(announced, dict):
        announced = state["announced"] = {}
    announced[_install_key()] = {"version": latest, "at": datetime.now(tz=UTC).isoformat()}


def claim_announcement(latest: str) -> bool:
    """Atomically decide and record that this process announces ``latest`` (#314).

    Every connection on the same install checks for updates on its own; the one that
    claims the announcement first delivers it, and the others, within the cooldown,
    don't. A security release is always claimed.
    """
    with _with_global_state_lock():
        state = _load_global_state()
        if not _should_announce(state, latest):
            return False
        _mark_announced(state, latest)
        _save_global_state(state)
    return True


def _cached_update(state: dict[str, Any]) -> str | None:
    """The newer version the last check found, from the state file (no network)."""
    latest = state.get("latest_known_version")
    if isinstance(latest, str) and _is_newer(latest, _get_current_version()):
        return latest
    return None


def update_notice(latest: str, *, security: bool = False, for_model: bool = True) -> str:
    """The update notice: what's out, how to upgrade this install, how to stop these
    notices and, for the model, to tell the user once (left out for ``check``, which a
    person reads)."""
    kind = "A security release" if security else "A new version"
    text = (
        f"{kind} of mcp-synology is available: {latest} (running {_get_current_version()}). "
        f"To upgrade, {_upgrade_hint()}. What's new: {_release_url(latest)}."
    )
    if not for_model:
        return text
    return (
        f"{text} To stop these notices, set `update_notices: security` or `off` in this "
        "connection's config. Mention this to the user once; don't repeat it."
    )


def _check_and_describe_update() -> str | None:
    """Run the update check (cached for a day) and return a person-facing notice, if any."""
    latest, security, _ = refresh_update_check()
    return None if latest is None else update_notice(latest, security=security, for_model=False)


def _global_state_path() -> Path:
    return Path.home() / ".local" / "state" / "mcp-synology" / "global.yaml"


def _load_global_state() -> dict[str, Any]:
    """Load global (non-instance) state for version tracking."""
    path = _global_state_path()
    if not path.exists():
        return {}
    try:
        raw = yaml.safe_load(path.read_text(encoding="utf-8"))
    except (OSError, ValueError, yaml.YAMLError):  # ValueError: not UTF-8
        return {}
    # Every instance on the machine shares this file: anything but a mapping is a bad
    # write, read as no state rather than stopping the server.
    return raw if isinstance(raw, dict) else {}


def _save_global_state(state: dict[str, Any]) -> None:
    """Save global state atomically."""
    from mcp_synology.core.fs import atomic_write_text

    raw_text = yaml.dump(state, default_flow_style=False, sort_keys=False)
    atomic_write_text(_global_state_path(), "# Auto-generated by mcp-synology.\n" + raw_text)


@contextlib.contextmanager
def _with_global_state_lock() -> Iterator[None]:
    """Hold an exclusive lock around a load → mutate → save sequence on global.yaml.

    Save-side atomicity (`atomic_write_text` from PR #69) prevents torn writes,
    but the read-modify-write sequence has no inter-process synchronization. Two
    callers can each load the file, mutate distinct keys, then save atomically —
    and the later writer's update overwrites the earlier writer's because each
    computed its new state from a stale read. The lock spans the whole critical
    section so callers (not the save function) hold it.

    Windows lacks `fcntl`; fall through with no locking. The practical risk
    surface is single-user dev boxes (per #93).
    """
    if sys.platform == "win32":
        yield
        return

    import fcntl

    lock_dir = _global_state_path().parent
    lock_dir.mkdir(parents=True, exist_ok=True)
    lock_path = lock_dir / "global.yaml.lock"
    with lock_path.open("a+") as fh:
        fcntl.flock(fh.fileno(), fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(fh.fileno(), fcntl.LOCK_UN)


def _check_for_update(state: dict[str, Any], *, force: bool = False) -> str | None:
    """Check PyPI for a newer version. Returns latest version string if newer, else None.

    PyPI is asked at most once per ``_VERSION_CHECK_INTERVAL``, or hourly after it didn't
    answer (unless ``force``); in between, the version it last reported is used. Used by
    auto-upgrade, with the caller holding the state lock; the update notices use
    ``refresh_update_check``, which also learns whether the release is a security release
    and keeps the network out of the lock (#314).
    """
    if not force and not _check_due(state, datetime.now(tz=UTC).timestamp()):
        return _cached_update(state)

    latest = _get_latest_pypi_version()
    if not latest:
        # Not stamped: an offline check mustn't hold off the notices' check for a day.
        state["version_check_failed_at"] = datetime.now(tz=UTC).isoformat()
        return None
    state["last_version_check"] = datetime.now(tz=UTC).isoformat()
    state.pop("version_check_failed_at", None)

    if state.get("latest_known_version") != latest:
        state.pop("latest_is_security", None)
    state["latest_known_version"] = latest
    if not _is_newer(latest, _get_current_version()):
        return None
    return latest


def _check_due(state: dict[str, Any], now: float) -> bool:
    """Whether to ask PyPI: once a day, or at most hourly after it didn't answer."""
    try:
        last = datetime.fromisoformat(str(state["last_version_check"])).timestamp()
    except (KeyError, ValueError, TypeError):
        last = None
    if last is not None and now - last < _VERSION_CHECK_INTERVAL:
        return False
    return _retry_due(state, "version_check_failed_at", now)


def _retry_due(state: dict[str, Any], key: str, now: float) -> bool:
    """Whether an hour has passed since the failed check recorded under ``key``."""
    try:
        last = datetime.fromisoformat(str(state[key])).timestamp()
    except (KeyError, ValueError, TypeError):
        return True
    return now - last >= 3600


def _security_retry_due(state: dict[str, Any], now: float) -> bool:
    """Whether to ask GitHub again after it didn't answer: at most once an hour."""
    return _retry_due(state, "security_checked_at", now)


def refresh_update_check(*, force: bool = False) -> tuple[str | None, bool, dict[str, Any]]:
    """The update check behind the notices: ``(newer version or None, security, state)``.

    PyPI is asked at most once a day (unless ``force``), and GitHub once per new version
    whether it's a security release, whatever version this install runs, so an older
    install sharing the cache doesn't miss the flag. A PyPI or GitHub that didn't answer
    (offline, or the release not published yet) is asked again at most hourly. The network calls run
    **outside** the global-state lock: it's held only to read, then to merge and save, so
    a connection claiming an announcement (#314) never waits on another's network round
    trip.
    """
    with _with_global_state_lock():
        state = _load_global_state()
    now = datetime.now(tz=UTC).timestamp()
    due = force or _check_due(state, now)
    fetched = _get_latest_pypi_version() if due else None
    known = fetched or state.get("latest_known_version")
    security: bool | None = None
    asked = False
    if isinstance(known, str) and (
        known != state.get("latest_known_version")
        or ("latest_is_security" not in state and _security_retry_due(state, now))
    ):
        security = _is_security_release(known)
        asked = True

    with _with_global_state_lock():
        state = _load_global_state()
        if fetched:
            state["last_version_check"] = datetime.now(tz=UTC).isoformat()
            state.pop("version_check_failed_at", None)
        elif due:
            state["version_check_failed_at"] = datetime.now(tz=UTC).isoformat()
        # Never move backwards: another process may have stored a newer version (from
        # another PyPI CDN node) between the two lock sections.
        if fetched and not _is_newer(state.get("latest_known_version"), fetched):
            if state.get("latest_known_version") != fetched:
                state.pop("latest_is_security", None)
                state.pop("security_checked_at", None)
            state["latest_known_version"] = fetched
        if asked and state.get("latest_known_version") == known:
            if security is None:
                state["security_checked_at"] = datetime.now(tz=UTC).isoformat()
            else:
                state["latest_is_security"] = security
                state.pop("security_checked_at", None)
        _save_global_state(state)
    latest = _cached_update(state)
    return latest, bool(latest and state.get("latest_is_security")), state


def _do_auto_upgrade() -> bool:
    """Upgrade to the latest version. Returns True on success."""
    installer = _detect_installer()
    current = _get_current_version()

    if installer == "uv":
        cmd = ["uv", "tool", "install", f"{_PYPI_PACKAGE}@latest"]
    elif installer == "pipx":
        cmd = ["pipx", "upgrade", _PYPI_PACKAGE]
    else:
        click.echo(
            "Cannot detect package manager (uv or pipx). "
            f"Upgrade manually: uv tool upgrade {_PYPI_PACKAGE}"
        )
        return False

    click.echo(f"Upgrading {_PYPI_PACKAGE} via {installer}...")
    result = subprocess.run(cmd, capture_output=True, text=True)
    if result.returncode == 0:
        with _with_global_state_lock():
            state = _load_global_state()
            state["previous_version"] = current
            _save_global_state(state)
        click.echo(click.style("Upgrade complete — takes effect on next run.", fg="green"))
        return True
    click.echo(click.style(f"Upgrade failed: {result.stderr.strip()}", fg="red"), err=True)
    return False


def _do_revert(target_version: str | None = None) -> None:
    """Revert to a specific version or the previously recorded one."""
    state = _load_global_state()
    current = _get_current_version()

    prev: str | None
    if target_version:
        prev = target_version
    else:
        prev = state.get("previous_version")
        if not prev:
            click.echo("No previous version recorded — nothing to revert to.")
            click.echo(f"Tip: specify a version explicitly: {_PYPI_PACKAGE} --revert 0.1.0")
            return

    _validate_version_string(prev)

    if prev == current:
        click.echo(f"Already running {current} — nothing to do.")
        return

    installer = _detect_installer()

    if installer == "uv":
        cmd = ["uv", "tool", "install", "--force", f"{_PYPI_PACKAGE}=={prev}"]
    elif installer == "pipx":
        cmd = ["pipx", "install", "--force", f"{_PYPI_PACKAGE}=={prev}"]
    else:
        click.echo(
            f"Cannot detect package manager. Install manually:\n"
            f"  uv tool install --force {_PYPI_PACKAGE}=={prev}\n"
            f"  pipx install --force {_PYPI_PACKAGE}=={prev}"
        )
        return

    click.echo(f"Reverting from {current} to {prev}...")
    result = subprocess.run(cmd, capture_output=True, text=True)
    if result.returncode == 0:
        with _with_global_state_lock():
            fresh = _load_global_state()
            fresh["previous_version"] = None
            fresh["auto_upgrade"] = False
            _save_global_state(fresh)
        click.echo(click.style(f"Reverted to {prev}. Takes effect on next run.", fg="green"))
        click.echo("Auto-upgrade has been disabled to prevent immediately re-upgrading.")
        click.echo(f"Re-enable with: {_PYPI_PACKAGE} --auto-upgrade enable")
    else:
        click.echo(click.style(f"Revert failed: {result.stderr.strip()}", fg="red"), err=True)
