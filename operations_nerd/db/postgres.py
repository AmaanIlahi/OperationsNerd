"""PostgreSQL implementation of the small SQLite interface used by core.

The adapter handles the existing core's qmark parameters, row access,
lastrowid, JSON aggregation and timestamp expressions. It is not a general
SQLite emulator. Domain modules should use a native psycopg connection.

Core writes deliberately retain SQLite's coarse serialization: every mutation
and BEGIN IMMEDIATE obtains the same database-local advisory transaction lock.
That keeps one-to-one checks and their writes atomic while the generic EAV
schema has no relational uniqueness constraint for these links. PostgreSQL's
default READ COMMITTED isolation is required so a waiting writer sees the
previous writer's committed result. Independent business databases do not
share this lock. A future finer-grained strategy needs its own concurrency
proof; replacing BEGIN IMMEDIATE with a plain BEGIN is not sufficient.
"""

import hashlib
import os
import re
import sqlite3
from pathlib import Path


CORE_WRITE_LOCK = 684728314502
SCHEMA_FILE = Path(__file__).with_name("schema_postgres.sql")
SCHEMA_VERSION = "001_core"
_IDENTITY_TABLES = frozenset({
    "businesses", "contacts", "follow_ups", "events", "drafted_actions",
    "approval_policies", "entities", "accounts", "sessions", "spec_versions", "proposals",
})
_INSERT = re.compile(r"^\s*INSERT\s+INTO\s+(?:(?:\"?core\"?)\.)?\"?([a-z_][a-z0-9_]*)\"?", re.I)
_MUTATION = re.compile(r"^\s*(?:INSERT|UPDATE|DELETE|REPLACE|CREATE|ALTER|DROP|TRUNCATE)\b", re.I)


def _driver():
    try:
        import psycopg
    except ImportError as exc:
        raise RuntimeError("PostgreSQL requires psycopg[binary]; install the project's requirements") from exc
    return psycopg


def raw_connection(url: str, *, autocommit: bool = True):
    """Return native psycopg, with core's namespace explicit and UTC timestamps.

    Modules may SET LOCAL search_path for their transaction. They should
    qualify module tables and must not inherit an ambient public schema.
    """
    psycopg = _driver()
    conn = psycopg.connect(url, autocommit=True)
    try:
        conn.execute("SET search_path TO core, public")
        conn.execute("SET TIME ZONE 'UTC'")
        conn.execute("SET default_transaction_isolation TO 'read committed'")
        conn.autocommit = autocommit
        return conn
    except Exception:
        conn.close()
        raise


def _parameters(sql: str, has_parameters: bool) -> str:
    """Translate placeholders outside strings, quoted names and SQL comments.

    Dollar-quoted bodies are preserved too, so migration SQL is safe to pass
    through this function. Literal percent signs are escaped only when
    psycopg parameter binding is active.
    """
    result, index, length = [], 0, len(sql)
    while index < length:
        char = sql[index]
        end = None
        if char in ("'", '"'):
            quote = char
            end = index + 1
            while end < length:
                if sql[end] == quote:
                    if end + 1 < length and sql[end + 1] == quote:
                        end += 2
                        continue
                    end += 1
                    break
                end += 1
        elif sql.startswith("--", index):
            found = sql.find("\n", index + 2)
            end = length if found < 0 else found
        elif sql.startswith("/*", index):
            end, depth = index + 2, 1
            while end < length and depth:
                if sql.startswith("/*", end):
                    depth += 1
                    end += 2
                elif sql.startswith("*/", end):
                    depth -= 1
                    end += 2
                else:
                    end += 1
        elif char == "$":
            match = re.match(r"\$(?:[A-Za-z_][A-Za-z_0-9]*)?\$", sql[index:])
            if match:
                delimiter = match.group()
                found = sql.find(delimiter, index + len(delimiter))
                end = length if found < 0 else found + len(delimiter)
        if end is not None:
            literal = sql[index:end]
            result.append(literal.replace("%", "%%") if has_parameters else literal)
            index = end
            continue
        if char == "?":
            result.append("%s")
        elif char == "%" and has_parameters:
            result.append("%%")
        else:
            result.append(char)
        index += 1
    return "".join(result)


def translate(sql: str, *, has_parameters: bool = False) -> tuple[str, bool]:
    """Return (PostgreSQL statement, auto-returned identity flag)."""
    sql = sql.strip().rstrip(";")
    ignored_insert = bool(re.match(r"INSERT\s+OR\s+IGNORE\s+INTO\b", sql, re.I))
    if ignored_insert:
        sql = re.sub(r"^INSERT\s+OR\s+IGNORE\s+INTO\b", "INSERT INTO", sql, flags=re.I)
    sql = re.sub(r"datetime\(\s*'now'\s*\)",
                 "to_char(timezone('UTC', statement_timestamp()), 'YYYY-MM-DD HH24:MI:SS')", sql, flags=re.I)
    sql = re.sub(r"json_group_object\(\s*attribute_name\s*,\s*value\s*\)",
                 "(json_object_agg(attribute_name, value)::text)", sql, flags=re.I)
    sql = re.sub(r"\bMAX\(\s*turns\s*-\s*1\s*,\s*0\s*\)", "GREATEST(turns - 1, 0)", sql, flags=re.I)
    if ignored_insert and not re.search(r"\bON\s+CONFLICT\b", sql, re.I):
        sql += " ON CONFLICT DO NOTHING"
    match = _INSERT.match(sql)
    auto_identity = bool(match and match.group(1).lower() in _IDENTITY_TABLES
                         and not re.search(r"\bRETURNING\b", sql, re.I))
    if auto_identity:
        sql += " RETURNING id"
    return _parameters(sql, has_parameters), auto_identity


