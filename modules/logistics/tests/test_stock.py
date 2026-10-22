"""
Stock engine v1: commands, idempotency, concurrency and the database rules.
Tests marked "raw" write SQL directly, bypassing stock.py, so whatever they see
rejected is rejected by the database itself. Each test gets its own throwaway
database under a temp directory. Run from the repo root with the venv:

    python -B modules/logistics/tests/test_stock.py
"""

import os
import sqlite3
import sys
import tempfile
import threading
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import stock as s

TMP = tempfile.TemporaryDirectory(prefix="logistics_stock_", ignore_cleanup_errors=True)
B1, B2 = 1, 2
ITEM = 101
TRIALS = 10


def new_db(name: str) -> tuple[sqlite3.Connection, str]:
    path = os.path.join(TMP.name, name + ".db")
    conn = s.connect(path)
    s.init_db(conn)
    return conn, path


def dump(conn) -> list:
    """Every row of every table, to show that something wrote nothing."""
    return [tuple(r) for t in ("stock_commands", "stock_movements", "stock_reservations")
            for r in conn.execute(f"SELECT * FROM {t} ORDER BY id")]


def levels(conn, business_id=B1, item_id=ITEM) -> tuple[int, int, int]:
    return (s.on_hand(conn, business_id, item_id), s.reserved(conn, business_id, item_id),
            s.available(conn, business_id, item_id))


def raw_movement(conn, quantity, movement_type, key, business_id=B1, item_id=ITEM, location_id=1, reservation_id=None):
    return conn.execute(
        """INSERT INTO stock_movements (business_id, item_id, location_id, quantity, movement_type, reservation_id,
                                        source_ref, idempotency_key)
           VALUES (?, ?, ?, ?, ?, ?, 'raw', ?)""",
        (business_id, item_id, location_id, quantity, movement_type, reservation_id, key)).lastrowid


def shipped_and_status(conn, reservation_id) -> tuple[int, str]:
    return tuple(conn.execute("SELECT quantity_shipped, status FROM stock_reservations WHERE id = ?",
                              (reservation_id,)).fetchone())


def raw_reservation(conn, quantity, key, business_id=B1, item_id=ITEM, status="active", shipped=0):
    return conn.execute(
        """INSERT INTO stock_reservations (business_id, item_id, location_id, order_ref, quantity, quantity_shipped,
                                           status, idempotency_key)
           VALUES (?, ?, 1, 'raw', ?, ?, ?, ?)""", (business_id, item_id, quantity, shipped, status, key)).lastrowid


def rejected(fn, expected: str, error=sqlite3.IntegrityError) -> None:
    try:
        fn()
    except error as e:
        assert expected in str(e), f"rejected for the wrong reason: {e!r}"
        return
    raise AssertionError(f"expected {error.__name__} ({expected!r}), but it was accepted")


# ---------------------------------------------------------------- idempotency

def test_receiving_the_same_delivery_twice_with_one_key_counts_once():
    conn, _ = new_db("receive_twice")
    first = s.receive(conn, B1, ITEM, 10, "po:7/line:1", "dlv-1")
    before = dump(conn)
    again = s.receive(conn, B1, ITEM, 10, "po:7/line:1", "dlv-1")
    assert again == first, (first, again)
    assert dump(conn) == before                                           # the replay wrote nothing
    assert levels(conn) == (10, 0, 10)
    assert conn.execute("SELECT COUNT(*) FROM stock_movements").fetchone()[0] == 1


def test_replays_of_every_command_return_the_original_result_without_writing():
    conn, _ = new_db("replays")
    s.receive(conn, B1, ITEM, 5, "po:1/line:1", "rcv")
    res = s.reserve(conn, B1, ITEM, 8, "order:1", "res")
    assert res["reserved"] == 5 and res["shortage"] == 3
    s.receive(conn, B1, ITEM, 20, "po:1/line:2", "rcv-2")                  # stock changes after the reserve
    shp = s.ship(conn, B1, res["reservation_id"], 2, "shp")
    rel = s.release(conn, B1, res["reservation_id"], "rel")
    before = dump(conn)
    assert s.reserve(conn, B1, ITEM, 8, "order:1", "res") == res           # still the original shortage, not a fresh reserve
    assert s.ship(conn, B1, res["reservation_id"], 2, "shp") == shp         # does not ship twice
    assert s.release(conn, B1, res["reservation_id"], "rel") == rel         # replays even though the reservation is now released
    assert dump(conn) == before
    assert levels(conn) == (23, 0, 23)


