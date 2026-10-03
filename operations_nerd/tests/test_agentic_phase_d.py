"""
Phase D tests: the /app page is served and never builds HTML from strings,
and link values on records (the backend half of the UI's link dropdowns).

Run like the others: python tests/test_agentic_phase_d.py from operations_nerd/.
"""

import json
import os
import re
import sys
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from fastapi.testclient import TestClient
from db import db as d
from main import app

os.environ["SIGNUP_INVITE_CODE"] = "let-me-in"
os.environ["RATE_LIMIT_ENABLED"] = "0"
d.init_db(reset=True)

PASSWORD = "correct horse battery"
_counter = [0]
APP_DIR = os.path.join(os.path.dirname(__file__), "..", "frontend_app")


def new_account() -> TestClient:
    _counter[0] += 1
    c = TestClient(app)
    r = c.post("/api/auth/signup", json={
        "email": f"d{_counter[0]}@example.com", "password": PASSWORD, "invite_code": "let-me-in"})
    assert r.status_code == 201, r.text
    return c


def make_business(c, template="health_club") -> dict:
    r = c.post("/api/businesses", json={"name": "Biz", "template": template})
    assert r.status_code == 201, r.text
    return r.json()


def entity(spec, label):
    return next(e for e in spec["entities"] if e["label"] == label)


def field(spec, ent, label):
    return next(f for f in entity(spec, ent)["fields"] if f["label"] == label)["key"]


def link(spec, label):
    return next(l for l in spec["links"] if l["label"] == label)["key"]


def add(c, bid, ent_key, values, expect=201):
    r = c.post(f"/api/businesses/{bid}/records/{ent_key}", json={"values": values})
    assert r.status_code == expect, r.text
    return r.json()


class Gym:
    """A health-club business with two locations and a way to add members."""
    def __init__(self, c, names=("Downtown", "Uptown")):
        self.c = c
        biz = make_business(c)
        self.id, self.spec = biz["id"], biz["spec"]
        self.member, self.location = entity(self.spec, "Member")["key"], entity(self.spec, "Location")["key"]
        self.member_name = field(self.spec, "Member", "Full name")
        self.loc_name = field(self.spec, "Location", "Name")
        self.home = link(self.spec, "Home location")
        self.loc_a = add(c, self.id, self.location, {self.loc_name: names[0]})
        self.loc_b = add(c, self.id, self.location, {self.loc_name: names[1]})


# ---------- the page ----------

def test_app_page_is_served():
    c = TestClient(app)
    for path in ("/app", "/app/"):
        r = c.get(path)
        assert r.status_code == 200, path
        assert "text/html" in r.headers["content-type"]
        assert "Operations Nerd" in r.text


def test_app_page_never_builds_html_from_strings():
    html = open(os.path.join(APP_DIR, "index.html"), encoding="utf-8").read()
    for banned in ("innerHTML", "outerHTML", "insertAdjacentHTML", "document.write", "createContextualFragment", "eval("):
        assert banned not in html, banned
    assert "textContent" in html or "text:" in html
    assert not re.search(r"\son[a-z]+\s*=\s*[\"']", html)          # no inline event-handler attributes
    served = TestClient(app).get("/app/").text
    assert "innerHTML" not in served


# ---------- link values ----------

def test_link_value_roundtrip_and_display():
    g = Gym(new_account())
    rec = add(g.c, g.id, g.member, {g.member_name: "Ann", g.home: g.loc_a["id"]})
    assert rec["links"] == {g.home: {"id": g.loc_a["id"], "label": "Downtown"}}
    assert g.home not in rec["values"]                                   # links are not field values
    assert g.loc_a["display"] == "Downtown"

    listed = g.c.get(f"/api/businesses/{g.id}/records/{g.member}").json()["records"]
    assert listed[0]["links"][g.home]["label"] == "Downtown"              # target's first field value

    r = g.c.patch(f"/api/businesses/{g.id}/records/{g.member}/{rec['id']}", json={"values": {g.home: g.loc_b["id"]}})
    assert r.status_code == 200 and r.json()["links"][g.home]["label"] == "Uptown"
    assert g.c.get(f"/api/businesses/{g.id}/records/{g.member}/{rec['id']}").json()["links"][g.home]["id"] == g.loc_b["id"]

    r = g.c.patch(f"/api/businesses/{g.id}/records/{g.member}/{rec['id']}", json={"values": {g.home: None}})
    assert r.status_code == 200 and r.json()["links"] == {}               # cleared
    add(g.c, g.id, g.member, {g.member_name: "Bob", g.home: str(g.loc_a["id"])})   # numeric string accepted


def test_link_target_must_be_a_record_of_the_target_type_in_same_business():
    g = Gym(new_account())
    other_member = add(g.c, g.id, g.member, {g.member_name: "Not a location"})
    bad_cases = {
        "wrong entity type": other_member["id"],
        "no such record": 999999,
        "not a number": "downtown",
        "a list": [g.loc_a["id"]],
        "a bool": True,
    }
    for why, value in bad_cases.items():
        r = g.c.post(f"/api/businesses/{g.id}/records/{g.member}", json={"values": {g.member_name: "X", g.home: value}})
        assert r.status_code == 422, why
        assert g.home in r.json()["detail"], why

    # a Location record from a second business of the same account
    second = Gym(g.c)
    r = g.c.post(f"/api/businesses/{g.id}/records/{g.member}",
                 json={"values": {g.member_name: "X", g.home: second.loc_a["id"]}})
    assert r.status_code == 422
    # ... and the reverse direction
    r = g.c.post(f"/api/businesses/{second.id}/records/{second.member}",
                 json={"values": {second.member_name: "X", second.home: g.loc_a["id"]}})
    assert r.status_code == 422
    # nothing was stored by any rejected request
    assert g.c.get(f"/api/businesses/{g.id}/records/{g.member}").json()["total"] == 1


