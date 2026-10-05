"""Audit log, stats, CSV export, edit-before-approve, reasoned rejection, bulk, overdue."""
import sys, os, json
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
from unittest.mock import patch
from fastapi.testclient import TestClient
from db import db as d
from main import app

d.init_db(reset=True)
client = TestClient(app)

def fake(prompt, response_schema=None):
    if response_schema is not None:
        return json.dumps({"intent": "availability_check", "listing_ref": "5th", "urgency": "high",
                           "action_type": "send_availability_reply"})
    return "Yes it is available."

bid = client.post("/businesses", json={"name": "R", "industry_pack": "realestate",
      "answers": {"office_hours": "9-6", "default_follow_up_days": 2, "property_types": ["condo"]}}).json()["business_id"]

ids = []
with patch("pipeline.service.call_llm", side_effect=fake):
    for i in range(5):
        r = client.post("/events", json={"business_id": bid, "source": "inbound_email", "raw_content": f"msg {i}"})
        assert r.status_code == 200, r.text
        ids.append(r.json()["drafted_action"]["id"] if "id" in r.json()["drafted_action"] else None)
with d.get_conn() as c:
    pend = [a["id"] for a in d.list_pending_actions(c, bid)]
assert len(pend) >= 3, pend

# edit then approve
r = client.post(f"/drafted-actions/{pend[0]}/edit-approve", json={"body": "=Hello, edited"})
assert r.status_code == 200 and r.json()["status"] == "sent" and r.json()["payload"]["body"] == "=Hello, edited"
assert client.post(f"/drafted-actions/{pend[0]}/edit-approve", json={"body": "x"}).status_code == 409
assert client.post(f"/drafted-actions/{pend[1]}/edit-approve", json={"body": "  "}).status_code == 409

# reject with reason
r = client.post(f"/drafted-actions/{pend[1]}/reject-with-reason", json={"reason": "wrong tone"})
assert r.status_code == 200 and r.json()["status"] == "rejected"
with d.get_conn() as c:
    assert c.execute("SELECT reason FROM action_reviews WHERE action_id=?", (pend[1],)).fetchone()[0] == "wrong tone"

# bulk with one bad id
r = client.post("/drafted-actions/bulk", json={"ids": pend[2:] + [99999], "decision": "approve"}).json()
assert set(r["done"]) == set(pend[2:]) and r["failed"][0]["id"] == 99999
assert client.post("/drafted-actions/bulk", json={"ids": [1], "decision": "nope"}).status_code == 422

# audit + stats + csv + overdue
kinds = [a["kind"] for a in client.get(f"/businesses/{bid}/audit").json()]
assert "edited_approved" in kinds and "rejected" in kinds and "approved" in kinds, kinds
assert client.get(f"/businesses/{bid}/audit?kind=rejected").json()[0]["action_id"] == pend[1]
st = client.get(f"/businesses/{bid}/stats").json()
assert st["rejection_rate"] is not None and st["edit_rate"] > 0 and st["events"] == 5
csv_text = client.get(f"/businesses/{bid}/export.csv").text
assert csv_text.splitlines()[0].startswith("id,event_id") and "'=Hello" in csv_text
assert client.get(f"/businesses/{bid}/overdue?minutes=0").json() == []
assert client.get("/businesses/9999/audit").status_code == 404

# pipeline failure is recorded
with patch("pipeline.service.call_llm", side_effect=lambda *a, **k: "not json"):
    r = client.post("/events", json={"business_id": bid, "source": "inbound_email", "raw_content": "bad"})
assert r.status_code == 422
assert client.get(f"/businesses/{bid}/audit?kind=pipeline_error").json(), "pipeline error not logged"
print("insights OK")
