"""
Smoke test for the questionnaire endpoints, hitting them the way a real
frontend would: HTTP requests through FastAPI's TestClient, not direct
function calls. Proves the routing, request/response shapes, and error
handling actually work end to end, not just the underlying functions.
"""

import sys
import os
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from fastapi.testclient import TestClient
from db import db as d
from main import app

d.init_db(reset=True)
client = TestClient(app)

# 1. Fetch the real estate questionnaire
resp = client.get("/packs/realestate/questionnaire")
print("GET /packs/realestate/questionnaire ->", resp.status_code)
assert resp.status_code == 200
body = resp.json()
print("  Questions returned:", [q["id"] for q in body["questions"]])
assert len(body["questions"]) == 3

# 2. Fetch a pack that doesn't exist
resp = client.get("/packs/nonexistent/questionnaire")
print("GET /packs/nonexistent/questionnaire ->", resp.status_code)
assert resp.status_code == 404

# 3. Submit valid answers -> should create a business
resp = client.post("/businesses", json={
    "name": "Riverbend Realty",
    "industry_pack": "realestate",
    "answers": {
        "office_hours": "9-6",
        "default_follow_up_days": 2,
        "property_types": ["condo", "single_family"],
    },
})
print("POST /businesses (valid) ->", resp.status_code)
assert resp.status_code == 200
print("  Response:", resp.json())
business_id = resp.json()["business_id"]

# 4. Confirm it actually landed in the database with the right settings
with d.get_conn() as conn:
    business = d.get_business(conn, business_id)
print("  Business in DB:", business)
assert business["settings_json"]["office_hours"] == "9-6"

# 5. Submit invalid answers -> should reject with details
resp = client.post("/businesses", json={
    "name": "Bad Realty",
    "industry_pack": "realestate",
    "answers": {
        "office_hours": "9-6",
        "default_follow_up_days": "two",     # wrong type
        "property_types": ["yacht"],          # invalid option
    },
})
print("POST /businesses (invalid) ->", resp.status_code)
assert resp.status_code == 422
print("  Issues:", resp.json()["detail"]["issues"])

print("\nQuestionnaire API smoke test passed.")
