"""
Warehouse re-check: proves the findings marked "(a) fixed" against current
main, by driving the real app through FastAPI's TestClient.

Standalone experiment. It imports operations_nerd/ to test it and never
modifies it. The model is always stubbed (agentic.chat.run_llm), so nothing
here touches the network, and the database is a fresh temporary file, so the
real one is never created or wiped.

Run from anywhere:  python experiments/warehouse_recheck/test_recheck.py
Prints one line per check (finding id, expected, actual, PASS/FAIL) and
exits non-zero if any check fails.
"""

import json
import os
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
CORE = os.path.abspath(os.path.join(HERE, "..", "..", "operations_nerd"))
sys.path.insert(0, CORE)
os.chdir(CORE)                           # main.py mounts static dirs by relative path

# All of this must be set before db is imported: db.DB_PATH is read at import time.
TMP_DB = os.path.join(tempfile.mkdtemp(prefix="warehouse_recheck_"), "recheck.db")
os.environ["DATABASE_PATH"] = TMP_DB
os.environ["LLM_PROVIDER"] = "stub"      # never reach a real model
os.environ["SIGNUP_INVITE_CODE"] = "let-me-in"
os.environ["RATE_LIMIT_ENABLED"] = "0"

from fastapi.testclient import TestClient
from db import db as d
from main import app
from agentic import chat
from agentic.anthropic_llm import LLMResult
from agentic.spec import empty_spec
from agentic.validator import validate_spec

if os.path.abspath(d.DB_PATH) != os.path.abspath(TMP_DB):
    sys.exit(f"refusing to run: database is {d.DB_PATH}, not the temporary file")
d.init_db(reset=True)

MISSING = 999999
results: list[tuple[str, str, str, bool]] = []


def check(fid: str, expected: str, actual: str, ok: bool):
    results.append((fid, expected, actual, bool(ok)))


# ---------- setup: a small warehouse business, built by (stubbed) chat ----------

def new_account() -> TestClient:
    c = TestClient(app)
    r = c.post("/api/auth/signup", json={
        "email": "recheck@example.com", "password": "correct horse battery", "invite_code": "let-me-in"})
    assert r.status_code == 201, r.text
    return c


def run_chat(c, bid, ops):
    """One agent turn with a canned answer, approved. Returns the new spec."""
    chat.run_llm = lambda s, m, sc: LLMResult(json.dumps({"reply": "ok", "operations": ops}), "stub", 1, 1, 1)
    p = c.post(f"/api/businesses/{bid}/chat", json={"messages": [{"role": "user", "content": "go"}]}).json()
    assert p["status"] == "pending" and not p["rejected_operations"], p
    assert c.post(f"/api/proposals/{p['proposal_id']}/approve").status_code == 200
    return c.get(f"/api/businesses/{bid}").json()["spec"]


WAREHOUSE_OPS = [
    {"op": "create_entity_type", "label": "Item", "ref": "item"},
    {"op": "add_field", "entity": "item", "label": "SKU", "type": "text", "required": True},
    {"op": "add_field", "entity": "item", "label": "Unit", "type": "select", "options": ["each", "box", "kg"]},
    {"op": "create_entity_type", "label": "Warehouse", "ref": "wh"},
    {"op": "add_field", "entity": "wh", "label": "Name", "type": "text"},
    {"op": "create_entity_type", "label": "Order", "ref": "order"},
    {"op": "add_field", "entity": "order", "label": "Order number", "type": "text"},
    {"op": "create_entity_type", "label": "Order line", "ref": "line"},
    {"op": "add_field", "entity": "line", "label": "Quantity", "type": "number"},
    {"op": "add_link", "label": "Order", "from": "line", "to": "order", "cardinality": "many_to_one"},
    {"op": "add_link", "label": "Item", "from": "line", "to": "item", "cardinality": "many_to_one"},
    {"op": "add_link", "label": "Ship from", "from": "line", "to": "wh", "cardinality": "many_to_one"},
    {"op": "create_entity_type", "label": "Stock level", "ref": "stock"},
    {"op": "add_field", "entity": "stock", "label": "On hand", "type": "number"},
    {"op": "add_link", "label": "Stocked item", "from": "stock", "to": "item", "cardinality": "many_to_one"},
    {"op": "add_link", "label": "Location", "from": "stock", "to": "wh", "cardinality": "many_to_one"},
]


