import sys, os, json, hmac, hashlib
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
os.environ["WEBHOOK_SECRET"] = "s3cret"
from unittest.mock import patch
from fastapi.testclient import TestClient
from db import db as d
from main import app

d.init_db(reset=True)
c = TestClient(app)
bid = c.post("/businesses", json={"name": "R", "industry_pack": "realestate",
      "answers": {"office_hours": "9-6", "default_follow_up_days": 2, "property_types": ["condo"]}}).json()["business_id"]

csv_text = "name,email,phone\nAna,ana@x.com,(555) 111-2222\nBob,,555-333-4444\nAna Dup,ANA@x.com,\nPhone Dup,,5551112222\n,nobody@x.com,\nBadMail,nope,\n"
r = c.post(f"/businesses/{bid}/contacts/import", content=csv_text).json()
assert r["added"] == 2 and r["duplicates_skipped"] == 2 and len(r["rejected"]) == 2, r
assert c.post(f"/businesses/{bid}/contacts/import", content="foo\nbar").status_code == 422
assert c.post(f"/businesses/{bid}/contacts/import", content=csv_text).json()["added"] == 0

def fake(prompt, response_schema=None):
    if response_schema is not None:
        return json.dumps({"intent": "availability_check", "listing_ref": "x", "urgency": "low", "action_type": "send_availability_reply"})
    return "Hello."
body = json.dumps({"raw_content": "Is it available?"}).encode()
sig = hmac.new(b"s3cret", body, hashlib.sha256).hexdigest()
with patch("pipeline.service.call_llm", side_effect=fake):
    assert c.post(f"/webhooks/{bid}/inbound_email", content=body).status_code == 401
    assert c.post(f"/webhooks/{bid}/inbound_email", content=body, headers={"X-Signature": "00"}).status_code == 401
    r = c.post(f"/webhooks/{bid}/inbound_email", content=body, headers={"X-Signature": "sha256=" + sig})
    assert r.status_code == 200, r.text
bad = b"{}"
s2 = hmac.new(b"s3cret", bad, hashlib.sha256).hexdigest()
assert c.post(f"/webhooks/{bid}/inbound_email", content=bad, headers={"X-Signature": s2}).status_code == 422
os.environ["WEBHOOK_SECRET"] = ""
assert c.post(f"/webhooks/{bid}/inbound_email", content=body, headers={"X-Signature": sig}).status_code == 401
print("intake OK")