def test_same_key_with_a_different_payload_is_rejected():
    conn, _ = new_db("key_conflict")
    s.receive(conn, B1, ITEM, 5, "po:1/line:1", "k")
    rid = s.reserve(conn, B1, ITEM, 2, "order:1", "r")["reservation_id"]
    before = dump(conn)
    rejected(lambda: s.receive(conn, B1, ITEM, 6, "po:1/line:1", "k"), "already used", s.IdempotencyConflict)
    rejected(lambda: s.receive(conn, B1, ITEM, 5, "po:1/line:2", "k"), "already used", s.IdempotencyConflict)
    rejected(lambda: s.receive(conn, B1, ITEM + 1, 5, "po:1/line:1", "k"), "already used", s.IdempotencyConflict)
    rejected(lambda: s.reserve(conn, B1, ITEM, 5, "order:1", "k"), "already used", s.IdempotencyConflict)
    rejected(lambda: s.reserve(conn, B1, ITEM, 3, "order:1", "r"), "already used", s.IdempotencyConflict)
    rejected(lambda: s.ship(conn, B1, rid, 1, "k"), "already used", s.IdempotencyConflict)
    rejected(lambda: s.release(conn, B1, rid, "k"), "already used", s.IdempotencyConflict)
    assert dump(conn) == before
    assert levels(conn) == (5, 2, 3)


def test_raw_a_key_is_unique_per_business_in_every_table():
    conn, _ = new_db("raw_keys")
    raw_movement(conn, 5, "receipt", "k")
    rejected(lambda: raw_movement(conn, 5, "receipt", "k"), "UNIQUE constraint failed: stock_movements")
    raw_reservation(conn, 1, "k")
    rejected(lambda: raw_reservation(conn, 1, "k"), "UNIQUE constraint failed: stock_reservations")
    cmd = "INSERT INTO stock_commands (business_id, idempotency_key, command, payload, result) VALUES (?, 'k', 'receive', '{}', '{}')"
    conn.execute(cmd, (B1,))
    rejected(lambda: conn.execute(cmd, (B1,)), "UNIQUE constraint failed: stock_commands")
    raw_movement(conn, 5, "receipt", "k", business_id=B2)                    # the same key is fine in another business
    raw_reservation(conn, 1, "k", business_id=B2)
    conn.execute(cmd, (B2,))


def test_raw_keys_refs_and_command_names_cannot_be_blank():
    conn, _ = new_db("raw_blank")
    raw_movement(conn, 5, "receipt", "seed")
    rejected(lambda: raw_movement(conn, 5, "receipt", ""), "CHECK constraint failed: idempotency_key")
    rejected(lambda: raw_reservation(conn, 1, ""), "CHECK constraint failed: idempotency_key")
    cmd = "INSERT INTO stock_commands (business_id, idempotency_key, command, payload, result) VALUES (1, ?, ?, '{}', '{}')"
    rejected(lambda: conn.execute(cmd, ("", "receive")), "CHECK constraint failed: idempotency_key")
    rejected(lambda: conn.execute(cmd, ("c", "teleport")), "CHECK constraint failed: command")
    rejected(lambda: conn.execute(
        """INSERT INTO stock_movements (business_id, item_id, location_id, quantity, movement_type, source_ref, idempotency_key)
           VALUES (1, 1, 1, 5, 'receipt', '', 'm')"""), "CHECK constraint failed: source_ref")
    rejected(lambda: conn.execute(
        """INSERT INTO stock_reservations (business_id, item_id, location_id, order_ref, quantity, idempotency_key)
           VALUES (1, ?, 1, '', 1, 'r')""", (ITEM,)), "CHECK constraint failed: order_ref")


# ---------------------------------------------------------------- reserve