class Warehouse:
    def __init__(self):
        self.c = new_account()
        r = self.c.post("/api/businesses", json={"name": "Depot"})
        assert r.status_code == 201, r.text
        self.id = r.json()["id"]
        self.spec = run_chat(self.c, self.id, WAREHOUSE_OPS)

    def entity(self, label):
        return next(e for e in self.spec["entities"] if e["label"] == label)

    def ekey(self, label):
        return self.entity(label)["key"]

    def fkey(self, ent, label):
        return next(f for f in self.entity(ent)["fields"] if f["label"] == label)["key"]

    def lkey(self, label):
        return next(l for l in self.spec["links"] if l["label"] == label)["key"]

    def url(self, ent, record_id=None):
        base = f"/api/businesses/{self.id}/records/{self.ekey(ent)}"
        return base if record_id is None else f"{base}/{record_id}"

    def add(self, ent, values):
        r = self.c.post(self.url(ent), json={"values": values})
        assert r.status_code == 201, r.text
        return r.json()

    def links_of(self, ent, record_id) -> dict:
        """{link key: target id} as the API reports it now."""
        got = self.c.get(self.url(ent, record_id)).json()["links"]
        return {k: v["id"] for k, v in got.items()}


def fmt(links: dict, names: dict) -> str:
    return ", ".join(f"{names[k]}={v}" for k, v in links.items()) or "(none)"


# ---------- cases ----------

def case_3(w: Warehouse):
    item = w.entity("Item")
    labels = [f["label"] for f in item["fields"]]
    types = [f["type"] for f in item["fields"]]
    unwanted = [f["label"] for f in item["fields"]
                if f["type"] in ("email", "phone") or "status" in f["label"].casefold()]
    check("3", "Item fields = [SKU, Unit]; no email/phone/status",
          f"fields={labels} types={types}",
          labels == ["SKU", "Unit"] and not unwanted)


def case_4(w: Warehouse):
    stocked = w.lkey("Stocked item")
    item_a = w.add("Item", {w.fkey("Item", "SKU"): "A-100"})
    rec = w.add("Stock level", {w.fkey("Stock level", "On hand"): 5, stocked: item_a["id"]})
    r = w.c.patch(w.url("Stock level", rec["id"]), json={"values": {stocked: MISSING}})
    after = w.links_of("Stock level", rec["id"]).get(stocked)
    named = r.status_code == 422 and stocked in r.json()["detail"]
    check("4", f"PATCH -> 422 naming the link; GET still {item_a['id']}",
          f"PATCH {r.status_code}{' naming link' if named else ''}; GET {after}",
          named and after == item_a["id"])


def case_6(w: Warehouse):
    stocked, location = w.lkey("Stocked item"), w.lkey("Location")
    names = {stocked: "Stocked item", location: "Location"}
    item = w.add("Item", {w.fkey("Item", "SKU"): "B-200"})
    wh = w.add("Warehouse", {w.fkey("Warehouse", "Name"): "North"})
    rec = w.add("Stock level", {stocked: item["id"], location: wh["id"]})
    before = w.links_of("Stock level", rec["id"])
    r = w.c.patch(w.url("Stock level", rec["id"]), json={"values": {location: MISSING}})
    after = w.links_of("Stock level", rec["id"])
    check("6", f"PATCH Location -> 422; both links still {fmt(before, names)}",
          f"PATCH {r.status_code}; links {fmt(after, names)}",
          r.status_code == 422 and len(before) == 2 and after == before)


def case_8(w: Warehouse):
    keys = {w.lkey(n): n for n in ("Order", "Item", "Ship from")}
    order = w.add("Order", {w.fkey("Order", "Order number"): "SO-8"})
    item = w.add("Item", {w.fkey("Item", "SKU"): "C-300"})
    wh = w.add("Warehouse", {w.fkey("Warehouse", "Name"): "South"})
    by_label = dict(zip(("Order", "Item", "Ship from"), (order, item, wh)))
    rec = w.add("Order line", {k: by_label[n]["id"] for k, n in keys.items()})
    before = w.links_of("Order line", rec["id"])
    for key, name in keys.items():
        r = w.c.patch(w.url("Order line", rec["id"]), json={"values": {key: MISSING}})
        after = w.links_of("Order line", rec["id"])
        others_before = {k: v for k, v in before.items() if k != key}
        others_after = {k: v for k, v in after.items() if k != key}
        check(f"8 ({name})", f"PATCH {name} -> 422; other two {fmt(others_before, keys)}",
              f"PATCH {r.status_code}; other two {fmt(others_after, keys)}",
              r.status_code == 422 and len(before) == 3 and after == before)


