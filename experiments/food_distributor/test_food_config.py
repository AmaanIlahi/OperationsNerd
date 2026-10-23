"""
Food distributor config experiment: checks each row of PREDICTIONS.md that
config or the stock engine can answer today, and prints each prediction next
to what was observed.

Standalone experiment. It imports operations_nerd/ to test it and never
modifies it. Two businesses, each run in its own process on its own temporary
database (one private database per company):
  A  a produce distributor: kg, short shelf life
  B  a frozen-goods distributor: cases, long shelf life
Data shapes are built only through the real chat -> proposal -> approval path,
with the model call replaced by scripted operation lists (agentic.chat.run_llm),
so no model is ever called. Records go through the records API.

Module-gap rows are checked against the stock engine on jasmeet/logistics-stock
as it stands: its two files are read with git show into a temporary folder and
run unchanged. No module code is built here.

Run from anywhere:  python -B experiments/food_distributor/test_food_config.py
Prints one line per row (id, predicted, observed, RIGHT/WRONG/UNTESTED) with
the evidence under it. Exits non-zero only if the script itself fails; a wrong
prediction is a result, not a failure.
"""

import importlib.util
import inspect
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
from datetime import date, timedelta
from decimal import Decimal

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.abspath(os.path.join(HERE, "..", ".."))
CORE = os.path.join(ROOT, "operations_nerd")
ENGINE_BRANCH = "jasmeet/logistics-stock"
ENGINE_FILES = ("modules/logistics/stock.py", "modules/logistics/schema.sql")
TODAY = date(2026, 10, 23)          # fixed, so the expiry window gives the same answer on any day

CHILD = "--company" in sys.argv

if CHILD:
    # A company run. The parent has set DATABASE_PATH to a file in a fresh
    # temporary folder; everything below must be set before db is imported,
    # because db.DB_PATH is read at import time.
    sys.path.insert(0, CORE)
    os.chdir(CORE)                       # main.py mounts static dirs by relative path
    os.environ["LLM_PROVIDER"] = "stub"  # never reach a real model
    os.environ["SIGNUP_INVITE_CODE"] = "let-me-in"
    os.environ["RATE_LIMIT_ENABLED"] = "0"
    os.environ["CHAT_DAILY_LIMIT"] = "1000"
    TMP_DB = os.environ.get("DATABASE_PATH", "")
    if not TMP_DB or not os.path.abspath(TMP_DB).startswith(os.path.abspath(tempfile.gettempdir())):
        sys.exit("refusing to run: DATABASE_PATH must be a file in a temporary folder")

    from fastapi.testclient import TestClient
    from db import db as d
    from main import app
    from agentic import chat, records
    from agentic.llm import LLMResult
    from agentic.spec import SpecField

    if os.path.abspath(d.DB_PATH) != os.path.abspath(TMP_DB):
        sys.exit(f"refusing to run: database is {d.DB_PATH}, not the temporary file")
    d.init_db(reset=True)


# ---------- the two businesses ----------

PROFILES = {
    "A": {
        "business": "Green Valley Produce",
        "units": ["kg", "box"],
        "storage": ["ambient", "chilled"],
        # sku, name, unit, storage, shelf life days
        "products": [("TOM-01", "Tomatoes", "kg", "chilled", 7), ("SPN-02", "Spinach", "kg", "chilled", 10)],
        # name, product index, received, expiry
        "lots": [("L1", 0, "2026-10-20", "2026-10-27"), ("L2", 0, "2026-10-22", "2026-10-29"),
                 ("L3", 1, "2026-10-23", "2026-11-05"), ("LX", 0, "2026-10-10", "2026-10-17")],
        "shelf_threshold": 8,           # Q1: products with shelf life above this, compared as text
        "decimals": ("1.1", "2.2"),     # D8a: two kg quantities, added up
    },
    "B": {
        "business": "Polar Frozen Foods",
        "units": ["case", "kg"],
        "storage": ["frozen"],
        "products": [("PEA-10", "Frozen peas", "case", "frozen", 540), ("FSH-20", "Cod fillets", "case", "frozen", 90)],
        "lots": [("L1", 0, "2026-03-01", "2026-10-28"), ("L2", 0, "2026-06-01", "2027-12-01"),
                 ("L3", 1, "2026-09-01", "2028-03-01"), ("LX", 0, "2024-09-01", "2026-09-30")],
        "shelf_threshold": 100,
        "decimals": ("4.35", "4.35", "4.35"),   # D8a: three lines of one case at 4.35
    },
}

SO_STATUSES = ["draft", "confirmed", "picked", "shipped", "delivered", "cancelled"]


