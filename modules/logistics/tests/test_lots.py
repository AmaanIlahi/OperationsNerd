"""
Stock engine v2: lots, expiry, holds, allocations and recall. Same conventions as
test_stock.py, whose helpers this uses: tests marked "raw" write SQL directly,
bypassing stock.py, so whatever they see rejected is rejected by the database
itself. Dates are fixed, so results don't depend on the day the tests run.
Run from the repo root with the venv:

    python -B modules/logistics/tests/test_lots.py
"""

import datetime
import sqlite3
import sys
import threading
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parent))
import stock as s
from test_stock import B1, B2, ITEM, TRIALS, new_db, raw_reservation, rejected

FOOD = 201                       # lot-tracked in every test that receives it; ITEM stays without lots
TODAY = "2026-10-23"
TABLES = ("stock_commands", "stock_movements", "stock_reservations", "stock_lot_tracking", "stock_lots",
          "stock_lot_holds", "stock_allocations")
MATCH_RULE = "allocations must match the reservation's item and quantity"


def tracked_db(name: str, item=FOOD):
    conn, path = new_db(name)
    s.track_lots(conn, B1, item, f"track-{item}")
    return conn, path


def dump_all(conn) -> list:
    return [(t, tuple(r)) for t in TABLES for r in conn.execute(f"SELECT * FROM {t} ORDER BY rowid")]


def receive_lot(conn, lot_number, quantity, expiry, item=FOOD, business_id=B1, source_ref="po:1/line:1"):
    """Receives a lot and returns its lot_id."""
    return s.receive(conn, business_id, item, quantity, source_ref, f"rcv-{item}-{lot_number}-{quantity}",
                     lot_number=lot_number, expiry_date=expiry)["lot_id"]


def reserve(conn, quantity, order_ref, as_of=TODAY, item=FOOD):
    return s.reserve(conn, B1, item, quantity, order_ref, f"res-{order_ref}", as_of=as_of)


def picked(result) -> list:
    """(lot_number, quantity) for each allocation of a reserve, or each lot of a ship."""
    return [(a["lot_number"], a["quantity"]) for a in result.get("allocations", result.get("lots"))]


def lot_levels(conn, item=FOOD, business_id=B1) -> dict:
    return {lot["lot_number"]: (lot["on_hand"], lot["allocated"], lot["free"]) for lot in s.lots(conn, business_id, item)}


def assert_allocations_add_up(conn) -> None:
    """Each reservation of a tracked item is exactly its allocations; any other reservation has none."""
    rows = conn.execute(
        """SELECT r.id, r.quantity, t.item_id IS NOT NULL AS tracked,
                  (SELECT COALESCE(SUM(a.quantity), 0) FROM stock_allocations a
                    WHERE a.business_id = r.business_id AND a.reservation_id = r.id) AS allocated
           FROM stock_reservations r
           LEFT JOIN stock_lot_tracking t ON t.business_id = r.business_id AND t.item_id = r.item_id""").fetchall()
    for r in rows:
        assert r["allocated"] == (r["quantity"] if r["tracked"] else 0), dict(r)


def assert_lots_within_stock(conn, item=FOOD) -> None:
    for lot in s.lots(conn, B1, item):
        assert 0 <= lot["allocated"] <= lot["on_hand"], lot


# raw writers: plain SQL, nothing from stock.py

def raw_lot(conn, lot_number, expiry=None, item=FOOD, business_id=B1):
    return conn.execute("INSERT INTO stock_lots (business_id, item_id, lot_number, expiry_date) VALUES (?, ?, ?, ?)",
                        (business_id, item, lot_number, expiry)).lastrowid


def raw_move(conn, quantity, key, lot_id, movement_type="receipt", reservation_id=None, as_of=None, item=FOOD,
             business_id=B1):
    return conn.execute(
        """INSERT INTO stock_movements (business_id, item_id, location_id, quantity, movement_type, reservation_id,
                                        lot_id, as_of, source_ref, idempotency_key)
           VALUES (?, ?, 1, ?, ?, ?, ?, ?, 'raw', ?)""",
        (business_id, item, quantity, movement_type, reservation_id, lot_id, as_of, key)).lastrowid


def raw_ship(conn, reservation_id, lot_id, quantity, key, as_of=TODAY, item=FOOD):
    return raw_move(conn, -quantity, key, lot_id, "shipment", reservation_id, as_of, item)


def raw_reserve(conn, allocations, key, quantity=None, item=FOOD, as_of=TODAY, business_id=B1):
    """Writes allocations [(lot_id, quantity) or (lot_id, quantity, item)] and then their reservation, in one
    transaction, the way stock.py does. quantity defaults to the allocations' total."""
    conn.execute("BEGIN IMMEDIATE")
    try:
        rid = conn.execute("SELECT COALESCE(MAX(id), 0) + 1 FROM stock_reservations").fetchone()[0]
        for lot_id, qty, *alloc_item in allocations:
            raw_allocation(conn, rid, lot_id, qty, as_of, alloc_item[0] if alloc_item else item, business_id)
        total = sum(a[1] for a in allocations) if quantity is None else quantity
        conn.execute("""INSERT INTO stock_reservations (id, business_id, item_id, location_id, order_ref, quantity,
                                                        idempotency_key)
                        VALUES (?, ?, ?, 1, 'raw', ?, ?)""", (rid, business_id, item, total, key))
        conn.execute("COMMIT")
    except BaseException:
        if conn.in_transaction:
            conn.execute("ROLLBACK")
        raise
    return rid


