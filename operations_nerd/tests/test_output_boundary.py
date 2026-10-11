"""Offline model-output boundary regressions; all provider calls mocked."""
import json
import sys
from pathlib import Path
from unittest.mock import patch
import pytest
sys.path.insert(0, str(Path(__file__).parents[1]))
from db import db as d
from packs.loader import load_pack
from pipeline.service import process_event, PipelineError
from inbox import service as inbox

VALID = {"intent": "availability_check", "listing_ref": "x", "urgency": "low",
         "action_type": "send_availability_reply"}

@pytest.fixture
def store(tmp_path):
    path = str(tmp_path / "boundary.db")
    d.init_db(path)
    with d.get_conn(path) as conn:
        bid = d.create_business(conn, "R", "realestate", "0.1.0", {"office_hours": "9-6", "default_follow_up_days": 2, "property_types": ["condo"]})
        eid = d.create_event(conn, bid, "inbound_email", "Is it available?")
    return path, bid, eid

@pytest.mark.parametrize("reply", [
    json.dumps({"action_type": VALID["action_type"]}),
    json.dumps({**VALID, "urgency": []}),
    json.dumps({**VALID, "intent": None}),
    json.dumps({**VALID, "listing_ref": 12}),
    json.dumps({**VALID, "untrusted_control": "send now"}),
    "[]", "null", "true", "not json",
], ids=["missing", "list-field", "null-field", "number-field", "extra-field", "array-root", "null-root", "bool-root", "bad-json"])
def test_invalid_extraction_fails_before_draft_or_writes(store, reply):
    path, bid, eid = store
    with patch("pipeline.service.call_llm", side_effect=[reply, "draft"]) as model:
        with pytest.raises(PipelineError):
            with d.get_conn(path) as conn:
                process_event(conn, load_pack("realestate"), eid)
        assert model.call_count == 1
    with d.get_conn(path) as conn:
        assert d.get_event(conn, eid)["processed"] == 0
        assert d.list_drafted_actions(conn, bid) == []

@pytest.mark.parametrize("draft", ["", "  \n", None, {}, "x" * 100001], ids=["empty", "whitespace", "null", "object", "oversized"])
def test_invalid_draft_never_enters_auto_approval(store, draft):
    path, bid, eid = store
    with d.get_conn(path) as conn:
        d.set_approval_policy(conn, bid, VALID["action_type"], True)
    with patch("pipeline.service.call_llm", side_effect=[json.dumps(VALID), draft]), \
         patch("approval.service._simulated_send") as send:
        with pytest.raises(PipelineError):
            with d.get_conn(path) as conn:
                process_event(conn, load_pack("realestate"), eid)
        send.assert_not_called()
    with d.get_conn(path) as conn:
        assert d.get_event(conn, eid)["processed"] == 0
        assert d.list_drafted_actions(conn, bid) == []

def test_valid_empty_unknown_field_stays_allowed(store):
    # Extraction schema declares strings, not factual truth or non-empty values.
    path, bid, eid = store
    valid = {**VALID, "listing_ref": ""}
    with patch("pipeline.service.call_llm", side_effect=[json.dumps(valid), "Review me"]):
        with d.get_conn(path) as conn:
            action = process_event(conn, load_pack("realestate"), eid)
    assert action["status"] == "pending_approval"

def test_invalid_inbox_attempt_can_retry_cleanly(store):
    path, bid, _ = store
    with d.get_conn(path) as conn:
        receipt = inbox.enqueue(conn, bid, "inbound_email", "a", "hello")
    with patch("pipeline.service.call_llm", return_value=json.dumps({"action_type": VALID["action_type"]})):
        with d.get_conn(path) as conn:
            failed = inbox.process_receipt(conn, bid, receipt["id"])
    assert failed["status"] == "failed" and failed["last_error"] == "PipelineError"
    with patch("pipeline.service.call_llm", side_effect=[json.dumps(VALID), "Review me"]):
        with d.get_conn(path) as conn:
            completed = inbox.process_receipt(conn, bid, receipt["id"])
    assert completed["status"] == "completed" and completed["attempts"] == 2
    with d.get_conn(path) as conn:
        assert len(d.list_drafted_actions(conn, bid)) == 1
