"""Signed webhook intake and CSV contact import with dedupe."""
import csv
import hashlib
import hmac
import io
import os

from db import db as d


def verify_signature(raw: bytes, signature: str, secret: str | None = None) -> bool:
    """HMAC-SHA256 of the raw body, hex, optionally prefixed 'sha256='. Off (always False) when no secret is set."""
    secret = secret if secret is not None else os.environ.get("WEBHOOK_SECRET", "")
    if not secret or not signature:
        return False
    sig = signature.split("=", 1)[1] if signature.startswith("sha256=") else signature
    want = hmac.new(secret.encode(), raw, hashlib.sha256).hexdigest()
    return hmac.compare_digest(want, sig.strip())


def import_contacts_csv(conn, business_id: int, text: str) -> dict:
    """Columns: name, email, phone (header required). Skips rows whose email or phone already exists."""
    if not isinstance(text, str) or len(text) > 5000000:
        raise ValueError("CSV must be text of at most 5000000 characters")
    try:
        reader = csv.reader(io.StringIO(text), strict=True)
        raw_header = next(reader)
        headers = [f.strip().lower() for f in raw_header]
        if "name" not in headers or len(set(headers)) != len(headers) or any(not f for f in headers):
            raise ValueError("CSV needs unique non-empty headers including name")
        data = []
        for n, cells in enumerate(reader, start=2):
            if not cells or not any(c.strip() for c in cells):
                continue
            if len(cells) != len(headers):
                raise ValueError(f"CSV row {n} does not match its header")
            data.append((n, dict(zip(headers, cells))))
            if len(data) > 10000:
                raise ValueError("CSV accepts at most 10000 rows")
    except (csv.Error, StopIteration):
        raise ValueError("CSV is empty or malformed") from None
    seen_email = {(c["email"] or "").lower() for c in d.list_contacts(conn, business_id) if c.get("email")}
    seen_phone = {"".join(ch for ch in (c["phone"] or "") if ch.isdigit()) for c in d.list_contacts(conn, business_id) if c.get("phone")}
    added, skipped, bad = 0, 0, []
    for n, row in data:
        row = {(k or "").strip().lower(): (v or "").strip() for k, v in row.items()}
        name, email, phone = row.get("name"), row.get("email", "").lower(), row.get("phone", "")
        digits = "".join(ch for ch in phone if ch.isdigit())
        if not name:
            bad.append({"row": n, "error": "missing name"})
            continue
        if email and "@" not in email:
            bad.append({"row": n, "error": "bad email"})
            continue
        if (email and email in seen_email) or (digits and digits in seen_phone):
            skipped += 1
            continue
        d.create_contact(conn, business_id, name, email or None, phone or None)
        if email:
            seen_email.add(email)
        if digits:
            seen_phone.add(digits)
        added += 1
    return {"added": added, "duplicates_skipped": skipped, "rejected": bad}
