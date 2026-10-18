"""
The one_to_one check and the write must be one atomic transaction: two requests
racing for the same free target must not both succeed. Also checks that the
approval card shows real counts for archive_link / restore_link.

Run from operations_nerd/:
    python tests/test_agentic_concurrency.py
"""

import json
import os
import sys
import threading

ROOT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..")
sys.path.insert(0, ROOT)
os.environ["LLM_PROVIDER"] = "stub"
os.environ["SIGNUP_INVITE_CODE"] = "inv"
os.environ["RATE_LIMIT_ENABLED"] = "0"

from fastapi.testclient import TestClient
from db import db as d

d.init_db(reset=True)
from main import app
from agentic import chat
from agentic.llm import LLMResult

TRIALS = 25
_accounts = [0]


def setup():
    _accounts[0] += 1
    c = TestClient(app, base_url="http://testserver")
    assert c.post("/api/auth/signup", json={"email": f"race{_accounts[0]}@example.com", "password": "correct horse battery",
                                            "invite_code": "inv"}).status_code == 201
    bid = c.post("/api/businesses", json={"name": "Race"}).json()["id"]
    ops = [{"op": "create_entity_type", "label": "Person", "ref": "p"}, {"op": "add_field", "entity": "p", "label": "Name", "type": "text"},
           {"op": "create_entity_type", "label": "Passport", "ref": "pp"}, {"op": "add_field", "entity": "pp", "label": "Number", "type": "text"},
           {"op": "add_link", "label": "Passport", "from": "p", "to": "pp", "cardinality": "one_to_one"}]
    chat.run_llm = lambda s, m, sc: LLMResult(json.dumps({"reply": "ok", "operations": ops}), "stub", 1, 1, 1)
    pid = c.post(f"/api/businesses/{bid}/chat", json={"messages": [{"role": "user", "content": "go"}]}).json()["proposal_id"]
    assert c.post(f"/api/proposals/{pid}/approve").status_code == 200
    spec = c.get(f"/api/businesses/{bid}").json()["spec"]
    ent = {e["label"]: e for e in spec["entities"]}
    return c, bid, {
        "person": ent["Person"]["key"], "passport": ent["Passport"]["key"],
        "name": ent["Person"]["fields"][0]["key"], "number": ent["Passport"]["fields"][0]["key"],
        "link": spec["links"][0]["key"]}


def race(requests):
    """Fires the given zero-argument callables at the same moment; returns their status codes."""
    barrier, out = threading.Barrier(len(requests)), []

    def go(fn):
        barrier.wait()
        out.append(fn().status_code)
    threads = [threading.Thread(target=go, args=(fn,)) for fn in requests]
    [t.start() for t in threads]
    [t.join() for t in threads]
    return out


def test_two_concurrent_creates_at_the_same_free_target_exactly_one_succeeds():
    c, bid, k = setup()
    for i in range(TRIALS):
        target = c.post(f"/api/businesses/{bid}/records/{k['passport']}", json={"values": {k["number"]: f"N{i}"}}).json()["id"]
        make = lambda n, t=target: (lambda: c.post(f"/api/businesses/{bid}/records/{k['person']}",
                                                   json={"values": {k["name"]: f"p{n}", k["link"]: t}}))
        codes = sorted(race([make(0), make(1)]))
        assert codes == [201, 422], (i, codes)
    with d.get_conn() as conn:                                                  # the database itself holds no target twice
        rows = [r[0] for r in conn.execute("SELECT value FROM entity_attributes WHERE attribute_name = ?", (k["link"],))]
    assert len(rows) == len(set(rows)) == TRIALS, (len(rows), len(set(rows)))


def test_two_concurrent_updates_to_the_same_free_target_exactly_one_succeeds():
    c, bid, k = setup()
    for i in range(TRIALS):
        target = c.post(f"/api/businesses/{bid}/records/{k['passport']}", json={"values": {k["number"]: f"U{i}"}}).json()["id"]
        a = c.post(f"/api/businesses/{bid}/records/{k['person']}", json={"values": {k["name"]: "a"}}).json()["id"]
        b = c.post(f"/api/businesses/{bid}/records/{k['person']}", json={"values": {k["name"]: "b"}}).json()["id"]
        make = lambda rid, t=target: (lambda: c.patch(f"/api/businesses/{bid}/records/{k['person']}/{rid}",
                                                      json={"values": {k["link"]: t}}))
        codes = sorted(race([make(a), make(b)]))
        assert codes == [200, 422], (i, codes)


def test_sequential_behaviour_is_unchanged():
    c, bid, k = setup()
    t1 = c.post(f"/api/businesses/{bid}/records/{k['passport']}", json={"values": {k["number"]: "S1"}}).json()["id"]
    t2 = c.post(f"/api/businesses/{bid}/records/{k['passport']}", json={"values": {k["number"]: "S2"}}).json()["id"]
    url = f"/api/businesses/{bid}/records/{k['person']}"
    a = c.post(url, json={"values": {k["name"]: "A", k["link"]: t1}})
    assert a.status_code == 201
    assert c.post(url, json={"values": {k["name"]: "B", k["link"]: t1}}).status_code == 422
    assert c.patch(f"{url}/{a.json()['id']}", json={"values": {k["name"]: "A2", k["link"]: t1}}).status_code == 200   # same record, same target
    assert c.patch(f"{url}/{a.json()['id']}", json={"values": {k["link"]: t2}}).status_code == 200                    # move to a free target
    assert c.post(url, json={"values": {k["name"]: "C", k["link"]: t1}}).status_code == 201                          # old target is free again
    assert c.post(url, json={"values": {k["name"]: "bad"}, "extra": 1}).status_code == 201                            # no link: unaffected
    assert c.post(url, json={"values": {"nope": "x"}}).status_code == 422                                           # a failed write rolls back cleanly
    assert c.post(url, json={"values": {k["name"]: "after failure", k["link"]: t2 + 100000}}).status_code == 422


def test_link_approval_card_shows_real_counts():
    html = open(os.path.join(ROOT, "frontend_app", "index.html"), encoding="utf-8").read()
    start = html.rindex("archive_link:")          # the impact map; the first one is the description map
    block = html[start:html.index("};", start)]
    assert "values_hidden" in block and "values_restored" in block and "records_affected" in block
    assert block.count("No record data affected") >= 2                        # still shown when the counts are 0


if __name__ == "__main__":
    tests = [(n, f) for n, f in sorted(globals().items()) if n.startswith("test_") and callable(f)]
    for name, fn in tests:
        fn()
        print(f"PASS {name}")
    print(f"\n{len(tests)} tests passed")
