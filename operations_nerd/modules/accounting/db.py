"""Module-owned command transactions; no dependency on another Nerd."""
import json
from datetime import date, datetime
from decimal import Decimal

import psycopg
from psycopg.types.json import Jsonb

from .errors import AccountingError, Conflict

LOCK_NAMESPACE = 1768842370


def json_value(value):
    if isinstance(value, (datetime, date)):
        return value.isoformat()
    if isinstance(value, Decimal):
        if value != value.to_integral_value():
            raise AccountingError("A monetary result was not an exact integer")
        return int(value)
    if isinstance(value, dict):
        return {key: json_value(item) for key, item in value.items()}
    if isinstance(value, (tuple, list)):
        return [json_value(item) for item in value]
    if value is None or type(value) in (str, bool, int):
        return value
    raise AccountingError("Unsupported accounting value")


def run_command(conn, operation, key, payload, work):
    if not isinstance(key, str) or not key.strip() or len(key) > 200:
        raise AccountingError("idempotency_key requires 1 to 200 nonblank characters")
    if not conn.autocommit or conn.info.transaction_status != psycopg.pq.TransactionStatus.IDLE:
        raise AccountingError("Accounting commands need an idle autocommit connection")
    canonical = json_value(payload)
    try:
        with conn.transaction():
            # One module lock covers command replay and the full financial
            # timeline. Database guards take the same lock for raw SQL writes.
            conn.execute("SELECT pg_advisory_xact_lock(%s,0)", (LOCK_NAMESPACE,))
            previous = conn.execute("SELECT operation,payload,result FROM accounting.command_requests WHERE idempotency_key=%s", (key,)).fetchone()
            if previous:
                if previous["operation"] != operation or previous["payload"] != canonical:
                    raise Conflict("Request key already belongs to another accounting operation or content")
                return previous["result"]
            result = json_value(work(conn))
            conn.execute("""INSERT INTO accounting.command_requests(idempotency_key,operation,payload,result)
                VALUES(%s,%s,%s,%s)""", (key, operation, Jsonb(canonical), Jsonb(result)))
            return result
    except (psycopg.errors.UniqueViolation, psycopg.errors.ExclusionViolation):
        raise Conflict("This accounting identity or posting already exists") from None
    except (psycopg.errors.CheckViolation, psycopg.errors.ForeignKeyViolation,
            psycopg.errors.NotNullViolation, psycopg.errors.NumericValueOutOfRange) as error:
        message = error.diag.message_primary or "Accounting quantity, balance, date or reference rule was violated"
        raise AccountingError(message[:300]) from None
    except (psycopg.errors.DeadlockDetected, psycopg.errors.SerializationFailure, psycopg.errors.LockNotAvailable):
        raise Conflict("Concurrent accounting update; retry the complete request with the same key") from None
