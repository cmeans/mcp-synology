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
from typing import Any

REPO_ROOT = Path(__file__).resolve().parent.parent
PYPROJECT = REPO_ROOT / "pyproject.toml"
SERVER_JSON = REPO_ROOT / "server.json"

ServerJson = dict[str, Any]


def read_pyproject_version(path: Path = PYPROJECT) -> str:
    try:
        with path.open("rb") as f:
            data = tomllib.load(f)
    except FileNotFoundError:
        raise SystemExit(f"error: {path} not found") from None
    except tomllib.TOMLDecodeError as exc:
        raise SystemExit(f"error: {path} is not valid TOML: {exc}") from None
    version = data.get("project", {}).get("version")
    if not isinstance(version, str):
        raise SystemExit(f"error: [project].version not found or not a string in {path}")
    return version


def load_server_json(path: Path = SERVER_JSON) -> ServerJson:
    try:
        with path.open("r", encoding="utf-8") as f:
            data: Any = json.load(f)
    except FileNotFoundError:
        raise SystemExit(f"error: {path} not found") from None
    except json.JSONDecodeError as exc:
        raise SystemExit(f"error: {path} is not valid JSON: {exc}") from None
    if not isinstance(data, dict):
        raise SystemExit(f"error: {path} top-level value must be a JSON object")
    return data


def collect_versions(server: ServerJson) -> dict[str, str]:
    """Return the version fields server.json currently advertises."""
    out: dict[str, str] = {"top_level": str(server.get("version", ""))}
    packages = server.get("packages") or []
    if isinstance(packages, list):
        for i, pkg in enumerate(packages):
            if isinstance(pkg, dict):
                out[f"packages[{i}]"] = str(pkg.get("version", ""))
    return out


def apply_version(server: ServerJson, version: str) -> ServerJson:
    """Return a new dict with all version fields set to `version`."""
    updated: ServerJson = dict(server)
    updated["version"] = version
    packages = updated.get("packages")
    if isinstance(packages, list):
        updated["packages"] = [
            {**pkg, "version": version} if isinstance(pkg, dict) else pkg for pkg in packages
        ]
    return updated


def serialize(server: ServerJson) -> str:
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

    pyproject_version = read_pyproject_version(PYPROJECT)
    server = load_server_json(SERVER_JSON)
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
    try:
        display_path = SERVER_JSON.relative_to(REPO_ROOT)
    except ValueError:
        display_path = SERVER_JSON
    print(f"Updated {display_path} to {pyproject_version}")
    for field, old in drifted.items():
        print(f"  {field}: {old!r} -> {pyproject_version!r}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
