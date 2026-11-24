"""Limits and response values shared by the Logistics CSV imports."""

import hashlib
import json


MAX_BYTES = 1024 * 1024
MAX_ROWS = 5_000
MAX_INTEGER = 2**63 - 1


def digest(value):
    return hashlib.sha256(
        json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode()
    ).hexdigest()


def row_error(row, field, message):
    return {"row": row, "field": field, "message": str(message)}
