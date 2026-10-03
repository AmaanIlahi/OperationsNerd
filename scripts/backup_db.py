"""
Back up the SQLite database to a timestamped file, safely while the app runs.

    python scripts/backup_db.py                       # DATABASE_PATH (or the default db) -> ./backups/
    python scripts/backup_db.py --db /data/operations_nerd.db --out-dir /data/backups

Uses SQLite's online backup API, which copies a consistent snapshot even
while the app is writing (plain file copy of a WAL database is NOT safe).
The copy is written under a temporary name, checked with PRAGMA
integrity_check, and only then renamed into place, so a half-written or
corrupt backup never looks like a good one.
"""

import argparse
import os
import sqlite3
import sys
from datetime import datetime, timezone

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "operations_nerd"))


def default_db_path() -> str:
    from db import db as d
    return d.DB_PATH


def backup(db_path: str, out_dir: str) -> str:
    """Returns the path of the new backup. Raises if the source is missing or
    the copy fails its integrity check."""
    if not os.path.isfile(db_path):
        raise FileNotFoundError(f"No database at {db_path}")
    os.makedirs(out_dir, exist_ok=True)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
    final = os.path.join(out_dir, f"operations_nerd-{stamp}.db")
    n = 1
    while os.path.exists(final):                 # two backups in the same second
        final = os.path.join(out_dir, f"operations_nerd-{stamp}-{n}.db")
        n += 1
    partial = final + ".partial"

    source = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True, timeout=30)
    try:
        target = sqlite3.connect(partial)
        try:
            source.backup(target)
            result = target.execute("PRAGMA integrity_check").fetchone()[0]
        finally:
            target.close()
    except Exception:
        _remove(partial)
        raise
    finally:
        source.close()

    if result != "ok":
        _remove(partial)
        raise RuntimeError(f"Backup failed its integrity check: {result}")
    os.replace(partial, final)
    return final


def _remove(path: str):
    for suffix in ("", "-wal", "-shm"):
        try:
            os.remove(path + suffix)
        except FileNotFoundError:
            pass


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--db", default=None, help="database file (default: DATABASE_PATH or the built-in default)")
    parser.add_argument("--out-dir", default="backups", help="directory for backups (default: ./backups)")
    args = parser.parse_args()
    db_path = args.db or os.environ.get("DATABASE_PATH") or default_db_path()
    try:
        path = backup(db_path, args.out_dir)
    except Exception as e:
        print(f"Backup failed: {e}", file=sys.stderr)
        return 1
    print(f"Backup written: {path} ({os.path.getsize(path)} bytes)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
