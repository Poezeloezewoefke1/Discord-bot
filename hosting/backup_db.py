"""Take a dated snapshot of the database and keep the last N.

On GitHub Actions the disk was wiped every few hours, so the only copy that
mattered was the one pushed to the bot-data branch. On a machine that keeps its
disk, the risk is the opposite one: the single file quietly goes bad, or
somebody runs the wrong command, and the newest copy is already broken.

Uses sqlite3's own backup API rather than copying the file, so a snapshot taken
while the bot is mid-write is a valid database rather than a torn one.

    python hosting/backup_db.py [--keep 7]
"""

from __future__ import annotations

import argparse
import os
import sqlite3
import sys
from datetime import datetime, timezone
from pathlib import Path

DEFAULT_KEEP = 7


def snapshot(db_path: Path, into: Path) -> Path:
    into.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    target = into / f"buildboard-{stamp}.db"

    source = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    try:
        destination = sqlite3.connect(target)
        try:
            source.backup(destination)
        finally:
            destination.close()
    finally:
        source.close()
    return target


def prune(into: Path, keep: int) -> list[Path]:
    snapshots = sorted(into.glob("buildboard-*.db"))
    dropped = snapshots[:-keep] if keep > 0 else []
    for old in dropped:
        old.unlink()
    return dropped


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--keep", type=int, default=DEFAULT_KEEP)
    parser.add_argument("--db", default=os.getenv("DB_PATH") or "buildboard.db")
    parser.add_argument("--into", default=os.getenv("BACKUP_DIR") or "backups")
    args = parser.parse_args()

    db_path = Path(args.db)
    if not db_path.exists():
        print(f"[backup] no database at {db_path} — nothing to do")
        return 0

    target = snapshot(db_path, Path(args.into))
    size = target.stat().st_size

    # A snapshot nobody checked is a snapshot you find out about too late.
    check = sqlite3.connect(target)
    try:
        ok = check.execute("PRAGMA integrity_check").fetchone()[0]
    finally:
        check.close()
    if ok != "ok":
        target.unlink()
        print(f"[backup] snapshot failed its integrity check ({ok}) — deleted", file=sys.stderr)
        return 1

    dropped = prune(Path(args.into), args.keep)
    print(f"[backup] {target.name} ({size:,} bytes), {len(dropped)} old one(s) removed")
    return 0


if __name__ == "__main__":
    sys.exit(main())
