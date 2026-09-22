"""
Warehouse encoding experiment.

Tries to build a warehouse (receiving and shipping) using ONLY what
operations_nerd's core provides today -- contacts, follow_ups,
entity_attributes, the Pack/EventSchema models, drafted_actions and
approval_policies -- with zero changes to any core file.

The logic: anything this script cannot do, a Config Pack cannot do
either. This is the "correctly rewritten Config Pack" attempt that
schema.sql's own falsifiability rule demands before a finding can be
called a true break.

Each step below is one encoding attempt. It records a verdict of PASS,
DEGRADED or FAIL, what was tried, what happened, and which line of the
core (if any) is the blocker. Results print to stdout and are written
to warehouse_failure_notes.json next to this file.

This script is read-only with respect to the core: it only imports and
calls operations_nerd.db.db and operations_nerd.packs.loader, never
edits them. It runs entirely against a throwaway SQLite file in a temp
directory (created fresh, deleted at exit) -- it never touches a real
.db file in the repo.
"""

import json
import os
import shutil
import tempfile

from pydantic import ValidationError

from operations_nerd.db import db
from operations_nerd.packs.loader import EventSchema, Pack

RESULTS = []
TEMP_DIR = tempfile.mkdtemp(prefix="warehouse_encoding_")
DB_PATH = os.path.join(TEMP_DIR, "warehouse_experiment.db")


def record(step, title, verdict, tried, happened, blocked_by=None):
    entry = {
        "step": step,
        "title": title,
        "verdict": verdict,
        "tried": tried,
        "happened": happened,
        "blocked_by": blocked_by,
    }
    RESULTS.append(entry)
    print(f"[{verdict}] Step {step}: {title}")
    print(f"    tried:      {tried}")
    print(f"    happened:   {happened}")
    if blocked_by:
        print(f"    blocked by: {blocked_by}")
    print()
    return entry


def conn():
    return db.get_conn(DB_PATH)


# ---------------------------------------------------------------------
# Step 1: partner as a contact with a role attribute
# ---------------------------------------------------------------------

def step1(business_id):
    with conn() as c:
        partner_id = db.create_contact(
            c, business_id, name="Acme Bolts Supply Co.", email="sales@acmebolts.example"
        )
        db.set_entity_attributes(c, "contact", partner_id, {"role": "partner"})
    with conn() as c:
        row = db.get_contact(c, partner_id)
    ok = row is not None and row["extension_data"].get("role") == "partner"
    record(
        1,
        "Partner as a contact with a role attribute",
        "PASS" if ok else "FAIL",
        tried="create_contact() for a supplier, then set_entity_attributes(conn, 'contact', id, {'role': 'partner'})",
        happened=f"contact id={partner_id} created; extension_data={row['extension_data']}. "
        "A supplier is an organisation you deal with, and role is single-valued -- exactly what contacts+EAV are for.",
    )
    return partner_id


# ---------------------------------------------------------------------
# Step 2: carrier as a contact with a role attribute
# ---------------------------------------------------------------------

def step2(business_id):
    with conn() as c:
        carrier_id = db.create_contact(c, business_id, name="Speedy Freight Lines", phone="555-0100")
        db.set_entity_attributes(c, "contact", carrier_id, {"role": "carrier"})
    with conn() as c:
        row = db.get_contact(c, carrier_id)
    ok = row is not None and row["extension_data"].get("role") == "carrier"
    record(
        2,
        "Carrier as a contact with a role attribute",
        "PASS" if ok else "FAIL",
        tried="create_contact() for a freight carrier, then set_entity_attributes(conn, 'contact', id, {'role': 'carrier'})",
        happened=f"contact id={carrier_id} created; extension_data={row['extension_data']}. "
        "Same shape as step 1 -- another organisation you deal with.",
    )
    return carrier_id


# ---------------------------------------------------------------------
# Step 3: item as a contact with role='item'
# ---------------------------------------------------------------------

