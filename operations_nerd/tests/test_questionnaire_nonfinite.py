"""
NaN and Infinity answers to a number question.

Python's JSON parser accepts the literals NaN, Infinity and -Infinity, so a
request could pass the "is it a number" check. The business was then saved
with an invalid settings_json and the response failed with a 500. These checks
make sure such answers are rejected with a 422 and nothing is saved, and that
ordinary numbers still work.
"""

import json
import os
import sys
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from fastapi.testclient import TestClient
from db import db as d
from main import app
from questionnaire.validation import validate_answers
from packs.loader import load_pack

d.init_db(reset=True)
client = TestClient(app, raise_server_exceptions=False)
pack = load_pack("realestate")


def count_businesses():
    with d.get_conn() as conn:
        return conn.execute("SELECT COUNT(*) FROM businesses").fetchone()[0]


def post_raw(days_literal, name="Riverbend Realty"):
    body = (
        '{"name": %s, "industry_pack": "realestate", "answers": '
        '{"office_hours": "9-6", "default_follow_up_days": %s, '
        '"property_types": ["condo"]}}' % (json.dumps(name), days_literal)
    )
    return client.post("/businesses", content=body, headers={"content-type": "application/json"})


base = {"office_hours": "9-6", "default_follow_up_days": 2, "property_types": ["condo"]}
checks = 0


def check(label, ok):
    global checks
    checks += 1
    print(("ok   " if ok else "FAIL ") + label)
    assert ok, label


# Validation function
for literal, value in [("NaN", float("nan")), ("Infinity", float("inf")), ("-Infinity", float("-inf"))]:
    issues = validate_answers(pack, {**base, "default_follow_up_days": value})
    check(f"validate_answers rejects {literal}", len(issues) == 1 and "finite number" in issues[0])
check("validate_answers still accepts an int", validate_answers(pack, base) == [])
check("validate_answers still accepts a float", validate_answers(pack, {**base, "default_follow_up_days": 2.5}) == [])
check("validate_answers still accepts zero", validate_answers(pack, {**base, "default_follow_up_days": 0}) == [])
check("validate_answers still accepts a negative number", validate_answers(pack, {**base, "default_follow_up_days": -3}) == [])
check("validate_answers still rejects a bool", len(validate_answers(pack, {**base, "default_follow_up_days": True})) == 1)
check("validate_answers still rejects a string", len(validate_answers(pack, {**base, "default_follow_up_days": "2"})) == 1)

# HTTP
before = count_businesses()
for literal in ["NaN", "Infinity", "-Infinity"]:
    resp = post_raw(literal)
    check(f"POST /businesses with {literal} -> 422", resp.status_code == 422)
    check(f"  422 body names the problem for {literal}", "finite number" in json.dumps(resp.json()))
check("nothing was saved by the rejected requests", count_businesses() == before)

resp = post_raw("2")
check("POST /businesses with 2 -> 200", resp.status_code == 200)
check("  valid number is stored as given", resp.json()["settings"]["default_follow_up_days"] == 2)
resp = post_raw("1e3")
check("POST /businesses with 1e3 -> 200", resp.status_code == 200)
check("one row per accepted request", count_businesses() == before + 2)

with d.get_conn() as conn:
    rows = [r[0] for r in conn.execute("SELECT settings_json FROM businesses").fetchall()]
check("every stored settings_json is strict JSON",
      all(json.loads(r, parse_constant=lambda c: (_ for _ in ()).throw(ValueError(c))) for r in rows))

print(f"\n{checks} checks passed")