def test_two_concurrent_reserves_for_the_last_units_never_exceed_on_hand():
    setup, path = new_db("race")
    original = s.available

    def slow_available(conn, business_id, item_id):
        n = original(conn, business_id, item_id)
        time.sleep(0.05)                                                     # widen the gap between the check and the write
        return n
    s.available = slow_available
    try:
        for trial in range(TRIALS):
            item = 1000 + trial
            s.receive(setup, B1, item, 5, f"po:{trial}/line:1", f"rcv-{trial}")
            barrier, results, errors = threading.Barrier(2), [], []

            def go(n):
                conn = s.connect(path)                                       # one connection per thread
                try:
                    barrier.wait()
                    results.append(s.reserve(conn, B1, item, 3, f"order:{trial}-{n}", f"res-{trial}-{n}"))
                except Exception as e:
                    errors.append(e)
                finally:
                    conn.close()
            threads = [threading.Thread(target=go, args=(n,)) for n in range(2)]
            [t.start() for t in threads]
            [t.join() for t in threads]
            assert not errors, (trial, errors)
            assert sorted((r["reserved"], r["shortage"]) for r in results) == [(2, 1), (3, 0)], (trial, results)
            assert levels(setup, B1, item) == (5, 5, 0), (trial, levels(setup, B1, item))
    finally:
        s.available = original


def test_reserve_more_than_available_returns_the_shortage():
    conn, _ = new_db("shortage")
    s.receive(conn, B1, ITEM, 10, "po:1/line:1", "rcv")
    assert s.reserve(conn, B1, ITEM, 4, "order:1", "r1")["shortage"] == 0
    r2 = s.reserve(conn, B1, ITEM, 9, "order:2", "r2")
    assert (r2["requested"], r2["reserved"], r2["shortage"]) == (9, 6, 3), r2
    before = dump(conn)
    r3 = s.reserve(conn, B1, ITEM, 2, "order:3", "r3")                       # nothing left: no reservation row
    assert (r3["reservation_id"], r3["reserved"], r3["shortage"]) == (None, 0, 2), r3
    assert len(dump(conn)) == len(before) + 1                                # only the command record
    assert levels(conn) == (10, 10, 0)
    r4 = s.reserve(conn, B1, ITEM + 1, 3, "order:4", "r4")                   # an item never received
    assert (r4["reserved"], r4["shortage"]) == (0, 3), r4


def test_raw_reservations_cannot_exceed_on_hand():
    conn, _ = new_db("raw_reserve")
    raw_movement(conn, 5, "receipt", "m1")
    raw_reservation(conn, 3, "r1")
    rejected(lambda: raw_reservation(conn, 3, "r2"), "reservation exceeds available stock")
    raw_reservation(conn, 2, "r3")
    rejected(lambda: raw_reservation(conn, 1, "r4", item_id=ITEM + 1), "reservation exceeds available stock")
    rejected(lambda: raw_reservation(conn, 0, "r5"), "CHECK constraint failed: quantity > 0")
    assert levels(conn) == (5, 5, 0)


# ---------------------------------------------------------------- receive, ship, release

def test_partial_receipt_and_partial_shipment():
    conn, _ = new_db("partial")
    s.receive(conn, B1, ITEM, 4, "po:9/line:1", "dlv-1")                     # a PO line of 10 arrives in two deliveries
    s.receive(conn, B1, ITEM, 6, "po:9/line:1", "dlv-2")
    assert levels(conn) == (10, 0, 10)
    rid = s.reserve(conn, B1, ITEM, 8, "order:1", "res")["reservation_id"]
    first = s.ship(conn, B1, rid, 3, "shp-1")
    assert (first["quantity_shipped"], first["remaining"], first["status"]) == (3, 5, "active"), first
    assert levels(conn) == (7, 5, 2)
    last = s.ship(conn, B1, rid, 5, "shp-2")
    assert (last["quantity_shipped"], last["remaining"], last["status"]) == (8, 0, "fulfilled"), last
    assert levels(conn) == (2, 0, 2)
    rejected(lambda: s.ship(conn, B1, rid, 1, "shp-3"), "is fulfilled", s.InvalidState)
    moves = conn.execute("SELECT quantity, movement_type, reservation_id, source_ref FROM stock_movements ORDER BY id")
    assert [tuple(m) for m in moves] == [(4, "receipt", None, "po:9/line:1"), (6, "receipt", None, "po:9/line:1"),
                                         (-3, "shipment", rid, f"reservation:{rid}"),
                                         (-5, "shipment", rid, f"reservation:{rid}")]