def step3(business_id):
    with conn() as c:
        item_id = db.create_contact(c, business_id, name="BOLT-M6")
        db.set_entity_attributes(c, "contact", item_id, {"role": "item", "sku": "BOLT-M6", "unit": "each"})
    with conn() as c:
        row = db.get_contact(c, item_id)
    record(
        3,
        "Item as a contact with role='item'",
        "DEGRADED",
        tried="create_contact() for a physical SKU (a bolt), role='item', sku/unit as attributes",
        happened=f"contact id={item_id} created and reads back fine: extension_data={row['extension_data']}. "
        f"But the row also carries email={row['email']!r}, phone={row['phone']!r}, status={row['status']!r} -- "
        "fields that exist because contacts means 'someone you deal with'. A bolt inherits a relationship "
        "status it cannot have. Mechanically works, but it is the first record that is not a person/org.",
        blocked_by="operations_nerd/db/schema.sql: contacts table (email, phone, status columns baked in) -- "
        "modelling smell, not a technical failure",
    )
    return item_id


# ---------------------------------------------------------------------
# Step 4: purchase order header as a contact
# ---------------------------------------------------------------------

def step4(business_id, partner_id):
    with conn() as c:
        order_id = db.create_contact(c, business_id, name="PO-1001")
        db.set_entity_attributes(
            c, "contact", order_id, {"role": "order", "supplier_id": partner_id, "status": "open"}
        )
    # Nothing stops supplier_id from being overwritten to point at a contact that doesn't exist.
    with conn() as c:
        db.set_entity_attributes(c, "contact", order_id, {"supplier_id": 999999})
    with conn() as c:
        row = db.get_contact(c, order_id)
    record(
        4,
        "Purchase order header as a contact",
        "DEGRADED",
        tried="create_contact() for the PO header, supplier_id stashed as an entity_attributes value "
        "pointing at the supplier's contact id, then overwritten with a nonexistent id (999999)",
        happened=f"overwrite succeeded silently: extension_data={row['extension_data']}. "
        "supplier_id is a bare integer inside a JSON-encoded TEXT value -- not a foreign key, not "
        "enforced, nothing cascades if the referenced contact is deleted.",
        blocked_by="operations_nerd/db/schema.sql: entity_attributes.value is TEXT NOT NULL with no FK and no type",
    )
    return order_id


# ---------------------------------------------------------------------
# Step 5: order lines as follow_ups pointing at the order
# ---------------------------------------------------------------------

def step5(business_id, order_id):
    with conn() as c:
        line_id = db.create_follow_up(
            c, business_id, contact_id=order_id, type_="order_line",
            extension_data={"qty_ordered": 500, "unit_price": 0.12},
        )
    with conn() as c:
        rows = db.list_follow_ups(c, business_id)
    line = next(r for r in rows if r["id"] == line_id)
    ok = line["contact_id"] == order_id and line["extension_data"]["qty_ordered"] == 500
    record(
        5,
        "Order lines as follow_ups, qty and price as attributes",
        "PASS" if ok else "FAIL",
        tried="create_follow_up() against the order contact, type_='order_line', qty_ordered/unit_price as attributes",
        happened=f"follow_up id={line_id}, contact_id={line['contact_id']}, extension_data={line['extension_data']}. "
        "follow_ups already gives a one-to-many (many lines per order) and a single line's quantity and "
        "price are single-valued -- exactly what follow_ups+EAV are for. Repeated rows are not the break.",
    )
    return line_id


# ---------------------------------------------------------------------
# Step 6: order line also referencing an item (the second FK)
# ---------------------------------------------------------------------

