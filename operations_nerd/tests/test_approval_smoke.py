"""
Approval smoke test with a mocked LLM.

Verifies Human Approval end to end against the real database: manual
approve/reject of a pending action, the guard against acting twice on a
non-pending action, and the auto-approve path via approval_policies
routing straight to "sent" with no pending step.
"""

import sys
import os
import json
from unittest.mock import patch

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from db import db as d
from packs.loader import load_pack
from pipeline.service import process_event
from approval.service import approve_and_send, reject_action, ApprovalError


def fake_call_llm(prompt, response_schema=None):
    if response_schema is not None:
        return json.dumps({
            "intent": "availability_check",
            "listing_ref": "5th and Main",
            "urgency": "high",
            "action_type": "send_availability_reply",
        })
    else:
        return "Yes, it's still available! Want to schedule a showing?"


d.init_db(reset=True)
pack = load_pack("realestate")

# ---- (a) normal approve, then double-approve must fail ----

print("Testing approve_and_send on a pending action...")
with d.get_conn() as conn:
    business_id = d.create_business(
        conn, "Riverbend Realty", "realestate", "0.1.0",
        settings={"office_hours": "9-6", "default_follow_up_days": 2, "property_types": ["condo"]},
    )
    event_id = d.create_event(conn, business_id, source="inbound_email",
                                raw_content="Is the condo at 5th and Main still available?")

    with patch("pipeline.service.call_llm", side_effect=fake_call_llm):
        result = process_event(conn, pack, event_id)

    assert result["status"] == "pending_approval"
    action_id = result["id"]

    updated = approve_and_send(conn, action_id)
    assert updated["status"] == "sent"
    print("  Approved -> status:", updated["status"])

    try:
        approve_and_send(conn, action_id)
        print("  ERROR: should have raised ApprovalError on double-approve")
        assert False
    except ApprovalError as e:
        print("  Correctly raised on double-approve:", e)

print("Scenario (a) passed.\n")

# ---- (b) reject a pending action ----

print("Testing reject_action on a pending action...")
with d.get_conn() as conn:
    event_id = d.create_event(conn, business_id, source="inbound_email",
                                raw_content="Is the condo at 5th and Main still available?")

    with patch("pipeline.service.call_llm", side_effect=fake_call_llm):
        result = process_event(conn, pack, event_id)

    assert result["status"] == "pending_approval"
    action_id = result["id"]

    updated = reject_action(conn, action_id)
    assert updated["status"] == "rejected"
    print("  Rejected -> status:", updated["status"])

    try:
        reject_action(conn, action_id)
        print("  ERROR: should have raised ApprovalError on acting on a rejected action")
        assert False
    except ApprovalError as e:
        print("  Correctly raised on acting on non-pending action:", e)

print("Scenario (b) passed.\n")

# ---- (c) auto-approve policy routes straight to "sent" ----

print("Testing auto_approve policy...")
with d.get_conn() as conn:
    d.set_approval_policy(conn, business_id, "send_availability_reply", auto_approve=True)

    event_id = d.create_event(conn, business_id, source="inbound_email",
                                raw_content="Is the condo at 5th and Main still available?")

    with patch("pipeline.service.call_llm", side_effect=fake_call_llm):
        result = process_event(conn, pack, event_id)

    assert result["status"] == "sent"
    print("  process_event with auto_approve policy -> status:", result["status"])

    pending = d.list_pending_actions(conn, business_id)
    pending_ids = [p["id"] for p in pending]
    assert result["id"] not in pending_ids
    print("  Confirmed not in pending list:", pending_ids)

print("Scenario (c) passed.\n")

print("Approval smoke test passed.")
