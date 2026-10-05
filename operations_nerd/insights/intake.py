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
    reader = csv.DictReader(io.StringIO(text))
    if not reader.fieldnames or "name" not in [f.strip().lower() for f in reader.fieldnames]:
        raise ValueError("CSV needs a header row with at least a 'name' column")
    seen_email = {(c["email"] or "").lower() for c in d.list_contacts(conn, business_id) if c.get("email")}
    seen_phone = {"".join(ch for ch in (c["phone"] or "") if ch.isdigit()) for c in d.list_contacts(conn, business_id) if c.get("phone")}
    added, skipped, bad = 0, 0, []
    for n, row in enumerate(reader, start=2):
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