def test_link_target_from_another_account_is_rejected_and_isolated():
    owner = Gym(new_account(), ("Owner Downtown", "Owner Uptown"))
    intruder = Gym(new_account(), ("Intruder East", "Intruder West"))

    # intruder tries to point at the owner's Location by id
    r = intruder.c.post(f"/api/businesses/{intruder.id}/records/{intruder.member}",
                        json={"values": {intruder.member_name: "X", intruder.home: owner.loc_a["id"]}})
    assert r.status_code == 422
    # and cannot use the owner's business, records or link values at all
    mine = add(owner.c, owner.id, owner.member, {owner.member_name: "Mine", owner.home: owner.loc_a["id"]})
    assert intruder.c.get(f"/api/businesses/{owner.id}/records/{owner.member}/{mine['id']}").status_code == 404
    assert intruder.c.patch(f"/api/businesses/{owner.id}/records/{owner.member}/{mine['id']}",
                            json={"values": {owner.home: owner.loc_b["id"]}}).status_code == 404
    assert owner.c.get(f"/api/businesses/{owner.id}/records/{owner.member}/{mine['id']}").json()["links"][owner.home]["id"] == owner.loc_a["id"]
    # the intruder's list never reveals the owner's location names
    body = intruder.c.get(f"/api/businesses/{intruder.id}/records/{intruder.member}").text
    assert "Owner" not in body
    assert intruder.c.get(f"/api/businesses/{intruder.id}/records/{intruder.location}").json()["total"] == 2


def test_to_many_links_and_reverse_side_are_not_writable():
    g = Gym(new_account())
    attends = link(g.spec, "Attends")                  # member -> class, many_to_many
    klass = add(g.c, g.id, entity(g.spec, "Class")["key"], {field(g.spec, "Class", "Name"): "Yoga"})
    r = g.c.post(f"/api/businesses/{g.id}/records/{g.member}", json={"values": {g.member_name: "X", attends: klass["id"]}})
    assert r.status_code == 422 and attends in r.json()["detail"]
    # the "to" side of Home location (a Location record) has no such value either
    r = g.c.post(f"/api/businesses/{g.id}/records/{g.location}", json={"values": {g.loc_name: "X", g.home: g.loc_a["id"]}})
    assert r.status_code == 422
    # a field key from another entity type is not accepted either
    r = g.c.post(f"/api/businesses/{g.id}/records/{g.location}", json={"values": {g.loc_name: "X", g.member_name: "no"}})
    assert r.status_code == 422


def test_archived_link_hides_value_and_restore_brings_it_back():
    g = Gym(new_account())
    rec = add(g.c, g.id, g.member, {g.member_name: "Ann", g.home: g.loc_a["id"]})

    def run(ops):
        from agentic import chat
        from agentic.anthropic_llm import LLMResult
        chat.run_llm = lambda s, m, sc: LLMResult(json.dumps({"reply": "ok", "operations": ops}), "stub", 1, 1, 1)
        p = g.c.post(f"/api/businesses/{g.id}/chat", json={"messages": [{"role": "user", "content": "go"}]}).json()
        assert p["status"] == "pending", p
        assert g.c.post(f"/api/proposals/{p['proposal_id']}/approve").status_code == 200

    run([{"op": "archive_link", "link": g.home}])
    got = g.c.get(f"/api/businesses/{g.id}/records/{g.member}/{rec['id']}").json()
    assert got["links"] == {}
    r = g.c.patch(f"/api/businesses/{g.id}/records/{g.member}/{rec['id']}", json={"values": {g.home: g.loc_b["id"]}})
    assert r.status_code == 422
    run([{"op": "restore_link", "link": g.home}])
    got = g.c.get(f"/api/businesses/{g.id}/records/{g.member}/{rec['id']}").json()
    assert got["links"][g.home]["label"] == "Downtown"                    # value was kept all along


def test_link_label_falls_back_to_id():
    g = Gym(new_account())
    with d.get_conn() as conn:                                           # a location with no values at all
        blank_id = d.create_entity(conn, g.id, g.location)
    blank = g.c.get(f"/api/businesses/{g.id}/records/{g.location}/{blank_id}").json()
    assert blank["display"] == f"#{blank['id']}"
    rec = add(g.c, g.id, g.member, {g.member_name: "Ann", g.home: blank["id"]})
    assert rec["links"][g.home]["label"] == f"#{blank['id']}"


if __name__ == "__main__":
    tests = [(n, f) for n, f in sorted(globals().items()) if n.startswith("test_") and callable(f)]
    for name, fn in tests:
        fn()
        print(f"PASS {name}")
    print(f"\n{len(tests)} tests passed")
