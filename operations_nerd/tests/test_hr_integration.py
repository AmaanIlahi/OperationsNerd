"""HR pack integration: real engine, mocked LLM, synthetic fixtures only.

Verifies end to end that the hr pack drives the actual pipeline and the
human-approval gate, and that one business cannot see another business's
people records or drafts. No real employee data anywhere in this file.
"""

import sys
import os
import json
from unittest.mock import patch

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from db import db as d
from packs.loader import load_pack
from pipeline.service import process_event
from approval.service import approve_and_send, ApprovalError

HR_SETTINGS = {
    "hr_contact_email": "hr@example.test",
    "pto_annual_days": 20,
    "leave_approver_role": "direct manager",
    "onboarding_owner": "People lead",
    "interview_response_days": 5,
}

d.init_db(reset=True)
pack = load_pack("hr")

with d.get_conn() as conn:
    biz_a = d.create_business(conn, "Synthetic People Co A", "hr", "0.1.0", settings=dict(HR_SETTINGS))
    biz_b = d.create_business(conn, "Synthetic People Co B", "hr", "0.1.0", settings=dict(HR_SETTINGS))

    # --- synthetic fixtures: one employee, one candidate, both under A ---
    emp = d.create_entity(conn, biz_a, "crm_employee",
                          {"first_name": "Test", "last_name": "Employee",
                           "email": "t.employee@example.test", "role": "Analyst",
                           "department": "Ops", "manager": "Test Manager",
                           "start_date": "2026-01-12"})
    cand = d.create_entity(conn, biz_a, "crm_candidate",
                           {"name": "Test Candidate", "email": "candidate@example.test",
                            "role_applied": "Coordinator", "stage": "applied"})

    # --- separation at the integration boundary: B sees none of A's records ---
    assert d.list_entities(conn, biz_b) == [], "business B must not see A's entities"
    a_employees = d.list_entities(conn, biz_a, entity_type="crm_employee")
    assert len(a_employees) == 1 and a_employees[0]["id"] == emp
    print("OK: entities scoped per business (A has 2, B sees 0)")

    # --- leave request through the real engine, LLM mocked ---
    def fake_call_llm(prompt, response_schema=None):
        if response_schema is not None:
            return json.dumps({
                "intent": "leave_request",
                "employee_ref": "Test Employee",
                "candidate_ref": "",
                "leave_dates": "2026-11-03 to 2026-11-05",
                "urgency": "normal",
                "action_type": "send_leave_decision_reply",
            })
        return "Hi Test, your leave for 2026-11-03 to 2026-11-05 is approved by your direct manager."

    event_id = d.create_event(
        conn, biz_a, source="inbound_email",
        raw_content="Hi, I would like to request leave from 3 to 5 November. - Test Employee")

    with patch("pipeline.service.call_llm", side_effect=fake_call_llm):
        result = process_event(conn, pack, event_id)

    assert result["action_type"] == "send_leave_decision_reply", result["action_type"]

    # --- human-approval gate: commitment action must wait for a human ---
    assert result["status"] == "pending_approval", result["status"]
    drafted = d.list_drafted_actions(conn, biz_a)
    assert len(drafted) == 1 and drafted[0]["status"] == "pending_approval"
    print("OK: leave decision drafted and held pending human approval")

    # business B's draft list stays empty even after A drafts
    assert d.list_drafted_actions(conn, biz_b) == [], "business B must not see A's drafts"

    sent = approve_and_send(conn, drafted[0]["id"])
    assert sent["status"] in ("approved", "sent"), sent["status"]
    print("OK: human approval releases the drafted reply (simulated send)")

    # a second approve must fail closed, not re-send
    try:
        approve_and_send(conn, drafted[0]["id"])
        raise SystemExit("ERROR: double approval did not fail closed")
    except ApprovalError:
        print("OK: re-approval of a sent action fails closed")

    # --- onboarding lane drafts from the same pack through the same engine ---
    def fake_onboarding_llm(prompt, response_schema=None):
        if response_schema is not None:
            return json.dumps({
                "intent": "onboarding", "employee_ref": "Test Employee",
                "candidate_ref": "", "leave_dates": "", "urgency": "normal",
                "action_type": "send_onboarding_pack",
            })
        return "Welcome! Your onboarding is owned by People lead. Checklist: first-day logistics, paperwork, system access."

    event2 = d.create_event(conn, biz_a, source="inbound_email",
                            raw_content="Test Employee joins us Monday, please start onboarding.")
    with patch("pipeline.service.call_llm", side_effect=fake_onboarding_llm):
        result2 = process_event(conn, pack, event2)
    assert result2["action_type"] == "send_onboarding_pack"
    assert result2["status"] == "pending_approval"
    print("OK: onboarding pack drafted through the same engine, also gated")

print("\nHR integration test (mocked LLM, synthetic fixtures) passed.")