def test_shipping_more_than_reserved_is_rejected():
    conn, _ = new_db("overship")
    s.receive(conn, B1, ITEM, 10, "po:1/line:1", "rcv")
    rid = s.reserve(conn, B1, ITEM, 5, "order:1", "res")["reservation_id"]
    before = dump(conn)
    rejected(lambda: s.ship(conn, B1, rid, 6, "shp"), "has 5 left to ship", s.OverShipment)
    assert dump(conn) == before                                              # nothing written, and the key is still free
    s.ship(conn, B1, rid, 3, "shp")
    rejected(lambda: s.ship(conn, B1, rid, 3, "shp-2"), "has 2 left to ship", s.OverShipment)
    assert levels(conn) == (7, 2, 5)
    rejected(lambda: raw_movement(conn, -3, "shipment", "raw", reservation_id=rid),
             "CHECK constraint failed: quantity_shipped <= quantity")        # raw: the shipment's trigger hits the CHECK
    assert levels(conn) == (7, 2, 5)


def test_release_frees_the_unshipped_remainder():
    conn, _ = new_db("release")
    s.receive(conn, B1, ITEM, 10, "po:1/line:1", "rcv")
    rid = s.reserve(conn, B1, ITEM, 6, "order:1", "res")["reservation_id"]
    s.ship(conn, B1, rid, 2, "shp")
    assert levels(conn) == (8, 4, 4)
    out = s.release(conn, B1, rid, "rel")
    assert (out["released"], out["status"]) == (4, "released"), out
    assert levels(conn) == (8, 0, 8)
    rejected(lambda: s.release(conn, B1, rid, "rel-2"), "is released", s.InvalidState)
    rejected(lambda: s.ship(conn, B1, rid, 1, "shp-2"), "is released", s.InvalidState)
    rejected(lambda: s.release(conn, B1, 999, "rel-3"), "no reservation 999", s.NotFound)


def test_on_hand_never_goes_negative():
    conn, _ = new_db("negative")
    s.receive(conn, B1, ITEM, 3, "po:1/line:1", "rcv")
    for n in range(3):                                                       # reserve, ship and release cycles
        r = s.reserve(conn, B1, ITEM, 2, f"order:{n}", f"res-{n}")
        if r["reservation_id"]:
            s.ship(conn, B1, r["reservation_id"], 1, f"shp-{n}")
            if r["reserved"] > 1:
                s.release(conn, B1, r["reservation_id"], f"rel-{n}")
        assert s.on_hand(conn, B1, ITEM) >= 0
    assert levels(conn) == (0, 0, 0)
    raw_movement(conn, 2, "receipt", "raw-1")                                                         # raw
    rid = raw_reservation(conn, 2, "raw-2")
    rejected(lambda: raw_movement(conn, -3, "shipment", "raw-3", reservation_id=rid), "on hand would go negative")
    raw_movement(conn, -2, "shipment", "raw-4", reservation_id=rid)                                   # down to exactly 0 is fine
    assert levels(conn) == (0, 0, 0)


# ---------------------------------------------------------------- database rules

def test_raw_movement_rows_cannot_be_updated_or_deleted():
    conn, _ = new_db("immutable")
    s.receive(conn, B1, ITEM, 5, "po:1/line:1", "rcv")
    rid = s.reserve(conn, B1, ITEM, 5, "order:1", "res")["reservation_id"]
    s.ship(conn, B1, rid, 5, "shp")
    s.receive(conn, B1, ITEM, 3, "po:1/line:2", "rcv-2")
    s.reserve(conn, B1, ITEM, 3, "order:2", "res-2")                         # no shipments, so no foreign key holds it
    before = dump(conn)
    for sql in ("UPDATE stock_movements SET quantity = 500 WHERE movement_type = 'receipt'",
                "UPDATE stock_movements SET business_id = 2",
                "DELETE FROM stock_movements", "DELETE FROM stock_movements WHERE quantity < 0"):
        rejected(lambda: conn.execute(sql), "stock_movements rows are immutable")
    rejected(lambda: conn.execute("UPDATE stock_commands SET result = '{}'"), "stock_commands rows are immutable")
    rejected(lambda: conn.execute("DELETE FROM stock_commands"), "stock_commands rows are immutable")
    rejected(lambda: conn.execute("DELETE FROM stock_reservations WHERE quantity_shipped = 0"), "stock_reservations rows are kept")
    rejected(lambda: conn.execute("DELETE FROM stock_reservations"), "stock_reservations rows are kept")
    assert dump(conn) == before