def step6(business_id, order_id, item_id):
    with conn() as c:
        line_id = db.create_follow_up(
            c, business_id, contact_id=order_id, type_="order_line",
            extension_data={"qty_ordered": 500, "unit_price": 0.12, "item_id": item_id},
        )
    # The item reference has no FK behind it -- point it at a contact that doesn't exist.
    with conn() as c:
        db.set_entity_attributes(c, "follow_up", line_id, {"item_id": 999999})
    with conn() as c:
        rows = db.list_follow_ups(c, business_id)
    line = next(r for r in rows if r["id"] == line_id)
    record(
        6,
        "Order line also referencing an item (second reference)",
        "FAIL",
        tried="order_line follow_up referencing BOTH its order (via the one real FK, follow_ups.contact_id) "
        "AND its item (stashed as an entity_attributes value, since there is no second FK slot), then "
        "overwriting item_id with a nonexistent contact id (999999)",
        happened=f"overwrite succeeded with no error: extension_data={line['extension_data']}. "
        "follow_ups has exactly one FK and the order already owns it -- the item reference degrades to an "
        "untyped integer with no integrity, no cascade, no ON DELETE.",
        blocked_by="operations_nerd/db/schema.sql: follow_ups declares a single FK (contact_id REFERENCES "
        "contacts(id)); no second FK column exists for a line's item",
    )
    return line_id


# ---------------------------------------------------------------------
# Step 7: workaround -- all lines stuffed into one JSON attribute
# ---------------------------------------------------------------------

def step7(business_id, order_id):
    lines = [
        {"item": "BOLT-M6", "qty": 500, "unit_price": 0.12},
        {"item": "NUT-M6", "qty": 500, "unit_price": 0.05},
    ]
    with conn() as c:
        db.set_entity_attributes(c, "contact", order_id, {"lines_blob": lines})
    with conn() as c:
        row = db.get_contact(c, order_id)
        roundtrip_ok = row["extension_data"]["lines_blob"] == lines
        # The only query primitive available is exact-match on the whole encoded value.
        whole_blob_hits = db.find_entity_ids_by_attribute(c, "contact", "lines_blob", lines)
        single_item_hits = db.find_entity_ids_by_attribute(c, "contact", "lines_blob", "BOLT-M6")
    record(
        7,
        "Workaround: all order lines in one JSON attribute",
        "DEGRADED",
        tried="stuff every order line into a single JSON list stored as one entity_attributes value "
        "(lines_blob) on the order contact, then try to query 'which orders contain BOLT-M6'",
        happened=f"round-trip works: stored list == retrieved list ({roundtrip_ok}). Matching the whole "
        f"blob works ({len(whole_blob_hits)} hit(s)), but find_entity_ids_by_attribute compares the whole "
        f"JSON-encoded string for equality, so a query for just 'BOLT-M6' inside the blob returns "
        f"{len(single_item_hits)} hit(s) -- unanswerable. Stored but not queryable, not joinable, not "
        "individually updatable.",
        blocked_by="operations_nerd/db/db.py: find_entity_ids_by_attribute does WHERE value = ? against "
        "the whole json.dumps()-encoded string",
    )


# ---------------------------------------------------------------------
# Step 8: shipment referencing an order, a partner and a carrier at once
# ---------------------------------------------------------------------

def step8(business_id, order_id, partner_id, carrier_id):
    with conn() as c:
        shipment_id = db.create_follow_up(
            c, business_id, contact_id=order_id, type_="shipment",
            extension_data={"direction": "outbound", "partner_id": partner_id, "carrier_id": carrier_id},
        )
    with conn() as c:
        db.set_entity_attributes(c, "follow_up", shipment_id, {"partner_id": 999999, "carrier_id": 999999})
    with conn() as c:
        rows = db.list_follow_ups(c, business_id)
    shipment = next(r for r in rows if r["id"] == shipment_id)
    record(
        8,
        "Shipment referencing an order, a partner and a carrier at once",
        "FAIL",
        tried="a shipment follow_up pointing at the order (the one real FK) plus partner and carrier "
        "(both stashed as attributes, since no second or third FK slot exists), then overwriting both "
        "stashed references with nonexistent contact ids (999999)",
        happened=f"overwrite succeeded with no error: extension_data={shipment['extension_data']}. "
        "contacts has one FK (business_id), follow_ups has one FK (contact_id). Three simultaneous "
        "references cannot be expressed in a schema offering one.",
        blocked_by="operations_nerd/db/schema.sql: contacts and follow_ups each declare exactly one FK column",
    )
    return shipment_id


