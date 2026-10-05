"""
Audit log, analytics and review tools. Builds on the existing tables; the core
pipeline and approval logic stay as they were, they only call log() when
something worth recording happens.
"""

import csv
import io
import json

from db import db as d
from approval.service import ApprovalError, _simulated_send


def log(conn, kind: str, detail: str = "", business_id=None, event_id=None, action_id=None):
    conn.execute(
        "INSERT INTO audit_log (business_id, event_id, action_id, kind, detail) VALUES (?, ?, ?, ?, ?)",
        (business_id, event_id, action_id, kind, str(detail)[:2000]),
    )


def list_audit(conn, business_id: int, kind: str = None, limit: int = 100) -> list[dict]:
    limit = max(1, min(int(limit), 1000))
    if kind:
        rows = conn.execute("SELECT * FROM audit_log WHERE business_id = ? AND kind = ? ORDER BY id DESC LIMIT ?", (business_id, kind, limit)).fetchall()
    else:
        rows = conn.execute("SELECT * FROM audit_log WHERE business_id = ? ORDER BY id DESC LIMIT ?", (business_id, limit)).fetchall()
    return [dict(r) for r in rows]


def _bodies(action):
    return (action.get("payload") or {}).get("body", "")


def edit_and_approve(conn, action_id: int, new_body: str) -> dict:
    """Approve with the human's own wording. The original draft and the edit are both kept."""
    new_body = (new_body or "").strip()
    if not new_body:
        raise ApprovalError("Edited body cannot be empty")
    action = d.get_drafted_action(conn, action_id)
    if not action:
        raise ApprovalError(f"No drafted action with id {action_id}")
    if action["status"] != "pending_approval":
        raise ApprovalError(f"Drafted action {action_id} is not pending approval (current status: {action['status']})")
    original = _bodies(action)
    payload = dict(action["payload"] or {})
    payload["body"] = new_body
    conn.execute("UPDATE drafted_actions SET payload = ? WHERE id = ?", (json.dumps(payload), action_id))
    conn.execute("INSERT OR REPLACE INTO action_reviews (action_id, original_body, edited_body, reason) VALUES (?, ?, ?, NULL)", (action_id, original, new_body))
    d.set_action_status(conn, action_id, "approved", decided_at=True)
    action["payload"] = payload
    action["status"] = "approved"
    _simulated_send(action)
    d.set_action_status(conn, action_id, "sent")
    log(conn, "edited_approved", f"edited {len(original)} -> {len(new_body)} chars", action["business_id"], action["event_id"], action_id)
    return d.get_drafted_action(conn, action_id)


def reject_with_reason(conn, action_id: int, reason: str) -> dict:
    from approval.service import reject_action
    result = reject_action(conn, action_id)
    reason = (reason or "").strip()[:500]
    conn.execute("INSERT OR REPLACE INTO action_reviews (action_id, original_body, edited_body, reason) VALUES (?, ?, NULL, ?)", (action_id, _bodies(result), reason))
    return result


def bulk_decide(conn, ids: list[int], decision: str) -> dict:
    """Approve or reject many pending actions. One bad id does not stop the rest."""
    from approval.service import approve_and_send, reject_action
    if decision not in ("approve", "reject"):
        raise ApprovalError("decision must be 'approve' or 'reject'")
    done, failed = [], []
    for action_id in ids[:200]:
        try:
            (approve_and_send if decision == "approve" else reject_action)(conn, int(action_id))
            done.append(int(action_id))
        except ApprovalError as e:
            failed.append({"id": action_id, "error": str(e)})
        except (TypeError, ValueError):
            failed.append({"id": action_id, "error": "not a valid id"})
    return {"done": done, "failed": failed}


def overdue_pending(conn, business_id: int, minutes: int = 60) -> list[dict]:
    rows = conn.execute(
        "SELECT *, CAST((julianday('now') - julianday(created_at)) * 1440 AS INTEGER) AS waiting_minutes "
        "FROM drafted_actions WHERE business_id = ? AND status = 'pending_approval' "
        "AND (julianday('now') - julianday(created_at)) * 1440 >= ? ORDER BY created_at",
        (business_id, int(minutes)),
    ).fetchall()
    out = []
    for r in rows:
        item = dict(r)
        item["payload"] = json.loads(item["payload"])
        out.append(item)
    return out


def stats(conn, business_id: int) -> dict:
    rows = conn.execute("SELECT action_type, status, created_at, decided_at FROM drafted_actions WHERE business_id = ?", (business_id,)).fetchall()
    total = len(rows)
    by_status, by_type = {}, {}
    waits = []
    for r in rows:
        by_status[r["status"]] = by_status.get(r["status"], 0) + 1
        t = by_type.setdefault(r["action_type"], {"drafted": 0, "sent": 0, "rejected": 0})
        t["drafted"] += 1
        if r["status"] in ("sent", "approved", "auto_approved"):
            t["sent"] += 1
        if r["status"] == "rejected":
            t["rejected"] += 1
    for r in conn.execute("SELECT (julianday(decided_at) - julianday(created_at)) * 1440 AS m FROM drafted_actions WHERE business_id = ? AND decided_at IS NOT NULL", (business_id,)):
        waits.append(r["m"])
    human = by_status.get("sent", 0) + by_status.get("rejected", 0) - by_status.get("auto_approved", 0)
    edited = conn.execute("SELECT COUNT(*) c FROM action_reviews ar JOIN drafted_actions da ON da.id = ar.action_id WHERE da.business_id = ? AND ar.edited_body IS NOT NULL", (business_id,)).fetchone()["c"]
    decided = len(waits)
    errors = conn.execute("SELECT COUNT(*) c FROM audit_log WHERE business_id = ? AND kind = 'pipeline_error'", (business_id,)).fetchone()["c"]
    events = conn.execute("SELECT COUNT(*) c FROM events WHERE business_id = ?", (business_id,)).fetchone()["c"]
    rejected = by_status.get("rejected", 0)
    return {
        "business_id": business_id,
        "events": events,
        "drafted_actions": total,
        "by_status": by_status,
        "by_action_type": by_type,
        "pipeline_errors": errors,
        "human_decisions": decided,
        "rejection_rate": round(rejected / decided, 3) if decided else None,
        "edit_rate": round(edited / decided, 3) if decided else None,
        "avg_minutes_to_decision": round(sum(waits) / len(waits), 2) if waits else None,
        "auto_approved": by_status.get("auto_approved", 0),
    }


def export_actions_csv(conn, business_id: int) -> str:
    out = io.StringIO()
    w = csv.writer(out)
    w.writerow(["id", "event_id", "action_type", "status", "created_at", "decided_at", "body"])
    for a in d.list_drafted_actions(conn, business_id):
        body = _bodies(a)
        if body[:1] in "=+-@":
            body = "'" + body  # keep spreadsheets from running a draft as a formula
        w.writerow([a["id"], a["event_id"], a["action_type"], a["status"], a["created_at"], a["decided_at"] or "", body])
    return out.getvalue()
