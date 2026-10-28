"""Scrub a real NAS's DSM captures before they go into the repo (#428).

Captures from the maintainer's own NAS become test fixtures and spec examples, so they
must not carry anything that identifies that NAS or its contents. This replaces, with
recognizable fakes of the same shape (the same fake everywhere a value appears):

- identifiers in fields named like ``serial``, ``unique_key`` or ``mac``/``macs`` (strings,
  or lists of strings): ``FAKE`` padded with digits;
- UUIDs, standard (``8-4-4-4-12``) and LVM-style (``6-4-4-4-4-4-6``), anywhere;
- shared-folder names, taken from every ``SYNO.Core.Share list`` reply in the file plus
  ``--share`` names: ``share01``, ``share02``, ... wherever a whole string, a path segment
  (``/volume1/<share>/...``) or a JSON-quoted request parameter, capture labels included;
- shared-folder descriptions, in the share entries themselves: ``Description NN`` (empty
  ones stay empty; a description that happens to equal another field, such as a USB
  stick's vendor, leaves that field alone);
- the hostname (from ``SYNO.Core.System.SystemHealth get``, plus ``--hostname``), as a
  whole word in any string, case-insensitively: ``NAS01``;
- private IPv4 addresses (10/8, 172.16/12, 192.168/16, and 100.64/10, the carrier-grade
  NAT range Tailscale uses): ``192.0.2.N`` (TEST-NET-1). IPv6 addresses (ULA ``fd00::/8``,
  link-local ``fe80::/10``) aren't handled yet; none has appeared in a capture so far.

Run it on every real-NAS capture before writing it under ``tests/``, as
``<name>-realnas.json``: the guard test checks every file named that way, and fails on
any fixture that names a real Synology model without it. ``check()`` (used by
``tests/scripts/test_scrub_nas_capture.py`` on every real-NAS fixture) fails on anything
that still looks real; given the raw names it also proves none of them is left.

    python scripts/scrub_nas_capture.py RAW.json OUT.json [--share NAME ...] [--hostname H]
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from collections.abc import Callable, Iterable

UUID_STD = re.compile(
    r"\b[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}\b"
)
UUID_LVM = re.compile(r"\b[0-9A-Za-z]{6}(?:-[0-9A-Za-z]{4}){5}-[0-9A-Za-z]{6}\b")
PRIVATE_IP = re.compile(
    r"\b(?:10\.\d{1,3}|172\.(?:1[6-9]|2\d|3[01])|192\.168"
    r"|100\.(?:6[4-9]|[7-9]\d|1[01]\d|12[0-7]))\.\d{1,3}\.\d{1,3}\b"
)
ID_KEY = re.compile(r"serial|unique_key|^macs?$|_macs?$|^mac_", re.IGNORECASE)
LABEL_NAME = re.compile(r'"name": "\\"(.*?)\\""')

FAKE_UUID_STD = re.compile(r"^00000000-0000-4000-8000-\d{12}$")
FAKE_UUID_LVM = re.compile(r"^FAKE00(?:-0000){5}-\d{6}$")
FAKE_SHARE = re.compile(r"^share\d{2}$")
FAKE_HOSTS = {"NAS01", "VirtualDSM"}
UNKNOWN_SHARE = re.compile(r"^no-such-")


def _walk(o: Any, fn: Callable[[str, Any], Any], key: str = "") -> Any:
    """Rebuild ``o``, passing every scalar to ``fn`` with the nearest dict key above it."""
    if isinstance(o, dict):
        return {k: _walk(v, fn, k) for k, v in o.items()}
    if isinstance(o, list):
        return [_walk(x, fn, key) for x in o]
    return fn(key, o)


def _share_entries(data: dict[str, Any]) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for entry in data.values():
        req, reply = entry.get("request", {}), entry.get("reply", {})
        is_list = req.get("api") == "SYNO.Core.Share" and req.get("method") == "list"
        if is_list and reply.get("success"):
            out.extend(reply.get("data", {}).get("shares", []))
    return out


def _hostnames(data: dict[str, Any]) -> set[str]:
    hosts: set[str] = set()
    for entry in data.values():
        if entry.get("request", {}).get("api") == "SYNO.Core.System.SystemHealth":
            host = entry.get("reply", {}).get("data", {}).get("hostname")
            if host:
                hosts.add(host)
    return hosts - FAKE_HOSTS


def raw_names(data: dict[str, Any]) -> set[str]:
    """Every share name, description and hostname a raw capture file holds.

    A description that DSM copied from another field (a USB share is described with the
    stick's vendor) isn't private and stays out.
    """
    entries = _share_entries(data)
    elsewhere: set[str] = set()

    def note(key: str, value: Any) -> Any:
        if key != "desc" and isinstance(value, str):
            elsewhere.add(value)
        return value

    _walk(data, note)
    return (
        {s["name"] for s in entries if s.get("name")}
        | {s["desc"] for s in entries if s.get("desc") and s["desc"] not in elsewhere}
        | _hostnames(data)
    )


def scrub(
    data: dict[str, Any], shares: Iterable[str] = (), hostnames: Iterable[str] = ()
) -> dict[str, Any]:
    """Return a scrubbed copy of a capture file's ``{label: {request, reply}}`` mapping."""
    entries = _share_entries(data)
    names = sorted({s["name"] for s in entries if s.get("name")} | set(shares))
    share_map = {n: f"share{i:02d}" for i, n in enumerate(names, 1)}
    descs = sorted({s["desc"] for s in entries if s.get("desc")})
    desc_map = {d: f"Description {i:02d}" for i, d in enumerate(descs, 1)}
    hosts = sorted(_hostnames(data) | set(hostnames), key=len, reverse=True)

    ids: dict[str, str] = {}

    def on_scalar(key: str, value: Any) -> Any:
        if isinstance(value, str) and value.strip() and ID_KEY.search(key):
            if value not in ids:
                ids[value] = "FAKE" + str(len(ids) + 1).zfill(max(len(value) - 4, 2))
            return ids[value]
        return value

    data = _walk(data, on_scalar)
    # Descriptions: only in the share entries, so a value that merely equals one elsewhere
    # (DSM names a USB share after the stick's vendor) is left alone.
    for entry in data.values():
        req, reply = entry.get("request", {}), entry.get("reply", {})
        if req.get("api") == "SYNO.Core.Share" and req.get("method") == "list":
            for s in reply.get("data", {}).get("shares", []) if reply.get("success") else []:
                if s.get("desc"):
                    s["desc"] = desc_map[s["desc"]]

    text = json.dumps(data, sort_keys=True)
    replacements: dict[str, str] = {}
    for m in sorted(set(UUID_STD.findall(text))):
        replacements[m] = f"00000000-0000-4000-8000-{len(replacements) + 1:012d}"
    for i, m in enumerate(sorted(set(UUID_LVM.findall(text))), 1):
        replacements[m] = f"FAKE00-0000-0000-0000-0000-0000-{i:06d}"
    for i, m in enumerate(sorted(set(PRIVATE_IP.findall(text))), 10):
        replacements[m] = f"192.0.2.{i}"
    for old, new in replacements.items():
        text = text.replace(old, new)
    for host in hosts:
        if len(host) >= 4:
            text = re.sub(rf"(?i)\b{re.escape(host)}\b", "NAS01", text)

    # Share names: a whole JSON string (a value or a key), a path segment, and each level
    # of JSON escaping a name can sit at (a JSON-quoted request parameter, and that
    # parameter inside a capture label).
    for old, new in sorted(share_map.items(), key=lambda kv: -len(kv[0])):
        q_old, q_new = json.dumps(old), json.dumps(new)
        for _ in range(3):
            text = text.replace(q_old, q_new)
            q_old, q_new = json.dumps(q_old)[1:-1], json.dumps(q_new)[1:-1]
        seg = re.escape(json.dumps(old)[1:-1])
        text = re.sub(rf"(?<=/){seg}(?=[/\\\"])", json.dumps(new)[1:-1], text)
    scrubbed: dict[str, Any] = json.loads(text)
    return scrubbed


def check(data: dict[str, Any], real: Iterable[str] = ()) -> list[str]:
    """Everything in a scrubbed file that still looks real (empty when clean).

    ``real`` adds known private strings (from ``raw_names()`` of the raw file), any
    occurrence of which is a problem.
    """
    problems: list[str] = []
    text = json.dumps(data)
    problems += [f"UUID {u}" for u in set(UUID_STD.findall(text)) if not FAKE_UUID_STD.match(u)]
    problems += [f"LVM UUID {u}" for u in set(UUID_LVM.findall(text)) if not FAKE_UUID_LVM.match(u)]
    problems += [f"private IP {ip}" for ip in set(PRIVATE_IP.findall(text))]

    def on_scalar(key: str, value: Any) -> Any:
        if not isinstance(value, str) or not value.strip():
            return value
        if ID_KEY.search(key) and not value.startswith("FAKE"):
            problems.append(f"identifier {key}={value}")
        if key == "share_name" and not FAKE_SHARE.match(value):
            problems.append(f"share {value}")
        return value

    _walk(data, on_scalar)
    for s in _share_entries(data):
        if not FAKE_SHARE.match(s.get("name", "")):
            problems.append(f"share {s.get('name')}")
        if s.get("desc") and not s["desc"].startswith("Description "):
            problems.append(f"share description of {s.get('name')}")
    for label, entry in data.items():
        req = entry.get("request", {})
        names = LABEL_NAME.findall(label)
        name = req.get("name")
        if isinstance(name, str) and req.get("api", "").startswith("SYNO.Core.Share"):
            names.append(json.loads(name) if name.startswith('"') else name)
        problems += [
            f"share in {label}: {n}"
            for n in names
            if not FAKE_SHARE.match(n) and not UNKNOWN_SHARE.match(n)
        ]
    problems += [f"hostname {h}" for h in _hostnames(data)]
    for value in real:
        escaped = json.dumps(value)[1:-1]
        if re.search(rf"(?<![\w-]){re.escape(value)}(?![\w-])", text) or (
            escaped != value and escaped in text
        ):
            problems.append(f"real value left: {value}")
    return sorted(set(problems))


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=(__doc__ or "").splitlines()[0])
    ap.add_argument("raw", type=Path)
    ap.add_argument("out", type=Path)
    ap.add_argument("--share", action="append", default=[], help="another share name to replace")
    ap.add_argument("--hostname", action="append", default=[], help="another hostname to replace")
    args = ap.parse_args(argv)
    raw = json.loads(args.raw.read_text())
    data = scrub(raw, args.share, args.hostname)
    problems = check(data, raw_names(raw) | set(args.share) | set(args.hostname))
    if problems:
        print("Still looks real; nothing written:", *problems, sep="\n  ", file=sys.stderr)
        return 1
    args.out.write_text(json.dumps(data, indent=1, sort_keys=True) + "\n")
    print(f"{args.out}: {len(data)} captures scrubbed")
    return 0


if __name__ == "__main__":
    sys.exit(main())
