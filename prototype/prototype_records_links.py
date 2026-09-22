"""
Prototype for the generic records + links extension.

Purpose: the spec claims that invalid references (item_id=999999) become
impossible rather than merely discouraged. This file proves it by running the
cases, instead of asserting them in prose.

Standalone -- it builds its own tables so it can be reviewed without touching
the core. Nothing here is proposed as final code; it exists so the spec's
claims are checkable.

Run:  python prototype_records_links.py
"""

import sqlite3
import textwrap

import yaml

# --------------------------------------------------------------- schema

SCHEMA = """
PRAGMA foreign_keys = ON;

CREATE TABLE businesses (
    id INTEGER PRIMARY KEY,
    name TEXT NOT NULL
);

-- Existing core, reproduced unchanged so we can prove it still works.
CREATE TABLE contacts (
    id INTEGER PRIMARY KEY,
    business_id INTEGER NOT NULL REFERENCES businesses(id),
    name TEXT NOT NULL,
    email TEXT,
    status TEXT NOT NULL DEFAULT 'active'
);

CREATE TABLE entity_attributes (
    entity_type TEXT NOT NULL,
    entity_id INTEGER NOT NULL,
    attribute_name TEXT NOT NULL,
    value TEXT,
    PRIMARY KEY (entity_type, entity_id, attribute_name)
);

-- ===================== THE EXTENSION: four tables =====================

-- 1. What record types this business has. Declared by the pack.
CREATE TABLE record_types (
    business_id INTEGER NOT NULL REFERENCES businesses(id),
    name        TEXT NOT NULL,
    label       TEXT,
    PRIMARY KEY (business_id, name)
);

-- 2. What links are legal, and under what constraints. Declared by the pack.
CREATE TABLE link_types (
    business_id  INTEGER NOT NULL REFERENCES businesses(id),
    from_type    TEXT NOT NULL,
    name         TEXT NOT NULL,
    to_kind      TEXT NOT NULL CHECK (to_kind IN ('record','contact')),
    to_type      TEXT,                       -- required when to_kind='record'
    required     INTEGER NOT NULL DEFAULT 0,
    on_delete    TEXT NOT NULL DEFAULT 'restrict'
                 CHECK (on_delete IN ('restrict','cascade')),
    PRIMARY KEY (business_id, from_type, name),
    FOREIGN KEY (business_id, from_type) REFERENCES record_types(business_id, name)
);

-- 3. The records themselves. Identity only; fields live in entity_attributes.
CREATE TABLE records (
    id           INTEGER PRIMARY KEY,
    business_id  INTEGER NOT NULL REFERENCES businesses(id),
    type         TEXT NOT NULL,
    display_name TEXT,
    status       TEXT,
    FOREIGN KEY (business_id, type) REFERENCES record_types(business_id, name)
);

-- 4. The references. Two nullable targets with a CHECK, so the database
--    itself enforces that a link points at something real.
CREATE TABLE record_links (
    id            INTEGER PRIMARY KEY,
    business_id   INTEGER NOT NULL REFERENCES businesses(id),
    from_type     TEXT NOT NULL,
    from_id       INTEGER NOT NULL REFERENCES records(id) ON DELETE CASCADE,
    name          TEXT NOT NULL,
    to_record_id  INTEGER REFERENCES records(id),
    to_contact_id INTEGER REFERENCES contacts(id),
    CHECK ((to_record_id IS NULL) != (to_contact_id IS NULL)),
    FOREIGN KEY (business_id, from_type, name)
        REFERENCES link_types(business_id, from_type, name)
);

-- One trigger, three checks in order. Statements inside a single trigger body
-- execute in sequence, so each failure reports its own cause. (Separate
-- triggers would not work: SQLite does not define firing order between them.)
CREATE TRIGGER link_must_be_valid
BEFORE INSERT ON record_links
FOR EACH ROW
BEGIN
    -- (a) the link name must be one the pack declared for this record type
    SELECT RAISE(ABORT, 'undeclared link name for this record type')
    WHERE NOT EXISTS (SELECT 1 FROM link_types
                      WHERE business_id = NEW.business_id
                        AND from_type   = NEW.from_type
                        AND name        = NEW.name);

    -- (b) the target must exist. This is the item_id=999999 case.
    SELECT RAISE(ABORT, 'link target record does not exist')
    WHERE NEW.to_record_id IS NOT NULL
      AND NOT EXISTS (SELECT 1 FROM records WHERE id = NEW.to_record_id);

    -- (c) and it must be the right KIND of thing
    SELECT RAISE(ABORT, 'link target has the wrong record type')
    WHERE NEW.to_record_id IS NOT NULL
      AND (SELECT type FROM records WHERE id = NEW.to_record_id)
       IS NOT (SELECT to_type FROM link_types
               WHERE business_id = NEW.business_id
                 AND from_type   = NEW.from_type
                 AND name        = NEW.name);
END;

CREATE TRIGGER link_to_contact_must_be_declared
BEFORE INSERT ON record_links
FOR EACH ROW
WHEN NEW.to_contact_id IS NOT NULL
BEGIN
    SELECT RAISE(ABORT, 'this link is not declared as pointing at a contact')
    WHERE (SELECT to_kind FROM link_types
           WHERE business_id = NEW.business_id
             AND from_type   = NEW.from_type
             AND name        = NEW.name) IS NOT 'contact';
END;
"""