def shape_ops(name: str) -> list[dict]:
    """The whole data shape as the owner would ask for it in chat."""
    p = PROFILES[name]
    ops = [
        {"op": "create_entity_type", "label": "Product", "ref": "product"},
        {"op": "add_field", "entity": "product", "label": "SKU", "type": "text", "required": True},
        {"op": "add_field", "entity": "product", "label": "Name", "type": "text"},
        {"op": "add_field", "entity": "product", "label": "Unit", "type": "select", "options": p["units"]},
        {"op": "add_field", "entity": "product", "label": "Storage temperature", "type": "select",
         "options": p["storage"]},
        {"op": "add_field", "entity": "product", "label": "Shelf life days", "type": "number"},
        {"op": "create_entity_type", "label": "Supplier", "ref": "supplier"},
        {"op": "add_field", "entity": "supplier", "label": "Name", "type": "text"},
        {"op": "create_entity_type", "label": "Customer", "ref": "customer"},
        {"op": "add_field", "entity": "customer", "label": "Name", "type": "text"},
        {"op": "create_entity_type", "label": "Purchase order", "ref": "po"},
        {"op": "add_field", "entity": "po", "label": "PO number", "type": "text"},
        {"op": "add_field", "entity": "po", "label": "Order date", "type": "date"},
        {"op": "add_field", "entity": "po", "label": "Status", "type": "select",
         "options": ["draft", "sent", "received", "closed"]},
        {"op": "add_link", "label": "Supplier", "from": "po", "to": "supplier", "cardinality": "many_to_one"},
        {"op": "create_entity_type", "label": "Purchase line", "ref": "pol"},
        {"op": "add_field", "entity": "pol", "label": "Quantity", "type": "number"},
        {"op": "add_field", "entity": "pol", "label": "Unit price", "type": "number"},
        {"op": "add_link", "label": "Order", "from": "pol", "to": "po", "cardinality": "many_to_one"},
        {"op": "add_link", "label": "Product", "from": "pol", "to": "product", "cardinality": "many_to_one"},
        {"op": "create_entity_type", "label": "Sales order", "ref": "so"},
        {"op": "add_field", "entity": "so", "label": "SO number", "type": "text"},
        {"op": "add_field", "entity": "so", "label": "Order date", "type": "date"},
        {"op": "add_field", "entity": "so", "label": "Status", "type": "select", "options": SO_STATUSES},
        {"op": "add_link", "label": "Customer", "from": "so", "to": "customer", "cardinality": "many_to_one"},
        {"op": "create_entity_type", "label": "Sales line", "ref": "sol"},
        {"op": "add_field", "entity": "sol", "label": "Quantity", "type": "number"},
        {"op": "add_field", "entity": "sol", "label": "Unit price", "type": "number"},
        {"op": "add_link", "label": "Order", "from": "sol", "to": "so", "cardinality": "many_to_one"},
        {"op": "add_link", "label": "Product", "from": "sol", "to": "product", "cardinality": "many_to_one"},
        {"op": "create_entity_type", "label": "Lot", "ref": "lot"},
        {"op": "add_field", "entity": "lot", "label": "Lot number", "type": "text"},
        {"op": "add_field", "entity": "lot", "label": "Received date", "type": "date"},
        {"op": "add_field", "entity": "lot", "label": "Expiry date", "type": "date"},
        {"op": "add_field", "entity": "lot", "label": "On hold", "type": "boolean"},
        {"op": "add_link", "label": "Product", "from": "lot", "to": "product", "cardinality": "many_to_one"},
        {"op": "add_link", "label": "Supplier", "from": "lot", "to": "supplier", "cardinality": "many_to_one"},
        {"op": "add_link", "label": "Received on", "from": "lot", "to": "pol", "cardinality": "many_to_one"},
    ]
    if name == "A":
        # A's shelf-life rule: at least N days left
        ops.append({"op": "add_field", "entity": "customer", "label": "Min days left", "type": "number"})
    else:
        # B's shelf-life rule: at least N percent of shelf life left; and catch weight on lines
        ops += [
            {"op": "add_field", "entity": "customer", "label": "Min shelf life left", "type": "number"},
            {"op": "add_field", "entity": "customer", "label": "Min shelf life basis", "type": "select",
             "options": ["days", "percent"]},
            {"op": "add_field", "entity": "sol", "label": "Weighed kg", "type": "number"},
            {"op": "add_field", "entity": "sol", "label": "Price per kg", "type": "number"},
        ]
    return ops


# ---------- company run (child process) ----------

class Company:
    def __init__(self, name: str):
        self.name = name
        self.c = TestClient(app)
        r = self.c.post("/api/auth/signup", json={
            "email": f"food{name.lower()}@example.com", "password": "correct horse battery",
            "invite_code": "let-me-in"})
        assert r.status_code == 201, r.text
        r = self.c.post("/api/businesses", json={"name": PROFILES[name]["business"]})
        assert r.status_code == 201, r.text
        self.id = r.json()["id"]
        self.spec = r.json()["spec"]

    # chat -> proposal -> approval

    def propose(self, ops: list[dict]) -> dict:
        chat.run_llm = lambda s, m, sc: LLMResult(json.dumps({"reply": "ok", "operations": ops}), "stub", 1, 1, 1)
        r = self.c.post(f"/api/businesses/{self.id}/chat", json={"messages": [{"role": "user", "content": "go"}]})
        assert r.status_code == 200, r.text
        return r.json()

    def approve(self, proposal: dict):
        r = self.c.post(f"/api/proposals/{proposal['proposal_id']}/approve")
        assert r.status_code == 200, r.text
        self.spec = r.json()["spec"]

    def run_chat(self, ops: list[dict]):
        p = self.propose(ops)
        assert p["status"] == "pending" and not p["rejected_operations"], p
        self.approve(p)

    def probe(self, op: dict) -> dict:
        """One operation through chat. Approved if the server accepts it, so later
        checks see its effect; otherwise the server's reasons."""
        p = self.propose([op])
        if p["status"] == "pending":
            self.approve(p)
            return {"accepted": True, "reasons": []}
        return {"accepted": False, "reasons": [r for x in p["rejected_operations"] for r in x["reasons"]]}

    # spec lookups by label, the way an owner names things

    def entity(self, label):
        return next(e for e in self.spec["entities"] if e["label"] == label)

    def ekey(self, label):
        return self.entity(label)["key"]

    def fkey(self, ent, label):
        return next((f["key"] for f in self.entity(ent)["fields"] if f["label"] == label), None)

    def lkey(self, ent, label):
        return next(l["key"] for l in self.spec["links"] if l["from"] == self.ekey(ent) and l["label"] == label)

    def values(self, ent, **by_label) -> dict:
        """{field or link key: value} from field labels; a key ending in _link names a link."""
        out = {}
        for label, value in by_label.items():
            if label.endswith("_link"):
                out[self.lkey(ent, label[:-5].replace("_", " "))] = value
            else:
                out[self.fkey(ent, label.replace("_", " "))] = value
        return out

    # records API

    def url(self, ent, record_id=None):
        base = f"/api/businesses/{self.id}/records/{self.ekey(ent)}"
        return base if record_id is None else f"{base}/{record_id}"

    def add(self, ent, values: dict) -> dict:
        r = self.c.post(self.url(ent), json={"values": values})
        assert r.status_code == 201, r.text
        return r.json()

    def try_add(self, ent, values: dict):
        r = self.c.post(self.url(ent), json={"values": values})
        return r.status_code, r.json()

    def patch(self, ent, record_id, values: dict):
        r = self.c.patch(self.url(ent, record_id), json={"values": values})
        return r.status_code, r.json()

    def get(self, ent, record_id) -> dict:
        r = self.c.get(self.url(ent, record_id))
        assert r.status_code == 200, r.text
        return r.json()

    def linked_by(self, ent, record_id, from_ent, link_label) -> list[int]:
        r = self.c.get(self.url(ent, record_id) + "/linked-by")
        assert r.status_code == 200, r.text
        key = self.lkey(from_ent, link_label)
        entry = next(x for x in r.json()["linked_by"] if x["link_key"] == key)
        return sorted(rec["id"] for rec in entry["records"])


def load_engine(engine_dir: str):
    spec = importlib.util.spec_from_file_location("stock", os.path.join(engine_dir, "stock.py"))
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def engine_error(fn, *args) -> str | None:
    try:
        fn(*args)
        return None
    except Exception as e:
        return f"{type(e).__name__}: {e}"


