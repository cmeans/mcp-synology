#!/usr/bin/env python3
"""Sync server.json version fields from pyproject.toml.

`pyproject.toml` is the single source of truth for the project version.
`server.json` (used by the MCP Registry and Glama.ai) carries two
independent version fields that have to match — the top-level `version`
and `packages[0].version`. Before this script, those were maintained by
hand and drifted in lockstep with releases.

Usage:
    python scripts/sync-server-json.py            # rewrite server.json in place
    python scripts/sync-server-json.py --check    # exit 1 if drift, 0 otherwise

CI calls --check to fail PRs that would ship a version mismatch. Release
flow calls the script without --check after bumping pyproject.toml.

Stdlib only — runs without `uv sync`, so CI can use it before any
project install.
"""

from __future__ import annotations

import argparse
import json
import sys
import tomllib
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
PYPROJECT = REPO_ROOT / "pyproject.toml"
SERVER_JSON = REPO_ROOT / "server.json"


def read_pyproject_version() -> str:
    with PYPROJECT.open("rb") as f:
        data = tomllib.load(f)
    version = data.get("project", {}).get("version")
    if not isinstance(version, str):
        raise SystemExit(f"error: [project].version not found or not a string in {PYPROJECT}")
    return version


def load_server_json() -> dict:
    with SERVER_JSON.open("r", encoding="utf-8") as f:
        return json.load(f)


def collect_versions(server: dict) -> dict[str, str]:
    """Return the version fields server.json currently advertises."""
    out: dict[str, str] = {"top_level": server.get("version", "")}
    packages = server.get("packages") or []
    for i, pkg in enumerate(packages):
        out[f"packages[{i}]"] = pkg.get("version", "")
    return out


def apply_version(server: dict, version: str) -> dict:
    """Return a new dict with all version fields set to `version`."""
    updated = dict(server)
    updated["version"] = version
    if "packages" in updated and isinstance(updated["packages"], list):
        updated["packages"] = [
            {**pkg, "version": version} if isinstance(pkg, dict) else pkg
            for pkg in updated["packages"]
        ]
    return updated


def serialize(server: dict) -> str:
    # Match the existing 2-space indent and trailing newline so diffs stay
    # quiet when only the version changed.
    return json.dumps(server, indent=2, ensure_ascii=False) + "\n"


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--check",
        action="store_true",
        help="Exit non-zero if server.json is out of sync. Do not modify files.",
    )
    args = parser.parse_args()

    pyproject_version = read_pyproject_version()
    server = load_server_json()
    current = collect_versions(server)
    drifted = {field: v for field, v in current.items() if v != pyproject_version}

    if args.check:
        if drifted:
            print(
                f"server.json version drift detected. pyproject.toml = {pyproject_version}",
                file=sys.stderr,
            )
            for field, value in drifted.items():
                print(f"  {field}: {value!r}", file=sys.stderr)
            print(
                "Run `python scripts/sync-server-json.py` to fix.",
                file=sys.stderr,
            )
            return 1
        print(f"server.json in sync with pyproject.toml ({pyproject_version})")
        return 0

    if not drifted:
        print(f"server.json already in sync ({pyproject_version}) — no changes")
        return 0

    updated = apply_version(server, pyproject_version)
    SERVER_JSON.write_text(serialize(updated), encoding="utf-8")
    print(f"Updated {SERVER_JSON.relative_to(REPO_ROOT)} to {pyproject_version}")
    for field, old in drifted.items():
        print(f"  {field}: {old!r} -> {pyproject_version!r}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