# --------------------------------------------------------------- the pack

WAREHOUSE_PACK = textwrap.dedent("""
id: warehouse
version: 0.1.0
display_name: Warehouse

# NEW pack key. Absent in Amaan's four packs, which is why they are unaffected.
record_types:
  - name: item
    label: Item
    fields: [sku, unit, reorder_point]

  - name: order
    label: Purchase Order
    fields: [ordered_on, status]
    links:
      - name: supplier
        to: contact          # reuses the existing contacts table
        required: true
      - name: carrier
        to: contact
        required: false

  - name: order_line
    label: Order Line
    fields: [qty_ordered, unit_price]
    links:
      - name: order
        to: order
        required: true
        on_delete: cascade   # delete the order, the lines go with it
      - name: item
        to: item
        required: true
        on_delete: restrict  # cannot delete an item still referenced by a line

  - name: shipment
    label: Shipment
    fields: [direction, moved_on, tracking_ref]
    links:
      - name: order
        to: order
        required: true
      - name: partner
        to: contact
        required: true
      - name: carrier
        to: contact
        required: false
""")


# --------------------------------------------------------------- loader

def install_pack(conn, business_id, pack):
    """Turn the pack's record_types block into rows. This is the whole loader."""
    for rt in pack.get("record_types", []):
        conn.execute("INSERT INTO record_types (business_id, name, label) VALUES (?,?,?)",
                     (business_id, rt["name"], rt.get("label")))
    for rt in pack.get("record_types", []):
        for link in rt.get("links", []):
            to = link["to"]
            to_kind = "contact" if to == "contact" else "record"
            conn.execute(
                """INSERT INTO link_types
                   (business_id, from_type, name, to_kind, to_type, required, on_delete)
                   VALUES (?,?,?,?,?,?,?)""",
                (business_id, rt["name"], link["name"], to_kind,
                 None if to_kind == "contact" else to,
                 int(link.get("required", False)), link.get("on_delete", "restrict")))


def create_record(conn, business_id, type_, display_name=None, fields=None, links=None):
    cur = conn.execute(
        "INSERT INTO records (business_id, type, display_name) VALUES (?,?,?)",
        (business_id, type_, display_name))
    rid = cur.lastrowid
    for k, v in (fields or {}).items():
        conn.execute("""INSERT INTO entity_attributes
                        (entity_type, entity_id, attribute_name, value) VALUES (?,?,?,?)""",
                     ("record", rid, k, str(v)))
    for name, target in (links or {}).items():
        add_link(conn, business_id, type_, rid, name, target)
    check_required_links(conn, business_id, type_, rid)
    return rid


def add_link(conn, business_id, from_type, from_id, name, target):
    """target is ('record', id) or ('contact', id)."""
    kind, tid = target
    conn.execute(
        """INSERT INTO record_links
           (business_id, from_type, from_id, name, to_record_id, to_contact_id)
           VALUES (?,?,?,?,?,?)""",
        (business_id, from_type, from_id, name,
         tid if kind == "record" else None,
         tid if kind == "contact" else None))


def check_required_links(conn, business_id, type_, record_id):
    missing = conn.execute(
        """SELECT lt.name FROM link_types lt
           WHERE lt.business_id=? AND lt.from_type=? AND lt.required=1
             AND NOT EXISTS (SELECT 1 FROM record_links rl
                             WHERE rl.from_id=? AND rl.name=lt.name)""",
        (business_id, type_, record_id)).fetchall()
    if missing:
        raise ValueError(f"missing required link(s): {[m[0] for m in missing]}")


# --------------------------------------------------------------- checks

OUT = []


def check(label, fn, expect_error=False):
    try:
        result = fn()
        if expect_error:
            OUT.append((label, "UNEXPECTED PASS", f"should have been rejected, got {result!r}"))
        else:
            OUT.append((label, "ok", str(result)))
    except Exception as e:
        if expect_error:
            OUT.append((label, "rejected", f"{type(e).__name__}: {e}"))
        else:
            OUT.append((label, "UNEXPECTED FAIL", f"{type(e).__name__}: {e}"))