def run_company(name: str, engine_dir: str) -> dict:
    p = PROFILES[name]
    co = Company(name)
    ev = {"business": p["business"], "db": os.path.basename(TMP_DB)}
    co.run_chat(shape_ops(name))
    ev["shape_ops"] = len(shape_ops(name))

    # ----- base records -----
    supplier = co.add("Supplier", co.values("Supplier", Name="Fresh Farms" if name == "A" else "Nordic Freeze"))
    customers = [co.add("Customer", co.values("Customer", Name=n)) for n in ("Corner Deli", "City Bistro")]
    products = [co.add("Product", co.values("Product", SKU=s, Name=n, Unit=u, Storage_temperature=t,
                                            Shelf_life_days=days))
                for s, n, u, t, days in p["products"]]
    po = co.add("Purchase order", co.values("Purchase order", PO_number="PO-1", Order_date="2026-10-18",
                                            Status="received", Supplier_link=supplier["id"]))
    po_lines = [co.add("Purchase line", co.values("Purchase line", Quantity=q, Unit_price=pr,
                                                  Order_link=po["id"], Product_link=prod["id"]))
                for q, pr, prod in ((50, 1.2, products[0]), (20, 2.5, products[1]))]
    lots = {}
    for lot_no, pi, received, expiry in p["lots"]:
        vals = co.values("Lot", Lot_number=f"{lot_no}-{name}", Received_date=received, Expiry_date=expiry,
                         On_hold=False, Product_link=products[pi]["id"], Supplier_link=supplier["id"])
        if lot_no in ("L1", "L2"):
            vals[co.lkey("Lot", "Received on")] = po_lines[0]["id"]    # one PO line, two lots (D11)
        lots[lot_no] = co.add("Lot", vals)
    sos = [co.add("Sales order", co.values("Sales order", SO_number=f"SO-{i + 1}", Order_date="2026-10-23",
                                           Status="confirmed", Customer_link=cust["id"]))
           for i, cust in enumerate(customers)]
    sol_qty = (30, 5)               # line 1 is later split across lots L1 (20) and L2 (10)
    so_lines = [co.add("Sales line", co.values("Sales line", Quantity=q, Unit_price=3, Order_link=so["id"],
                                               Product_link=products[0]["id"]))
                for q, so in zip(sol_qty, sos)]

    # ----- D1 to D5: plain shapes -----
    got = [co.get("Product", prod["id"])["values"] for prod in products]
    ev["D1"] = {"fields": [(f["label"], f["type"]) for f in co.entity("Product")["fields"]],
                "round_trip": all(g[co.fkey("Product", "Unit")] == row[2]
                                  and g[co.fkey("Product", "Storage temperature")] == row[3]
                                  and g[co.fkey("Product", "Shelf life days")] == row[4]
                                  for g, row in zip(got, p["products"]))}
    ev["D2"] = {"supplier": co.get("Supplier", supplier["id"])["display"],
                "customers": [co.get("Customer", c["id"])["display"] for c in customers]}
    ev["D3"] = {"lines_on_po": co.linked_by("Purchase order", po["id"], "Purchase line", "Order"),
                "expected": sorted(l["id"] for l in po_lines),
                "line_product": co.get("Purchase line", po_lines[0]["id"])["links"]
                .get(co.lkey("Purchase line", "Product"), {}).get("id") == products[0]["id"]}
    ev["D4"] = {"lines_on_so": co.linked_by("Sales order", sos[0]["id"], "Sales line", "Order"),
                "expected": [so_lines[0]["id"]],
                "line_product": co.get("Sales line", so_lines[0]["id"])["links"]
                .get(co.lkey("Sales line", "Product"), {}).get("id") == products[0]["id"]}
    lot1 = co.get("Lot", lots["L1"]["id"])
    ev["D5"] = {"dates": [lot1["values"].get(co.fkey("Lot", "Received date")),
                          lot1["values"].get(co.fkey("Lot", "Expiry date"))],
                "links": sorted(l["label"] for l in co.spec["links"]
                                if l["from"] == co.ekey("Lot") and l["key"] in lot1["links"])}

    # ----- D6: lot number unique per product -----
    lot_no_key = co.fkey("Lot", "Lot number")
    ev["D6"] = {"unique_probe": co.probe({"op": "update_field", "entity": co.ekey("Lot"), "field": lot_no_key,
                                          "unique": True}),
                "unique_on_add": co.probe({"op": "add_field", "entity": co.ekey("Lot"), "label": "Lot code",
                                           "type": "text", "unique": True})}
    status, body = co.try_add("Lot", co.values("Lot", Lot_number=f"L1-{name}", Received_date="2026-10-21",
                                               Expiry_date="2026-10-28", Product_link=products[0]["id"]))
    ev["D6"]["duplicate_same_product"] = status

    # ----- D7: every order line must have a product -----
    ev["D7"] = {"required_link_probe": co.probe({"op": "add_link", "label": "Must have product",
                                                 "from": co.ekey("Sales line"), "to": co.ekey("Product"),
                                                 "cardinality": "many_to_one", "required": True}),
                "required_via_update_field": co.probe({"op": "update_field", "entity": co.ekey("Sales line"),
                                                       "field": co.lkey("Sales line", "Product"),
                                                       "required": True})}
    status, _ = co.try_add("Sales line", co.values("Sales line", Quantity=4, Order_link=sos[0]["id"]))
    ev["D7"]["line_without_product"] = status

    # ----- D8a: exact amounts -----
    ev["D8a"] = {"decimal_probe": co.probe({"op": "add_field", "entity": co.ekey("Sales line"), "label": "Exact",
                                            "type": "decimal"})}
    stored = []
    for text in p["decimals"]:
        line = co.add("Sales line", co.values("Sales line", Quantity=text if name == "A" else 1,
                                              Unit_price=4.35 if name == "A" else text,
                                              Order_link=sos[1]["id"], Product_link=products[1]["id"]))
        stored.append(co.get("Sales line", line["id"])["values"][co.fkey("Sales line",
                                                                        "Quantity" if name == "A" else "Unit price")])
    ev["D8a"].update({"entered": list(p["decimals"]), "stored": stored,
                      "round_trip": [Decimal(str(s)) == Decimal(t) for s, t in zip(stored, p["decimals"])],
                      "float_sum": repr(sum(stored)),
                      "exact_sum": str(sum(Decimal(t) for t in p["decimals"]))})

    # ----- D8b: decimal comma -----
    status, body = co.try_add("Sales line", co.values("Sales line", Quantity="1,5", Order_link=sos[1]["id"],
                                                      Product_link=products[1]["id"]))
    ev["D8b"] = {"status": status, "stored": body.get("values", {}).get(co.fkey("Sales line", "Quantity"))}

    # ----- R1: line and order totals -----
    ev["R1"] = {"formula_type_probe": co.probe({"op": "add_field", "entity": co.ekey("Sales line"),
                                                "label": "Line total", "type": "formula"}),
                "formula_attr_probe": co.probe({"op": "add_field", "entity": co.ekey("Sales line"),
                                                "label": "Line total", "type": "number",
                                                "formula": "Quantity * Unit price"})}
    line = co.get("Sales line", so_lines[0]["id"])
    order = co.get("Sales order", sos[0]["id"])
    ev["R1"]["line_value_keys"] = sorted(f["label"] for f in co.entity("Sales line")["fields"]
                                         if f["key"] in line["values"])
    ev["R1"]["order_value_keys"] = sorted(f["label"] for f in co.entity("Sales order")["fields"]
                                          if f["key"] in order["values"])

    # ----- R2: expiry after received -----
    ev["R2"] = {"rule_probe": co.probe({"op": "add_rule", "entity": co.ekey("Lot"),
                                        "rule": "Expiry date > Received date"})}
    status, _ = co.try_add("Lot", co.values("Lot", Lot_number=f"BAD-{name}", Received_date="2026-10-23",
                                            Expiry_date="2026-10-01", Product_link=products[0]["id"]))
    ev["R2"]["expiry_before_received"] = status

    # ----- R3: order status flow -----
    status_key = co.fkey("Sales order", "Status")
    flow = []
    for target in ("delivered", "draft", "cancelled", "shipped"):
        code, _ = co.patch("Sales order", sos[1]["id"], {status_key: target})
        flow.append((target, code))
    ev["R3"] = {"options": next(f["options"] for f in co.entity("Sales order")["fields"] if f["key"] == status_key),
                "transitions": flow,
                "flow_probe": co.probe({"op": "set_status_flow", "entity": co.ekey("Sales order"),
                                        "field": status_key, "allowed": {"draft": ["confirmed"]}})}

    # ----- P1 (keys) and template rekeying, before any rename -----
    r = co.c.post("/api/businesses", json={"name": "Template check", "template": "health_club"})
    assert r.status_code == 201, r.text
    tspec = r.json()["spec"]
    ev["P1"] = {"unit_key": co.fkey("Product", "Unit"),
                "template_first_field": [tspec["entities"][0]["label"], tspec["entities"][0]["fields"][0]["label"],
                                         tspec["entities"][0]["fields"][0]["key"]]}

    # ----- engine, unchanged, on this company's private database -----
    stock = load_engine(engine_dir)
    conn = stock.connect(TMP_DB)
    stock.init_db(conn)
    src = "\n".join(open(os.path.join(engine_dir, os.path.basename(f)), encoding="utf-8").read()
                    for f in ENGINE_FILES).lower()
    # code and SQL only: a comment like "reservations never hold more than is on hand" is not a hold
    src = re.sub(r'"""(.|\n)*?"""|--[^\n]*|#[^\n]*', "", src)
    words = {"lot": r"\blots?\b", "expiry": r"expir", "hold": r"\bhold\b|on_hold|\bheld\b", "fefo": r"fefo",
             "unit": r"\bunits?\b", "scale": r"\bscale\b", "customer": r"customer",
             "order status": r"\bdraft\b|\bconfirmed\b", "weight": r"weigh", "lot allocation": r"allocation"}
    eng = {
        "columns": {t: [c[1] for c in conn.execute(f"PRAGMA table_info({t})")]
                    for t in ("stock_movements", "stock_reservations")},
        "signatures": {fn: list(inspect.signature(getattr(stock, fn)).parameters)
                       for fn in ("receive", "reserve", "release", "ship")},
        "mentions": {w: bool(re.search(rx, src)) for w, rx in words.items()},
    }
    bid = co.id
    p0, p1 = products[0]["id"], products[1]["id"]
    eng["fraction_receive"] = engine_error(stock.receive, conn, bid, p1, 1.5, "po:1/line:2", f"{name}-frac")
    eng["integer_receive"] = engine_error(stock.receive, conn, bid, p1, 40, "po:1/line:2", f"{name}-int")
    eng["unknown_item_receive"] = engine_error(stock.receive, conn, bid, 987654, 3, "po:x", f"{name}-unknown")
    eng["free_text_source_ref"] = engine_error(stock.receive, conn, bid, p1, 1, "any text at all, not a PO line",
                                               f"{name}-free")
    # lot used as the item: an expired lot is received and reserved like any other
    lx = lots["LX"]["id"]
    stock.receive(conn, bid, lx, 10, "po:1/line:1", f"{name}-lx-in")
    eng["expired_lot_reserve"] = stock.reserve(conn, bid, lx, 5, "so:1", f"{name}-lx-res")
    ev["engine"] = eng

    # ----- D9: lot quantity as a config field -----
    ev["D9"] = {"probe": co.probe({"op": "add_field", "entity": co.ekey("Lot"), "label": "Quantity",
                                   "type": "number"})}
    lq = co.fkey("Lot", "Quantity")
    co.patch("Lot", lots["L1"]["id"], {lq: 100})
    stock.receive(conn, bid, p0, 100, "po:1/line:1", f"{name}-d9-in")
    res = stock.reserve(conn, bid, p0, 30, f"so:{sos[0]['id']}", f"{name}-d9-res")
    stock.ship(conn, bid, res["reservation_id"], 30, f"{name}-d9-ship")
    ev["D9"]["after_ship"] = {"lot_field": co.get("Lot", lots["L1"]["id"])["values"].get(lq),
                              "ledger_on_hand": stock.on_hand(conn, bid, p0)}
    code, _ = co.patch("Lot", lots["L1"]["id"], {lq: 250})
    ev["D9"]["after_edit"] = {"status": code, "lot_field": co.get("Lot", lots["L1"]["id"])["values"].get(lq),
                              "ledger_on_hand": stock.on_hand(conn, bid, p0)}

    # ----- D10 and Q2: which lots a sales line shipped from; recall -----
    sol, lot_e = co.ekey("Sales line"), co.ekey("Lot")
    co.run_chat([
        {"op": "add_link", "label": "Shipped from lot", "from": sol, "to": lot_e, "cardinality": "many_to_one"},
        {"op": "create_entity_type", "label": "Lot allocation", "ref": "alloc"},
        {"op": "add_field", "entity": "alloc", "label": "Quantity", "type": "number"},
        {"op": "add_link", "label": "Line", "from": "alloc", "to": sol, "cardinality": "many_to_one"},
        {"op": "add_link", "label": "Lot", "from": "alloc", "to": lot_e, "cardinality": "many_to_one"},
    ])
    ship_key = co.lkey("Sales line", "Shipped from lot")
    l1, l2 = lots["L1"]["id"], lots["L2"]["id"]
    # line 1 (Corner Deli, 30) drew 20 from L1 and 10 from L2; line 2 (City Bistro, 5) drew from L2
    co.patch("Sales line", so_lines[0]["id"], {ship_key: l1})
    co.patch("Sales line", so_lines[0]["id"], {ship_key: l2})
    held_after_second = co.get("Sales line", so_lines[0]["id"])["links"][ship_key]["id"]
    list_status, _ = co.patch("Sales line", so_lines[0]["id"], {ship_key: [l1, l2]})
    co.patch("Sales line", so_lines[0]["id"], {ship_key: l1})          # the owner keeps the larger part
    co.patch("Sales line", so_lines[1]["id"], {ship_key: l2})

    def customers_of(line_ids):
        names = set()
        for lid in line_ids:
            so_id = co.get("Sales line", lid)["links"][co.lkey("Sales line", "Order")]["id"]
            cust = co.get("Sales order", so_id)["links"][co.lkey("Sales order", "Customer")]["label"]
            names.add(cust)
        return sorted(names)

    truth = sorted(c["display"] for c in customers)
    link_answer = customers_of(co.linked_by("Lot", l2, "Sales line", "Shipped from lot"))
    alloc = lambda line, lot, q: co.add("Lot allocation", co.values("Lot allocation", Quantity=q, Line_link=line,
                                                                    Lot_link=lot))
    alloc(so_lines[0]["id"], l1, 20)
    alloc(so_lines[0]["id"], l2, 10)
    alloc(so_lines[1]["id"], l2, 5)
    alloc_lines = [co.get("Lot allocation", a)["links"][co.lkey("Lot allocation", "Line")]["id"]
                   for a in co.linked_by("Lot", l2, "Lot allocation", "Lot")]
    alloc_answer = customers_of(alloc_lines)
    over_status, _ = co.try_add("Lot allocation", co.values("Lot allocation", Quantity=500,
                                                             Line_link=so_lines[1]["id"], Lot_link=l2))
    ev["D10"] = {"link_holds_after_second_lot": "L2" if held_after_second == l2 else held_after_second,
                 "list_of_lots_status": list_status, "alloc_over_line_and_lot_status": over_status}
    ev["Q2"] = {"truth_for_L2": truth, "via_line_link": link_answer, "via_hand_allocations": alloc_answer}

    # ----- D11: one PO line received as several lots -----
    ev["D11"] = {"lots_on_po_line": co.linked_by("Purchase line", po_lines[0]["id"], "Lot", "Received on"),
                 "expected": sorted([l1, l2])}

    # ----- Q1: lots expiring in the next 7 days -----
    exp_key = co.fkey("Lot", "Expiry date")
    all_lots = co.c.get(co.url("Lot"), params={"limit": 500}).json()["records"]
    window = (TODAY.isoformat(), (TODAY + timedelta(days=7)).isoformat())
    truth = sorted(r["id"] for r in all_lots if window[0] <= r["values"].get(exp_key, "") <= window[1])
    filtered = co.c.get(co.url("Lot"), params={"expiry_from": window[0], "expiry_to": window[1],
                                               "filter": f"{exp_key}<={window[1]}"}).json()
    with d.get_conn() as c:
        sql_dates = sorted(r[0] for r in c.execute(
            "SELECT entity_id FROM entity_attributes WHERE entity_type = ? AND attribute_name = ?"
            " AND value >= ? AND value <= ?", (co.ekey("Lot"), exp_key, json.dumps(window[0]), json.dumps(window[1]))))
        shelf_key, cut = co.fkey("Product", "Shelf life days"), p["shelf_threshold"]
        sql_numbers = sorted(r[0] for r in c.execute(
            "SELECT entity_id FROM entity_attributes WHERE entity_type = ? AND attribute_name = ? AND value > ?",
            (co.ekey("Product"), shelf_key, json.dumps(cut))))
    number_truth = sorted(pr["id"] for pr, row in zip(products, p["products"]) if row[4] > cut)
    ev["Q1"] = {"window": window, "truth": truth, "lots_total": len(all_lots),
                "filtered_total": filtered["total"],
                "list_params": [n for n in inspect.signature(records.list_records).parameters],
                "exact_match_only": "AND value = ?" in inspect.getsource(d.find_entity_ids_by_attribute),
                "sql_date_range": sql_dates, "sql_number_gt": {"cut": cut, "got": sql_numbers,
                                                               "truth": number_truth}}

    # ----- M3b, M5, M6: config fields the module would need -----
    hold_key = co.fkey("Lot", "On hold")
    code, _ = co.patch("Lot", lots["L2"]["id"], {hold_key: True})
    ev["M3b"] = {"hold_field_status": code, "held": co.get("Lot", lots["L2"]["id"])["values"].get(hold_key)}
    if name == "A":
        code, _ = co.patch("Customer", customers[0]["id"], co.values("Customer", Min_days_left=5))
        ev["M5"] = {"form": "at least 5 days left", "status": code}
    else:
        code, _ = co.patch("Customer", customers[0]["id"], co.values("Customer", Min_shelf_life_left=70,
                                                                     Min_shelf_life_basis="percent"))
        ev["M5"] = {"form": "at least 70 percent of shelf life left", "status": code}
        cw = co.add("Sales line", co.values("Sales line", Quantity=10, Weighed_kg=183.4, Price_per_kg=6.2,
                                            Order_link=sos[0]["id"], Product_link=p1))
        res = stock.reserve(conn, bid, p1, 10, f"so:{sos[0]['id']}", "B-cw-res")
        shipped = stock.ship(conn, bid, res["reservation_id"], 10, "B-cw-ship")
        ev["M6"] = {"line_values": {f["label"]: cw["values"].get(f["key"]) for f in co.entity("Sales line")["fields"]
                                    if f["key"] in cw["values"]},
                    "engine_shipped_cases": shipped["shipped"],
                    "ship_params": eng["signatures"]["ship"]}
    conn.close()

    # ----- P1: rename, and a role attribute; last, since the rename changes labels -----
    ev["P1"]["role_probe"] = co.probe({"op": "add_field", "entity": co.ekey("Product"), "label": "Pack unit",
                                       "type": "text", "role": "unit"})
    ev["P1"]["rename_probe"] = co.probe({"op": "update_field", "entity": co.ekey("Product"),
                                         "field": co.fkey("Product", "Unit"), "label": "Selling unit"})
    ev["P1"]["found_by_label_unit"] = co.fkey("Product", "Unit") is not None
    ev["P1"]["spec_field_attributes"] = sorted(SpecField.model_fields)
    return ev


