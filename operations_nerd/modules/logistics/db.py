"""One transaction and one request registry for purchasing and stock."""
from __future__ import annotations

import dataclasses
from datetime import date, datetime
from decimal import Decimal
import json
from typing import Any, Callable

import psycopg
from psycopg.pq import TransactionStatus
from psycopg.rows import dict_row
from psycopg.types.json import Jsonb

from .errors import (AuthorizationError, ConfigurationError, Conflict,
                     IdempotencyConflict, ValidationError)

REQUEST_LOCK_NAMESPACE = 1768842361


def json_value(value: Any) -> Any:
    """Return stable JSON data without losing integer money/quantity values."""
    if dataclasses.is_dataclass(value) and not isinstance(value, type):
        value = dataclasses.asdict(value)
    if value is None or type(value) in (str, int, bool):
        return value
    if isinstance(value, (date, datetime)):
        return value.isoformat()
    if isinstance(value, Decimal):
        return int(value) if value == value.to_integral_value() else str(value)
    if isinstance(value, dict):
        if any(type(k) is not str for k in value):
            raise ValidationError('JSON object keys must be strings')
        return {k: json_value(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [json_value(v) for v in value]
    raise ValidationError(f'Unsupported JSON result type: {type(value).__name__}')


def canonical_payload(value: Any) -> Any:
    """Versioned type-sensitive identity, including bool vs integer and dates."""
    if dataclasses.is_dataclass(value) and not isinstance(value, type):
        value = dataclasses.asdict(value)
    if value is None:
        return ['null']
    if type(value) is bool:
        return ['bool', value]
    if type(value) is int:
        return ['int', str(value)]
    if type(value) is str:
        return ['str', value]
    if type(value) is datetime:
        if value.utcoffset() is None:
            raise ValidationError('Request timestamps must include a timezone')
        return ['datetime', value.isoformat()]
    if type(value) is date:
        return ['date', value.isoformat()]
    if type(value) is Decimal:
        return ['decimal', str(value)]
    if type(value) in (tuple, list):
        return ['list', [canonical_payload(v) for v in value]]
    if type(value) is dict:
        if any(type(k) is not str for k in value):
            raise ValidationError('Request object keys must be strings')
        return ['dict', [[k, canonical_payload(value[k])] for k in sorted(value)]]
    # Floats are deliberately excluded: money/quantities have exact representations.
    raise ValidationError(f'Unsupported request value: {type(value).__name__}')


def verify_business(conn, business_key: str) -> None:
    if not isinstance(business_key, str) or not business_key.strip():
        raise ConfigurationError('A configured business identity is required')
    row = conn.execute('SELECT business_key FROM logistics.database_identity WHERE singleton=true').fetchone()
    if row is None or row['business_key'] != business_key:
        raise AuthorizationError('Database identity does not match the configured business')


def connect(dsn: str, business_key: str | None = None):
    if not dsn:
        raise ConfigurationError('A host-supplied business database connection is required')
    from psycopg.conninfo import conninfo_to_dict
    parts=conninfo_to_dict(dsn)
    security={'sslmode':'require'} if parts.get('host','').endswith(('.supabase.com','.supabase.co')) and 'sslmode' not in parts else {}
    conn = psycopg.connect(dsn, **security, autocommit=True, row_factory=dict_row,
                           connect_timeout=10, prepare_threshold=None)
    try:
        return prepare_connection(conn, business_key)
    except Exception:
        conn.close()
        raise


def prepare_connection(conn, business_key: str | None = None):
    """Configure a host-owned connection without reading global settings.

    Routing and authentication remain the host's responsibility. The assertion
    checks the selected private database; it does not authorize a request. Public
    commands own their transaction and cannot join a core approval transaction.
    """
    if not conn.autocommit or conn.info.transaction_status != TransactionStatus.IDLE:
        raise ConfigurationError('Logistics requires an idle autocommit connection')
    conn.row_factory = dict_row
    conn.execute('SET search_path TO logistics, public')
    conn.execute("SET statement_timeout TO '30s'")
    conn.execute("SET lock_timeout TO '10s'")
    if business_key is not None:
        verify_business(conn, business_key)
    return conn


@dataclasses.dataclass(frozen=True)
class Database:
    url: str = dataclasses.field(repr=False)
    business_key: str

    def connect(self):
        return connect(self.url, self.business_key)


def run_command(conn, operation: str, idempotency_key: str, payload: Any,
                work: Callable[[Any], dict]) -> dict:
    """Serialize key replay and commit all domain rows/result atomically.

    Internal helpers receive this connection; they cannot call another public
    command with its own commit. Failed requests reserve no completed key.
    """
    for label, value in [('operation', operation), ('idempotency_key', idempotency_key)]:
        if type(value) is not str or not value.strip() or len(value) > 256:
            raise ValidationError(f'{label} must contain 1 through 256 characters')
    if not conn.autocommit or conn.info.transaction_status != TransactionStatus.IDLE:
        raise ConfigurationError('Public commands require an idle autocommit connection')
    identity = {'version': 1, 'content': canonical_payload(payload)}
    try:
        with conn.transaction():
            # A hash collision only serializes unrelated requests; actual uniqueness
            # and replay equality use the complete key and content in PostgreSQL.
            conn.execute('SELECT pg_advisory_xact_lock(%s, hashtext(%s))',
                         (REQUEST_LOCK_NAMESPACE, idempotency_key))
            existing = conn.execute('SELECT operation,payload,result FROM command_requests WHERE idempotency_key=%s',
                                    (idempotency_key,)).fetchone()
            if existing:
                if existing['operation'] != operation or existing['payload'] != identity:
                    raise IdempotencyConflict('Request key was already used for another command or content')
                return existing['result']
            result = json_value(work(conn))
            if not isinstance(result, dict):
                raise ConfigurationError('A command must return a JSON object')
            conn.execute('INSERT INTO command_requests(idempotency_key,operation,payload,result) VALUES(%s,%s,%s,%s)',
                         (idempotency_key, operation, Jsonb(identity), Jsonb(result)))
            return result
    except psycopg.errors.UniqueViolation as exc:
        raise Conflict('A record with this identity already exists') from exc
    except (psycopg.errors.CheckViolation, psycopg.errors.ForeignKeyViolation,
            psycopg.errors.NotNullViolation, psycopg.errors.NumericValueOutOfRange) as exc:
        raise ValidationError('Command violates a database quantity, state, or reference rule') from exc
    except (psycopg.errors.DeadlockDetected, psycopg.errors.SerializationFailure,
            psycopg.errors.LockNotAvailable) as exc:
        raise Conflict('Concurrent update; retry the complete request with the same key') from exc
