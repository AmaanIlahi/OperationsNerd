"""Durable owner-triggered intake. No provider call during enqueue, no new sends."""
import hashlib
from db import db as d
from packs.loader import load_pack
from pipeline.service import process_event


class InboxConflict(ValueError):
    pass


def _receipt(conn, receipt_id, business_id):
    row = conn.execute("SELECT * FROM event_inbox WHERE id=? AND business_id=?",
                       (receipt_id, business_id)).fetchone()
    if row is None:
        raise LookupError("Inbox receipt not found")
    return dict(row)


def enqueue(conn, business_id, source, key, raw_content):
    if not d.get_business(conn, business_id):
        raise LookupError("Business not found")
    if not isinstance(key, str) or not key.strip() or len(key) > 200:
        raise ValueError("Idempotency key must be 1-200 characters")
    if not isinstance(source, str) or not source.strip() or len(source) > 40:
        raise ValueError("Source must be 1-40 characters")
    if not isinstance(raw_content, str) or not raw_content.strip() or len(raw_content) > 100000:
        raise ValueError("Content must be 1-100000 characters")
    # Reject unknown source before queuing a permanently unprocessable message.
    business = d.get_business(conn, business_id)
    pack = load_pack(business["industry_pack"])
    if source not in {schema.event_type for schema in pack.event_schemas}:
        raise ValueError("Source is not declared in this business's config pack")
    digest = hashlib.sha256(raw_content.encode()).hexdigest()
    conn.execute("INSERT OR IGNORE INTO event_inbox (business_id,source,idempotency_key,payload_hash,raw_content) VALUES (?,?,?,?,?)",
                 (business_id, source, key, digest, raw_content))
    row = dict(conn.execute("SELECT * FROM event_inbox WHERE business_id=? AND source=? AND idempotency_key=?",
                            (business_id, source, key)).fetchone())
    if row["payload_hash"] != digest:
        raise InboxConflict("Idempotency key was already used with different content")
    return row


def list_receipts(conn, business_id, status=None, limit=100):
    if status and status not in {"queued", "failed", "completed"}:
        raise ValueError("Unknown inbox status")
    limit = max(1, min(int(limit), 500))
    if status:
        rows = conn.execute("SELECT * FROM event_inbox WHERE business_id=? AND status=? ORDER BY id LIMIT ?",
                            (business_id, status, limit))
    else:
        rows = conn.execute("SELECT * FROM event_inbox WHERE business_id=? ORDER BY id DESC LIMIT ?",
                            (business_id, limit))
    return [dict(row) for row in rows]


def process_receipt(conn, business_id, receipt_id):
    """One atomic attempt. A completed receipt replays without LLM calls.

    SQLite write reservation serializes attempts, including from separate processes.
    This synchronous endpoint holds that lock through the model calls; it is not a
    high-throughput job queue. On failure, rollback all event/draft/approval writes
    to a savepoint, then persist only the failed receipt and attempt count.
    """
    if conn.in_transaction:
        raise RuntimeError("Process receipts on a fresh transaction")
    conn.execute("BEGIN IMMEDIATE")
    receipt = _receipt(conn, receipt_id, business_id)
    if receipt["status"] == "completed":
        return receipt
    conn.execute("SAVEPOINT inbox_attempt")
    try:
        business = d.get_business(conn, business_id)
        pack = load_pack(business["industry_pack"])
        event_id = d.create_event(conn, business_id, receipt["source"], receipt["raw_content"])
        action = process_event(conn, pack, event_id)
    except Exception as exc:
        conn.execute("ROLLBACK TO inbox_attempt")
        conn.execute("RELEASE inbox_attempt")
        # Don't persist provider messages/prompts/credentials in error records.
        conn.execute("UPDATE event_inbox SET status='failed',attempts=attempts+1,last_error=?,updated_at=datetime('now') WHERE id=?",
                     (type(exc).__name__, receipt_id))
    else:
        conn.execute("RELEASE inbox_attempt")
        conn.execute("UPDATE event_inbox SET status='completed',attempts=attempts+1,event_id=?,action_id=?,last_error=NULL,updated_at=datetime('now') WHERE id=?",
                     (event_id, action["id"], receipt_id))
    return _receipt(conn, receipt_id, business_id)