# ---------- classifying (parent process) ----------

def refused(probe: dict) -> bool:
    return not probe["accepted"]


def both(a, b, fn) -> bool:
    return fn(a) and fn(b)


def classify(row: str, a: dict, b: dict) -> tuple[set | None, str]:
    """Observed classes for a row and the evidence behind them. None means the
    row cannot be tested here."""
    ea, eb = a["engine"], b["engine"]
    cols = set(ea["columns"]["stock_movements"]) | set(ea["columns"]["stock_reservations"])
    lot_in_engine = any("lot" in c for c in cols) or ea["mentions"]["lot"]
    expiry_in_engine = any("expir" in c for c in cols) or ea["mentions"]["expiry"]

    if row == "D1":
        ok = both(a, b, lambda e: e["D1"]["round_trip"])
        types = sorted({t for _, t in a["D1"]["fields"]})
        return ({"pass"} if ok else {"config gap"},
                f"Product fields {[l for l, _ in a['D1']['fields']]} with types {types} built by chat in A and B; "
                f"values read back unchanged: {ok}")
    if row == "D2":
        return ({"pass"}, f"A: {a['D2']['supplier']} / {a['D2']['customers']}; "
                          f"B: {b['D2']['supplier']} / {b['D2']['customers']}")
    if row in ("D3", "D4"):
        ok = both(a, b, lambda e: e[row]["lines_on_" + ("po" if row == "D3" else "so")] == e[row]["expected"]
                  and e[row]["line_product"])
        what = "PO" if row == "D3" else "sales order"
        return ({"pass"} if ok else {"config gap"},
                f"line entity with many_to_one links to its {what} and to Product; linked-by on the order lists "
                f"exactly its lines and each line resolves its product, in A and B: {ok}")
    if row == "D5":
        ok = both(a, b, lambda e: all(e["D5"]["dates"]) and e["D5"]["links"] == ["Product", "Received on", "Supplier"])
        return ({"pass"} if ok else {"config gap"},
                f"lot stores dates A {a['D5']['dates']}, B {b['D5']['dates']} and links {a['D5']['links']}")
    if row == "D6":
        e = a["D6"]
        gap = both(a, b, lambda x: refused(x["D6"]["unique_probe"]) and refused(x["D6"]["unique_on_add"])
                   and x["D6"]["duplicate_same_product"] == 201)
        return ({"config gap"} if gap else {"pass"},
                f"'unique' refused by chat on update_field ({e['unique_probe']['reasons'][:1]}) and add_field; "
                f"a second lot with the same number for the same product was saved "
                f"(HTTP {e['duplicate_same_product']} in A, {b['D6']['duplicate_same_product']} in B)")
    if row == "D7":
        e = a["D7"]
        gap = both(a, b, lambda x: refused(x["D7"]["required_link_probe"])
                   and refused(x["D7"]["required_via_update_field"]) and x["D7"]["line_without_product"] == 201)
        return ({"config gap"} if gap else {"pass"},
                f"add_link with required refused ({e['required_link_probe']['reasons'][:1]}); update_field on the "
                f"link refused ({e['required_via_update_field']['reasons'][:1]}); a sales line with no product was "
                f"saved (HTTP {e['line_without_product']})")
    if row == "D8a":
        lossy = both(a, b, lambda x: refused(x["D8a"]["decimal_probe"]) and all(x["D8a"]["round_trip"])
                     and Decimal(x["D8a"]["float_sum"]) != Decimal(x["D8a"]["exact_sum"]))
        return ({"config gap"} if lossy else {"pass"},
                f"type 'decimal' refused ({a['D8a']['decimal_probe']['reasons'][:1]}); stored values read back "
                f"exactly (A {a['D8a']['stored']}, B {b['D8a']['stored']}); adding them: A {a['D8a']['float_sum']} "
                f"vs exact {a['D8a']['exact_sum']}, B {b['D8a']['float_sum']} vs exact {b['D8a']['exact_sum']}")
    if row == "D8b":
        wrong = a["D8b"]["status"] == 201 and a["D8b"]["stored"] == 15
        return ({"silently wrong"} if wrong else {"pass"},
                f"quantity \"1,5\" saved with HTTP {a['D8b']['status']}, stored as {a['D8b']['stored']}")
    if row == "D9":
        e = a["D9"]
        drift = both(a, b, lambda x: x["D9"]["probe"]["accepted"]
                     and x["D9"]["after_ship"]["lot_field"] != x["D9"]["after_ship"]["ledger_on_hand"]
                     and x["D9"]["after_edit"]["status"] == 200
                     and x["D9"]["after_edit"]["ledger_on_hand"] == x["D9"]["after_ship"]["ledger_on_hand"])
        return ({"silently wrong"} if drift else {"pass"},
                f"chat added Lot.Quantity (accepted: {e['probe']['accepted']}); set to 100, then the engine "
                f"received 100, reserved and shipped 30: field {e['after_ship']['lot_field']}, ledger on hand "
                f"{e['after_ship']['ledger_on_hand']}; editing the field to 250 was accepted (HTTP "
                f"{e['after_edit']['status']}) and moved nothing in the ledger ({e['after_edit']['ledger_on_hand']})")
    if row == "D10":
        e = a["D10"]
        config_wrong = both(a, b, lambda x: x["D10"]["link_holds_after_second_lot"] == "L2"
                            and x["D10"]["list_of_lots_status"] == 422
                            and x["D10"]["alloc_over_line_and_lot_status"] == 201)
        classes = {"module gap"} if (config_wrong and not lot_in_engine) else {"pass"}
        return (classes,
                f"config link Sales line -> Lot holds one lot: setting L2 replaced L1, a list was refused (HTTP "
                f"{e['list_of_lots_status']}), so a split line loses a lot. A config 'Lot allocation' entity (line, "
                f"lot, quantity) can be built by chat, but accepted 500 from a line of 5 and a lot nothing tracks "
                f"(HTTP {e['alloc_over_line_and_lot_status']}). Engine has a lot column or mention: {lot_in_engine}")
    if row == "D11":
        shape = both(a, b, lambda x: x["D11"]["lots_on_po_line"] == x["D11"]["expected"])
        stock_gap = not lot_in_engine and "lot" not in " ".join(ea["signatures"]["receive"]) \
            and ea["free_text_source_ref"] is None
        classes = ({"pass"} if shape else {"config gap"}) | ({"module gap"} if stock_gap else set())
        return (classes,
                f"two lots linked to one purchase line, listed by linked-by in A and B: {shape}; receive"
                f"{tuple(ea['signatures']['receive'][1:])} has no lot; source_ref 'any text at all, not a PO line' "
                f"accepted: {ea['free_text_source_ref'] is None}; movement columns {ea['columns']['stock_movements']}")
    if row == "R1":
        e = a["R1"]
        gap = both(a, b, lambda x: refused(x["R1"]["formula_type_probe"]) and refused(x["R1"]["formula_attr_probe"])
                   and "Line total" not in x["R1"]["line_value_keys"])
        return ({"config gap"} if gap else {"pass"},
                f"type 'formula' refused ({e['formula_type_probe']['reasons'][:1]}); a 'formula' attribute refused "
                f"({e['formula_attr_probe']['reasons'][:1]}); a saved line has only {e['line_value_keys']}, the "
                f"order only {e['order_value_keys']}: no total anywhere")
    if row == "R2":
        e = a["R2"]
        gap = both(a, b, lambda x: refused(x["R2"]["rule_probe"]) and x["R2"]["expiry_before_received"] == 201)
        return ({"config gap"} if gap else {"pass"},
                f"op add_rule refused ({e['rule_probe']['reasons'][:1]}); a lot received 2026-10-23 expiring "
                f"2026-10-01 was saved (HTTP {e['expiry_before_received']})")
    if row == "R3":
        e = a["R3"]
        field_ok = e["options"] == SO_STATUSES
        unenforced = both(a, b, lambda x: all(code == 200 for _, code in x["R3"]["transitions"])
                          and refused(x["R3"]["flow_probe"]))
        classes = ({"pass"} if field_ok else {"config gap"}) | \
            ({"module gap"} if unenforced and not ea["mentions"]["order status"] else set())
        return (classes,
                f"Status select holds {e['options']}; delivered -> draft -> cancelled -> shipped all saved "
                f"({[c for _, c in e['transitions']]}); a flow op refused ({e['flow_probe']['reasons'][:1]}); "
                f"engine mentions order statuses: {ea['mentions']['order status']}")
    if row == "Q1":
        e = a["Q1"]
        no_filter = both(a, b, lambda x: x["Q1"]["filtered_total"] == x["Q1"]["lots_total"] > len(x["Q1"]["truth"]))
        dates_ok = both(a, b, lambda x: x["Q1"]["sql_date_range"] == x["Q1"]["truth"])
        numbers_wrong = both(a, b, lambda x: x["Q1"]["sql_number_gt"]["got"] != x["Q1"]["sql_number_gt"]["truth"])
        classes = {"config gap"} if no_filter and e["exact_match_only"] else {"pass"}
        if not (dates_ok and numbers_wrong):
            classes.add("contradicted: range filter works on dates, not on numbers")
        return (classes,
                f"list endpoint params {e['list_params'][3:]}; with filter params it returned {e['filtered_total']} "
                f"of {e['lots_total']} lots (A; truth {len(e['truth'])}), B {b['Q1']['filtered_total']} of "
                f"{b['Q1']['lots_total']} (truth {len(b['Q1']['truth'])}); find_entity_ids_by_attribute is exact "
                f"match only: {e['exact_match_only']}. Raw SQL range on stored JSON text: dates right in A and B: "
                f"{dates_ok}; 'shelf life > {e['sql_number_gt']['cut']}' gave {e['sql_number_gt']['got']} vs "
                f"{e['sql_number_gt']['truth']} (A), {b['Q1']['sql_number_gt']['got']} vs "
                f"{b['Q1']['sql_number_gt']['truth']} (B)")
    if row == "Q2":
        e = a["Q2"]
        link_misses = both(a, b, lambda x: x["Q2"]["via_line_link"] != x["Q2"]["truth_for_L2"])
        classes = {"module gap", "plus D10"} if link_misses and not lot_in_engine else {"pass"}
        return (classes,
                f"who received lot L2 (truth {e['truth_for_L2']}): through the config line->lot link "
                f"{e['via_line_link']} (misses the split line); through hand-entered allocation rows "
                f"{e['via_hand_allocations']} (right only because the rows were typed in right; see D10); "
                f"engine has no lot: {not lot_in_engine}")
    if row == "P1":
        e = a["P1"]
        keys_differ = a["P1"]["unit_key"] != b["P1"]["unit_key"]
        tmpl_differ = a["P1"]["template_first_field"][2] != b["P1"]["template_first_field"][2]
        gap = keys_differ and tmpl_differ and both(a, b, lambda x: x["P1"]["rename_probe"]["accepted"]
                                                   and not x["P1"]["found_by_label_unit"]
                                                   and refused(x["P1"]["role_probe"]))
        return ({"config gap"} if gap else {"pass"},
                f"Product.Unit key A {a['P1']['unit_key']}, B {b['P1']['unit_key']}; the same template field "
                f"{e['template_first_field'][:2]} got A {a['P1']['template_first_field'][2]}, B "
                f"{b['P1']['template_first_field'][2]}; renaming Unit to 'Selling unit' accepted, then no field "
                f"labelled Unit; 'role' refused ({e['role_probe']['reasons'][:1]}); a field has only "
                f"{e['spec_field_attributes']}")
    if row == "M1":
        return ({"module gap"} if not lot_in_engine else {"pass"},
                f"movement columns {ea['columns']['stock_movements']}; reservation columns "
                f"{ea['columns']['stock_reservations']}; no lot anywhere. Using a lot id as item_id is accepted "
                f"(item_id is unchecked), but then nothing sums lots per product")
    if row == "M2":
        return ({"module gap"} if not (lot_in_engine or expiry_in_engine or ea["mentions"]["fefo"]) else {"pass"},
                f"reserve{tuple(ea['signatures']['reserve'][1:])}: no lot, no expiry, no ordering; engine mentions "
                f"expiry: {expiry_in_engine}, FEFO: {ea['mentions']['fefo']}")
    if row == "M3a":
        r = ea["expired_lot_reserve"]
        return ({"module gap"} if not expiry_in_engine and r["reserved"] == 5 else {"pass"},
                f"engine mentions expiry: {expiry_in_engine}; lot LX (expired 2026-10-17 in A, 2026-09-30 in B) "
                f"used as the item: reserve of 5 gave reserved {r['reserved']}, B {eb['expired_lot_reserve']['reserved']}")
    if row == "M3b":
        p1_gap = classify("P1", a, b)[0] == {"config gap"}
        field_ok = both(a, b, lambda x: x["M3b"]["hold_field_status"] == 200 and x["M3b"]["held"] is True)
        classes = {"module gap", "needs P1"} if not ea["mentions"]["hold"] and p1_gap else {"pass"}
        if not field_ok:
            classes.add("contradicted: hold set through a config field")
        return (classes,
                f"'On hold' boolean on Lot set by config in A and B: {field_ok}; engine mentions a hold: "
                f"{ea['mentions']['hold']}; the engine imports nothing from the core, so it would need P1 to find "
                f"the field, and P1 is a config gap: {p1_gap}")
    if row == "M4":
        p1_gap = classify("P1", a, b)[0] == {"config gap"}
        b_fits = ea["integer_receive"] is None and eb["integer_receive"] is None
        a_needs_scale = ea["fraction_receive"] is not None
        unchecked = ea["unknown_item_receive"] is None
        classes = {"module gap", "needs P1", "needs items from the core"} \
            if not (ea["mentions"]["unit"] or ea["mentions"]["scale"]) and p1_gap and unchecked else {"pass"}
        if not (b_fits and a_needs_scale):
            classes.add("contradicted: B fits today, only A needs a scale")
        return (classes,
                f"receive 1.5 (kg): {ea['fraction_receive']}; receive 40 (cases) accepted: {b_fits}; item 987654, "
                f"which exists nowhere, accepted: {unchecked}; engine mentions unit or scale: "
                f"{ea['mentions']['unit'] or ea['mentions']['scale']}")
    if row == "M5":
        p1_gap = classify("P1", a, b)[0] == {"config gap"}
        stored = a["M5"]["status"] == 200 and b["M5"]["status"] == 200
        no_engine = not (ea["mentions"]["customer"] or expiry_in_engine)
        classes = ({"config gap (P1)"} if p1_gap else set()) | ({"module gap"} if no_engine else set())
        if not stored:
            classes.add("contradicted: setting stored by config")
        return (classes or {"pass"},
                f"A stored '{a['M5']['form']}', B stored '{b['M5']['form']}' (number plus a days/percent select) "
                f"on Customer by config: {stored}; P1 config gap: {p1_gap}; engine mentions customer or expiry: "
                f"{not no_engine}. The two companies' rules differ in form; whether one module serves both forms "
                f"is not testable without the module")
    if row == "M6":
        e = b["M6"]
        line_ok = e["line_values"].get("Quantity") == 10 and e["line_values"].get("Weighed kg") == 183.4
        cases_ok = e["engine_shipped_cases"] == 10
        measured_in_ship = any("weigh" in prm or "measured" in prm for prm in e["ship_params"])
        classes = {"module gap"} if line_ok and cases_ok and not measured_in_ship else {"true break"}
        return (classes,
                f"B's line holds {e['line_values']} by config; the engine reserved and shipped 10 cases for B "
                f"({cases_ok}); ship{tuple(e['ship_params'][1:])} has no measured quantity, so the weight at "
                f"shipping is not in the module; the invoice amount (kg x price per kg) is R1. No step needed code "
                f"for one company")
    if row == "T1":
        return (None,
                f"precondition not met: P1 is a config gap and M1 to M4 are not built. Today, one engine serves "
                f"both: B's cases fit ({eb['integer_receive'] is None}), A's 1.5 kg does not "
                f"({ea['fraction_receive']})")
    raise KeyError(row)