class Row(dict):
    """sqlite3.Row-compatible named and positional indexing."""
    def __getitem__(self, key):
        if isinstance(key, int):
            return tuple(self.values())[key]
        return super().__getitem__(key)


class Cursor:
    def __init__(self, cursor, lastrowid=None):
        self.raw = cursor
        self.lastrowid = lastrowid

    @property
    def rowcount(self):
        return self.raw.rowcount

    @property
    def description(self):
        return self.raw.description

    def fetchone(self):
        row = self.raw.fetchone()
        return Row(row) if row is not None else None

    def fetchall(self):
        return [Row(row) for row in self.raw.fetchall()]

    def fetchmany(self, size=1):
        return [Row(row) for row in self.raw.fetchmany(size)]

    def __iter__(self):
        return (Row(row) for row in self.raw)


class Connection:
    dialect = "postgresql"

    def __init__(self, url: str):
        from psycopg.rows import dict_row
        self.raw = raw_connection(url, autocommit=False)
        self.raw.row_factory = dict_row
        self._write_locked = False

    @classmethod
    def from_native(cls, raw):
        """Borrow the host's native transaction without opening another one.

        The caller owns commit/rollback/close and must keep this wrapper inside
        that unit of work. SQL remains core-schema qualified by its caller when
        a domain connection uses another search_path.
        """
        from psycopg.rows import dict_row
        instance = cls.__new__(cls)
        instance.raw = raw
        instance.raw.row_factory = dict_row
        instance._write_locked = False
        return instance

    @property
    def in_transaction(self):
        from psycopg.pq import TransactionStatus
        return self.raw.info.transaction_status != TransactionStatus.IDLE

    def _begin_write(self):
        if not self._write_locked:
            self.raw.execute("SELECT pg_advisory_xact_lock(%s)", (CORE_WRITE_LOCK,))
            self._write_locked = True

    def execute(self, sql: str, parameters=()):
        if sql.strip().upper().rstrip(";") == "BEGIN IMMEDIATE":
            self._begin_write()
            return Cursor(self.raw.execute("SELECT 1 WHERE false"))
        statement, auto_identity = translate(sql, has_parameters=bool(parameters))
        if _MUTATION.match(statement):
            self._begin_write()
        try:
            cur = self.raw.execute(statement, parameters if parameters else None)
            inserted = cur.fetchone() if auto_identity else None
            return Cursor(cur, inserted["id"] if inserted else None)
        except _driver().IntegrityError as exc:
            # Existing core catches sqlite3.IntegrityError for duplicate email.
            # The outer unit of work still rolls back the aborted PG transaction.
            raise sqlite3.IntegrityError(str(exc)) from exc

    def executemany(self, sql: str, parameters):
        self._begin_write()
        statement, _ = translate(sql, has_parameters=True)
        cur = self.raw.cursor()
        cur.executemany(statement, parameters)
        return Cursor(cur)

    def executescript(self, sql: str):
        self._begin_write()
        return Cursor(self.raw.execute(sql))

    def commit(self):
        self.raw.commit()
        self._write_locked = False

    def rollback(self):
        self.raw.rollback()
        self._write_locked = False

    def close(self):
        self.raw.close()


def initialize(url: str, *, reset: bool = False):
    """Apply the immutable initial core migration to the explicitly selected DB.

    This never creates/deletes a database and never resets a normal business
    database. The host owns provisioning and routing metadata. Reset is only
    available for explicitly opted-in disposable test databases.
    """
    with raw_connection(url, autocommit=False) as conn:
        conn.execute("SELECT pg_advisory_xact_lock(%s)", (CORE_WRITE_LOCK,))
        if reset:
            name = conn.info.dbname
            disposable = name.startswith(("opsnerd_test_", "operationsnerd_test_", "operations_test_"))
            if (os.environ.get("OPERATIONS_ALLOW_DATABASE_RESET") != "1" or not disposable
                    or os.environ.get("APP_ENV", "").lower() == "production"):
                raise ValueError("PostgreSQL reset requires an opted-in disposable test database")
            conn.execute("DROP SCHEMA IF EXISTS core CASCADE")
        conn.execute("CREATE SCHEMA IF NOT EXISTS core")
        conn.execute("""CREATE TABLE IF NOT EXISTS core.schema_migrations (
            version TEXT PRIMARY KEY, checksum TEXT NOT NULL,
            applied_at TIMESTAMPTZ NOT NULL DEFAULT now())""")
        sql = SCHEMA_FILE.read_text(encoding="utf-8")
        checksum = hashlib.sha256(sql.encode("utf-8")).hexdigest()
        previous = conn.execute("SELECT checksum FROM core.schema_migrations WHERE version = %s",
                                (SCHEMA_VERSION,)).fetchone()
        if previous:
            if previous[0] != checksum:
                raise RuntimeError("Applied core migration checksum differs; add a forward migration")
            return
        conn.execute(sql)
        conn.execute("INSERT INTO core.schema_migrations(version, checksum) VALUES (%s, %s)",
                     (SCHEMA_VERSION, checksum))