def raw_allocation(conn, reservation_id, lot_id, quantity, as_of=TODAY, item=FOOD, business_id=B1):
    return conn.execute("""INSERT INTO stock_allocations (business_id, reservation_id, item_id, lot_id, quantity, as_of)
                           VALUES (?, ?, ?, ?, ?, ?)""",
                        (business_id, reservation_id, item, lot_id, quantity, as_of)).lastrowid


def raw_hold(conn, lot_id, event, key, reason="qa", business_id=B1):
    return conn.execute("""INSERT INTO stock_lot_holds (business_id, lot_id, event, reason, idempotency_key)
                           VALUES (?, ?, ?, ?, ?)""", (business_id, lot_id, event, reason, key)).lastrowid


def in_one_transaction(conn, *steps):
    """Runs raw steps in one transaction and rolls back whatever fails, including a failed COMMIT."""
    conn.execute("BEGIN IMMEDIATE")
    try:
        for step in steps:
            step()
        conn.execute("COMMIT")
    except BaseException:
        if conn.in_transaction:
            conn.execute("ROLLBACK")
        raise


# ---------------------------------------------------------------- lot tracking

def test_items_without_lot_tracking_take_no_lot_and_keep_the_v1_results():
    conn, _ = new_db("untracked")
    rejected(lambda: s.receive(conn, B1, ITEM, 5, "po:1/line:1", "k", lot_number="A1"), "is not lot-tracked",
             s.LotMismatch)
    assert s.receive(conn, B1, ITEM, 5, "po:1/line:1", "k").keys() == {"movement_id", "item_id", "received"}
    res = s.reserve(conn, B1, ITEM, 2, "order:1", "res", as_of=TODAY)              # as_of is ignored without lots
    assert res.keys() == {"reservation_id", "item_id", "requested", "reserved", "shortage"}
    shp = s.ship(conn, B1, res["reservation_id"], 2, "shp", as_of=TODAY)
    assert "movement_id" in shp and "lots" not in shp
    assert s.lots(conn, B1, ITEM) == []
    rejected(lambda: raw_lot(conn, "A1", item=ITEM), "FOREIGN KEY constraint failed")   # raw: lots only for tracked items
    assert conn.execute("SELECT COUNT(*) FROM stock_allocations").fetchone()[0] == 0


def test_lot_tracking_starts_before_any_stock_and_never_stops():
    conn, _ = new_db("track")
    s.receive(conn, B1, ITEM, 5, "po:1/line:1", "rcv")
    rejected(lambda: s.track_lots(conn, B1, ITEM, "t1"), "already has stock without lots", s.InvalidState)
    out = s.track_lots(conn, B1, FOOD, "t2")
    assert out == {"item_id": FOOD, "lot_tracked": True}
    assert s.track_lots(conn, B1, FOOD, "t2") == out                                    # replay
    assert s.track_lots(conn, B1, FOOD, "t3") == out                                    # already tracked: nothing changes
    assert conn.execute("SELECT COUNT(*) FROM stock_lot_tracking").fetchone()[0] == 1
    rejected(lambda: s.receive(conn, B1, FOOD, 5, "po:1/line:1", "rcv-2"), "is lot-tracked: receive needs a lot_number",
             s.LotMismatch)
    rejected(lambda: s.receive(conn, B2, FOOD, 5, "po:1/line:1", "rcv-3", lot_number="A1"), "is not lot-tracked",
             s.LotMismatch)                                                               # tracking is per business
    track = "INSERT INTO stock_lot_tracking (business_id, item_id) VALUES (?, ?)"        # raw
    rejected(lambda: conn.execute(track, (B1, ITEM)), "item already has stock without lots")
    rejected(lambda: conn.execute(track, (B1, FOOD)), "UNIQUE constraint failed: stock_lot_tracking")
    rejected(lambda: conn.execute("UPDATE stock_lot_tracking SET item_id = 7"), "stock_lot_tracking rows are immutable")
    rejected(lambda: conn.execute("DELETE FROM stock_lot_tracking"), "stock_lot_tracking rows are immutable")


# ---------------------------------------------------------------- receive

def test_one_po_line_received_as_several_lots_and_a_lot_is_unique_per_business_item_and_number():
    conn, _ = tracked_db("receive_lots")
    s.track_lots(conn, B1, FOOD + 1, "track-other-item")
    a = s.receive(conn, B1, FOOD, 6, "po:7/line:1", "dlv-1", lot_number="A1", expiry_date="2026-11-01")
    b = s.receive(conn, B1, FOOD, 4, "po:7/line:1", "dlv-2", lot_number="B2", expiry_date="2026-11-15")
    assert (a["lot_number"], a["expiry_date"], a["received"]) == ("A1", "2026-11-01", 6), a
    assert a["lot_id"] != b["lot_id"]
    more = s.receive(conn, B1, FOOD, 3, "po:7/line:2", "dlv-3", lot_number="A1", expiry_date="2026-11-01")
    assert more["lot_id"] == a["lot_id"]                                                  # more of the same lot
    before = dump_all(conn)
    rejected(lambda: s.receive(conn, B1, FOOD, 1, "po:7/line:2", "dlv-4", lot_number="A1", expiry_date="2026-11-02"),
             "expires 2026-11-01, not 2026-11-02", s.LotMismatch)
    rejected(lambda: s.receive(conn, B1, FOOD, 1, "po:7/line:2", "dlv-4", lot_number="A1"),
             "expires 2026-11-01, not never", s.LotMismatch)
    assert dump_all(conn) == before
    other_item = s.receive(conn, B1, FOOD + 1, 2, "po:8/line:1", "dlv-5", lot_number="A1", expiry_date="2026-12-01")
    s.track_lots(conn, B2, FOOD, "track")
    theirs = s.receive(conn, B2, FOOD, 2, "po:1/line:1", "dlv-1", lot_number="A1", expiry_date="2027-01-01")
    assert len({a["lot_id"], other_item["lot_id"], theirs["lot_id"]}) == 3
    no_expiry = s.receive(conn, B1, FOOD, 1, "po:7/line:3", "dlv-6", lot_number="NX")
    assert no_expiry["expiry_date"] is None
    assert lot_levels(conn) == {"A1": (9, 0, 9), "B2": (4, 0, 4), "NX": (1, 0, 1)}
    assert s.on_hand(conn, B1, FOOD) == 14
    moves = conn.execute("""SELECT source_ref, lot_id FROM stock_movements WHERE business_id = ? AND item_id = ?
                            ORDER BY id""", (B1, FOOD)).fetchall()
    assert [tuple(m) for m in moves] == [("po:7/line:1", a["lot_id"]), ("po:7/line:1", b["lot_id"]),
                                         ("po:7/line:2", a["lot_id"]), ("po:7/line:3", no_expiry["lot_id"])]
    rejected(lambda: raw_lot(conn, "A1", "2026-11-01"), "UNIQUE constraint failed: stock_lots.business_id, "
             "stock_lots.item_id, stock_lots.lot_number")                                # raw


