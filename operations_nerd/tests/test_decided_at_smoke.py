"""
Smoke test for the decided_at semantic fix.

Before this fix, set_action_status always set decided_at = datetime('now'),
including for the auto-approve path where no human decision happened --
conflating policy-driven auto-approval with a human "yes".

After this fix, set_action_status takes a decided_at: bool flag (default
False). approve_and_send and reject_action pass decided_at=True;
maybe_auto_approve passes decided_at=False. The column now reflects only
human decisions.
"""

import sys
import os
import json
from unittest.mock import patch

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from db import db as d
from packs.loader import load_pack
from pipeline.service import process_event
from approval.service import approve_and_send, reject_action


def _fake_llm(prompt, response_schema=None):
    if response_schema is not None:
        return json.dumps({
            "intent": "availability_check",
            "listing_ref": "5th and Main",
            "urgency": "high",
            "action_type": "send_availability_reply",
        })
    return "Yes, it's still available!"


# ---- (a) Human approve sets decided_at ----
print("Testing that human approve_and_send sets decided_at...")
d.init_db(reset=True)
pack = load_pack("realestate")
with d.get_conn() as conn:
    business_id = d.create_business(
        conn, "Riverbend Realty", "realestate", "0.1.0",
        settings={"office_hours": "9-6", "default_follow_up_days": 2},
    )
    event_id = d.create_event(
        conn, business_id, source="inbound_email",
        raw_content="Is the condo at 5th and Main still available?",
    )
    with patch("pipeline.service.call_llm", side_effect=_fake_llm):
        result = process_event(conn, pack, event_id)
    action_id = result["id"]
    # Sanity: pending_approval, decided_at NULL
    pre = d.get_drafted_action(conn, action_id)
    assert pre["status"] == "pending_approval"
    assert pre["decided_at"] is None, f"pending action should have decided_at=NULL, got {pre['decided_at']!r}"
    print(f"  Pre-approve: status={pre['status']}, decided_at={pre['decided_at']}")

    approved = approve_and_send(conn, action_id)
    assert approved["status"] == "sent"
    assert approved["decided_at"] is not None, (
        f"human-approved action must have decided_at set, got {approved['decided_at']!r}"
    )
    print(f"  Post-approve: status={approved['status']}, decided_at={approved['decided_at']}")
print("Scenario (a) passed.\n")

# ---- (b) Human reject sets decided_at ----
print("Testing that human reject_action sets decided_at...")
with d.get_conn() as conn:
    event_id = d.create_event(
        conn, business_id, source="inbound_email",
        raw_content="Another inquiry, same day.",
    )
    with patch("pipeline.service.call_llm", side_effect=_fake_llm):
        result = process_event(conn, pack, event_id)
    action_id = result["id"]

    rejected = reject_action(conn, action_id)
    assert rejected["status"] == "rejected"
    assert rejected["decided_at"] is not None, (
        f"rejected action must have decided_at set, got {rejected['decided_at']!r}"
    )
    print(f"  Post-reject: status={rejected['status']}, decided_at={rejected['decided_at']}")
print("Scenario (b) passed.\n")

# ---- (c) Auto-approve does NOT set decided_at ----
print("Testing that maybe_auto_approve leaves decided_at NULL...")
d.init_db(reset=True)
pack = load_pack("realestate")
with d.get_conn() as conn:
    business_id = d.create_business(
        conn, "Riverbend Realty", "realestate", "0.1.0",
        settings={"office_hours": "9-6", "default_follow_up_days": 2},
    )
    d.set_approval_policy(conn, business_id, "send_availability_reply", auto_approve=True)

    event_id = d.create_event(
        conn, business_id, source="inbound_email",
        raw_content="Auto-approve scenario.",
    )
    with patch("pipeline.service.call_llm", side_effect=_fake_llm):
        result = process_event(conn, pack, event_id)
    action_id = result["id"]
    assert result["status"] == "sent", f"auto-approved should reach sent, got {result['status']}"

    final = d.get_drafted_action(conn, action_id)
    assert final["status"] == "sent"
    assert final["decided_at"] is None, (
        f"auto-approved action must have decided_at=NULL (no human decided), "
        f"got {final['decided_at']!r}"
    )
    print(f"  Post-auto: status={final['status']}, decided_at={final['decided_at']}")
    print("  -> decided_at remains NULL because no human made a decision.")
print("Scenario (c) passed.\n")

print("decided_at semantic smoke test passed.")