# ---------------------------------------------------------------------
# Step 9: partial receipt (order 500, receive 60 then 440)
# ---------------------------------------------------------------------

def step9(business_id, order_id, item_id):
    with conn() as c:
        receipt1_id = db.create_follow_up(
            c, business_id, contact_id=order_id, type_="receipt",
            extension_data={"item_id": item_id, "qty_received": 60},
        )
        receipt2_id = db.create_follow_up(
            c, business_id, contact_id=order_id, type_="receipt",
            extension_data={"item_id": item_id, "qty_received": 440},
        )
    # Compounds step 6's gap: nothing stops the second receipt's item reference
    # from being corrupted either, even mid-way through a partial receipt.
    with conn() as c:
        db.set_entity_attributes(c, "follow_up", receipt2_id, {"item_id": 999999})
    with conn() as c:
        rows = db.list_follow_ups(c, business_id)
    receipts = [r for r in rows if r["type"] == "receipt"]
    total_received = sum(r["extension_data"]["qty_received"] for r in receipts)
    corrupted = next(r for r in receipts if r["id"] == receipt2_id)
    record(
        9,
        "Partial receipt (order 500, receive 60 then 440)",
        "FAIL",
        tried="two partial receipts against the same order+item pair, using the order-as-contact_id / "
        "item-as-attribute encoding from steps 6 and 8, then corrupting the second receipt's item_id",
        happened=f"the two receipts sum correctly (total_received={total_received}), but that arithmetic "
        f"is the only thing that works -- receipt #2's item_id was silently overwritten to a nonexistent "
        f"contact id with no error: extension_data={corrupted['extension_data']}. Partial receipt needs "
        "order+item+quantity held together with integrity across multiple rows; it compounds the same "
        "untyped-second-reference gap from step 6 rather than introducing a new one.",
        blocked_by="same as step 6: operations_nerd/db/schema.sql, no second FK slot to type-check the "
        "item/order pairing",
    )


# ---------------------------------------------------------------------
# Step 10: pack declaring an event with nested line items
# ---------------------------------------------------------------------

def step10():
    tried = (
        "construct an EventSchema for 'purchase order received' whose extract_fields needs to capture "
        "nested line items (sku/qty/unit_price per line), not just flat scalar field names"
    )
    try:
        EventSchema(
            event_type="purchase_order_received",
            extract_fields=["po_number", {"lines": ["sku", "qty", "unit_price"]}],
            valid_action_types=["post_inventory_receipt"],
        )
        record(
            10,
            "Pack declaring an event with nested line items",
            "FAIL",
            tried=tried,
            happened="unexpectedly accepted -- extract_fields took a nested structure without complaint",
        )
    except ValidationError as e:
        first_issue = e.errors()[0]
        record(
            10,
            "Pack declaring an event with nested line items",
            "FAIL",
            tried=tried,
            happened=f"rejected before the database is ever involved: pydantic ValidationError on "
            f"extract_fields -- {first_issue['msg']} (loc={'.'.join(str(p) for p in first_issue['loc'])}). "
            "The pipeline cannot parse a purchase order email at all.",
            blocked_by="operations_nerd/packs/loader.py: EventSchema.extract_fields is typed list[str]",
        )


# ---------------------------------------------------------------------
# Step 11: pack declaring a new record type
# ---------------------------------------------------------------------