ROWS = [
    # id, prediction as written in PREDICTIONS.md, the classes it names
    ("D1", "pass", {"pass"}),
    ("D2", "pass", {"pass"}),
    ("D3", "pass", {"pass"}),
    ("D4", "pass", {"pass"}),
    ("D5", "pass", {"pass"}),
    ("D6", "config gap", {"config gap"}),
    ("D7", "config gap", {"config gap"}),
    ("D8a", "config gap", {"config gap"}),
    ("D8b", "observed during review, not predicted", None),
    ("D9", "silently wrong", {"silently wrong"}),
    ("D10", "module gap", {"module gap"}),
    ("D11", "pass for the shape; module gap for the stock", {"pass", "module gap"}),
    ("R1", "config gap", {"config gap"}),
    ("R2", "config gap", {"config gap"}),
    ("R3", "pass for the field; module gap for the flow", {"pass", "module gap"}),
    ("Q1", "config gap", {"config gap"}),
    ("Q2", "module gap, plus D10", {"module gap", "plus D10"}),
    ("P1", "config gap", {"config gap"}),
    ("M1", "module gap", {"module gap"}),
    ("M2", "module gap", {"module gap"}),
    ("M3a", "module gap", {"module gap"}),
    ("M3b", "module gap, needs P1", {"module gap", "needs P1"}),
    ("M4", "module gap, needs P1 and items from the core", {"module gap", "needs P1", "needs items from the core"}),
    ("M5", "config gap (P1) plus module gap", {"config gap (P1)", "module gap"}),
    ("M6", "candidate true break", {"true break"}),
    ("T1", "pass, if P1 exists and units, shelf-life policy and FEFO are data, not code", {"pass"}),
]


