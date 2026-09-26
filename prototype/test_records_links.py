"""
Focused tests for the records + links extension.

Covers the cases Harsh asked for:
  1. an absent record_types block registers nothing
  2. a valid declaration registers types and links
  3. an unknown record type is rejected
  4. an undeclared link is rejected
  5. a wrong target type is rejected
  6. a missing target (item_id=999999) is rejected

Plus the required-link check, and a guard that each rejection reports its own
cause rather than one generic message.

Standalone: builds its own in-memory database and imports nothing from
operations_nerd. Plain script with assertions, matching the convention in
operations_nerd/tests (no pytest dependency added).

Run:  python prototype/test_records_links.py
"""

import os
import sqlite3
import sys

import yaml

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from prototype_records_links import (
    SCHEMA, WAREHOUSE_PACK, install_pack, create_record, add_link,
)

failures = []
checks = 0


def check(label, condition, detail=""):
    global checks
    checks += 1
    if condition:
        print(f"  ok    {label}")
    else:
        print(f"  FAIL  {label}  {detail}")
        failures.append(label)


def rejects(label, fn, expect_message=None):
    """Assert fn raises, and optionally that it raises for the stated reason."""
    global checks
    checks += 1
    try:
        fn()
    except Exception as e:
        msg = str(e)
        if expect_message and expect_message not in msg:
            print(f"  FAIL  {label}  rejected, but for the wrong reason: {msg!r}")
            failures.append(label)
        else:
            print(f"  ok    {label}  ({msg})")
        return
    print(f"  FAIL  {label}  was accepted, should have been rejected")
    failures.append(label)


def fresh(with_pack=True):
    """A database with the schema, a business, and optionally the warehouse pack."""
    conn = sqlite3.connect(":memory:")
    conn.executescript(SCHEMA)
    bid = conn.execute("INSERT INTO businesses (name) VALUES ('Test Co')").lastrowid
    if with_pack:
        install_pack(conn, bid, yaml.safe_load(WAREHOUSE_PACK))
    return conn, bid


# ------------------------------------------------------------------ 1

def test_absent_record_types_registers_nothing():
    print("\n--- 1. absent record_types block ---")
    conn, bid = fresh(with_pack=False)

    # a pack shaped like Amaan's: no record_types key at all
    legacy_pack = yaml.safe_load("""
    id: healthclub
    version: 0.1.0
    display_name: Health Club
    """)
    install_pack(conn, bid, legacy_pack)

    n_types = conn.execute("SELECT COUNT(*) FROM record_types WHERE business_id=?",
                           (bid,)).fetchone()[0]
    n_links = conn.execute("SELECT COUNT(*) FROM link_types WHERE business_id=?",
                           (bid,)).fetchone()[0]
    check("a pack with no record_types registers 0 record types", n_types == 0, f"got {n_types}")
    check("a pack with no record_types registers 0 link types", n_links == 0, f"got {n_links}")

    # and the existing contact + EAV path is untouched
    cid = conn.execute("INSERT INTO contacts (business_id, name) VALUES (?, 'Jane')",
                       (bid,)).lastrowid
    conn.execute("INSERT INTO entity_attributes VALUES ('contact', ?, 'tier', 'gold')", (cid,))
    tier = conn.execute("""SELECT value FROM entity_attributes
                           WHERE entity_type='contact' AND entity_id=?""", (cid,)).fetchone()[0]
    check("contacts + entity_attributes still work alongside", tier == "gold")


# ------------------------------------------------------------------ 2

def test_valid_declaration_registers():
    print("\n--- 2. valid declaration ---")
    conn, bid = fresh()

    types = {r[0] for r in conn.execute(
        "SELECT name FROM record_types WHERE business_id=?", (bid,))}
    check("declared record types are registered",
          types == {"item", "order", "order_line", "shipment"}, f"got {sorted(types)}")

    links = {(r[0], r[1]) for r in conn.execute(
        "SELECT from_type, name FROM link_types WHERE business_id=?", (bid,))}
    for expected in [("order", "supplier"), ("order_line", "order"),
                     ("order_line", "item"), ("shipment", "order"),
                     ("shipment", "partner"), ("shipment", "carrier")]:
        check(f"link {expected[0]}.{expected[1]} is registered", expected in links)

    # required and on_delete survive the round trip
    row = conn.execute("""SELECT required, on_delete FROM link_types
                          WHERE business_id=? AND from_type='order_line' AND name='item'""",
                       (bid,)).fetchone()
    check("order_line.item is required", row[0] == 1)
    check("order_line.item is on_delete=restrict", row[1] == "restrict")

    # and the three shapes that failed in the encoding experiment now work
    supplier = conn.execute("INSERT INTO contacts (business_id,name) VALUES (?,'Acme')",
                            (bid,)).lastrowid
    carrier = conn.execute("INSERT INTO contacts (business_id,name) VALUES (?,'Redline')",
                           (bid,)).lastrowid
    bolt = create_record(conn, bid, "item", "M6 Bolt", fields={"sku": "BOLT-M6"})
    order = create_record(conn, bid, "order", "PO-1001",
                          links={"supplier": ("contact", supplier)})
    line = create_record(conn, bid, "order_line", fields={"qty_ordered": 500},
                         links={"order": ("record", order), "item": ("record", bolt)})
    shipment = create_record(conn, bid, "shipment", "SHP-5001",
                             links={"order": ("record", order),
                                    "partner": ("contact", supplier),
                                    "carrier": ("contact", carrier)})

    check("order -> partner", order is not None)
    check("order_line -> order AND item (two typed references)", line is not None)

    n = conn.execute("SELECT COUNT(*) FROM record_links WHERE from_id=?",
                     (shipment,)).fetchone()[0]
    check("shipment holds three simultaneous references", n == 3, f"got {n}")

    # the query that was unanswerable under the old encoding
    rows = conn.execute(
        """SELECT o.display_name FROM records o
           JOIN record_links lo ON lo.name='order' AND lo.to_record_id = o.id
           JOIN record_links li ON li.name='item'  AND li.from_id = lo.from_id
           WHERE li.to_record_id = ?""", (bolt,)).fetchall()
    check("query: which orders contain this item", rows == [("PO-1001",)], f"got {rows}")


