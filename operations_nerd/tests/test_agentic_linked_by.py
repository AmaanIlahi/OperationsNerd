"""
Tests for GET .../records/{entity_key}/{record_id}/linked-by: the backlink
query. Forward links live on the linking record, so a record's own read
cannot see what points at it; this endpoint answers that.

Runs like the other tests: python tests/test_agentic_linked_by.py from
operations_nerd/, and is also collectable by pytest.
"""

import os
import sys
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

os.environ["LLM_PROVIDER"] = "stub"
os.environ["SIGNUP_INVITE_CODE"] = "let-me-in"
os.environ["RATE_LIMIT_ENABLED"] = "0"
from fastapi.testclient import TestClient
from db import db as d
from main import app
from agentic import chat
from agentic.anthropic_llm import LLMResult
import json

d.init_db(reset=True)

PASSWORD = "correct horse battery"
_counter = [0]


class StubLLM:
    def __init__(self):
        self.ops, self.reply, self.calls, self.raw = [], "ok", [], None
    def __call__(self, system, messages, schema):
        self.calls.append({"system": system, "messages": messages, "schema": schema})
        text = self.raw if self.raw is not None else json.dumps({"reply": self.reply, "operations": self.ops})
        return LLMResult(text=text, model="stub-model", input_tokens=111, output_tokens=22, latency_ms=33)


STUB = StubLLM()
chat.run_llm = STUB


def new_account() -> TestClient:
    _counter[0] += 1
    client = TestClient(app)
    r = client.post("/api/auth/signup", json={
        "email": f"linkedby{_counter[0]}@example.com", "password": PASSWORD, "invite_code": "let-me-in"})
    assert r.status_code == 201, r.text
    return client


def apply_ops(client, bid, ops) -> dict:
    STUB.ops, STUB.reply, STUB.raw = ops, "ok", None
    r = client.post(f"/api/businesses/{bid}/chat", json={"messages": [{"role": "user", "content": "change"}]})
    assert r.status_code == 200, r.text
    p = r.json()
    assert p["status"] == "pending", p
    r = client.post(f"/api/proposals/{p['proposal_id']}/approve")
    assert r.status_code == 200, r.text
    return r.json()


def get_spec(client, bid) -> dict:
    return client.get(f"/api/businesses/{bid}").json()["spec"]


def _setup():
    """Business with Member --Home--> Location (many_to_one), one location,
    two members pointing at it, one member pointing nowhere."""
    client = new_account()
    r = client.post("/api/businesses", json={"name": "Link Biz"})
    assert r.status_code == 201, r.text
    bid = r.json()["id"]
    apply_ops(client, bid, [
        {"op": "create_entity_type", "label": "Member", "ref": "member"},
        {"op": "create_entity_type", "label": "Location", "ref": "loc"},
        {"op": "add_field", "entity": "member", "label": "Name", "type": "text"},
        {"op": "add_field", "entity": "loc", "label": "City", "type": "text"},
        {"op": "add_link", "label": "Home", "from": "member", "to": "loc", "cardinality": "many_to_one"},
    ])
    spec = get_spec(client, bid)
    member_key = next(e["key"] for e in spec["entities"] if e["label"] == "Member")
    loc_key = next(e["key"] for e in spec["entities"] if e["label"] == "Location")
    home_key = next(l for l in spec["links"] if l["label"] == "Home")["key"]
    name_key = next(f["key"] for e in spec["entities"] if e["key"] == member_key for f in e["fields"] if f["label"] == "Name")
    city_key = next(f["key"] for e in spec["entities"] if e["key"] == loc_key for f in e["fields"] if f["label"] == "City")

    r = client.post(f"/api/businesses/{bid}/records/{loc_key}", json={"values": {city_key: "Jaipur"}})
    assert r.status_code == 201, r.text
    loc_id = r.json()["id"]
    for name in ("Asha", "Bikram"):
        r = client.post(f"/api/businesses/{bid}/records/{member_key}",
                        json={"values": {name_key: name, home_key: loc_id}})
        assert r.status_code == 201, r.text
    r = client.post(f"/api/businesses/{bid}/records/{member_key}", json={"values": {name_key: "Chitra"}})
    assert r.status_code == 201, r.text
    return client, bid, member_key, loc_key, loc_id, home_key


def test_linked_by_returns_all_holders():
    client, bid, member_key, loc_key, loc_id, home_key = _setup()
    r = client.get(f"/api/businesses/{bid}/records/{loc_key}/{loc_id}/linked-by")
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["entity"] == loc_key and body["record_id"] == loc_id
    assert len(body["linked_by"]) == 1
    entry = body["linked_by"][0]
    assert entry["link_key"] == home_key and entry["link_label"] == "Home"
    assert entry["from_entity"] == member_key and entry["cardinality"] == "many_to_one"
    names = sorted(rec["label"] for rec in entry["records"])
    assert names == ["Asha", "Bikram"], names  # Chitra has no link, must not appear


def test_linked_by_empty_when_nothing_points():
    client, bid, member_key, loc_key, loc_id, _ = _setup()
    # a second location with no members pointing at it
    r = client.post(f"/api/businesses/{bid}/records/{loc_key}", json={"values": {}})
    assert r.status_code == 201, r.text
    other_id = r.json()["id"]
    r = client.get(f"/api/businesses/{bid}/records/{loc_key}/{other_id}/linked-by")
    assert r.status_code == 200, r.text
    entry = r.json()["linked_by"][0]
    assert entry["records"] == []


def test_linked_by_scoped_to_account_and_business():
    client, bid, member_key, loc_key, loc_id, _ = _setup()
    other = new_account()
    r = other.get(f"/api/businesses/{bid}/records/{loc_key}/{loc_id}/linked-by")
    assert r.status_code == 404, r.text  # not your business


def test_linked_by_missing_record_404():
    client, bid, member_key, loc_key, loc_id, _ = _setup()
    r = client.get(f"/api/businesses/{bid}/records/{loc_key}/999999/linked-by")
    assert r.status_code == 404, r.text


def test_linked_by_entity_without_inbound_links():
    client, bid, member_key, loc_key, loc_id, _ = _setup()
    # member has no inbound links; endpoint returns an empty linked_by list
    r = client.post(f"/api/businesses/{bid}/records/{member_key}", json={"values": {}})
    mid = r.json()["id"]
    r = client.get(f"/api/businesses/{bid}/records/{member_key}/{mid}/linked-by")
    assert r.status_code == 200, r.text
    assert r.json()["linked_by"] == []


if __name__ == "__main__":
    for name, fn in sorted({k: v for k, v in globals().items() if k.startswith("test_")}.items()):
        d.init_db(reset=True)
        _counter[0] = 0
        fn()
        print(f"  ok {name}")
    print("all linked-by tests passed")
