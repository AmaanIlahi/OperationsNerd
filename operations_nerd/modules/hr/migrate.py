"""Checksum-verified native PostgreSQL schema installed by the trusted host."""
import hashlib
from pathlib import Path

from . import db
from .errors import Conflict, ValidationError

MIGRATIONS = Path(__file__).with_name('migrations')


def initialize_database(conn, business_key):
    if not isinstance(business_key, str) or not business_key.strip():
        raise ValidationError('A business identity is required')
    db.prepare_connection(conn)
    applied = []
    with conn.transaction():
        conn.execute('SELECT pg_advisory_xact_lock(684728314521)')
        conn.execute('CREATE SCHEMA IF NOT EXISTS hr')
        if conn.execute("SELECT to_regclass('hr.database_identity') AS name").fetchone()['name']:
            db.verify_business(conn, business_key)
        conn.execute('''CREATE TABLE IF NOT EXISTS hr.schema_migrations(
            version TEXT PRIMARY KEY, checksum TEXT NOT NULL, applied_at TIMESTAMPTZ NOT NULL DEFAULT now())''')
        history = {row['version']: row['checksum'] for row in conn.execute('SELECT * FROM hr.schema_migrations').fetchall()}
        files = sorted(MIGRATIONS.glob('*.sql'))
        if set(history) - {path.name for path in files}:
            raise Conflict('HR database has migrations unavailable in this code')
        for path in files:
            source = path.read_bytes()
            checksum = hashlib.sha256(source).hexdigest()
            if path.name in history:
                if history[path.name] != checksum:
                    raise Conflict('An applied HR migration changed; add a forward migration')
                continue
            conn.execute(source.decode(), prepare=False)
            conn.execute('INSERT INTO hr.schema_migrations(version,checksum) VALUES(%s,%s)', (path.name, checksum))
            applied.append(path.name)
        if conn.execute('SELECT 1 FROM hr.database_identity WHERE singleton').fetchone() is None:
            conn.execute('INSERT INTO hr.database_identity(singleton,business_key) VALUES(true,%s)', (business_key,))
        db.verify_business(conn, business_key)
    return applied
