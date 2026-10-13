#!/usr/bin/env python3
"""Measure what mcp-synology sends a model before any conversation (#163).

Reports, for an offline all-modules config (host 192.0.2.1, TEST-NET-1, so nothing can
reach a NAS):
- size in characters: schemas, descriptions, instructions, total;
- exact Claude token counts (``/v1/messages/count_tokens``; needs ANTHROPIC_API_KEY);
- optional Ollama prompt tokens (``--ollama URL --ollama-model M``);
- mcpscore and ``tdqs lint`` (pinned, via uvx; mcpscore never gets ``--smoke``, so no
  tool is ever called);
- optional ``tdqs score`` graded by Claude (``--tdqs-score``; needs ANTHROPIC_API_KEY).

Usage:
    uv run python scripts/measure_tools.py                       # report to .cache/measure/
    uv run python scripts/measure_tools.py --compare OLD.json     # print the difference
    uv run python scripts/measure_tools.py --no-external          # size and tokens only
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import shlex
import shutil
import subprocess
import sys
import tempfile
import urllib.request
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

REPO = Path(__file__).resolve().parents[1]
MCPSCORE = "mcpscore==1.22.1"
TDQS = "tdqs==0.1.0"
CLAUDE_MODELS = ("claude-haiku-4-5-20251001", "claude-haiku-5-5", "claude-sonnet-5-5")
TDQS_MODEL = "claude-sonnet-5-5"
OFFLINE_CONFIG = """\
# measure_tools.py: all modules at write tier; TEST-NET-1 host, so nothing reaches a NAS.
schema_version: 1
instance_id: measure-tools
connection: {host: 192.0.2.1, port: 5000, https: false}
auth: {username: nobody}
modules:
  filestation: {enabled: true, permission: write}
  system: {enabled: true}
  downloadstation: {enabled: true, permission: write}