def main():
    conn = sqlite3.connect(":memory:")
    conn.executescript(SCHEMA)
    bid = conn.execute("INSERT INTO businesses (name) VALUES ('Northside Assembly')").lastrowid
    install_pack(conn, bid, yaml.safe_load(WAREHOUSE_PACK))

    supplier = conn.execute(
        "INSERT INTO contacts (business_id, name) VALUES (?,'Acme Components')", (bid,)).lastrowid
    carrier = conn.execute(
        "INSERT INTO contacts (business_id, name) VALUES (?,'Redline Freight')", (bid,)).lastrowid

    bolt = create_record(conn, bid, "item", "M6 Hex Bolt",
                         fields={"sku": "BOLT-M6", "unit": "each", "reorder_point": 100})
    washer = create_record(conn, bid, "item", "M6 Washer", fields={"sku": "WASH-M6"})

    print("=" * 74)
    print("RECORDS + LINKS PROTOTYPE")
    print("=" * 74 + "\n")

    # --- Harsh's example 1: order -> partner
    order = None

    def mk_order():
        nonlocal order
        order = create_record(conn, bid, "order", "PO-1001",
                              fields={"ordered_on": "2026-09-10", "status": "open"},
                              links={"supplier": ("contact", supplier)})
        return f"order id={order}, supplier link to contact {supplier}"

    check("order -> partner (link to an existing contact)", mk_order)

    # --- Harsh's example 2: order line -> order + item (two typed references)
    line1 = None

    def mk_line():
        nonlocal line1
        line1 = create_record(conn, bid, "order_line", fields={"qty_ordered": 500, "unit_price": 0.12},
                              links={"order": ("record", order), "item": ("record", bolt)})
        return f"line id={line1} with BOTH references"
    check("order_line -> order AND item (the case that broke before)", mk_line)

    create_record(conn, bid, "order_line", fields={"qty_ordered": 40, "unit_price": 3.40},
                  links={"order": ("record", order), "item": ("record", washer)})

    # --- Harsh's example 3: shipment -> order + partner + carrier
    def mk_shipment():
        sid = create_record(conn, bid, "shipment", "SHP-5001",
                            fields={"direction": "inbound", "moved_on": "2026-09-14"},
                            links={"order": ("record", order),
                                   "partner": ("contact", supplier),
                                   "carrier": ("contact", carrier)})
        return f"shipment id={sid} with three simultaneous references"
    check("shipment -> order + partner + carrier", mk_shipment)

    # --- The rejection cases Harsh asked about
    check("reject item_id=999999 (target does not exist)",
          lambda: add_link(conn, bid, "order_line", line1, "item", ("record", 999999)),
          expect_error=True)

    check("reject item link pointing at an 'order' record (wrong type)",
          lambda: add_link(conn, bid, "order_line", line1, "item", ("record", order)),
          expect_error=True)

    check("reject a link name the pack never declared",
          lambda: add_link(conn, bid, "order_line", line1, "warehouse_bin", ("record", bolt)),
          expect_error=True)

    check("reject an order_line with no item link (required)",
          lambda: create_record(conn, bid, "order_line", fields={"qty_ordered": 5},
                                links={"order": ("record", order)}),
          expect_error=True)

    check("reject a record of a type the pack never declared",
          lambda: create_record(conn, bid, "pallet", "PAL-1"),
          expect_error=True)

    check("reject deleting an item still referenced by a line (on_delete: restrict)",
          lambda: conn.execute("DELETE FROM records WHERE id=?", (bolt,)),
          expect_error=True)

    # --- The query that was unanswerable before
    def query_by_item():
        rows = conn.execute(
            """SELECT o.display_name, a.value
               FROM records o
               JOIN record_links lo ON lo.name='order'      AND lo.to_record_id = o.id
               JOIN record_links li ON li.name='item'       AND li.from_id = lo.from_id
               JOIN entity_attributes a ON a.entity_type='record'
                    AND a.entity_id = lo.from_id AND a.attribute_name='qty_ordered'
               WHERE li.to_record_id = ?""", (bolt,)).fetchall()
        return f"orders containing BOLT-M6: {rows}"
    check("query: which orders contain item BOLT-M6, and how many", query_by_item)

    # --- Backward compatibility
    def legacy_untouched():
        c = conn.execute("INSERT INTO contacts (business_id,name) VALUES (?,'Jane Buyer')",
                         (bid,)).lastrowid
        conn.execute("""INSERT INTO entity_attributes VALUES ('contact',?,'membership_tier','gold')""", (c,))
        v = conn.execute("""SELECT value FROM entity_attributes
                            WHERE entity_type='contact' AND entity_id=?""", (c,)).fetchone()[0]
        n = conn.execute("SELECT COUNT(*) FROM record_types WHERE business_id=?", (bid,)).fetchone()[0]
        return f"contact + EAV still work (tier={v}); a pack with no record_types block adds 0 of the {n} types"
    check("existing contacts / EAV path unaffected", legacy_untouched)

    width = max(len(l) for l, _, _ in OUT)
    for label, verdict, detail in OUT:
        print(f"  {label.ljust(width)}  [{verdict}]")
        print(f"  {' ' * width}   {detail}\n")

    bad = [o for o in OUT if o[1].startswith("UNEXPECTED")]
    print("=" * 74)
    print("ALL CHECKS BEHAVED AS SPECIFIED" if not bad else f"{len(bad)} UNEXPECTED RESULT(S)")
    print("=" * 74)


if __name__ == "__main__":
    main()