def test_raw_lots_and_lot_movements_are_checked():
    conn, _ = tracked_db("raw_lots")
    conn.execute("INSERT INTO stock_lot_tracking (business_id, item_id) VALUES (?, ?)", (B1, FOOD + 1))
    conn.execute("INSERT INTO stock_lot_tracking (business_id, item_id) VALUES (?, ?)", (B2, FOOD))
    lot = raw_lot(conn, "A1", "2026-11-01")
    other_item_lot = raw_lot(conn, "A1", item=FOOD + 1)
    other_business_lot = raw_lot(conn, "A1", business_id=B2)
    rejected(lambda: raw_lot(conn, ""), "CHECK constraint failed: lot_number")
    for bad in ("2026-02-30", "2026/11/01", "20261101", "2026-11-01 00:00"):
        rejected(lambda: raw_lot(conn, f"bad-{bad}", bad), "CHECK constraint failed: expiry_date")
    rejected(lambda: raw_move(conn, 5, "m0", None), "movements of a lot-tracked item need a lot")
    rejected(lambda: raw_move(conn, 5, "m1", other_item_lot), "FOREIGN KEY constraint failed")
    rejected(lambda: raw_move(conn, 5, "m2", other_business_lot), "FOREIGN KEY constraint failed")
    rejected(lambda: raw_move(conn, 5, "m3", lot, as_of="2026-13-01"), "CHECK constraint failed: as_of")
    raw_move(conn, 5, "m4", lot)
    for sql in ("UPDATE stock_lots SET expiry_date = '2027-01-01'", "UPDATE stock_lots SET lot_number = 'Z'",
                f"DELETE FROM stock_lots WHERE id = {other_item_lot}",          # nothing refers to it, so no FK holds it
                "DELETE FROM stock_lots"):
        rejected(lambda: conn.execute(sql), "stock_lots rows are immutable")


# ---------------------------------------------------------------- reserve

def test_reserve_allocates_earliest_expiry_first_and_one_reservation_can_span_lots():
    conn, _ = tracked_db("fefo")
    receive_lot(conn, "LATE", 5, "2026-12-01")
    receive_lot(conn, "NONE", 5, None)
    receive_lot(conn, "SOON", 2, "2026-10-30")
    receive_lot(conn, "TIE", 1, "2026-10-30")                                        # same expiry: received later goes later
    receive_lot(conn, "MID", 4, "2026-11-10")
    first = reserve(conn, 9, "order:1")
    assert (first["reserved"], first["shortage"]) == (9, 0), first
    assert picked(first) == [("SOON", 2), ("TIE", 1), ("MID", 4), ("LATE", 2)]
    second = reserve(conn, 6, "order:2")
    assert picked(second) == [("LATE", 3), ("NONE", 3)]                                # no expiry comes last
    assert lot_levels(conn) == {"SOON": (2, 2, 0), "TIE": (1, 1, 0), "MID": (4, 4, 0), "LATE": (5, 5, 0),
                                "NONE": (5, 3, 2)}
    assert (s.on_hand(conn, B1, FOOD), s.reserved(conn, B1, FOOD), s.available(conn, B1, FOOD)) == (17, 15, 2)
    stored = conn.execute("SELECT lot_id, quantity, as_of FROM stock_allocations WHERE reservation_id = ? ORDER BY id",
                          (first["reservation_id"],)).fetchall()
    assert [(r["lot_id"], r["quantity"], r["as_of"]) for r in stored] == \
           [(a["lot_id"], a["quantity"], TODAY) for a in first["allocations"]]
    assert_allocations_add_up(conn)