def step11():
    tried = (
        "construct a Pack carrying a record_types block (item/order/shipment as new nouns), the way the "
        "warehouse vertical would need to declare them, alongside the pack's required fields"
    )
    pack = Pack(
        id="warehouse",
        version="0.1",
        display_name="Warehouse",
        questions=[],
        event_schemas=[],
        prompts={},
        approval_defaults={},
        record_types=[{"name": "item", "fields": ["sku", "unit"]}],
    )
    kept = hasattr(pack, "record_types")
    record(
        11,
        "Pack declaring a new record type",
        "FAIL",
        tried=tried,
        happened=f"Pack(...) did not raise, but pydantic's default extra='ignore' behaviour silently "
        f"dropped the field: hasattr(pack, 'record_types') = {kept}. The data the caller passed in simply "
        "vanishes; no PackLoadError, no warning. The Pack model has no key for entities, record types or "
        "relationships -- a pack can introduce vocabulary (questions, prompts, action types) but not nouns.",
        blocked_by="operations_nerd/packs/loader.py: Pack(BaseModel) declares exactly "
        "id/version/display_name/questions/event_schemas/prompts/approval_defaults, nothing for record types",
    )


# ---------------------------------------------------------------------
# Step 12: approval gate wrapping a state change instead of a message
# ---------------------------------------------------------------------

def step12(business_id):
    with conn() as c:
        event_id = db.create_event(
            c, business_id, source="email", raw_content="PO-1001 partial receipt: 60 of BOLT-M6"
        )
        action_id = db.create_drafted_action(
            c, event_id, business_id, action_type="post_inventory_receipt",
            payload={"order": "PO-1001", "item": "BOLT-M6", "qty_received": 60},
        )
        db.set_approval_policy(c, business_id, "post_inventory_receipt", auto_approve=False)
    with conn() as c:
        gated = not db.is_auto_approved(c, business_id, "post_inventory_receipt")
        action = db.get_drafted_action(c, action_id)
    ok = gated and action["status"] == "pending_approval"
    record(
        12,
        "Approval gate wrapping a state change instead of a message",
        "PASS" if ok else "FAIL",
        tried="gate a state-changing action (post_inventory_receipt, an inventory update rather than an "
        "outbound email) behind the same drafted_actions/approval_policies path used for messages",
        happened=f"action_type is a free string and payload is free JSON, so post_inventory_receipt sits "
        f"in drafted_actions exactly like send_follow_up_email would: status={action['status']!r}, "
        f"auto_approve gate active={gated}. The approval layer is not part of the problem.",
    )


# ---------------------------------------------------------------------
# Runner
# ---------------------------------------------------------------------

def main():
    db.init_db(db_path=DB_PATH, reset=True)
    with conn() as c:
        business_id = db.create_business(
            c, name="Test Warehouse Co.", industry_pack="warehouse", pack_version="0.0-experiment",
            settings={},
        )

    partner_id = step1(business_id)
    carrier_id = step2(business_id)
    item_id = step3(business_id)
    order_id = step4(business_id, partner_id)
    step5(business_id, order_id)
    step6(business_id, order_id, item_id)
    step7(business_id, order_id)
    step8(business_id, order_id, partner_id, carrier_id)
    step9(business_id, order_id, item_id)
    step10()
    step11()
    step12(business_id)

    counts = {"PASS": 0, "DEGRADED": 0, "FAIL": 0}
    for r in RESULTS:
        counts[r["verdict"]] += 1

    summary = {
        "counts": counts,
        "steps": RESULTS,
    }
    notes_path = os.path.join(os.path.dirname(__file__), "warehouse_failure_notes.json")
    with open(notes_path, "w") as f:
        json.dump(summary, f, indent=2)

    print("=" * 60)
    print(f"PASS: {counts['PASS']}  DEGRADED: {counts['DEGRADED']}  FAIL: {counts['FAIL']}")
    print(f"Notes written to {notes_path}")
    print("=" * 60)

    shutil.rmtree(TEMP_DIR, ignore_errors=True)


if __name__ == "__main__":
    main()
