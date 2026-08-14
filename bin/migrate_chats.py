"""Migrate chat data from the old per-workspace layout to the new global one.

Old layout (each workspace had its own chats directory):
    <workspace_root>/.codewood/chats/
        chats.json                      # per-workspace chat index
        <hex>.json                      # chat record files (flat)
        data/<record-stem>/             # per-chat side data

New layout (all workspaces share one global chats directory):
    ~/.config/codewood/chats/
        <workspace id>.json             # per-workspace chat index (was chats.json)
        <YYYY>/<MM>/<DD>/<hex>.json     # chat record, grouped by creation date
        <YYYY>/<MM>/<DD>/data/<record-stem>/   # per-chat side data

The target date directory is derived from each chat's creation date
(``created_at``, falling back to ``updated_at``, the record file's mtime, or
today). The old ``chats.json`` is renamed to ``chats.json.migrated`` after a
successful migration so the script is idempotent; the old directory itself is
left in place (only record/data files are moved).

Usage:
    python bin/migrate_chats.py [--dry-run] [--force] [--config-dir PATH]
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import sys
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional

ROOT_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT_DIR not in sys.path:
    sys.path.insert(0, ROOT_DIR)


def _parse_date(text: str) -> Optional[datetime]:
    t = str(text or "").strip()
    if not t:
        return None
    try:
        return datetime.strptime(t[:10], "%Y-%m-%d")
    except ValueError:
        return None


def _chat_date_dir(chat: Dict[str, Any], record_path: Path) -> str:
    for key in ("created_at", "updated_at"):
        d = _parse_date(str(chat.get(key) or ""))
        if d is not None:
            return f"{d.year:04d}/{d.month:02d}/{d.day:02d}"
    try:
        mtime = datetime.fromtimestamp(record_path.stat().st_mtime)
        return f"{mtime.year:04d}/{mtime.month:02d}/{mtime.day:02d}"
    except OSError:
        now = datetime.now()
        return f"{now.year:04d}/{now.month:02d}/{now.day:02d}"


def _load_json(path: Path) -> Optional[Dict[str, Any]]:
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
        return data if isinstance(data, dict) else None
    except Exception:
        return None


def migrate_workspace(
    ws_id: str,
    old_chats_dir: Path,
    new_chats_root: Path,
    dry_run: bool,
    force: bool,
) -> Dict[str, Any]:
    """Migrate one workspace's ``chats`` directory to the new layout.

    Returns a stats dict ``{status, moved, skipped, data_dirs, error}``.
    """
    stats: Dict[str, Any] = {
        "status": "ok",
        "moved": 0,
        "skipped": 0,
        "data_dirs": 0,
        "error": "",
    }
    old_index = old_chats_dir / "chats.json"
    if not old_index.is_file():
        stats["status"] = "no-op"
        stats["skipped"] = 0
        return stats
    new_index_path = new_chats_root / f"{ws_id}.json"
    if new_index_path.is_file() and not force:
        stats["status"] = "already-migrated"
        return stats

    index = _load_json(old_index)
    if index is None:
        stats["status"] = "error"
        stats["error"] = f"cannot read {old_index}"
        return stats
    chats = index.get("chats")
    if not isinstance(chats, list):
        stats["status"] = "error"
        stats["error"] = f"{old_index} has no chats list"
        return stats

    new_entries: List[Dict[str, Any]] = []
    for entry in chats:
        if not isinstance(entry, dict):
            continue
        record_file = str(entry.get("record_file") or "").strip()
        if not record_file:
            continue
        rel = Path(record_file)
        old_record = (old_chats_dir / rel).resolve()
        try:
            old_record.relative_to(old_chats_dir.resolve())
        except ValueError:
            stats["skipped"] += 1
            continue
        if not old_record.is_file():
            stats["skipped"] += 1
            continue
        record = _load_json(old_record) or {}
        date_dir = _chat_date_dir(record, old_record)
        name = old_record.name
        new_rel = f"{date_dir}/{name}"
        new_record = (new_chats_root / new_rel).resolve()
        try:
            new_record.relative_to(new_chats_root.resolve())
        except ValueError:
            stats["skipped"] += 1
            continue
        stem = old_record.stem
        old_data = old_chats_dir / "data" / stem
        new_data = new_record.parent / "data" / stem
        if not dry_run:
            new_record.parent.mkdir(parents=True, exist_ok=True)
            if new_record.exists():
                stats["skipped"] += 1
                continue
            shutil.move(str(old_record), str(new_record))
            if old_data.is_dir() and not new_data.exists():
                new_data.parent.mkdir(parents=True, exist_ok=True)
                shutil.move(str(old_data), str(new_data))
                stats["data_dirs"] += 1
        else:
            stats["data_dirs"] += 1 if old_data.is_dir() else 0
        entry["record_file"] = new_rel
        new_entries.append(entry)
        stats["moved"] += 1

    if not dry_run:
        new_chats_root.mkdir(parents=True, exist_ok=True)
        new_index = {
            "version": int(index.get("version") or 1),
            "active": str(index.get("active") or ""),
            "workspace_id": str(index.get("workspace_id") or ws_id),
            "chats": new_entries,
        }
        with open(new_index_path, "w", encoding="utf-8") as f:
            json.dump(new_index, f, ensure_ascii=False, indent=2)
            f.write("\n")
        # Rename the old index so a re-run of this script skips the workspace.
        try:
            os.replace(str(old_index), str(old_index.with_name("chats.json.migrated")))
        except OSError:
            pass
    return stats


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dry-run", action="store_true", help="print what would be moved without touching files")
    parser.add_argument("--force", action="store_true", help="re-migrate even when the new index already exists")
    parser.add_argument(
        "--config-dir",
        default="",
        help="global config dir (default: $CODEWOOD_HOME or ~/.config/codewood)",
    )
    args = parser.parse_args()

    from cli.config.app_info import get_app_config_dirname, get_app_global_config_dir

    config_dir = Path(args.config_dir).expanduser() if args.config_dir else get_app_global_config_dir()
    new_chats_root = config_dir / "chats"
    registry_path = config_dir / "workspaces.json"
    registry = _load_json(registry_path) if registry_path.is_file() else None

    workspaces: Dict[str, Dict[str, Any]] = {}
    if registry:
        raw = registry.get("workspaces")
        if isinstance(raw, dict):
            workspaces = {k: v for k, v in raw.items() if isinstance(v, dict)}
    if not workspaces:
        workspaces["default"] = {"id": "default", "kind": "default"}

    print(f"Global config dir : {config_dir}")
    print(f"New chats root    : {new_chats_root}")
    print(f"Mode              : {'dry-run' if args.dry_run else 'migrate'}")
    print()

    total_moved = 0
    for ws_id, entry in workspaces.items():
        ws_id = str(entry.get("id") or ws_id)
        kind = str(entry.get("kind") or "").lower()
        root = str(entry.get("root") or "").strip()
        if kind == "default" or ws_id == "default":
            old_chats_dir = config_dir / "workspace" / "chats"
        elif root:
            old_chats_dir = Path(root).expanduser() / get_app_config_dirname() / "chats"
        else:
            print(f"[{ws_id}] skipped: no root recorded")
            continue
        stats = migrate_workspace(
            ws_id, old_chats_dir, new_chats_root, args.dry_run, args.force
        )
        if stats["status"] == "ok":
            print(
                f"[{ws_id}] migrated: {stats['moved']} chats, "
                f"{stats['data_dirs']} data dirs, {stats['skipped']} skipped "
                f"-> {new_chats_root / f'{ws_id}.json'}"
            )
            total_moved += stats["moved"]
        elif stats["status"] == "already-migrated":
            print(f"[{ws_id}] already migrated ({new_chats_root / f'{ws_id}.json'} exists)")
        elif stats["status"] == "no-op":
            print(f"[{ws_id}] no old chat index at {old_chats_dir / 'chats.json'}")
        else:
            print(f"[{ws_id}] ERROR: {stats['error']}")

    print()
    if args.dry_run:
        print(f"Dry run complete: {total_moved} chats would be migrated.")
    else:
        print(f"Migration complete: {total_moved} chats migrated.")
        print(f"Old per-workspace chats dirs were left in place "
              f"(index renamed to chats.json.migrated); you may delete them manually.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
