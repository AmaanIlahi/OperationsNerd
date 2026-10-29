"""
Versioned schema migrations for a module's tables in a business's database.

The team decided that each business gets its own private database and that
database upgrades are versioned, with an automatic backup first. This is that
hook for the HR module, written so any module can use it:

- A module keeps its schema as numbered files, migrations/0001_name.sql,
  0002_name.sql, ... Numbers start at 1 and have no gaps. A file, once
  released, never changes: a change to the schema is a new file.
- module_migrations records, per module, every version applied to this
  database. Several modules can share one business database, each with its own
  versions, so nothing here uses PRAGMA user_version, which is one per file.
- migrate() applies the missing versions in order. Before it changes a database
  that already has tables, it copies the whole file to a backup next to it (or
  into backup_dir) with SQLite's online backup API.
- The whole upgrade holds one BEGIN IMMEDIATE write lock: the current version
  is read again under it, the backup is taken under it, and each version runs
  in its own savepoint together with its module_migrations row. So two
  processes migrating at once apply each version once and take one backup
  (the second finds nothing to do), and a version that fails rolls back
  completely and leaves the database at the version before it.

Standalone: stdlib sqlite3 only.
"""

import datetime
import re
import sqlite3
from pathlib import Path

_FILE = re.compile(r"^(\d{4})_([a-z0-9_]+)\.sql$")

_LOG = """CREATE TABLE IF NOT EXISTS module_migrations (
    module     TEXT    NOT NULL CHECK (module <> ''),
    version    INTEGER NOT NULL CHECK (version >= 1),
    name       TEXT    NOT NULL CHECK (name <> ''),
    applied_at TEXT    NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')),
    PRIMARY KEY (module, version)
)"""


class MigrationError(Exception):
    pass


def migrations(folder: Path) -> list[tuple[int, str, Path]]:
    """The migration files in folder, as (version, name, path), checked to run 1, 2, 3 ... with no gaps."""
    found = []
    for path in sorted(Path(folder).glob("*.sql")):
        m = _FILE.match(path.name)
        if not m:
            raise MigrationError(f"{path.name} is not named NNNN_name.sql")
        found.append((int(m.group(1)), m.group(2), path))
    for expected, (version, _, path) in enumerate(found, start=1):
        if version != expected:
            raise MigrationError(f"migration {path.name}: expected version {expected:04d}")
    return found


def current_version(conn: sqlite3.Connection, module: str) -> int:
    if not conn.execute("SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'module_migrations'").fetchone():
        return 0
    return conn.execute("SELECT COALESCE(MAX(version), 0) FROM module_migrations WHERE module = ?",
                        (module,)).fetchone()[0]


def migrate(conn: sqlite3.Connection, module: str, folder: Path, backup_dir: str | Path | None = None,
            target: int | None = None) -> dict:
    """Brings module's tables up to target (default: the latest file). Returns what it did.

    conn must be in autocommit mode (isolation_level=None), as the modules' connect() opens it.
    Result: {"module", "from_version", "to_version", "applied": [versions], "backup": path or None}.
    """
    files = migrations(folder)
    latest = len(files)
    target = latest if target is None else target
    if not 0 <= target <= latest:
        raise MigrationError(f"{module}: no migration {target}; the latest is {latest}")
    conn.execute(_LOG)
    start = current_version(conn, module)
    _check_start(module, start, latest, target)
    result = {"module": module, "from_version": start, "to_version": start, "applied": [], "backup": None}
    if start == target:
        return result
    conn.execute("BEGIN IMMEDIATE")
    try:
        # Read again under the lock: another process may have migrated while we waited for it.
        start = current_version(conn, module)
        _check_start(module, start, latest, target)
        result["from_version"] = result["to_version"] = start
        if start == target:
            conn.execute("COMMIT")
            return result
        result["backup"] = backup(conn, module, start, backup_dir)
        for version, name, path in files[start:target]:
            statements = _statements(path.read_text())
            conn.execute("SAVEPOINT module_migration")
            try:
                for sql in statements:
                    conn.execute(sql)
                conn.execute("INSERT INTO module_migrations (module, version, name) VALUES (?, ?, ?)",
                             (module, version, name))
                conn.execute("RELEASE module_migration")
            except BaseException:
                # Undo this version only; the versions before it are kept.
                conn.execute("ROLLBACK TO module_migration")
                conn.execute("RELEASE module_migration")
                conn.execute("COMMIT")
                raise
            result["applied"].append(version)
        conn.execute("COMMIT")
    except BaseException:
        if conn.in_transaction:
            conn.execute("ROLLBACK")
        raise
    result["to_version"] = current_version(conn, module)
    return result


def _check_start(module, start, latest, target):
    if start > latest:
        raise MigrationError(f"{module}: the database is at version {start}, newer than this code ({latest})")
    if start > target:
        raise MigrationError(f"{module}: the database is at version {start}; migrations only go forward")


def backup(conn: sqlite3.Connection, module: str, version: int, backup_dir: str | Path | None = None) -> str | None:
    """Copies the whole database file before an upgrade. None for an in-memory or brand-new empty database (or
    one whose only tables are module_migrations)."""
    main = conn.execute("PRAGMA database_list").fetchone()
    path = main[2] if main else ""
    if not path:
        return None
    user_tables = conn.execute("""SELECT COUNT(*) FROM sqlite_master
                                  WHERE type = 'table' AND name NOT LIKE 'sqlite_%'
                                    AND name <> 'module_migrations'""").fetchone()[0]
    if not user_tables:
        return None
    stamp = datetime.datetime.now(datetime.timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
    folder = Path(backup_dir) if backup_dir is not None else Path(path).parent
    folder.mkdir(parents=True, exist_ok=True)
    target = folder / f"{Path(path).name}.{module}-v{version:04d}.{stamp}.bak"
    # migrate() calls this holding the write lock, and a connection can't back itself up then (its backup
    # waits on its own lock), so a second, read-only connection copies the file. It sees exactly what the
    # lock holder sees, since nothing is written until the backup is done.
    source = conn if not conn.in_transaction else sqlite3.connect(Path(path).absolute().as_uri() + "?mode=ro",
                                                                  uri=True, timeout=30)
    dest = sqlite3.connect(str(target))
    try:
        source.backup(dest)
    finally:
        dest.close()
        if source is not conn:
            source.close()
    return str(target)


def _statements(script: str) -> list[str]:
    """Splits a migration file into statements; sqlite3.complete_statement keeps a trigger's BEGIN ... END whole."""
    out, buf = [], ""
    for line in script.splitlines(keepends=True):
        buf += line
        if sqlite3.complete_statement(buf):
            if buf.strip():
                out.append(buf.strip())
            buf = ""
    rest = "\n".join(l for l in buf.splitlines() if l.strip() and not l.strip().startswith("--"))
    if rest.strip():
        raise MigrationError(f"incomplete statement at the end of the migration: {rest.strip()[:80]!r}")
    return out
