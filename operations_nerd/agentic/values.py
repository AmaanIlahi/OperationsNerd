"""
Record value checking, shared by the record endpoints (writes) and the
impact check (would existing values survive a type change?).

coerce_value() is deliberately lenient about representation ("42" is a fine
number, 42 is a fine text value) and strict about meaning (a select value
must be one of the options, a date must be a real date).
"""

import math
import re
from datetime import date

_EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
_PHONE_RE = re.compile(r"^[+0-9()\-.\s]{5,30}$")
_DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")

MAX_TEXT = 500
MAX_LONG_TEXT = 20000


def is_empty(value) -> bool:
    return value is None or value == "" or value == []


def coerce_value(ftype: str, options: list[str] | None, value) -> tuple[bool, object, str | None]:
    """Returns (ok, coerced_value, problem). Empty values are always ok and
    coerce to None (meaning: no value)."""
    if is_empty(value):
        return True, None, None

    if ftype in ("text", "long_text"):
        if isinstance(value, bool):
            text = "true" if value else "false"
        elif isinstance(value, (int, float)):
            text = str(value)
        elif isinstance(value, list) and all(isinstance(v, str) for v in value):
            text = ", ".join(value)
        elif isinstance(value, str):
            text = value
        else:
            return False, None, "must be text"
        limit = MAX_TEXT if ftype == "text" else MAX_LONG_TEXT
        if len(text) > limit:
            return False, None, f"must be at most {limit} characters"
        return True, text, None

    if ftype == "number":
        if isinstance(value, bool):
            return False, None, "must be a number"
        if isinstance(value, (int, float)):
            number = value
        elif isinstance(value, str):
            try:
                number = float(value.strip().replace(",", ""))
            except ValueError:
                return False, None, "must be a number"
            if number.is_integer() and "." not in value and "e" not in value.lower():
                number = int(number)
        else:
            return False, None, "must be a number"
        if isinstance(number, float) and not math.isfinite(number):
            return False, None, "must be a finite number"
        return True, number, None

    if ftype == "date":
        if not isinstance(value, str) or not _DATE_RE.match(value.strip()):
            return False, None, "must be a date in YYYY-MM-DD format"
        try:
            date.fromisoformat(value.strip())
        except ValueError:
            return False, None, "must be a real calendar date"
        return True, value.strip(), None

    if ftype == "boolean":
        if isinstance(value, bool):
            return True, value, None
        if isinstance(value, str) and value.strip().lower() in ("true", "false", "yes", "no"):
            return True, value.strip().lower() in ("true", "yes"), None
        return False, None, "must be true or false"

    if ftype == "select":
        if not isinstance(value, str):
            return False, None, "must be one of the listed options"
        if value not in (options or []):
            return False, None, f"'{value}' is not one of the options"
        return True, value, None

    if ftype == "multiselect":
        items = [value] if isinstance(value, str) else value
        if not isinstance(items, list) or not all(isinstance(v, str) for v in items):
            return False, None, "must be a list of options"
        bad = [v for v in items if v not in (options or [])]
        if bad:
            return False, None, f"'{bad[0]}' is not one of the options"
        return True, list(dict.fromkeys(items)), None

    if ftype == "email":
        if not isinstance(value, str) or len(value) > 254 or not _EMAIL_RE.match(value.strip()):
            return False, None, "must be a valid email address"
        return True, value.strip(), None

    if ftype == "phone":
        if (not isinstance(value, str) or not _PHONE_RE.match(value.strip())
                or sum(c.isdigit() for c in value) < 5):
            return False, None, "must be a valid phone number"
        return True, value.strip(), None

    return False, None, f"unsupported field type '{ftype}'"
