#!/usr/bin/env python3
"""Migrate from synology-mcp (<=0.3.x) to mcp-synology (>=0.4.0).

Moves config/state directories and migrates keyring entries.
Safe to run multiple times — skips already-migrated items.

Usage:
    python scripts/migrate-from-synology-mcp.py          # dry run
    python scripts/migrate-from-synology-mcp.py --apply   # apply changes
"""

from __future__ import annotations

import argparse
import json
import shutil
from pathlib import Path

OLD_NAME = "synology-mcp"
NEW_NAME = "mcp-synology"

KEYRING_KEYS = ("username", "password", "device_id")


def migrate_directory(old: Path, new: Path, *, dry_run: bool) -> bool:
    """Move old directory to new location. Returns True if action taken."""
    if not old.exists():
        return False
    if new.exists():
        print(f"  SKIP  {old} -> {new}  (destination already exists)")
        return False
    if dry_run:
        print(f"  MOVE  {old} -> {new}")
        return True
    new.parent.mkdir(parents=True, exist_ok=True)
    shutil.move(str(old), str(new))
    print(f"  MOVED {old} -> {new}")
    return True


def discover_instances(config_dir: Path, state_dir: Path) -> set[str]:
    """Find instance IDs from config filenames and state subdirectories."""
    instances: set[str] = set()

    # Config files: ~/.config/synology-mcp/<instance_id>.yaml or config.yaml
    if config_dir.exists():
        for f in config_dir.iterdir():
            if f.suffix in (".yaml", ".yml") and f.stem != "config":
                instances.add(f.stem)
        # default instance uses config.yaml
        if (config_dir / "config.yaml").exists():
            instances.add("default")

    # State dirs: ~/.local/state/synology-mcp/<instance_id>/
    if state_dir.exists():
        for d in state_dir.iterdir():
            if d.is_dir():
                instances.add(d.name)

    return instances


def migrate_keyring(instances: set[str], *, dry_run: bool) -> int:
    """Migrate keyring entries from old service name to new. Returns count."""
    try:
        import keyring as kr
    except ImportError:
        print("  SKIP  keyring not installed — cannot migrate credentials")
        return 0

    count = 0
    for instance_id in sorted(instances):
        old_service = f"{OLD_NAME}/{instance_id}"
        new_service = f"{NEW_NAME}/{instance_id}"

        migrated_any = False
        for key in KEYRING_KEYS:
            try:
                value = kr.get_password(old_service, key)
            except Exception:
                continue

            if not value:
                continue

            # Check if already migrated
            try:
                existing = kr.get_password(new_service, key)
                if existing:
                    continue
            except Exception:
                pass

            if dry_run:
                print(f"  COPY  keyring {old_service}/{key} -> {new_service}/{key}")
            else:
                try:
                    kr.set_password(new_service, key, value)
                    print(f"  COPIED keyring {old_service}/{key} -> {new_service}/{key}")
                except Exception as e:
                    print(f"  ERROR keyring {new_service}/{key}: {e}")
                    continue
            migrated_any = True

        if migrated_any:
            count += 1

    return count


def cleanup_keyring(instances: set[str], *, dry_run: bool) -> None:
    """Delete old keyring entries after successful migration."""
    try:
        import keyring as kr
    except ImportError:
        return

    for instance_id in sorted(instances):
        old_service = f"{OLD_NAME}/{instance_id}"
        for key in KEYRING_KEYS:
            try:
                value = kr.get_password(old_service, key)
            except Exception:
                continue
            if not value:
                continue
            if dry_run:
                print(f"  DELETE keyring {old_service}/{key}")
            else:
                try:
                    kr.delete_password(old_service, key)
                    print(f"  DELETED keyring {old_service}/{key}")
                except Exception as e:
                    print(f"  ERROR  deleting {old_service}/{key}: {e}")


def _find_claude_desktop_config() -> Path | None:
    """Locate claude_desktop_config.json across platforms."""
    home = Path.home()
    candidates = [
        home / ".config" / "Claude" / "claude_desktop_config.json",  # Linux
        home / "Library" / "Application Support" / "Claude" / "claude_desktop_config.json",  # macOS
        home / "AppData" / "Roaming" / "Claude" / "claude_desktop_config.json",  # Windows
    ]
    for path in candidates:
        if path.exists():
            return path
    return None