def test_expired_and_held_lots_are_never_allocated_and_the_shortage_is_returned():
    conn, _ = tracked_db("unusable")
    receive_lot(conn, "OLD", 5, "2026-10-22")                                         # expired yesterday
    receive_lot(conn, "EDGE", 2, TODAY)                                               # expires today: still usable today
    held = receive_lot(conn, "HELD", 5, "2026-10-25")
    receive_lot(conn, "FRESH", 3, "2026-11-30")
    s.hold_lot(conn, B1, held, "QA: temperature excursion", "hold-1")
    r = reserve(conn, 8, "order:1")
    assert picked(r) == [("EDGE", 2), ("FRESH", 3)] and (r["reserved"], r["shortage"]) == (5, 3), r
    view = {lot["lot_number"]: (lot["expired"], lot["on_hold"], lot["usable"]) for lot in s.lots(conn, B1, FOOD, TODAY)}
    assert view == {"OLD": (True, False, False), "EDGE": (False, False, False), "HELD": (False, True, False),
                    "FRESH": (False, False, False)}                                    # EDGE and FRESH: nothing free left
    before = dump_all(conn)
    none_left = reserve(conn, 1, "order:2")
    assert (none_left["reservation_id"], none_left["shortage"], none_left["allocations"]) == (None, 1, []), none_left
    assert len(dump_all(conn)) == len(before) + 1                                    # only the command record
    assert s.available(conn, B1, FOOD) == 10                                          # available still counts OLD and HELD
    s.release_hold(conn, B1, held, "QA: cleared", "unhold-1")
    assert picked(reserve(conn, 2, "order:3")) == [("HELD", 2)]
    late = reserve(conn, 4, "order:4", as_of="2026-10-26")                            # HELD expired the day before
    assert (late["reserved"], late["shortage"]) == (0, 4), late
    assert_allocations_add_up(conn)


def test_raw_allocations_never_come_from_held_or_expired_lots():
    conn, _ = tracked_db("raw_unusable")
    lot = raw_lot(conn, "A", "2026-10-25")
    raw_move(conn, 10, "m", lot)
    rejected(lambda: raw_reserve(conn, [(lot, 1)], "r1", as_of="2026-10-26"), "cannot allocate from an expired lot")
    raw_reserve(conn, [(lot, 1)], "r2", as_of="2026-10-25")                              # on its expiry date is fine
    raw_hold(conn, lot, "hold", "h1")
    rejected(lambda: raw_reserve(conn, [(lot, 1)], "r3"), "cannot allocate from a held lot")
    raw_hold(conn, lot, "release", "h2")
    raw_reserve(conn, [(lot, 1)], "r4")


def test_two_concurrent_reserves_cannot_over_allocate_one_lot():
    setup, path = new_db("lot_race")
    original = s.lots

    def slow_lots(*args, **kwargs):
        out = original(*args, **kwargs)
        time.sleep(0.05)                                                             # widen the gap between the read and the write
        return out
    s.lots = slow_lots
    try:
        for trial in range(TRIALS):
            item = 2000 + trial
            s.track_lots(setup, B1, item, f"track-{trial}")
            receive_lot(setup, "L", 5, "2026-12-01", item=item)
            receive_lot(setup, "OLD", 10, "2026-10-01", item=item)                     # expired: so the item has stock to spare
            barrier, results, errors = threading.Barrier(2), [], []

            def go(n):
                conn = s.connect(path)
                try:
                    barrier.wait()
                    results.append(s.reserve(conn, B1, item, 3, f"order:{trial}-{n}", f"res-{trial}-{n}", as_of=TODAY))
                except Exception as e:
                    errors.append(e)
                finally:
                    conn.close()
            threads = [threading.Thread(target=go, args=(n,)) for n in range(2)]
            [t.start() for t in threads]
            [t.join() for t in threads]
            assert not errors, (trial, errors)
            assert sorted((r["reserved"], r["shortage"]) for r in results) == [(2, 1), (3, 0)], (trial, results)
            assert lot_levels(setup, item) == {"L": (5, 5, 0), "OLD": (10, 0, 10)}, (trial, lot_levels(setup, item))
        assert_allocations_add_up(setup)
    finally:
        s.lots = original


# ---------------------------------------------------------------- allocation rules

def test_an_allocation_never_exceeds_the_lots_remaining_stock():
    conn, _ = tracked_db("within_lot")
    a = receive_lot(conn, "A", 4, "2026-11-01")
    receive_lot(conn, "B", 3, "2026-11-20")
    steps = [("reserve", 3, "o1"), ("ship", "o1", 2), ("reserve", 2, "o2"), ("release", "o1"), ("reserve", 3, "o3"),
             ("ship", "o3", 3), ("reserve", 1, "o4")]
    rids = {}
    for step in steps:                                                               # after every command
        if step[0] == "reserve":
            rids[step[2]] = reserve(conn, step[1], step[2])["reservation_id"]
        elif step[0] == "ship":
            s.ship(conn, B1, rids[step[1]], step[2], f"shp-{step[1]}", as_of=TODAY)
        else:
            s.release(conn, B1, rids[step[1]], f"rel-{step[1]}")
        assert_lots_within_stock(conn)
        assert_allocations_add_up(conn)
    assert lot_levels(conn) == {"A": (1, 1, 0), "B": (1, 1, 0)}                     # o2's allocations, nothing free
    assert rids["o4"] is None
    # raw. Other stock of the item is plenty, so the item-level rule is not what rejects.
    lot = raw_lot(conn, "R", "2026-12-01")
    raw_move(conn, 5, "m-r", lot)
    plenty = raw_lot(conn, "PLENTY", "2026-12-31")
    raw_move(conn, 100, "m-plenty", plenty)
    r1 = raw_reserve(conn, [(lot, 3)], "r1")
    rejected(lambda: raw_reserve(conn, [(lot, 3)], "r2"), "allocation exceeds the free stock of its lot")
    r3 = raw_reserve(conn, [(lot, 2)], "r3")
    raw_ship(conn, r1, lot, 3, "s1")                                                   # shipping frees nothing in the lot
    rejected(lambda: raw_reserve(conn, [(lot, 1)], "r4"), "allocation exceeds the free stock of its lot")
    conn.execute("UPDATE stock_reservations SET status = 'released' WHERE id = ?", (r3,))   # releasing frees the rest
    raw_reserve(conn, [(lot, 2)], "r5")
    rejected(lambda: raw_reserve(conn, [(lot, 1)], "r6"), "allocation exceeds the free stock of its lot")
    lot2 = raw_lot(conn, "R2", "2026-12-01")
    raw_move(conn, 4, "m-r2", lot2)
    pending = """INSERT INTO stock_reservations (id, business_id, item_id, location_id, order_ref, quantity, idempotency_key)
                 VALUES (?, ?, ?, 1, 'raw', 3, ?)"""
    rejected(lambda: in_one_transaction(conn, lambda: raw_allocation(conn, 900, lot2, 3),   # two reservations whose
                                        lambda: raw_allocation(conn, 901, lot2, 3),          # allocations are both pending
                                        lambda: conn.execute(pending, (900, B1, FOOD, "r900")),
                                        lambda: conn.execute(pending, (901, B1, FOOD, "r901"))),
             "allocation exceeds the free stock of its lot")
    assert_lots_within_stock(conn)


