"""
Pipeline smoke test with a mocked LLM.

This verifies the pipeline's own logic, event lookup, schema matching,
action_type constraint building, template rendering, database writes,
without depending on a real Gemini call. The actual Gemini connection was
already verified separately (test_llm_smoke.py). This test would catch
bugs in the orchestration even if Gemini were down or the key were missing.
"""

import sys
import os
import json
from unittest.mock import patch

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from db import db as d
from packs.loader import load_pack
from pipeline.service import process_event, _build_extraction_schema
from pipeline.templating import render_template, TemplateRenderError

# ---- templating ----

print("Testing render_template...")
result = render_template(
    "Office hours are {{settings.office_hours}}. Email: {{event.raw_content}}",
    {"settings": {"office_hours": "9-6"}, "event": {"raw_content": "Hi there"}},
)
assert result == "Office hours are 9-6. Email: Hi there"
print("  OK:", result)

try:
    render_template("{{settings.nonexistent}}", {"settings": {}})
    print("  ERROR: should have raised TemplateRenderError")
except TemplateRenderError as e:
    print("  Correctly raised on missing ref:", e)

# ---- schema building ----

print("\nTesting _build_extraction_schema...")
schema = _build_extraction_schema(["intent", "urgency"], ["send_availability_reply", "escalate_to_agent"])
print(" ", schema)
assert schema["properties"]["action_type"]["enum"] == ["send_availability_reply", "escalate_to_agent"]
assert set(schema["required"]) == {"intent", "urgency", "action_type"}

# ---- full pipeline, LLM mocked ----

print("\nTesting process_event with mocked LLM...")
d.init_db(reset=True)
pack = load_pack("realestate")

def fake_call_llm(prompt, response_schema=None):
    if response_schema is not None:
        # extraction call
        return json.dumps({
            "intent": "availability_check",
            "listing_ref": "5th and Main",
            "urgency": "high",
            "action_type": "send_availability_reply",
        })
    else:
        # drafting call
        return "Yes, it's still available! Want to schedule a showing?"

with d.get_conn() as conn:
    business_id = d.create_business(
        conn, "Riverbend Realty", "realestate", "0.1.0",
        settings={"office_hours": "9-6", "default_follow_up_days": 2, "property_types": ["condo"]},
    )
    event_id = d.create_event(conn, business_id, source="inbound_email",
                                raw_content="Is the condo at 5th and Main still available?")

    with patch("pipeline.service.call_llm", side_effect=fake_call_llm):
        result = process_event(conn, pack, event_id)

    print("  Result:", result)
    assert result["action_type"] == "send_availability_reply"
    assert "available" in result["payload"]["body"].lower()

    event = d.get_event(conn, event_id)
    assert event["processed"] == 1
    assert event["parsed_data"]["listing_ref"] == "5th and Main"
    print("  Event marked processed with parsed_data:", event["parsed_data"])

    drafted = d.list_drafted_actions(conn, business_id)
    assert len(drafted) == 1
    print("  Drafted action in DB:", drafted[0])

print("\nPipeline smoke test (mocked LLM) passed.")