# ---------- parent ----------

def git(*args) -> bytes:
    return subprocess.run(["git", *args], cwd=ROOT, capture_output=True, check=True).stdout


def company_process(name: str, engine_dir: str, workdir: str) -> dict:
    db_dir = tempfile.mkdtemp(prefix=f"company_{name}_", dir=workdir)
    out = os.path.join(db_dir, "evidence.json")
    env = {**os.environ, "DATABASE_PATH": os.path.join(db_dir, f"company_{name}.db"), "LLM_PROVIDER": "stub"}
    done = subprocess.run([sys.executable, "-B", os.path.abspath(__file__), "--company", name, engine_dir, out],
                          env=env, capture_output=True, text=True, timeout=300)
    if done.returncode != 0:
        sys.exit(f"company {name} run failed:\n{done.stdout}{done.stderr}")
    with open(out, encoding="utf-8") as f:
        return json.load(f)


def main():
    workdir = tempfile.mkdtemp(prefix="food_distributor_")
    try:
        engine_dir = os.path.join(workdir, "engine")
        os.mkdir(engine_dir)
        for path in ENGINE_FILES:
            with open(os.path.join(engine_dir, os.path.basename(path)), "wb") as f:
                f.write(git("show", f"{ENGINE_BRANCH}:{path}"))
        commit = git("rev-parse", "--short", ENGINE_BRANCH).decode().strip()
        a = company_process("A", engine_dir, workdir)
        b = company_process("B", engine_dir, workdir)
    finally:
        shutil.rmtree(workdir, ignore_errors=True)

    print(f"A: {a['business']} on {a['db']}; B: {b['business']} on {b['db']} (separate temporary databases)")
    print(f"stock engine: {ENGINE_BRANCH} at {commit}, run unchanged\n")
    tally = {"RIGHT": 0, "WRONG": 0, "UNTESTED": 0, "n/a": 0}
    for row, predicted, predicted_classes in ROWS:
        observed, evidence = classify(row, a, b)
        if predicted_classes is None:
            verdict = "n/a"
        elif observed is None:
            verdict = "UNTESTED"
        else:
            verdict = "RIGHT" if observed == predicted_classes else "WRONG"
        tally[verdict] += 1
        shown = "untestable here" if observed is None else ", ".join(sorted(observed))
        print(f"{row:<4} predicted: {predicted} | observed: {shown} | {verdict}")
        print(f"     {evidence}")
    print("\n" + ", ".join(f"{k} {v}" for k, v in tally.items()))
    return 0


if __name__ == "__main__":
    if CHILD:
        _, _, company, engine, out_path = sys.argv
        evidence = run_company(company, engine)
        with open(out_path, "w", encoding="utf-8") as f:
            json.dump(evidence, f)
    else:
        sys.exit(main())
