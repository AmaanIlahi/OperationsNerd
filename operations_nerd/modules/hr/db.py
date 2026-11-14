"""HR-owned transaction and cross-command replay boundary; no other Nerd imports."""
from datetime import date, datetime, time

import psycopg
from psycopg.pq import TransactionStatus
from psycopg.rows import dict_row
from psycopg.types.json import Jsonb

from .errors import AuthorizationError, Conflict, IdempotencyConflict, ValidationError


def json_value(value):
    if value is None or type(value) in (str, int, bool):
        return value
    if isinstance(value, (date, datetime, time)):
        return value.isoformat()
    if isinstance(value, (list, tuple)):
        return [json_value(item) for item in value]
    if isinstance(value, dict) and all(isinstance(key, str) for key in value):
        return {key: json_value(item) for key, item in value.items()}
    raise ValidationError('HR request/results must use exact JSON values, dates or integer half-days')


def prepare_connection(conn, business_key=None):
    if not conn.autocommit or conn.info.transaction_status != TransactionStatus.IDLE:
        raise ValidationError('HR commands require an idle autocommit connection')
    conn.row_factory = dict_row
    conn.execute('SET search_path TO hr, core, public')
    conn.execute("SET TIME ZONE 'UTC'")
    conn.execute("SET statement_timeout TO '30s'")
    conn.execute("SET lock_timeout TO '10s'")
    if business_key is not None:
        verify_business(conn, business_key)
    return conn


def verify_business(conn, business_key):
    row = conn.execute('SELECT business_key FROM hr.database_identity WHERE singleton').fetchone()
    if row is None or row['business_key'] != business_key:
        raise AuthorizationError('HR database identity does not match the selected business')


def run_command(conn, operation, key, payload, work):
    if not isinstance(key, str) or not key or len(key) > 200 or any(char.isspace() for char in key):
        raise ValidationError('Use a nonblank request key of at most 200 characters without whitespace')
    if not conn.autocommit or conn.info.transaction_status != TransactionStatus.IDLE:
        raise ValidationError('HR public commands cannot join another open transaction')
    identity = json_value(payload)
    try:
        with conn.transaction():
            conn.execute('SELECT pg_advisory_xact_lock(684728314,hashtext(%s))', (key,))
            old = conn.execute('SELECT operation,payload,result FROM hr.command_requests WHERE idempotency_key=%s', (key,)).fetchone()
            if old:
                if old['operation'] != operation or old['payload'] != identity:
                    raise IdempotencyConflict('This HR request key already belongs to another command or payload')
                return old['result']
            conn.execute('SELECT hr.lock_resources()')
            result = json_value(work(conn))
            conn.execute('INSERT INTO hr.command_requests(idempotency_key,operation,payload,result) VALUES(%s,%s,%s,%s)',
                         (key, operation, Jsonb(identity), Jsonb(result)))
            return result
    except psycopg.errors.UniqueViolation:
        raise Conflict('This HR identity or operation step already exists') from None
    except (psycopg.errors.CheckViolation, psycopg.errors.ForeignKeyViolation,
            psycopg.errors.NotNullViolation, psycopg.errors.NumericValueOutOfRange):
        raise Conflict('HR rejected a state, date, leave balance, overlap or reference rule') from None
    except (psycopg.errors.DeadlockDetected, psycopg.errors.SerializationFailure, psycopg.errors.LockNotAvailable):
        raise Conflict('A concurrent HR operation changed this resource; retry with the same key') from None
