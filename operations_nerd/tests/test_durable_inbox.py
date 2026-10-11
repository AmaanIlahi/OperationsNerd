"""Offline durable-intake integration checks using temporary SQLite databases."""
import json
import sys
from pathlib import Path
from unittest.mock import patch
import pytest
sys.path.insert(0, str(Path(__file__).parents[1]))
from db import db as d
from inbox import service as s


@pytest.fixture
def store(tmp_path):
    path = str(tmp_path / "inbox.db")
    d.init_db(path)
    with d.get_conn(path) as conn:
        business = d.create_business(conn, "R", "realestate", "0.1.0",
                                     {"office_hours": "9-6", "default_follow_up_days": 2,
                                      "property_types": ["condo"]})
    return path, business


def fake(prompt, response_schema=None):
    if response_schema:
        return json.dumps({"intent": "availability_check", "listing_ref": "x",
                           "urgency": "low", "action_type": "send_availability_reply"})
    return "Draft for human review."


def queue(store, key="delivery-1", content="Is it available?"):
    path, bid = store
    with d.get_conn(path) as conn:
        return s.enqueue(conn, bid, "inbound_email", key, content)


def test_queue_does_not_call_model_or_create_actions(store):
    path, bid = store
    with patch("pipeline.service.call_llm") as model:
        receipt = queue(store)
    model.assert_not_called()
    assert receipt["status"] == "queued" and receipt["attempts"] == 0
    with d.get_conn(path) as conn:
        assert d.list_events(conn, bid) == []
        assert d.list_drafted_actions(conn, bid) == []


def test_duplicate_receipt_returns_same_id(store):
    assert queue(store)["id"] == queue(store)["id"]


def test_conflicting_content_is_rejected(store):
    queue(store)
    with pytest.raises(s.InboxConflict):
        queue(store, content="Different event")


def test_success_and_replay_draft_only_once(store):
    receipt = queue(store)
    path, bid = store
    with patch("pipeline.service.call_llm", side_effect=fake) as model:
        with d.get_conn(path) as conn:
            first = s.process_receipt(conn, bid, receipt["id"])
        with d.get_conn(path) as conn:
            second = s.process_receipt(conn, bid, receipt["id"])
        assert model.call_count == 2
    assert first == second and second["status"] == "completed"
    with d.get_conn(path) as conn:
        assert len(d.list_events(conn, bid)) == 1
        actions = d.list_drafted_actions(conn, bid)
        assert len(actions) == 1 and actions[0]["status"] == "pending_approval"


def test_failure_is_durable_and_retry_rolls_back_partial_event(store):
    receipt = queue(store)
    path, bid = store
    with patch("pipeline.service.call_llm", side_effect=[fake("", True), RuntimeError("secret-provider-message")]):
        with d.get_conn(path) as conn:
            failure = s.process_receipt(conn, bid, receipt["id"])
    assert failure["status"] == "failed" and failure["attempts"] == 1
    assert failure["last_error"] == "RuntimeError"
    with d.get_conn(path) as conn:
        assert d.list_events(conn, bid) == []
        assert d.list_drafted_actions(conn, bid) == []
        assert s.list_receipts(conn, bid, "failed")[0]["id"] == receipt["id"]
    with patch("pipeline.service.call_llm", side_effect=fake):
        with d.get_conn(path) as conn:
            retry = s.process_receipt(conn, bid, receipt["id"])
    assert retry["status"] == "completed" and retry["attempts"] == 2
    assert retry["last_error"] is None


def test_business_isolation(store):
    receipt = queue(store)
    path, bid = store
    with d.get_conn(path) as conn:
        other = d.create_business(conn, "Other", "realestate", "0.1.0", {})
    with patch("pipeline.service.call_llm") as model:
        with d.get_conn(path) as conn:
            with pytest.raises(LookupError):
                s.process_receipt(conn, other, receipt["id"])
        model.assert_not_called()
    with d.get_conn(path) as conn:
        assert s.list_receipts(conn, other) == []
        row = s.enqueue(conn, other, "inbound_email", "delivery-1", "Is it available?")
        assert row["id"] != receipt["id"]


def test_unknown_source_rejected_before_queue(store):
    path, bid = store
    with d.get_conn(path) as conn:
        with pytest.raises(ValueError):
            s.enqueue(conn, bid, "unknown", "key", "content")


def test_schema_upgrade_preserves_existing_business(store):
    path, bid = store
    d.init_db(path)
    with d.get_conn(path) as conn:
        assert d.get_business(conn, bid)["name"] == "R"


def test_concurrent_processing_only_drafts_once(store):
    from concurrent.futures import ThreadPoolExecutor
    import time
    receipt = queue(store)
    path, bid = store
    def slow(prompt, response_schema=None):
        time.sleep(0.03)
        return fake(prompt, response_schema)
    def process():
        with d.get_conn(path) as conn:
            return s.process_receipt(conn, bid, receipt["id"])
    with patch("pipeline.service.call_llm", side_effect=slow) as model:
        with ThreadPoolExecutor(max_workers=2) as pool:
            results = list(pool.map(lambda _: process(), range(2)))
        assert model.call_count == 2
    assert results[0]["action_id"] == results[1]["action_id"]


def test_healthclub_config_pack_without_core_changes(tmp_path):
    path = str(tmp_path / "health.db")
    d.init_db(path)
    with d.get_conn(path) as conn:
        bid = d.create_business(conn, "Health", "healthclub", "0.1.0", {})
        receipt = s.enqueue(conn, bid, "inbound_email", "h1", "Membership options?")
    assert receipt["status"] == "queued"


def test_api_enqueue_conflict_process_and_key_gate(store, monkeypatch):
    from fastapi.testclient import TestClient
    import os
    monkeypatch.chdir(Path(__file__).parents[1])
    from main import app
    path, bid = store
    original = d.get_conn
    monkeypatch.setattr(d, "get_conn", lambda: original(path))
    monkeypatch.setenv("OPERATIONS_API_KEY", "test-key")
    client = TestClient(app)
    url = f"/businesses/{bid}/inbox"
    body = {"source": "inbound_email", "idempotency_key": "api-key", "raw_content": "Available?"}
    assert client.post(url, json=body).status_code == 401
    headers = {"X-API-Key": "test-key"}
    response = client.post(url, json=body, headers=headers)
    assert response.status_code == 202
    rid = response.json()["id"]
    assert client.post(url, json=body, headers=headers).json()["id"] == rid
    assert client.post(url, json=dict(body, raw_content="Changed"), headers=headers).status_code == 409
    assert client.post(url, json=dict(body, source="missing"), headers=headers).status_code == 422
    assert client.get(url + "?status=bogus", headers=headers).status_code == 422
    with patch("pipeline.service.call_llm", side_effect=fake):
        result = client.post(url + f"/{rid}/process", headers=headers)
    assert result.status_code == 200 and result.json()["status"] == "completed"
    assert client.get(url, headers=headers).json()[0]["attempts"] == 1