def test_raw_movement_sign_matches_its_type():
    conn, _ = new_db("raw_sign")
    raw_movement(conn, 100, "receipt", "seed")                               # so the on-hand trigger is not what rejects
    for qty, kind in ((0, "receipt"), (-5, "receipt"), (5, "shipment"), (0, "shipment"), (5, "adjustment")):
        rejected(lambda: raw_movement(conn, qty, kind, f"m-{qty}-{kind}"), "CHECK constraint failed")
    raw_movement(conn, 5, "receipt", "ok")


def test_raw_reservation_status_only_moves_forward():
    conn, _ = new_db("raw_status")
    raw_movement(conn, 100, "receipt", "m")
    rejected(lambda: raw_reservation(conn, 2, "born-fulfilled", status="fulfilled", shipped=2), "starts active")
    rejected(lambda: raw_reservation(conn, 2, "born-released", status="released"), "starts active")
    upd = lambda sql, rid: conn.execute(f"UPDATE stock_reservations SET {sql} WHERE id = ?", (rid,))
    b = raw_reservation(conn, 5, "b")
    upd("status = 'released'", b)
    rejected(lambda: upd("status = 'active'", b), "no longer active")
    rejected(lambda: raw_movement(conn, -1, "shipment", "ship-b", reservation_id=b), "no longer active")
    a = raw_reservation(conn, 5, "a")
    rejected(lambda: upd("status = 'lost'", a), "CHECK constraint failed: status")
    rejected(lambda: upd("status = 'fulfilled'", a), "CHECK constraint failed")          # fulfilled means fully shipped
    for sql in ("quantity = 50", "business_id = 2", "item_id = 7", "order_ref = 'x'", "idempotency_key = 'z'"):
        rejected(lambda: upd(sql, a), "only status and quantity_shipped may change")
    raw_movement(conn, -3, "shipment", "ship-a1", reservation_id=a)
    assert shipped_and_status(conn, a) == (3, "active")
    raw_movement(conn, -2, "shipment", "ship-a2", reservation_id=a)
    assert shipped_and_status(conn, a) == (5, "fulfilled")                               # the last shipment fulfils it
    rejected(lambda: upd("status = 'active'", a), "no longer active")


def test_raw_shipments_belong_to_a_reservation_of_the_same_business_item_and_location():
    conn, _ = new_db("raw_link")
    assert conn.execute("PRAGMA foreign_keys").fetchone()[0] == 1
    raw_movement(conn, 10, "receipt", "m1")
    raw_movement(conn, 10, "receipt", "m2", item_id=ITEM + 1)                # stock everywhere, so on hand is not what rejects
    raw_movement(conn, 10, "receipt", "m3", location_id=2)
    raw_movement(conn, 10, "receipt", "m4", business_id=B2)
    rid = raw_reservation(conn, 5, "r")
    theirs = raw_reservation(conn, 5, "r", business_id=B2)
    link_rule = "CHECK constraint failed: (movement_type = 'shipment') = (reservation_id IS NOT NULL)"
    rejected(lambda: raw_movement(conn, -1, "shipment", "s1"), link_rule)                         # a shipment needs one
    rejected(lambda: raw_movement(conn, 5, "receipt", "s2", reservation_id=rid), link_rule)       # a receipt has none
    rejected(lambda: raw_movement(conn, -1, "shipment", "s3", reservation_id=9999), "FOREIGN KEY constraint failed")
    rejected(lambda: raw_movement(conn, -1, "shipment", "s4", reservation_id=theirs), "FOREIGN KEY constraint failed")
    rejected(lambda: raw_movement(conn, -1, "shipment", "s5", item_id=ITEM + 1, reservation_id=rid),
             "does not match its reservation")
    rejected(lambda: raw_movement(conn, -1, "shipment", "s6", location_id=2, reservation_id=rid),
             "does not match its reservation")
    assert shipped_and_status(conn, rid) == shipped_and_status(conn, theirs) == (0, "active")
    raw_movement(conn, -2, "shipment", "s7", reservation_id=rid)
    assert shipped_and_status(conn, rid) == (2, "active")