# ------------------------------------------------------------------ 3 to 6

def test_rejections():
    print("\n--- 3 to 6. rejections ---")
    conn, bid = fresh()
    supplier = conn.execute("INSERT INTO contacts (business_id,name) VALUES (?,'Acme')",
                            (bid,)).lastrowid
    bolt = create_record(conn, bid, "item", "M6 Bolt", fields={"sku": "BOLT-M6"})
    order = create_record(conn, bid, "order", "PO-1001",
                          links={"supplier": ("contact", supplier)})
    line = create_record(conn, bid, "order_line", fields={"qty_ordered": 500},
                         links={"order": ("record", order), "item": ("record", bolt)})

    # 3. unknown record type
    rejects("unknown record type is rejected",
            lambda: create_record(conn, bid, "pallet", "PAL-1"),
            expect_message="FOREIGN KEY")

    # 4. undeclared link
    rejects("undeclared link name is rejected",
            lambda: add_link(conn, bid, "order_line", line, "warehouse_bin", ("record", bolt)),
            expect_message="undeclared link name")

    # 5. wrong target type
    rejects("link pointing at the wrong record type is rejected",
            lambda: add_link(conn, bid, "order_line", line, "item", ("record", order)),
            expect_message="wrong record type")

    # 6. missing target -- the case that succeeded silently before
    rejects("item_id=999999 is rejected (target does not exist)",
            lambda: add_link(conn, bid, "order_line", line, "item", ("record", 999999)),
            expect_message="does not exist")

    # required link missing
    rejects("order_line with no item link is rejected",
            lambda: create_record(conn, bid, "order_line", fields={"qty_ordered": 5},
                                  links={"order": ("record", order)}),
            expect_message="missing required link")

    # on_delete: restrict
    rejects("deleting an item a line still references is rejected",
            lambda: conn.execute("DELETE FROM records WHERE id=?", (bolt,)),
            expect_message="FOREIGN KEY")


def test_rejection_reasons_are_distinct():
    """
    Regression guard. An earlier version used three separate triggers, and
    SQLite does not define firing order between them, so all three failures
    reported the same misleading message. They now live in one trigger body
    with ordered statements. This test fails if that regresses.
    """
    print("\n--- distinct rejection reasons ---")
    conn, bid = fresh()
    supplier = conn.execute("INSERT INTO contacts (business_id,name) VALUES (?,'Acme')",
                            (bid,)).lastrowid
    bolt = create_record(conn, bid, "item", "M6 Bolt")
    order = create_record(conn, bid, "order", "PO-1001",
                          links={"supplier": ("contact", supplier)})
    line = create_record(conn, bid, "order_line", fields={"qty_ordered": 1},
                         links={"order": ("record", order), "item": ("record", bolt)})

    messages = []
    for name, target in [("warehouse_bin", ("record", bolt)),
                         ("item", ("record", 999999)),
                         ("item", ("record", order))]:
        try:
            add_link(conn, bid, "order_line", line, name, target)
            messages.append("NOT REJECTED")
        except Exception as e:
            messages.append(str(e))

    check("three different problems give three different messages",
          len(set(messages)) == 3, f"got {messages}")


def main():
    print("=" * 70)
    print("RECORDS + LINKS: focused tests")
    print("=" * 70)

    test_absent_record_types_registers_nothing()
    test_valid_declaration_registers()
    test_rejections()
    test_rejection_reasons_are_distinct()

    print("\n" + "=" * 70)
    if failures:
        print(f"{len(failures)} FAILED of {checks} checks:")
        for f in failures:
            print(f"  - {f}")
        print("=" * 70)
        sys.exit(1)
    print(f"ALL {checks} CHECKS PASSED")
    print("=" * 70)


if __name__ == "__main__":
    main()