def test_allocations_for_a_reservation_always_add_up_to_its_quantity():
    conn, _ = tracked_db("add_up")
    s.track_lots(conn, B1, FOOD + 1, "track-other")
    a = raw_lot(conn, "A", "2026-12-01")
    b = raw_lot(conn, "B", "2026-12-01")
    other = raw_lot(conn, "O", "2026-12-01", item=FOOD + 1)
    for lot, key in ((a, "ma"), (b, "mb")):
        raw_move(conn, 50, key, lot)
    raw_move(conn, 50, "mo", other, item=FOOD + 1)
    raw_move(conn, 50, "mi", None, item=ITEM)
    raw_move(conn, 50, "mi2", None, item=ITEM, business_id=B2)
    rejected(lambda: raw_reserve(conn, [(a, 2)], "r1", quantity=3), MATCH_RULE)          # too little
    rejected(lambda: raw_reserve(conn, [(a, 2), (b, 2)], "r2", quantity=3), MATCH_RULE)  # too much
    rejected(lambda: raw_reservation(conn, 3, "r3", item_id=FOOD), MATCH_RULE)           # none at all
    rejected(lambda: raw_reserve(conn, [(a, 2), (other, 1, FOOD + 1)], "r4", quantity=2), MATCH_RULE)   # another item's lot
    rejected(lambda: raw_reserve(conn, [(other, 1, FOOD + 1)], "r5", item=ITEM), MATCH_RULE)  # an item without lots has none
    rejected(lambda: raw_reserve(conn, [(a, 0), (b, 2)], "r6"), "CHECK constraint failed: quantity > 0")
    rejected(lambda: raw_reserve(conn, [(other, 1)], "r6b"), "FOREIGN KEY constraint failed")   # labelled FOOD, lot of FOOD+1
    rejected(lambda: raw_reserve(conn, [(9999, 1)], "r6c"), "FOREIGN KEY constraint failed")    # no such lot
    rejected(lambda: raw_reserve(conn, [(a, 1)], "r7", as_of="2026-10-32"), "CHECK constraint failed: as_of")
    rejected(lambda: raw_reserve(conn, [(a, 1), (a, 1)], "r8"), "UNIQUE constraint failed: stock_allocations")
    rid = raw_reserve(conn, [(a, 2), (b, 1)], "r9")
    c = raw_lot(conn, "C", "2026-12-01")
    raw_move(conn, 50, "mc", c)
    rejected(lambda: raw_allocation(conn, rid, c, 1), "allocations are written before their reservation")   # a lot it lacks
    rejected(lambda: in_one_transaction(conn, lambda: raw_allocation(conn, 500, a, 1)),   # the reservation never comes
             "FOREIGN KEY constraint failed")
    theirs = """INSERT INTO stock_reservations (id, business_id, item_id, location_id, order_ref, quantity, idempotency_key)
                VALUES (501, ?, ?, 1, 'raw', 1, 'r10')"""
    rejected(lambda: in_one_transaction(conn, lambda: raw_allocation(conn, 501, a, 1),    # 501 comes, in another business
                                        lambda: conn.execute(theirs, (B2, ITEM))),
             "FOREIGN KEY constraint failed")
    for sql in ("UPDATE stock_allocations SET quantity = 3 WHERE lot_id = ?", "DELETE FROM stock_allocations WHERE lot_id = ?"):
        rejected(lambda: conn.execute(sql, (b,)), "stock_allocations rows are immutable")
    assert conn.execute("SELECT COUNT(*) FROM stock_allocations").fetchone()[0] == 2
    assert_allocations_add_up(conn)


# ---------------------------------------------------------------- ship

def test_ship_goes_lot_by_lot_through_the_reservations_allocations():
    conn, _ = tracked_db("ship_lots")
    a = receive_lot(conn, "A", 3, "2026-11-01")
    b = receive_lot(conn, "B", 4, "2026-11-10")
    c = receive_lot(conn, "C", 5, "2026-12-01")
    rid = reserve(conn, 9, "order:1")["reservation_id"]
    first = s.ship(conn, B1, rid, 5, "shp-1", as_of=TODAY)
    assert picked(first) == [("A", 3), ("B", 2)]
    assert (first["quantity_shipped"], first["remaining"], first["status"]) == (5, 4, "active"), first
    last = s.ship(conn, B1, rid, 4, "shp-2", as_of="2026-10-24")
    assert picked(last) == [("B", 2), ("C", 2)] and last["status"] == "fulfilled", last
    moves = conn.execute("""SELECT lot_id, quantity, reservation_id, as_of, idempotency_key FROM stock_movements
                            WHERE movement_type = 'shipment' ORDER BY id""").fetchall()
    assert [tuple(m) for m in moves] == [(a, -3, rid, TODAY, f"shp-1/lot:{a}"), (b, -2, rid, TODAY, f"shp-1/lot:{b}"),
                                         (b, -2, rid, "2026-10-24", f"shp-2/lot:{b}"),
                                         (c, -2, rid, "2026-10-24", f"shp-2/lot:{c}")]
    assert [m["movement_id"] for m in first["lots"] + last["lots"]] == \
           [r[0] for r in conn.execute("SELECT id FROM stock_movements WHERE movement_type = 'shipment' ORDER BY id")]
    assert lot_levels(conn) == {"A": (0, 0, 0), "B": (0, 0, 0), "C": (3, 0, 3)}
    rejected(lambda: s.ship(conn, B1, rid, 1, "shp-3", as_of=TODAY), "is fulfilled", s.InvalidState)