"""


def write_offline_config(directory: Path) -> Path:
    path = directory / "measure-tools.yaml"
    path.write_text(OFFLINE_CONFIG, encoding="utf-8")
    return path


def _served(config_path: Path) -> tuple[list[dict[str, Any]], str]:
    from mcp_synology.core.config import load_config
    from mcp_synology.server import create_server

    server = create_server(load_config(str(config_path)))
    tools = asyncio.run(server.list_tools())
    served = [
        {"name": t.name, "description": t.description or "", "input_schema": t.inputSchema}
        for t in tools
    ]
    return served, server.instructions or ""


def measure_size(config_path: Path) -> dict[str, int]:
    tools, instructions = _served(config_path)
    schemas = sum(len(json.dumps(t["input_schema"])) for t in tools)
    descriptions = sum(len(t["description"]) for t in tools)
    return {
        "tools": len(tools),
        "schemas": schemas,
        "descriptions": descriptions,
        "instructions": len(instructions),
        "total": schemas + descriptions + len(instructions),
        "schema_titles": sum(json.dumps(t["input_schema"]).count('"title"') for t in tools),
    }


def _post_json(url: str, body: dict[str, Any], headers: dict[str, str]) -> dict[str, Any]:
    request = urllib.request.Request(
        url, data=json.dumps(body).encode(), headers={"content-type": "application/json", **headers}
    )
    with urllib.request.urlopen(request, timeout=60) as response:
        result: dict[str, Any] = json.load(response)
    return result


def claude_tokens(config_path: Path) -> dict[str, Any]:
    key = os.environ.get("ANTHROPIC_API_KEY")
    if not key:
        return {"status": "skipped", "reason": "ANTHROPIC_API_KEY not set"}
    tools, instructions = _served(config_path)
    counts: dict[str, Any] = {}
    for model in CLAUDE_MODELS:
        try:
            counts[model] = _post_json(
                "https://api.anthropic.com/v1/messages/count_tokens",
                {
                    "model": model,
                    "system": instructions,
                    "tools": tools,
                    "messages": [{"role": "user", "content": "hi"}],
                },
                {"x-api-key": key, "anthropic-version": "2023-06-01"},
            )["input_tokens"]
        except Exception as e:  # report and keep measuring
            counts[model] = f"unavailable: {type(e).__name__}"
    return counts


def ollama_tokens(config_path: Path, base_url: str, model: str) -> dict[str, Any]:
    tools, instructions = _served(config_path)
    body = {
        "model": model,
        "stream": False,
        "options": {"num_predict": 1, "num_ctx": 32768},
        "messages": [
            {"role": "system", "content": instructions},
            {"role": "user", "content": "hi"},
        ],
        "tools": [
            {
                "type": "function",
                "function": {
                    "name": t["name"],
                    "description": t["description"],
                    "parameters": t["input_schema"],
                },
            }
            for t in tools
        ],
    }
    try:
        result = _post_json(f"{base_url.rstrip('/')}/api/chat", body, {})
    except Exception as e:  # report instead of failing the whole run
        return {"status": "unavailable", "reason": f"{type(e).__name__}: {e}"}
    return {model: result.get("prompt_eval_count")}


def run_external(argv: list[str], env: dict[str, str] | None = None) -> dict[str, Any]:
    """Run a pinned external tool that prints JSON; never raise."""
    if shutil.which(argv[0]) is None:
        return {"status": "unavailable", "reason": f"{argv[0]} not found"}
    try:
        proc = subprocess.run(
            argv,
            capture_output=True,
            text=True,
            timeout=1200,
            env={**os.environ, **(env or {})},
            check=False,
        )
    except subprocess.TimeoutExpired:
        return {"status": "unavailable", "reason": "timed out"}
    if proc.returncode not in (0, 1):  # tdqs exits 1 when a threshold isn't met
        return {"status": "unavailable", "reason": f"exit {proc.returncode}: {proc.stderr[-300:]}"}
    try:
        parsed: dict[str, Any] = json.loads(proc.stdout)
    except json.JSONDecodeError:
        return {"status": "unavailable", "reason": f"no JSON output: {proc.stderr[-300:]}"}
    return parsed


def _serve_argv(config_path: Path, repo: Path) -> list[str]:
    return [
        "uv",
        "run",
        "--project",
        str(repo),
        "mcp-synology",
        "serve",
        "--config",
        str(config_path),
    ]


def mcpscore(config_path: Path, repo: Path = REPO) -> dict[str, Any]:
    report = run_external(
        [
            "uvx",
            "--from",
            MCPSCORE,
            "mcpscore",
            "--no-config",
            "--json",
            "--stdio",
            *_serve_argv(config_path, repo),
        ]
    )
    if "score" not in report:
        return report
    failed = [r["rule_id"] for r in report.get("results", []) if not r.get("passed")]
    return {"score": report["score"], "max_score": report["max_score"], "failed_rules": failed}


def tdqs(mode: str, config_path: Path, repo: Path = REPO) -> dict[str, Any]:
    env: dict[str, str] = {}
    if mode == "score":
        key = os.environ.get("ANTHROPIC_API_KEY")
        if not key:
            return {"status": "skipped", "reason": "ANTHROPIC_API_KEY not set"}
        env = {
            "TDQS_BASE_URL": "https://api.anthropic.com/v1",
            "TDQS_MODEL": TDQS_MODEL,
            "TDQS_API_KEY": key,
            "TDQS_REQUEST_OVERRIDES": '{"response_format":null,"temperature":null}',
        }
    report = run_external(
        [
            "uvx",
            "--from",
            TDQS,
            "tdqs",
            mode,
            "--command",
            shlex.join(_serve_argv(config_path, repo)),
            "--format",
            "json",
        ],
        env,
    )
    if mode == "lint" and "findings" in report:
        rules: dict[str, int] = {}
        for finding in report["findings"]:
            rules[finding["rule"]] = rules.get(finding["rule"], 0) + 1
        return {"warnings": len(report["findings"]), "by_rule": rules}
    if mode == "score" and "serverScore" in report:
        server = report["serverScore"]
        keep = (
            "overallScore",
            "overallTier",
            "descriptionQualityScore",
            "coherenceScore",
            "meanTdqs",
            "minTdqs",
            "toolCountAppropriateness",
        )
        return {
            **{k: server.get(k) for k in keep},
            "model": report.get("model"),
            "tools": {t["name"]: t["tdqs"] for t in report.get("tools", [])},
        }
    return report


def compare(old: dict[str, Any], new: dict[str, Any]) -> str:
    lines = []
    a, b = old["size"]["total"], new["size"]["total"]
    lines.append(f"size total: {a} -> {b} ({b - a:+d}, {100 * (b - a) / a:+.0f}%)")
    for model, before in (old.get("claude_tokens") or {}).items():
        after = (new.get("claude_tokens") or {}).get(model)
        if isinstance(before, int) and isinstance(after, int):
            lines.append(f"{model}: {before} -> {after} ({after - before:+d})")
    for name in ("mcpscore",):
        o, n = old.get(name) or {}, new.get(name) or {}
        if "score" in o and "score" in n:
            lines.append(f"{name}: {o['score']}/{o['max_score']} -> {n['score']}/{n['max_score']}")
    o, n = old.get("tdqs_lint") or {}, new.get("tdqs_lint") or {}
    if "warnings" in o and "warnings" in n:
        lines.append(f"tdqs lint warnings: {o['warnings']} -> {n['warnings']}")
    o, n = old.get("tdqs_score") or {}, new.get("tdqs_score") or {}
    if "overallScore" in o and "overallScore" in n:
        lines.append(
            f"tdqs score: {o['overallScore']} ({o['overallTier']}) -> "
            f"{n['overallScore']} ({n['overallTier']})"
        )
    return "\n".join(lines)


def _git_sha(repo: Path) -> str:
    proc = subprocess.run(
        ["git", "-C", str(repo), "rev-parse", "--short", "HEAD"],
        capture_output=True,
        text=True,
        check=False,
    )
    return proc.stdout.strip() or "unknown"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Measure the mcp-synology tool payload (#163).")
    parser.add_argument(
        "--repo",
        type=Path,
        default=REPO,
        help="checkout whose server mcpscore/tdqs launch (default: this script's checkout); "
        "run the script with `uv run --project <repo>` so in-process sizing uses it too",
    )
    parser.add_argument("--out", type=Path)
    parser.add_argument("--compare", type=Path)
    parser.add_argument("--tdqs-score", action="store_true")
    parser.add_argument("--no-external", action="store_true", help="skip mcpscore and tdqs")
    parser.add_argument("--ollama")
    parser.add_argument("--ollama-model")
    args = parser.parse_args(argv)

    with tempfile.TemporaryDirectory() as tmp:
        config = write_offline_config(Path(tmp))
        report: dict[str, Any] = {
            "git_sha": _git_sha(args.repo),
            "generated_utc": datetime.now(UTC).isoformat(timespec="seconds"),
            "size": measure_size(config),
            "claude_tokens": claude_tokens(config),
            "ollama_tokens": (
                ollama_tokens(config, args.ollama, args.ollama_model)
                if args.ollama and args.ollama_model
                else {"status": "skipped", "reason": "no --ollama"}
            ),
            "mcpscore": {"status": "skipped"} if args.no_external else mcpscore(config, args.repo),
            "tdqs_lint": (
                {"status": "skipped"} if args.no_external else tdqs("lint", config, args.repo)
            ),
            "tdqs_score": tdqs("score", config, args.repo)
            if args.tdqs_score
            else {"status": "skipped", "reason": "no --tdqs-score"},
        }

    out = args.out or args.repo / ".cache" / "measure" / (
        f"{datetime.now(UTC):%Y%m%dT%H%M%SZ}-{report['git_sha']}.json"
    )
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")

    size = report["size"]
    print(f"## Tool payload ({report['git_sha']})\n")
    print(
        f"- size: {size['total']} chars (schemas {size['schemas']}, descriptions "
        f"{size['descriptions']}, instructions {size['instructions']}; {size['tools']} tools)"
    )
    print(f"- Claude tokens: {report['claude_tokens']}")
    print(f"- mcpscore: {report['mcpscore']}")
    print(f"- tdqs lint: {report['tdqs_lint']}")
    if args.tdqs_score:
        print(f"- tdqs score: {report['tdqs_score']}")
    if args.compare:
        print("\n### Compared with " + args.compare.name + "\n")
        print(compare(json.loads(args.compare.read_text()), report))
    print(f"\nreport: {out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