def test_raw_quantity_shipped_always_equals_the_shipments():
    conn, _ = new_db("raw_ledger")
    raw_movement(conn, 10, "receipt", "m")
    rid = raw_reservation(conn, 6, "r")
    upd = lambda sql: conn.execute(f"UPDATE stock_reservations SET {sql} WHERE id = ?", (rid,))
    for sql in ("quantity_shipped = 2", "quantity_shipped = 6, status = 'fulfilled'"):
        rejected(lambda: upd(sql), "quantity_shipped must equal the shipments")
    raw_movement(conn, -4, "shipment", "s", reservation_id=rid)             # the trigger keeps it in step
    assert shipped_and_status(conn, rid) == (4, "active")
    for sql in ("quantity_shipped = 3", "quantity_shipped = 6, status = 'fulfilled'"):
        rejected(lambda: upd(sql), "quantity_shipped must equal the shipments")
    upd("status = 'released'")
    assert levels(conn) == (6, 0, 6)


def test_one_business_cannot_see_or_touch_another_businesss_stock():
    conn, _ = new_db("tenancy")
    s.receive(conn, B1, ITEM, 10, "po:1/line:1", "rcv")
    rid = s.reserve(conn, B1, ITEM, 4, "order:1", "res")["reservation_id"]
    before = dump(conn)
    assert levels(conn, B2, ITEM) == (0, 0, 0)                                # same item id, other business
    rejected(lambda: s.ship(conn, B2, rid, 1, "shp"), f"no reservation {rid} in business {B2}", s.NotFound)
    rejected(lambda: s.release(conn, B2, rid, "rel"), f"no reservation {rid} in business {B2}", s.NotFound)
    assert dump(conn) == before
    out = s.reserve(conn, B2, ITEM, 3, "order:9", "res")                     # B1's stock is not B2's; B1's key is not B2's
    assert (out["reserved"], out["shortage"]) == (0, 3), out
    s.receive(conn, B2, ITEM, 2, "po:5/line:1", "rcv")
    assert levels(conn, B2, ITEM) == (2, 0, 2)
    assert levels(conn, B1, ITEM) == (10, 4, 6)


def test_bad_input_is_rejected_before_anything_is_written():
    conn, _ = new_db("bad_input")
    s.receive(conn, B1, ITEM, 5, "po:1/line:1", "rcv")
    before = dump(conn)
    for qty in (0, -1, 1.5, True, "3", None):
        rejected(lambda: s.receive(conn, B1, ITEM, qty, "po:1/line:1", "k"), "quantity must be", ValueError)
        rejected(lambda: s.reserve(conn, B1, ITEM, qty, "order:1", "k"), "quantity must be", ValueError)
    for key in ("", "  ", None, 7):
        rejected(lambda: s.receive(conn, B1, ITEM, 1, "po:1/line:1", key), "idempotency_key must be", ValueError)
    rejected(lambda: s.receive(conn, B1, ITEM, 1, "", "k"), "source_ref must be", ValueError)
    rejected(lambda: s.reserve(conn, B1, ITEM, 1, "", "k"), "order_ref must be", ValueError)
    rejected(lambda: s.receive(conn, 0, ITEM, 1, "po:1/line:1", "k"), "business_id must be", ValueError)
    assert dump(conn) == before
    assert not conn.in_transaction
    s.receive(conn, B1, ITEM, 1, "po:1/line:1", "k")                          # the connection is still usable


if __name__ == "__main__":
    print(f"Python {sys.version.split()[0]}, SQLite {sqlite3.sqlite_version}")
    tests = [(n, f) for n, f in globals().items() if n.startswith("test_") and callable(f)]
    for name, fn in tests:
        fn()
        print(f"PASS {name}")
    print(f"\n{len(tests)} tests passed")