def test_shipping_from_a_held_or_expired_lot_is_rejected():
    conn, _ = tracked_db("ship_blocked")
    a = receive_lot(conn, "A", 3, "2026-10-25")
    receive_lot(conn, "B", 4, "2026-11-10")
    rid = reserve(conn, 7, "order:1")["reservation_id"]
    s.hold_lot(conn, B1, a, "QA: damaged pallet", "hold-a")
    before = dump_all(conn)
    rejected(lambda: s.ship(conn, B1, rid, 5, "shp-1", as_of=TODAY),
             "1 of it is allocated in lots that are held or expired: A (held)", s.LotUnavailable)
    assert dump_all(conn) == before                                                  # B's part was not shipped either
    out = s.ship(conn, B1, rid, 4, "shp-1", as_of=TODAY)                               # around the held lot
    assert picked(out) == [("B", 4)], out
    rejected(lambda: s.ship(conn, B1, rid, 1, "shp-2", as_of=TODAY), "A (held)", s.LotUnavailable)
    s.release_hold(conn, B1, a, "QA: cleared", "unhold-a")
    rejected(lambda: s.ship(conn, B1, rid, 1, "shp-2", as_of="2026-10-26"), "A (expired)", s.LotUnavailable)
    out = s.ship(conn, B1, rid, 3, "shp-2", as_of="2026-10-25")                        # on its expiry date is fine
    assert picked(out) == [("A", 3)] and out["status"] == "fulfilled", out


def test_raw_lot_shipments_come_out_of_their_allocation_from_a_usable_lot():
    conn, _ = tracked_db("raw_ship")
    s.track_lots(conn, B1, FOOD + 1, "track-other")
    a = raw_lot(conn, "A", "2026-10-25")
    b = raw_lot(conn, "B", "2026-12-01")
    other = raw_lot(conn, "O", "2026-12-01", item=FOOD + 1)
    for lot, key, item in ((a, "ma", FOOD), (b, "mb", FOOD), (other, "mo", FOOD + 1)):
        raw_move(conn, 10, key, lot, item=item)
    rid = raw_reserve(conn, [(a, 3), (b, 2)], "r")
    rejected(lambda: raw_ship(conn, rid, None, 1, "s0"), "movements of a lot-tracked item need a lot")
    rejected(lambda: raw_ship(conn, rid, b, 3, "s1"), "shipment exceeds its allocation")
    rejected(lambda: raw_ship(conn, rid, other, 1, "s2"), "FOREIGN KEY constraint failed")   # another item's lot
    c = raw_lot(conn, "C", "2026-12-01")
    raw_move(conn, 10, "mc", c)
    rejected(lambda: raw_ship(conn, rid, c, 1, "s3"), "FOREIGN KEY constraint failed")       # a lot it has no allocation in
    rejected(lambda: raw_ship(conn, rid, a, 1, "s4", as_of=None), "CHECK constraint failed: movement_type <> 'shipment'")
    rejected(lambda: raw_ship(conn, rid, a, 1, "s5", as_of="2026-10-26"), "cannot ship from an expired lot")
    raw_hold(conn, a, "hold", "h1")
    rejected(lambda: raw_ship(conn, rid, a, 1, "s6", as_of="2026-10-25"), "cannot ship from a held lot")
    raw_hold(conn, a, "release", "h2")
    raw_ship(conn, rid, a, 2, "s7", as_of="2026-10-25")
    raw_ship(conn, rid, a, 1, "s8", as_of="2026-10-25")
    rejected(lambda: raw_ship(conn, rid, a, 1, "s9", as_of="2026-10-25"), "shipment exceeds its allocation")
    raw_ship(conn, rid, b, 2, "s10")
    assert conn.execute("SELECT quantity_shipped, status FROM stock_reservations WHERE id = ?", (rid,)).fetchone()[:] \
        == (5, "fulfilled")


def test_raw_lot_on_hand_never_goes_negative():
    conn, _ = tracked_db("raw_lot_negative")
    lot = raw_lot(conn, "A", "2026-12-01")
    plenty = raw_lot(conn, "P", "2026-12-01")
    raw_move(conn, 3, "m", lot)
    raw_move(conn, 100, "mp", plenty)                                                 # so the item-level rule is not what rejects
    rid = raw_reserve(conn, [(lot, 3)], "r")
    raw_ship(conn, rid, lot, 2, "s1")
    rejected(lambda: raw_ship(conn, rid, lot, 2, "s2"), "lot on hand would go negative")
    raw_ship(conn, rid, lot, 1, "s3")
    assert lot_levels(conn)["A"] == (0, 0, 0)


# ---------------------------------------------------------------- holds

