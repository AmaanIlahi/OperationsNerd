"""
Human Approval.

Default is to ask a human before anything goes out. A business owner can
mark specific action_types as auto-approve via approval_policies, which
routes drafted actions straight through instead of waiting in the pending
list. Either path funnels through the same _simulated_send, so approved
and auto-approved actions can never diverge in what "sending" means.
"""

import logging

from db import db as d


_log = logging.getLogger(__name__)


class ApprovalError(Exception):
    pass


def _audit(conn, kind, action):
    # Recording a decision must never block the decision itself.
    try:
        conn.execute(
            "INSERT INTO audit_log (business_id, event_id, action_id, kind, detail) VALUES (?, ?, ?, ?, ?)",
            (action["business_id"], action["event_id"], action["id"], kind, action["action_type"]),
        )
    except Exception:
        _log.warning("audit log write failed for action %s", action.get("id"))


def _simulated_send(action: dict):
    """Dummy send for this POC. No real delivery, this is confirmed scope.
    Logs at INFO so sends are visible in uvicorn's structured logs."""
    _log.info(
        "simulated_send action_id=%s action_type=%s payload=%s",
        action["id"], action["action_type"], action["payload"],
    )


def approve_and_send(conn, action_id: int) -> dict:
    action = d.get_drafted_action(conn, action_id)
    if not action:
        raise ApprovalError(f"No drafted action with id {action_id}")
    if action["status"] != "pending_approval":
        raise ApprovalError(
            f"Drafted action {action_id} is not pending approval (current status: {action['status']})"
        )

    d.set_action_status(conn, action_id, "approved", decided_at=True)
    action["status"] = "approved"
    _simulated_send(action)
    d.set_action_status(conn, action_id, "sent")
    _audit(conn, "approved", action)

    return d.get_drafted_action(conn, action_id)


def reject_action(conn, action_id: int) -> dict:
    action = d.get_drafted_action(conn, action_id)
    if not action:
        raise ApprovalError(f"No drafted action with id {action_id}")
    if action["status"] != "pending_approval":
        raise ApprovalError(
            f"Drafted action {action_id} is not pending approval (current status: {action['status']})"
        )

    # Human decision: mark the moment a person said no.
    d.set_action_status(conn, action_id, "rejected", decided_at=True)
    _audit(conn, "rejected", action)

    return d.get_drafted_action(conn, action_id)


def maybe_auto_approve(conn, action_id: int) -> dict | None:
    action = d.get_drafted_action(conn, action_id)
    if not d.is_auto_approved(conn, action["business_id"], action["action_type"]):
        return None

    # Auto-approval is policy-driven, not a human decision: decided_at stays
    # NULL. created_at + the status value reconstruct the timeline without
    # conflating auto-approval with a human "yes".
    d.set_action_status(conn, action_id, "auto_approved", decided_at=False)
    action["status"] = "auto_approved"
    _simulated_send(action)
    d.set_action_status(conn, action_id, "sent", decided_at=False)
    _audit(conn, "auto_approved", action)

    return d.get_drafted_action(conn, action_id)
