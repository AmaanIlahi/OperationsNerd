"""
Smoke test for atomicity in the event pipeline.

Before this fix, process_event did three separate writes inside the route
handler's `with d.get_conn() as conn:` block (mark_event_processed,
create_drafted_action, maybe_auto_approve). If any of them raised, the
connection's autocommit would still fire on context exit, leaving partial
state. After this fix, get_conn() catches exceptions and rolls back the
whole transaction.

This test forces a failure mid-pipeline and verifies nothing landed.
"""

import sys
import os
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from db import db as d
from packs.loader import load_pack
from pipeline.service import process_event, PipelineError


print("Testing that a failed process_event rolls back the whole transaction...")
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

# Sanity: before the call, 1 event exists and it's not processed.
with d.get_conn() as conn:
    pre_event = d.get_event(conn, event_id)
    pre_actions = d.list_drafted_actions(conn, business_id)
assert pre_event["processed"] == 0, "event should start unprocessed"
assert pre_actions == [], "no drafted actions should exist before the failed call"
print("  Pre-state OK: event unprocessed, no drafted actions")

# Now run a pipeline call that WILL fail mid-flight. We use a fresh
# realestate pack but with the extraction template stripped, so the
# PipelineError fires after the LLM call (or here, after we short-circuit
# by giving the pack a source the schema doesn't recognize).
class _BadSourceEvent(dict):
    """Wrap a real event so .get() returns our bad source for the lookup."""
    pass

# Easier: create an event whose `source` is not declared in the pack's
# event_schemas. The pipeline will raise PipelineError before any write.
with d.get_conn() as conn:
    bad_event_id = d.create_event(
        conn, business_id, source="not_a_real_source",
        raw_content="This should fail before any pipeline write.",
    )

try:
    with d.get_conn() as conn:
        process_event(conn, pack, bad_event_id)
    print("  ERROR: should have raised PipelineError")
    assert False
except PipelineError as e:
    print("  PipelineError raised as expected:", e)

# Post-state: the bad event should be unprocessed and no new actions.
with d.get_conn() as conn:
    post_event = d.get_event(conn, bad_event_id)
    post_actions = d.list_drafted_actions(conn, business_id)

assert post_event["processed"] == 0, (
    f"failed-pipeline event should NOT be marked processed, got processed={post_event['processed']}"
)
assert post_actions == [], (
    f"no drafted actions should exist after a failed pipeline, got {post_actions}"
)
print("  Post-state OK: failed event unprocessed, no drafted actions")

# Now the stronger check: simulate a failure that happens AFTER the first
# write (mark_event_processed) but BEFORE the second (create_drafted_action).
# We do this by patching call_llm to raise during the drafting step.
from unittest.mock import patch

print("\nTesting rollback when failure happens AFTER the first write...")
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

import json
def _ok_first_then_fail(prompt, response_schema=None):
    # First call (extraction) returns valid JSON; second call (drafting)
    # raises, simulating an LLM outage mid-pipeline.
    if response_schema is not None:
        return json.dumps({
            "intent": "availability_check",
            "listing_ref": "5th and Main",
            "urgency": "high",
            "action_type": "send_availability_reply",
        })
    raise RuntimeError("simulated LLM outage during drafting")

try:
    with d.get_conn() as conn:
        with patch("pipeline.service.call_llm", side_effect=_ok_first_then_fail):
            process_event(conn, pack, event_id)
    print("  ERROR: should have raised")
    assert False
except RuntimeError as e:
    print("  RuntimeError raised as expected:", e)

# The atomicity proof: even though the first LLM call (extraction) succeeded
# and the pipeline *would have* called mark_event_processed before failing,
# the event must end up unprocessed in the DB.
with d.get_conn() as conn:
    post_event = d.get_event(conn, event_id)
    post_actions = d.list_drafted_actions(conn, business_id)

assert post_event["processed"] == 0, (
    f"event must be unprocessed after rollback, got processed={post_event['processed']} "
    f"(this is the bug Fix #3 prevents)"
)
assert post_actions == [], (
    f"no drafted actions must exist after rollback, got {post_actions}"
)
print("  ATOMICITY PROVEN: failure mid-pipeline left zero writes in the DB")

print("\nAtomicity smoke test passed.")