def test_holds_replay_alternate_and_leave_existing_allocations_in_place():
    conn, _ = tracked_db("holds")
    a = receive_lot(conn, "A", 5, "2026-11-01")
    b = receive_lot(conn, "B", 5, "2026-11-20")
    rid = reserve(conn, 3, "order:1")["reservation_id"]
    held = s.hold_lot(conn, B1, a, "QA: supplier recall notice", "hold-1")
    assert held == {"lot_id": a, "lot_number": "A", "on_hold": True, "allocated": 3}, held
    before = dump_all(conn)
    assert s.hold_lot(conn, B1, a, "QA: supplier recall notice", "hold-1") == held        # replay
    rejected(lambda: s.hold_lot(conn, B1, a, "another reason", "hold-1"), "already used", s.IdempotencyConflict)
    rejected(lambda: s.hold_lot(conn, B1, a, "again", "hold-2"), f"lot {a} is already on hold", s.InvalidState)
    rejected(lambda: s.release_hold(conn, B1, b, "never held", "unhold-0"), f"lot {b} is not on hold", s.InvalidState)
    assert dump_all(conn) == before
    assert lot_levels(conn)["A"] == (5, 3, 2) and s.reserved(conn, B1, FOOD) == 3   # the allocation stays, still reserved
    assert picked(reserve(conn, 4, "order:2")) == [("B", 4)]                         # new reserves go around the held lot
    s.release(conn, B1, rid, "rel-1")                                                 # releasing frees it on the held lot
    assert lot_levels(conn)["A"] == (5, 0, 5)
    unheld = s.release_hold(conn, B1, a, "QA: cleared", "unhold-1")
    assert unheld == {"lot_id": a, "lot_number": "A", "on_hold": False, "allocated": 0}, unheld
    assert s.release_hold(conn, B1, a, "QA: cleared", "unhold-1") == unheld
    assert picked(reserve(conn, 6, "order:3")) == [("A", 5), ("B", 1)]
    history = conn.execute("SELECT event, reason FROM stock_lot_holds ORDER BY id").fetchall()
    assert [tuple(h) for h in history] == [("hold", "QA: supplier recall notice"), ("release", "QA: cleared")]


def test_raw_holds_alternate_and_are_immutable():
    conn, _ = tracked_db("raw_holds")
    s.track_lots(conn, B2, FOOD, "track")
    lot = raw_lot(conn, "A")
    rejected(lambda: raw_hold(conn, lot, "release", "h0"), "a lot is held and released in turn")
    raw_hold(conn, lot, "hold", "h1")
    rejected(lambda: raw_hold(conn, lot, "hold", "h2"), "a lot is held and released in turn")
    rejected(lambda: raw_hold(conn, lot, "release", "h3", reason=""), "CHECK constraint failed: reason")
    rejected(lambda: raw_hold(conn, lot, "release", ""), "CHECK constraint failed: idempotency_key")
    rejected(lambda: raw_hold(conn, lot, "freeze", "h4"), "CHECK constraint failed: event")
    rejected(lambda: raw_hold(conn, lot, "release", "h1"), "UNIQUE constraint failed: stock_lot_holds")
    rejected(lambda: raw_hold(conn, lot, "hold", "h5", business_id=B2), "FOREIGN KEY constraint failed")  # not B2's lot
    raw_hold(conn, lot, "release", "h6")
    raw_hold(conn, raw_lot(conn, "A", business_id=B2), "hold", "h1", business_id=B2)   # the same key in another business
    for sql in ("UPDATE stock_lot_holds SET event = 'hold' WHERE idempotency_key = 'h6'", "DELETE FROM stock_lot_holds"):
        rejected(lambda: conn.execute(sql), "stock_lot_holds rows are immutable")
    assert s.lots(conn, B1, FOOD)[0]["on_hold"] is False


# ---------------------------------------------------------------- recall

def test_recall_lists_every_reservation_and_order_a_lot_went_to():
    conn, _ = tracked_db("recall")
    l1 = receive_lot(conn, "L1", 10, "2026-11-01")
    l2 = receive_lot(conn, "L2", 10, "2026-11-20")
    receive_lot(conn, "L3", 10, "2026-12-01")
    r1 = reserve(conn, 4, "order:1")["reservation_id"]                                 # L1 4
    r2 = reserve(conn, 8, "order:2")["reservation_id"]                                 # L1 6, L2 2: a split line
    r3 = reserve(conn, 3, "order:3")["reservation_id"]                                 # L2 3
    r4 = reserve(conn, 2, "order:4")["reservation_id"]                                 # L2 2
    s.ship(conn, B1, r1, 4, "shp-1", as_of=TODAY)
    s.ship(conn, B1, r2, 7, "shp-2", as_of=TODAY)                                      # L1 6, L2 1
    s.ship(conn, B1, r3, 1, "shp-3", as_of=TODAY)
    s.release(conn, B1, r3, "rel-3")
    s.hold_lot(conn, B1, l2, "customer complaint", "hold-l2")
    rec = s.recall(conn, B1, l2)
    assert {k: rec[k] for k in ("lot_number", "expiry_date", "on_hold", "received", "on_hand")} == \
           {"lot_number": "L2", "expiry_date": "2026-11-20", "on_hold": True, "received": 10, "on_hand": 8}, rec
    assert rec["reservations"] == [
        {"reservation_id": r2, "order_ref": "order:2", "status": "active", "allocated": 2, "shipped": 1},
        {"reservation_id": r3, "order_ref": "order:3", "status": "released", "allocated": 3, "shipped": 1},
        {"reservation_id": r4, "order_ref": "order:4", "status": "active", "allocated": 2, "shipped": 0}], rec
    assert [(x["order_ref"], x["status"], x["allocated"], x["shipped"]) for x in s.recall(conn, B1, l1)["reservations"]] \
        == [("order:1", "fulfilled", 4, 4), ("order:2", "active", 6, 6)]
    assert s.recall(conn, B1, receive_lot(conn, "L4", 1, None))["reservations"] == []
    rejected(lambda: s.recall(conn, B2, l2), f"no lot {l2} in business {B2}", s.NotFound)


