"""Run pinned Logistics migrations on a host-selected private business database.

No connection URL, global environment, seed data or deployment is consulted.
The original three migrations stay byte-for-byte immutable. The standalone
record metadata they create is transitional historical schema, not a public
record/onboarding API. A later forward migration may retire those dependencies.
"""
from __future__ import annotations

import hashlib
import json
from pathlib import Path

from .db import prepare_connection, verify_business
from .errors import ConfigurationError

MIGRATIONS = Path(__file__).with_name('migrations')
SOURCE_CHECKSUMS = json.loads((MIGRATIONS / 'source-checksums.json').read_text())


def initialize_database(conn, business_key: str, *, migrations: Path = MIGRATIONS) -> list[str]:
    """Migrate once atomically, verifying checksum history and business identity.

    The supplied idle autocommit connection remains owned by the caller. This
    function must not run inside a host enable/approval transaction. A host may
    mark enablement ready only after this migration and its binding work succeed.
    ``migrations`` is an explicit filesystem source, chiefly for verification.
    """
    if not isinstance(business_key, str) or not business_key.strip():
        raise ConfigurationError('A business identity is required')
    files = sorted(Path(migrations).glob('*.sql'))
    sources = {path.name: path.read_bytes() for path in files}
    if not set(SOURCE_CHECKSUMS).issubset(sources):
        raise ConfigurationError('A pinned Logistics migration is unavailable')
    checksums = {name: hashlib.sha256(contents).hexdigest()
                 for name, contents in sources.items()}
    if any(checksums[name] != checksum for name, checksum in SOURCE_CHECKSUMS.items()):
        raise ConfigurationError('A pinned Logistics migration checksum has changed')
    prepare_connection(conn)
    applied = []
    with conn.transaction():
        conn.execute('SELECT pg_advisory_xact_lock(1768842362,1)')
        if conn.execute("SELECT to_regclass('logistics.database_identity') AS name").fetchone()['name'] is not None:
            verify_business(conn, business_key)
        conn.execute('CREATE SCHEMA IF NOT EXISTS logistics')
        conn.execute('''CREATE TABLE IF NOT EXISTS logistics.schema_migrations(
            version text PRIMARY KEY, checksum text NOT NULL,
            applied_at timestamptz NOT NULL DEFAULT now())''')
        history = {row['version']: row['checksum'] for row in conn.execute(
            'SELECT version,checksum FROM logistics.schema_migrations').fetchall()}
        if set(history) - sources.keys():
            raise ConfigurationError('Database migration history contains unavailable files')
        for path in files:
            name = path.name
            if name in history:
                if history[name] != checksums[name]:
                    raise ConfigurationError('Applied migration checksum does not match its source')
                continue
            conn.execute(sources[name].decode('utf-8'), prepare=False)
            conn.execute('INSERT INTO logistics.schema_migrations(version,checksum) VALUES(%s,%s)',
                         (name, checksums[name]))
            applied.append(name)
        identity = conn.execute(
            'SELECT business_key FROM logistics.database_identity WHERE singleton=true').fetchone()
        if identity is None:
            conn.execute('INSERT INTO logistics.database_identity(singleton,business_key) VALUES(true,%s)',
                         (business_key,))
        verify_business(conn, business_key)
    return applied


apply_migrations = initialize_database