def migrate_claude_desktop_config(*, dry_run: bool) -> bool:
    """Update Claude Desktop config: replace old synology-mcp references.

    Rewrites command/args entries that reference synology-mcp to use
    uvx mcp-synology, and updates config paths from synology-mcp to mcp-synology.
    Returns True if changes were made (or would be made in dry run).
    """
    config_path = _find_claude_desktop_config()
    if not config_path:
        print("  SKIP  claude_desktop_config.json not found")
        return False

    text = config_path.read_text(encoding="utf-8")
    try:
        data = json.loads(text)
    except json.JSONDecodeError:
        print(f"  ERROR could not parse {config_path}")
        return False

    servers = data.get("mcpServers", {})
    changed = False
    uvx_path = shutil.which("uvx") or "uvx"

    for name, entry in servers.items():
        args = entry.get("args", [])

        # Detect old-style configs: command is synology-mcp, or args contain synology-mcp
        is_old_direct = entry.get("command", "").endswith("synology-mcp") and "serve" in args
        is_old_uv_run = "run" in args and "synology-mcp" in args

        if not is_old_direct and not is_old_uv_run:
            continue

        # Extract the config path from args (follows --config)
        config_arg = None
        for i, arg in enumerate(args):
            if arg == "--config" and i + 1 < len(args):
                config_arg = args[i + 1]
                break

        # Update config path references
        if config_arg:
            config_arg = config_arg.replace("synology-mcp", "mcp-synology")

        # Collect extra args (anything that's not the old command structure or --config)
        extra_args: list[str] = []
        skip_next = False
        known_old = {"--directory", "run", "synology-mcp", "mcp-synology", "serve", "--config"}
        for arg in args:
            if skip_next:
                skip_next = False
                continue
            if arg in known_old:
                if arg in ("--directory", "--config"):
                    skip_next = True  # skip the value that follows
                continue
            # Skip the --directory value (path)
            extra_args.append(arg)

        # Build new entry with uvx
        new_args = ["mcp-synology", "serve"]
        if config_arg:
            new_args.extend(["--config", config_arg])
        new_args.extend(extra_args)

        old_desc = f"{entry.get('command', '?')} {' '.join(args)}"
        new_desc = f"{uvx_path} {' '.join(new_args)}"

        if dry_run:
            print(f"  UPDATE [{name}]")
            print(f"         old: {old_desc}")
            print(f"         new: {new_desc}")
            if extra_args:
                print(f"         preserved extra args: {extra_args}")
        else:
            entry["command"] = uvx_path
            entry["args"] = new_args
            print(f"  UPDATED [{name}] -> {uvx_path} {' '.join(new_args)}")
            if extra_args:
                print(f"         preserved extra args: {extra_args}")

        changed = True

    if not changed:
        print("  OK    no synology-mcp references found in Claude Desktop config")
        return False

    if not dry_run:
        backup = config_path.with_suffix(".json.bak")
        shutil.copy2(config_path, backup)
        config_path.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")
        print(f"  SAVED {config_path} (backup: {backup})")

    return changed


def main() -> None:
    parser = argparse.ArgumentParser(description="Migrate from synology-mcp to mcp-synology")
    parser.add_argument(
        "--apply",
        action="store_true",
        help="Apply changes (default is dry run)",
    )
    parser.add_argument(
        "--cleanup",
        action="store_true",
        help="Delete old keyring entries after migration (requires --apply)",
    )
    args = parser.parse_args()
    dry_run = not args.apply

    if dry_run:
        print("DRY RUN — pass --apply to make changes\n")

    home = Path.home()
    old_config = home / ".config" / OLD_NAME
    new_config = home / ".config" / NEW_NAME
    old_state = home / ".local" / "state" / OLD_NAME
    new_state = home / ".local" / "state" / NEW_NAME

    # --- Discover instances before moving directories ---
    instances = discover_instances(old_config, old_state)
    if not instances:
        # Try the new locations in case directories already moved but keyring didn't
        instances = discover_instances(new_config, new_state)

    # --- Directories ---
    print("Directories:")
    dir_actions = 0
    dir_actions += migrate_directory(old_config, new_config, dry_run=dry_run)
    dir_actions += migrate_directory(old_state, new_state, dry_run=dry_run)
    if not dir_actions and not old_config.exists() and not old_state.exists():
        if new_config.exists() or new_state.exists():
            print("  OK    directories already at new location")
        else:
            print("  SKIP  no config or state directories found")
    print()

    # --- Keyring ---
    print("Keyring:")
    if not instances:
        print("  SKIP  no instances found to migrate")
    else:
        print(f"  Found instances: {', '.join(sorted(instances))}")
        count = migrate_keyring(instances, dry_run=dry_run)
        if count == 0:
            print("  OK    all keyring entries already migrated (or not present)")

        if args.cleanup:
            print()
            print("Cleanup (removing old keyring entries):")
            cleanup_keyring(instances, dry_run=dry_run)
        elif not dry_run and count > 0:
            print("\n  TIP: Run with --apply --cleanup to remove old keyring entries")
    print()

    # --- Claude Desktop config ---
    print("Claude Desktop:")
    desktop_changed = migrate_claude_desktop_config(dry_run=dry_run)
    print()

    # --- Summary ---
    if dry_run:
        print("Re-run with --apply to execute these changes.")
    else:
        print("Migration complete.")
        print(f"  - Config: {new_config}")
        print(f"  - State:  {new_state}")
        if desktop_changed:
            print("  - Claude Desktop config updated — restart Claude Desktop")


if __name__ == "__main__":
    main()