# ---------------------------------------------------------------- idempotency, tenancy, input

def test_lot_commands_replay_and_reject_a_changed_payload():
    conn, _ = tracked_db("lot_replays")
    rcv = s.receive(conn, B1, FOOD, 5, "po:1/line:1", "rcv", lot_number="A", expiry_date="2026-11-01")
    res = s.reserve(conn, B1, FOOD, 3, "order:1", "res", as_of=TODAY)
    shp = s.ship(conn, B1, res["reservation_id"], 2, "shp", as_of=TODAY)
    before = dump_all(conn)
    assert s.receive(conn, B1, FOOD, 5, "po:1/line:1", "rcv", lot_number="A", expiry_date="2026-11-01") == rcv
    assert s.reserve(conn, B1, FOOD, 3, "order:1", "res", as_of=TODAY) == res
    assert s.ship(conn, B1, res["reservation_id"], 2, "shp", as_of=TODAY) == shp
    assert dump_all(conn) == before
    conflict = lambda fn: rejected(fn, "already used", s.IdempotencyConflict)
    conflict(lambda: s.receive(conn, B1, FOOD, 5, "po:1/line:1", "rcv", lot_number="B", expiry_date="2026-11-01"))
    conflict(lambda: s.receive(conn, B1, FOOD, 5, "po:1/line:1", "rcv", lot_number="A", expiry_date="2026-11-02"))
    conflict(lambda: s.reserve(conn, B1, FOOD, 3, "order:1", "res", as_of="2026-10-24"))
    conflict(lambda: s.ship(conn, B1, res["reservation_id"], 2, "shp", as_of="2026-10-24"))
    conflict(lambda: s.track_lots(conn, B1, FOOD, "rcv"))
    conflict(lambda: s.hold_lot(conn, B1, rcv["lot_id"], "qa", "shp"))
    assert dump_all(conn) == before


def test_one_business_cannot_see_or_touch_another_businesss_lots():
    conn, _ = tracked_db("lot_tenancy")
    lot = receive_lot(conn, "A", 5, "2026-11-01")
    before = dump_all(conn)
    for fn in (lambda: s.hold_lot(conn, B2, lot, "qa", "h"), lambda: s.release_hold(conn, B2, lot, "qa", "h"),
               lambda: s.recall(conn, B2, lot)):
        rejected(fn, f"no lot {lot} in business {B2}", s.NotFound)
    assert s.lots(conn, B2, FOOD) == []
    out = s.reserve(conn, B2, FOOD, 2, "order:9", "res")                               # FOOD has no lots in B2, and no stock
    assert (out["reserved"], out["shortage"]) == (0, 2) and "allocations" not in out, out
    assert [t for t, _ in dump_all(conn) if (t, _) not in before] == ["stock_commands"]   # only the command record


def test_bad_lot_input_is_rejected_before_anything_is_written():
    conn, _ = tracked_db("lot_bad_input")
    lot = receive_lot(conn, "A", 5, "2026-11-01")
    rid = reserve(conn, 2, "order:1")["reservation_id"]
    before = dump_all(conn)
    for d in ("2026-02-30", "2026/10/23", "20261023", "2026-10-23T00:00", "", 20261023, datetime.date(2026, 10, 23)):
        rejected(lambda: s.receive(conn, B1, FOOD, 1, "po:1/line:1", "k", lot_number="A", expiry_date=d),
                 "expiry_date must be a date", ValueError)
        rejected(lambda: s.reserve(conn, B1, FOOD, 1, "order:2", "k", as_of=d), "as_of must be a date", ValueError)
        rejected(lambda: s.ship(conn, B1, rid, 1, "k", as_of=d), "as_of must be a date", ValueError)
        rejected(lambda: s.lots(conn, B1, FOOD, as_of=d), "as_of must be a date", ValueError)
    for number in ("", "  ", 5):
        rejected(lambda: s.receive(conn, B1, FOOD, 1, "po:1/line:1", "k", lot_number=number), "lot_number must be",
                 ValueError)
    rejected(lambda: s.receive(conn, B1, ITEM, 1, "po:1/line:1", "k", expiry_date="2026-11-01"),
             "expiry_date needs a lot_number", ValueError)
    rejected(lambda: s.reserve(conn, B1, FOOD, 1, "order:2", "k"), "reserve needs as_of", ValueError)
    rejected(lambda: s.ship(conn, B1, rid, 1, "k"), "ship needs as_of", ValueError)
    for reason in ("", " ", None):
        rejected(lambda: s.hold_lot(conn, B1, lot, reason, "k"), "reason must be", ValueError)
    rejected(lambda: s.hold_lot(conn, B1, 0, "qa", "k"), "lot_id must be", ValueError)
    rejected(lambda: s.track_lots(conn, B1, -1, "k"), "item_id must be", ValueError)
    rejected(lambda: s.recall(conn, B1, "1"), "lot_id must be", ValueError)
    assert dump_all(conn) == before
    assert not conn.in_transaction
    s.receive(conn, B1, FOOD, 1, "po:1/line:1", "k", lot_number="A", expiry_date="2026-11-01")   # still usable


if __name__ == "__main__":
    print(f"Python {sys.version.split()[0]}, SQLite {sqlite3.sqlite_version}")
    tests = [(n, f) for n, f in globals().items() if n.startswith("test_") and callable(f)]
    for name, fn in tests:
        fn()
        print(f"PASS {name}")
    print(f"\n{len(tests)} tests passed")