def case_7(w: Warehouse):
    order_link, item_link = w.lkey("Order"), w.lkey("Item")
    line_key = w.ekey("Order line")
    sku, num = w.fkey("Item", "SKU"), w.fkey("Order", "Order number")
    x = w.add("Item", {sku: "X-1"})
    y = w.add("Item", {sku: "Y-1"})
    o1 = w.add("Order", {num: "SO-1"})
    o2 = w.add("Order", {num: "SO-2"})
    w.add("Order line", {order_link: o1["id"], item_link: x["id"], w.fkey("Order line", "Quantity"): 2})
    w.add("Order line", {order_link: o2["id"], item_link: y["id"], w.fkey("Order line", "Quantity"): 1})

    def orders_containing(item_id) -> list[int]:
        """Core helpers only: lines whose Item link is item_id, then their Order link.
        find_entity_ids_by_attribute is not business-scoped, so check ownership too."""
        with d.get_conn() as conn:
            line_ids = d.find_entity_ids_by_attribute(conn, line_key, item_link, item_id)
            lines = [d.get_entity(conn, i) for i in line_ids]
        return sorted({l["attributes"][order_link] for l in lines if l["business_id"] == w.id})

    for item, want in ((x, [o1["id"]]), (y, [o2["id"]])):
        got = orders_containing(item["id"])
        check("7", f"orders containing {item['display']} = {want}", f"{got}", got == want)

    # No API endpoint answers it: the record routes are create/list/get/patch, and
    # the list ignores a link filter in the query string.
    record_routes = sorted(f"{method.upper()} {path}" for path, ops in app.openapi()["paths"].items()
                           if path.startswith("/api/businesses/{business_id}/records") for method in ops)
    listed = w.c.get(f"/api/businesses/{w.id}/records/{line_key}", params={item_link: x["id"]}).json()
    filtered = listed["total"] == 1
    check("7 (API)", "no endpoint filters records by link (gap still open)",
          f"{len(record_routes)} record routes; list ?Item=X returned {listed['total']} lines (X is on 1)",
          len(record_routes) == 4 and not filtered)


def case_11a():
    issues = validate_spec({**empty_spec("Depot"), "surprise": True})
    check("11a", "validate_spec rejects unknown top-level key 'surprise'",
          f"{issues}", bool(issues) and any("surprise" in i for i in issues))


def case_s3(w: Warehouse):
    bad = "l_undeclared"
    r = w.c.post(w.url("Stock level"), json={"values": {w.fkey("Stock level", "On hand"): 1, bad: 1}})
    detail = r.json().get("detail")
    named = isinstance(detail, dict) and bad in detail
    check("S3", f"POST with undeclared link key -> 422 naming '{bad}'",
          f"{r.status_code} {json.dumps(detail)}", r.status_code == 422 and named)


def case_s4(w: Warehouse):
    r = w.c.post(f"/api/businesses/{w.id}/records/crm_undeclared", json={"values": {}})
    check("S4", "POST to undeclared entity type -> 404",
          f"{r.status_code} {json.dumps(r.json().get('detail'))}", r.status_code == 404)


def main() -> int:
    w = Warehouse()
    case_3(w)
    case_4(w)
    case_6(w)
    case_8(w)
    case_7(w)
    case_11a()
    case_s3(w)
    case_s4(w)

    rows = [("finding", "expected", "actual", "result")] + [
        (f, e, a, "PASS" if ok else "FAIL") for f, e, a, ok in results]
    widths = [max(len(r[i]) for r in rows) for i in range(3)]
    for r in rows:
        print("  ".join(r[i].ljust(widths[i]) for i in range(3)) + "  " + r[3])
    failed = sum(not ok for *_, ok in results)
    print(f"\n{len(results) - failed}/{len(results)} checks passed")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
