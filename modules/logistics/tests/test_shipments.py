"""
Stock engine v3: opening stock, receive-and-reserve, and shipments as records
(lines, a single dispatch, tracking events). Same conventions as test_stock.py
and test_lots.py, whose helpers this uses: tests marked "raw" write SQL
directly, bypassing stock.py, so whatever they see rejected is rejected by the
database itself. Dates are fixed. Run from the repo root with the venv:

    python -B modules/logistics/tests/test_shipments.py
"""

import sqlite3
import sys
import threading
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parent))
import stock as s
from test_stock import B1, B2, ITEM, new_db, raw_reservation, rejected
from test_lots import FOOD, TODAY, assert_allocations_add_up, in_one_transaction, raw_move, receive_lot, tracked_db
from test_lots import TABLES as LOT_TABLES

TABLES = LOT_TABLES + ("stock_shipments", "stock_shipment_lines", "stock_shipment_dispatches", "stock_shipment_events")
STOCK_TABLES = ("stock_movements", "stock_reservations", "stock_allocations", "stock_lots")
EVERY_LINE = "a dispatch ships every line of its shipment in full"
TRIALS = 5


def dump_all(conn) -> list:
    return [(t, tuple(r)) for t in TABLES for r in conn.execute(f"SELECT * FROM {t} ORDER BY rowid")]


def stock_rows(conn) -> list:
    return [(t, tuple(r)) for t in STOCK_TABLES for r in conn.execute(f"SELECT * FROM {t} ORDER BY rowid")]


def levels(conn, item=ITEM, business_id=B1) -> tuple:
    return s.on_hand(conn, business_id, item), s.reserved(conn, business_id, item), s.available(conn, business_id, item)


def stocked_db(name, quantity=20):
    conn, path = new_db(name)
    s.receive(conn, B1, ITEM, quantity, "po:1/line:1", "rcv")
    return conn, path


def reserve(conn, quantity, order_ref, item=ITEM):
    return s.reserve(conn, B1, item, quantity, order_ref, f"res-{order_ref}", as_of=TODAY)["reservation_id"]


def new_shipment(conn, number, lines=()):
    """A draft shipment with lines [(reservation_id, quantity)]. Returns (shipment_id, [line_id, ...])."""
    sid = s.create_shipment(conn, B1, number, f"create-{number}")["shipment_id"]
    return sid, [s.add_shipment_line(conn, B1, sid, rid, qty, f"line-{number}-{rid}")["line_id"] for rid, qty in lines]


def dispatch(conn, sid, key, as_of=TODAY, **refs):
    return s.dispatch(conn, B1, sid, "DHL", key, as_of, **(refs or {"tracking_ref": f"TRK-{sid}"}))


def shipped_lots(line) -> list:
    return [(m["lot_number"], m["quantity"]) for m in line["movements"]]


# raw writers

def raw_shipment(conn, number, key, business_id=B1):
    return conn.execute("INSERT INTO stock_shipments (business_id, shipment_number, idempotency_key) VALUES (?, ?, ?)",
                        (business_id, number, key)).lastrowid


def raw_line(conn, sid, rid, quantity, key, business_id=B1):
    return conn.execute("""INSERT INTO stock_shipment_lines (business_id, shipment_id, reservation_id, quantity,
                                                             idempotency_key)
                           VALUES (?, ?, ?, ?, ?)""", (business_id, sid, rid, quantity, key)).lastrowid


def raw_line_move(conn, quantity, key, rid, sid, line_id, movement_type="shipment"):
    """A movement carrying shipment columns. quantity is signed."""
    return conn.execute(
        """INSERT INTO stock_movements (business_id, item_id, location_id, quantity, movement_type, reservation_id,
                                        as_of, shipment_id, shipment_line_id, source_ref, idempotency_key)
           VALUES (?, ?, 1, ?, ?, ?, ?, ?, ?, 'raw', ?)""",
        (B1, ITEM, quantity, movement_type, rid, TODAY, sid, line_id, key)).lastrowid


def raw_dispatch(conn, sid, key, carrier="DHL", label_ref="LBL", tracking_ref=None, as_of=TODAY, business_id=B1):
    conn.execute("""INSERT INTO stock_shipment_dispatches (business_id, shipment_id, carrier, label_ref, tracking_ref,
                                                           as_of, idempotency_key)
                    VALUES (?, ?, ?, ?, ?, ?, ?)""", (business_id, sid, carrier, label_ref, tracking_ref, as_of, key))


def raw_dispatch_txn(conn, sid, moves, key, extra=(), **dispatch_columns):
    """In one transaction: a shipment movement per (line_id, reservation_id, quantity), any extra steps, the dispatch."""
    steps = [lambda i=i, m=m: raw_line_move(conn, -m[2], f"{key}-{sid}-m{i}", m[1], sid, m[0]) for i, m in enumerate(moves)]
    in_one_transaction(conn, *steps, *extra, lambda: raw_dispatch(conn, sid, key, **dispatch_columns))


def raw_event(conn, sid, event, key, occurred_at="2026-10-23T12:00:00Z", business_id=B1):
    return conn.execute("""INSERT INTO stock_shipment_events (business_id, shipment_id, event, occurred_at, idempotency_key)
                           VALUES (?, ?, ?, ?, ?)""", (business_id, sid, event, occurred_at, key)).lastrowid


# ---------------------------------------------------------------- opening stock

def test_opening_stock_is_its_own_movement_kind_and_comes_first():
    conn, _ = new_db("opening")
    out = s.open_stock(conn, B1, ITEM, 7, "count:2026-10-01/shelf-a", "open-1")
    assert out == {"movement_id": out["movement_id"], "item_id": ITEM, "received": 7}, out
    assert s.open_stock(conn, B1, ITEM, 7, "count:2026-10-01/shelf-a", "open-1") == out           # replay
    s.open_stock(conn, B1, ITEM, 3, "count:2026-10-01/shelf-b", "open-2")                         # several are fine
    s.receive(conn, B1, ITEM, 5, "po:1/line:1", "rcv")
    kinds = conn.execute("SELECT movement_type, quantity, source_ref FROM stock_movements ORDER BY id").fetchall()
    assert [tuple(k) for k in kinds] == [("opening", 7, "count:2026-10-01/shelf-a"),
                                         ("opening", 3, "count:2026-10-01/shelf-b"), ("receipt", 5, "po:1/line:1")]
    assert levels(conn) == (15, 0, 15)
    before = dump_all(conn)
    rejected(lambda: s.open_stock(conn, B1, ITEM, 1, "count:late", "open-3"), "opening stock comes first", s.InvalidState)
    assert dump_all(conn) == before
    s.open_stock(conn, B1, ITEM + 1, 2, "count:2026-10-01", "open-4")                              # per item
    s.track_lots(conn, B1, FOOD, "track")
    lot = s.open_stock(conn, B1, FOOD, 4, "count:2026-10-01", "open-5", lot_number="A", expiry_date="2026-12-01")
    assert (lot["lot_number"], lot["expiry_date"], lot["received"]) == ("A", "2026-12-01", 4), lot
    assert s.recall(conn, B1, lot["lot_id"])["received"] == 4
    rejected(lambda: s.open_stock(conn, B1, FOOD, 1, "count", "open-6"), "is lot-tracked: open_stock needs a lot_number",
             s.LotMismatch)
    rejected(lambda: raw_move(conn, 1, "raw-1", None, "opening", item=ITEM),                        # raw
             "opening stock comes before any other movement of the item")
    raw_move(conn, 10, "raw-2", None, "opening", item=ITEM + 2)                  # stock, so on hand is not what rejects
    rejected(lambda: raw_move(conn, -1, "raw-3", None, "opening", item=ITEM + 2),
             "CHECK constraint failed: (movement_type IN ('receipt', 'opening') AND quantity > 0)")
    rid = raw_reservation(conn, 1, "raw-r", item_id=ITEM + 2)
    rejected(lambda: raw_move(conn, 1, "raw-4", None, "opening", reservation_id=rid, item=ITEM + 2),
             "CHECK constraint failed: (movement_type = 'shipment') = (reservation_id IS NOT NULL)")


# ---------------------------------------------------------------- receive and reserve

def test_receive_and_reserve_is_one_transaction():
    conn, _ = new_db("receive_reserve")
    s.receive(conn, B1, ITEM, 5, "po:1/line:1", "rcv")
    s.reserve(conn, B1, ITEM, 5, "order:0", "res-0")                                      # older stock all taken
    out = s.receive_and_reserve(conn, B1, ITEM, 8, "po:2/line:1", "order:1/line:1", "rr-1")
    assert (out["received"], out["reserved"], out["order_ref"]) == (8, 8, "order:1/line:1"), out
    assert levels(conn) == (13, 13, 0)
    move = conn.execute("SELECT movement_type, source_ref FROM stock_movements WHERE id = ?", (out["movement_id"],))
    assert tuple(move.fetchone()) == ("receipt", "po:2/line:1")
    res = conn.execute("SELECT order_ref, quantity FROM stock_reservations WHERE id = ?", (out["reservation_id"],))
    assert tuple(res.fetchone()) == ("order:1/line:1", 8)
    before = dump_all(conn)
    assert s.receive_and_reserve(conn, B1, ITEM, 8, "po:2/line:1", "order:1/line:1", "rr-1") == out    # replay
    for changed in (dict(order_ref="order:9"), dict(reserve_quantity=7), dict(source_ref="po:2/line:2")):
        args = dict(source_ref="po:2/line:1", order_ref="order:1/line:1") | changed
        rejected(lambda: s.receive_and_reserve(conn, B1, ITEM, 8, args.pop("source_ref"), args.pop("order_ref"), "rr-1",
                                               **args), "already used", s.IdempotencyConflict)
    assert dump_all(conn) == before
    part = s.receive_and_reserve(conn, B1, ITEM, 10, "po:3/line:1", "order:2/line:1", "rr-2", reserve_quantity=4)
    assert (part["received"], part["reserved"]) == (10, 4), part
    assert levels(conn) == (23, 17, 6)
    raw_reservation(conn, 1, "rr-3")                                   # a reservation row already holds key rr-3
    before = dump_all(conn)
    rejected(lambda: s.receive_and_reserve(conn, B1, ITEM, 2, "po:4/line:1", "order:3", "rr-3"),
             "UNIQUE constraint failed: stock_reservations")
    assert dump_all(conn) == before                                    # the reservation failed, so no receipt either
    rejected(lambda: s.receive_and_reserve(conn, B1, ITEM, 2, "po:4/line:1", "order:3", "rr-4", reserve_quantity=3),
             "more than the 2 received", ValueError)


def test_receive_and_reserve_a_lot_reserves_that_lot():
    conn, _ = tracked_db("receive_reserve_lots")
    receive_lot(conn, "OLD", 5, "2026-11-01")                          # earlier expiry and free, but not for this order
    out = s.receive_and_reserve(conn, B1, FOOD, 6, "po:9/line:1", "order:1", "rr-1", lot_number="NEW",
                                expiry_date="2026-12-15", as_of=TODAY)
    assert (out["lot_number"], out["expiry_date"], out["reserved"]) == ("NEW", "2026-12-15", 6), out
    alloc = conn.execute("SELECT lot_id, quantity, as_of FROM stock_allocations WHERE reservation_id = ?",
                         (out["reservation_id"],)).fetchall()
    assert [tuple(a) for a in alloc] == [(out["lot_id"], 6, TODAY)]
    more = s.receive_and_reserve(conn, B1, FOOD, 4, "po:9/line:2", "order:2", "rr-2", lot_number="NEW",
                                 expiry_date="2026-12-15", as_of=TODAY, reserve_quantity=1)
    assert more["lot_id"] == out["lot_id"]
    assert {lot["lot_number"]: (lot["on_hand"], lot["allocated"]) for lot in s.lots(conn, B1, FOOD)} == \
           {"OLD": (5, 0), "NEW": (10, 7)}
    assert [(r["order_ref"], r["allocated"]) for r in s.recall(conn, B1, out["lot_id"])["reservations"]] == \
           [("order:1", 6), ("order:2", 1)]
    before = dump_all(conn)
    rejected(lambda: s.receive_and_reserve(conn, B1, FOOD, 1, "po:9/line:3", "order:3", "rr-3", lot_number="NEW",
                                           expiry_date="2026-12-15"), "receive_and_reserve needs as_of", ValueError)
    s.hold_lot(conn, B1, out["lot_id"], "QA: seal broken", "hold")
    before = dump_all(conn)
    rejected(lambda: s.receive_and_reserve(conn, B1, FOOD, 1, "po:9/line:3", "order:3", "rr-3", lot_number="NEW",
                                           expiry_date="2026-12-15", as_of=TODAY),
             "lot 'NEW' is held: nothing received or reserved", s.LotUnavailable)
    rejected(lambda: s.receive_and_reserve(conn, B1, FOOD, 1, "po:9/line:4", "order:4", "rr-4", lot_number="GONE",
                                           expiry_date="2026-10-01", as_of=TODAY),
             "lot 'GONE' is expired on 2026-10-23", s.LotUnavailable)
    assert dump_all(conn) == before                                    # no receipt and no lot either
    assert_allocations_add_up(conn)


# ---------------------------------------------------------------- shipments and dispatch

def test_dispatch_ships_every_line_and_records_the_shipment():
    conn, _ = stocked_db("dispatch")
    r1, r2 = reserve(conn, 6, "order:1"), reserve(conn, 4, "order:2")
    sid, _ = new_shipment(conn, "SHP-1")
    draft = s.shipment(conn, B1, sid)
    assert (draft["status"], draft["carrier"], draft["lines"], draft["events"]) == ("draft", None, [], []), draft
    l1 = s.add_shipment_line(conn, B1, sid, r1, 6, "l1")["line_id"]
    l2 = s.add_shipment_line(conn, B1, sid, r2, 3, "l2")["line_id"]
    assert levels(conn) == (20, 10, 10)                                # nothing moves before dispatch
    out = s.dispatch(conn, B1, sid, "DHL", "d1", TODAY, label_ref="LBL-1", tracking_ref="TRK-1")
    assert {k: out[k] for k in ("shipment_number", "status", "carrier", "label_ref", "tracking_ref", "dispatched_on")} \
        == {"shipment_number": "SHP-1", "status": "dispatched", "carrier": "DHL", "label_ref": "LBL-1",
            "tracking_ref": "TRK-1", "dispatched_on": TODAY}, out
    assert [(line["line_id"], line["order_ref"], line["quantity"], shipped_lots(line)) for line in out["lines"]] == \
           [(l1, "order:1", 6, [(None, 6)]), (l2, "order:2", 3, [(None, 3)])]
    assert levels(conn) == (11, 1, 10)
    moves = conn.execute("""SELECT shipment_id, shipment_line_id, reservation_id, quantity, source_ref, as_of,
                                   idempotency_key FROM stock_movements WHERE shipment_id IS NOT NULL ORDER BY id""")
    assert [tuple(m) for m in moves] == [(sid, l1, r1, -6, "shipment:SHP-1", TODAY, f"d1/line:{l1}"),
                                         (sid, l2, r2, -3, "shipment:SHP-1", TODAY, f"d1/line:{l2}")]
    status = conn.execute("SELECT id, status FROM stock_reservations ORDER BY id").fetchall()
    assert [tuple(r) for r in status] == [(r1, "fulfilled"), (r2, "active")]
    assert s.shipment(conn, B1, sid) == out


def test_dispatching_twice_changes_stock_once():
    conn, _ = stocked_db("dispatch_twice")
    rid = reserve(conn, 5, "order:1")
    sid, (line,) = new_shipment(conn, "SHP-1", [(rid, 5)])
    first = dispatch(conn, sid, "d1")
    before = dump_all(conn)
    assert dispatch(conn, sid, "d1") == first                          # replay: the stored result, nothing written
    rejected(lambda: dispatch(conn, sid, "d2"), f"shipment {sid} is already dispatched", s.InvalidState)
    assert dump_all(conn) == before and levels(conn) == (15, 0, 15)
    s.record_tracking_event(conn, B1, sid, "delivered", "2026-10-24T10:00:00Z", "e1")
    rejected(lambda: dispatch(conn, sid, "d3"), f"shipment {sid} is already delivered", s.InvalidState)
    rejected(lambda: raw_dispatch(conn, sid, "raw-d"), "UNIQUE constraint failed: stock_shipment_dispatches")   # raw
    rejected(lambda: raw_line_move(conn, -1, "raw-m", rid, sid, line), "shipment movements are written before their dispatch")
    assert levels(conn) == (15, 0, 15)


def test_two_concurrent_dispatches_of_one_shipment_move_stock_once():
    setup, path = stocked_db("dispatch_race", quantity=100)
    original = s.shipment

    def slow_shipment(*args, **kwargs):
        out = original(*args, **kwargs)
        time.sleep(0.05)                                               # widen the gap between the status read and the write
        return out
    s.shipment = slow_shipment
    try:
        for trial in range(TRIALS):
            rid = reserve(setup, 6, f"order:{trial}")                   # room for a second 3: the reservation can't stop it
            sid, _ = new_shipment(setup, f"SHP-{trial}", [(rid, 3)])
            barrier, results, errors = threading.Barrier(2), [], []

            def go(n):
                conn = s.connect(path)
                try:
                    barrier.wait()
                    results.append(dispatch(conn, sid, f"d-{trial}-{n}"))
                except Exception as e:
                    errors.append(e)
                finally:
                    conn.close()
            threads = [threading.Thread(target=go, args=(n,)) for n in range(2)]
            [t.start() for t in threads]
            [t.join() for t in threads]
            assert len(results) == 1 and len(errors) == 1, (trial, results, errors)
            assert isinstance(errors[0], s.InvalidState) and "already dispatched" in str(errors[0]), (trial, errors)
            n = trial + 1
            assert levels(setup) == (100 - 3 * n, 3 * n, 100 - 6 * n), (trial, levels(setup))   # 3 shipped per trial, once
    finally:
        s.shipment = original


def test_one_customer_line_shipped_in_two_shipments():
    conn, _ = stocked_db("split_line")
    rid = reserve(conn, 10, "order:7/line:1")
    a, _ = new_shipment(conn, "SHP-A", [(rid, 6)])
    b, _ = new_shipment(conn, "SHP-B", [(rid, 4)])
    c, _ = new_shipment(conn, "SHP-C")
    rejected(lambda: s.add_shipment_line(conn, B1, c, rid, 1, "lc"),
             "has 0 left to ship that is not already on a draft shipment", s.OverShipment)
    dispatch(conn, a, "da")
    assert conn.execute("SELECT quantity_shipped, status FROM stock_reservations WHERE id = ?", (rid,)).fetchone()[:] \
        == (6, "active")
    dispatch(conn, b, "db", as_of="2026-10-24")
    assert conn.execute("SELECT quantity_shipped, status FROM stock_reservations WHERE id = ?", (rid,)).fetchone()[:] \
        == (10, "fulfilled")
    assert [shipped_lots(s.shipment(conn, B1, x)["lines"][0]) for x in (a, b)] == [[(None, 6)], [(None, 4)]]
    assert levels(conn) == (10, 0, 10)


def test_dispatch_needs_a_carrier_a_label_or_tracking_reference_and_shippable_lines():
    conn, _ = stocked_db("dispatch_rules")
    rid = reserve(conn, 5, "order:1")
    sid, _ = new_shipment(conn, "SHP-1", [(rid, 5)])
    for carrier, refs, message in (("", {"label_ref": "L"}, "carrier must be"), ("DHL", {}, "label_ref or a tracking_ref"),
                                   ("DHL", {"label_ref": ""}, "label_ref must be"),
                                   ("DHL", {"tracking_ref": " "}, "tracking_ref must be")):
        rejected(lambda: s.dispatch(conn, B1, sid, carrier, "d", TODAY, **refs), message, ValueError)
    rejected(lambda: s.dispatch(conn, B1, sid, "DHL", "d", "23/10/2026", label_ref="L"), "as_of must be", ValueError)
    empty, _ = new_shipment(conn, "SHP-0")
    rejected(lambda: dispatch(conn, empty, "d0"), f"shipment {empty} has no lines", s.InvalidState)
    s.ship(conn, B1, rid, 2, "direct")                                 # ship still works; the line now asks too much
    before = dump_all(conn)
    rejected(lambda: dispatch(conn, sid, "d1"), "has 3 left to ship, the line has 5", s.OverShipment)
    assert dump_all(conn) == before
    other = reserve(conn, 2, "order:2")
    sid2, _ = new_shipment(conn, "SHP-2", [(other, 2)])
    s.release(conn, B1, other, "rel")
    rejected(lambda: dispatch(conn, sid2, "d2"), f"reservation {other} is released", s.InvalidState)
    rejected(lambda: s.add_shipment_line(conn, B1, sid, rid, 1, "again"), f"reservation {rid} is already on shipment",
             s.InvalidState)
    rejected(lambda: s.create_shipment(conn, B1, "SHP-1", "dup"), f"'SHP-1' is already shipment {sid}", s.InvalidState)
    ok = reserve(conn, 1, "order:3")
    sid3, _ = new_shipment(conn, "SHP-3", [(ok, 1)])
    dispatch(conn, sid3, "d3")
    rejected(lambda: s.add_shipment_line(conn, B1, sid3, rid, 1, "late"), "is dispatched: lines are added before dispatch",
             s.InvalidState)
    rejected(lambda: dispatch(conn, 999, "d4"), "no shipment 999", s.NotFound)


def test_ship_keeps_working_beside_dispatch():
    conn, _ = stocked_db("ship_and_dispatch")
    rid = reserve(conn, 8, "order:1")
    direct = s.ship(conn, B1, rid, 3, "direct")
    assert "movement_id" in direct and direct["remaining"] == 5, direct
    sid, (line,) = new_shipment(conn, "SHP-1", [(rid, 5)])
    dispatch(conn, sid, "d1")
    moves = conn.execute("""SELECT quantity, shipment_id, shipment_line_id, source_ref FROM stock_movements
                            WHERE reservation_id = ? ORDER BY id""", (rid,)).fetchall()
    assert [tuple(m) for m in moves] == [(-3, None, None, f"reservation:{rid}"), (-5, sid, line, "shipment:SHP-1")]
    assert conn.execute("SELECT status FROM stock_reservations WHERE id = ?", (rid,)).fetchone()[0] == "fulfilled"


def test_dispatch_follows_the_lot_rules():
    conn, _ = tracked_db("dispatch_lots")
    a = receive_lot(conn, "A", 3, "2026-10-25")
    receive_lot(conn, "B", 6, "2026-11-10")
    rid = reserve(conn, 9, "order:1", item=FOOD)                       # A 3, B 6
    s.hold_lot(conn, B1, a, "QA: damaged pallet", "hold")
    first, (l1,) = new_shipment(conn, "SHP-1", [(rid, 4)])
    out = dispatch(conn, first, "d1")                                  # around the held lot
    assert shipped_lots(out["lines"][0]) == [("B", 4)], out
    second, (l2,) = new_shipment(conn, "SHP-2", [(rid, 5)])
    before = dump_all(conn)
    rejected(lambda: dispatch(conn, second, "d2"), "3 of it is allocated in lots that are held or expired: A (held)",
             s.LotUnavailable)
    assert dump_all(conn) == before and s.shipment(conn, B1, second)["status"] == "draft"
    s.release_hold(conn, B1, a, "QA: cleared", "unhold")
    rejected(lambda: dispatch(conn, second, "d2", as_of="2026-10-26"), "A (expired)", s.LotUnavailable)
    out = dispatch(conn, second, "d2", as_of="2026-10-25")
    assert shipped_lots(out["lines"][0]) == [("A", 3), ("B", 2)], out
    keys = conn.execute("SELECT idempotency_key FROM stock_movements WHERE shipment_id = ? ORDER BY id", (second,))
    assert [k[0] for k in keys] == [f"d2/line:{l2}/lot:{a}", f"d2/line:{l2}/lot:{a + 1}"]
    assert [(r["order_ref"], r["shipped"]) for r in s.recall(conn, B1, a)["reservations"]] == [("order:1", 3)]
    assert conn.execute("SELECT status FROM stock_reservations WHERE id = ?", (rid,)).fetchone()[0] == "fulfilled"


# ---------------------------------------------------------------- tracking events

def test_tracking_events_never_change_stock_and_a_delivered_shipment_stays_delivered():
    conn, _ = stocked_db("tracking")
    rid = reserve(conn, 5, "order:1")
    draft, _ = new_shipment(conn, "SHP-0", [(rid, 1)])
    rejected(lambda: s.record_tracking_event(conn, B1, draft, "in_transit", "2026-10-23T12:00:00Z", "e0"),
             f"shipment {draft} is not dispatched yet", s.InvalidState)
    sid, _ = new_shipment(conn, "SHP-1", [(rid, 4)])
    dispatch(conn, sid, "d1")
    stock_before, levels_before = stock_rows(conn), levels(conn)
    first = s.record_tracking_event(conn, B1, sid, "in_transit", "2026-10-23T12:00:00Z", "e1")
    assert first["status"] == "dispatched", first
    assert s.record_tracking_event(conn, B1, sid, "delivered", "2026-10-24T09:30:00Z", "e2")["status"] == "delivered"
    late = s.record_tracking_event(conn, B1, sid, "in_transit", "2026-10-23T18:00:00Z", "e3", detail="late hub scan")
    assert late["status"] == "delivered", late                         # a late event doesn't move it back
    assert s.record_tracking_event(conn, B1, sid, "exception", "2026-10-25T08:00:00Z", "e4")["status"] == "delivered"
    assert s.record_tracking_event(conn, B1, sid, "in_transit", "2026-10-23T12:00:00Z", "e1") == first   # replay
    assert stock_rows(conn) == stock_before and levels(conn) == levels_before
    events = s.shipment(conn, B1, sid)["events"]
    assert [(e["event"], e["occurred_at"], e["detail"]) for e in events] == [
        ("in_transit", "2026-10-23T12:00:00Z", None), ("delivered", "2026-10-24T09:30:00Z", None),
        ("in_transit", "2026-10-23T18:00:00Z", "late hub scan"), ("exception", "2026-10-25T08:00:00Z", None)]
    for event, at in (("lost", "2026-10-23T12:00:00Z"), ("delivered", "2026-10-23 12:00:00"),
                      ("delivered", "2026-10-23T12:00Z"), ("delivered", "2026-10-23T25:00:00Z")):
        rejected(lambda: s.record_tracking_event(conn, B1, sid, event, at, "bad"), "must be", ValueError)
    rejected(lambda: raw_event(conn, draft, "in_transit", "raw-e0"), "FOREIGN KEY constraint failed")   # raw


# ---------------------------------------------------------------- raw rules

def test_raw_shipments_and_lines_are_checked_and_immutable():
    conn, _ = stocked_db("raw_shipments")
    s.receive(conn, B2, ITEM, 5, "po:1/line:1", "rcv")
    rid = reserve(conn, 5, "order:1")
    theirs = s.reserve(conn, B2, ITEM, 2, "order:9", "res")["reservation_id"]
    sid = raw_shipment(conn, "S1", "k1")
    rejected(lambda: raw_shipment(conn, "", "k2"), "CHECK constraint failed: shipment_number")
    rejected(lambda: raw_shipment(conn, "S2", ""), "CHECK constraint failed: idempotency_key")
    rejected(lambda: raw_shipment(conn, "S1", "k3"), "UNIQUE constraint failed: stock_shipments.business_id, "
             "stock_shipments.shipment_number")
    rejected(lambda: raw_shipment(conn, "S4", "k1"), "UNIQUE constraint failed: stock_shipments.business_id, "
             "stock_shipments.idempotency_key")
    their_sid = raw_shipment(conn, "S1", "k1", business_id=B2)                                  # fine in another business
    rejected(lambda: raw_line(conn, sid, rid, 0, "l0"), "CHECK constraint failed: quantity > 0")
    rejected(lambda: raw_line(conn, sid, rid, 1, ""), "CHECK constraint failed: idempotency_key")
    rejected(lambda: raw_line(conn, sid, 999, 1, "l1"), "FOREIGN KEY constraint failed")
    rejected(lambda: raw_line(conn, sid, theirs, 1, "l2"), "FOREIGN KEY constraint failed")      # another business's reservation
    rejected(lambda: raw_line(conn, their_sid, rid, 1, "l3"), "FOREIGN KEY constraint failed")   # another business's shipment
    line = raw_line(conn, sid, rid, 2, "l4")
    rejected(lambda: raw_line(conn, sid, rid, 1, "l5"), "UNIQUE constraint failed: stock_shipment_lines.business_id, "
             "stock_shipment_lines.shipment_id, stock_shipment_lines.reservation_id")
    other = raw_shipment(conn, "S5", "k5")
    rejected(lambda: raw_line(conn, other, rid, 1, "l4"), "UNIQUE constraint failed: stock_shipment_lines.business_id, "
             "stock_shipment_lines.idempotency_key")
    for sql in (f"DELETE FROM stock_shipments WHERE id = {other}",                # no lines hold it
                f"UPDATE stock_shipments SET shipment_number = 'S9' WHERE id = {sid}", "DELETE FROM stock_shipments"):
        rejected(lambda: conn.execute(sql), "stock_shipments rows are immutable")
    for sql in (f"DELETE FROM stock_shipment_lines WHERE id = {line}", "UPDATE stock_shipment_lines SET quantity = 5"):
        rejected(lambda: conn.execute(sql), "stock_shipment_lines rows are immutable")


def test_raw_a_dispatch_ships_every_line_in_full_once_and_is_immutable():
    conn, _ = stocked_db("raw_dispatch", quantity=40)
    r1, r2, r3 = (raw_reservation(conn, q, f"r{n}") for n, q in enumerate((10, 6, 10)))
    sid = raw_shipment(conn, "S1", "k1")
    l1, l2 = raw_line(conn, sid, r1, 3, "l1"), raw_line(conn, sid, r2, 2, "l2")
    full = [(l1, r1, 3), (l2, r2, 2)]
    empty = raw_shipment(conn, "S0", "k0")
    rejected(lambda: raw_dispatch(conn, empty, "d0"), EVERY_LINE)                                 # no lines
    rejected(lambda: raw_dispatch(conn, sid, "d1"), EVERY_LINE)                                   # nothing moved
    rejected(lambda: raw_dispatch_txn(conn, sid, [(l1, r1, 3)], "d2"), EVERY_LINE)               # one line left out
    rejected(lambda: raw_dispatch_txn(conn, sid, [(l1, r1, 2), (l2, r2, 2)], "d3"), EVERY_LINE)  # one line short
    rejected(lambda: in_one_transaction(conn, lambda: raw_line_move(conn, -3, "m4", r1, sid, l1)),
             "FOREIGN KEY constraint failed")                                                       # the dispatch never comes
    for columns, message in ((dict(carrier=""), "carrier"), (dict(label_ref=None), "label_ref IS NOT NULL OR"),
                             (dict(label_ref=""), "label_ref"), (dict(tracking_ref=""), "tracking_ref"),
                             (dict(as_of="2026-10-32"), "as_of"), (dict(as_of=None), "NOT NULL constraint failed")):
        rejected(lambda: raw_dispatch_txn(conn, sid, full, "d5", **columns), message)
    rejected(lambda: raw_dispatch_txn(conn, sid, full, ""), "CHECK constraint failed: idempotency_key")
    # movements tied to the shipment but not to its lines, or to the wrong line, are rejected
    stray = lambda: conn.execute("""INSERT INTO stock_movements (business_id, item_id, location_id, quantity, movement_type,
                                    reservation_id, shipment_id, source_ref, idempotency_key)
                                    VALUES (?, ?, 1, -1, 'shipment', ?, ?, 'raw', 'stray')""", (B1, ITEM, r3, sid))
    rejected(lambda: raw_dispatch_txn(conn, sid, full, "d6", extra=[stray]),
             "CHECK constraint failed: (shipment_id IS NULL) = (shipment_line_id IS NULL)")
    phantom = lambda: raw_line_move(conn, 3, "phantom", None, sid, l1, movement_type="receipt")  # a receipt on a line
    rejected(lambda: raw_dispatch_txn(conn, sid, [(l1, r1, 6), (l2, r2, 2)], "d7", extra=[phantom]),
             "CHECK constraint failed: shipment_line_id IS NULL OR movement_type = 'shipment'")
    rejected(lambda: raw_dispatch_txn(conn, sid, [(l1, r2, 3), (l2, r2, 2)], "d8"),   # l1 is r1's line, shipped from r2
             "FOREIGN KEY constraint failed")
    raw_dispatch_txn(conn, sid, full, "d9", tracking_ref="TRK")
    assert [tuple(r) for r in conn.execute("SELECT quantity_shipped FROM stock_reservations ORDER BY id")] == \
           [(3,), (2,), (0,)]
    rejected(lambda: raw_dispatch(conn, sid, "d10"), "UNIQUE constraint failed: stock_shipment_dispatches.business_id, "
             "stock_shipment_dispatches.shipment_id")                                                 # exactly once
    rejected(lambda: raw_line_move(conn, -1, "m11", r1, sid, l1), "shipment movements are written before their dispatch")
    late_line = lambda: conn.execute("""INSERT INTO stock_movements (business_id, item_id, location_id, quantity,
                                        movement_type, reservation_id, shipment_line_id, source_ref, idempotency_key)
                                        VALUES (?, ?, 1, -1, 'shipment', ?, ?, 'raw', 'm12')""", (B1, ITEM, r1, l1))
    rejected(late_line, "CHECK constraint failed: (shipment_id IS NULL) = (shipment_line_id IS NULL)")
    rejected(lambda: raw_line(conn, sid, r3, 1, "l13"), "lines are added before dispatch")
    s2 = raw_shipment(conn, "S2", "k2")
    l3 = raw_line(conn, s2, r3, 1, "l3")
    rejected(lambda: raw_dispatch_txn(conn, s2, [(l3, r3, 1)], "d9"), "UNIQUE constraint failed: "
             "stock_shipment_dispatches.business_id, stock_shipment_dispatches.idempotency_key")
    for sql in ("UPDATE stock_shipment_dispatches SET carrier = 'UPS'", "DELETE FROM stock_shipment_dispatches"):
        rejected(lambda: conn.execute(sql), "stock_shipment_dispatches rows are immutable")


def test_raw_tracking_events_are_checked_and_immutable():
    conn, _ = stocked_db("raw_events")
    rid = reserve(conn, 5, "order:1")
    sid, _ = new_shipment(conn, "SHP-1", [(rid, 5)])
    dispatch(conn, sid, "d1")
    stock_before = stock_rows(conn)
    raw_event(conn, sid, "delivered", "e1")
    rejected(lambda: raw_event(conn, sid, "lost", "e2"), "CHECK constraint failed: event")
    for at in ("2026-10-23 12:00:00", "2026-10-23T12:00:00", "2026-10-23T12:00:00.000Z"):
        rejected(lambda: raw_event(conn, sid, "in_transit", f"e-{at}", occurred_at=at), "CHECK constraint failed: occurred_at")
    rejected(lambda: raw_event(conn, sid, "in_transit", ""), "CHECK constraint failed: idempotency_key")
    rejected(lambda: raw_event(conn, sid, "in_transit", "e1"), "UNIQUE constraint failed: stock_shipment_events")
    rejected(lambda: raw_event(conn, sid, "in_transit", "e3", business_id=B2), "FOREIGN KEY constraint failed")
    for sql in ("UPDATE stock_shipment_events SET event = 'in_transit'", "DELETE FROM stock_shipment_events"):
        rejected(lambda: conn.execute(sql), "stock_shipment_events rows are immutable")
    assert stock_rows(conn) == stock_before
    assert s.shipment(conn, B1, sid)["status"] == "delivered"


# ---------------------------------------------------------------- idempotency, tenancy, input

def test_a_reused_key_with_different_content_is_rejected():
    conn, _ = stocked_db("shipment_keys")
    rid, other = reserve(conn, 5, "order:1"), reserve(conn, 5, "order:2")
    sid = s.create_shipment(conn, B1, "SHP-1", "c1")["shipment_id"]
    s.add_shipment_line(conn, B1, sid, rid, 2, "l1")
    s.dispatch(conn, B1, sid, "DHL", "d1", TODAY, tracking_ref="TRK-1")
    s.record_tracking_event(conn, B1, sid, "in_transit", "2026-10-23T12:00:00Z", "e1")
    before = dump_all(conn)
    conflict = lambda fn: rejected(fn, "already used", s.IdempotencyConflict)
    conflict(lambda: s.create_shipment(conn, B1, "SHP-2", "c1"))
    conflict(lambda: s.add_shipment_line(conn, B1, sid, rid, 3, "l1"))
    conflict(lambda: s.add_shipment_line(conn, B1, sid, other, 2, "l1"))
    conflict(lambda: s.dispatch(conn, B1, sid, "UPS", "d1", TODAY, tracking_ref="TRK-1"))
    conflict(lambda: s.dispatch(conn, B1, sid, "DHL", "d1", TODAY, tracking_ref="TRK-2"))
    conflict(lambda: s.dispatch(conn, B1, sid, "DHL", "d1", "2026-10-24", tracking_ref="TRK-1"))
    conflict(lambda: s.record_tracking_event(conn, B1, sid, "delivered", "2026-10-23T12:00:00Z", "e1"))
    conflict(lambda: s.record_tracking_event(conn, B1, sid, "in_transit", "2026-10-23T13:00:00Z", "e1"))
    conflict(lambda: s.create_shipment(conn, B1, "SHP-3", "d1"))
    conflict(lambda: s.open_stock(conn, B1, ITEM + 1, 1, "count", "e1"))
    assert dump_all(conn) == before


def test_one_business_cannot_see_or_touch_another_businesss_shipments():
    conn, _ = stocked_db("shipment_tenancy")
    rid = reserve(conn, 5, "order:1")
    sid, _ = new_shipment(conn, "SHP-1", [(rid, 2)])
    before = dump_all(conn)
    missing = f"no shipment {sid} in business {B2}"
    rejected(lambda: s.shipment(conn, B2, sid), missing, s.NotFound)
    rejected(lambda: s.add_shipment_line(conn, B2, sid, rid, 1, "l"), missing, s.NotFound)
    rejected(lambda: s.dispatch(conn, B2, sid, "DHL", "d", TODAY, tracking_ref="T"), missing, s.NotFound)
    rejected(lambda: s.record_tracking_event(conn, B2, sid, "in_transit", "2026-10-23T12:00:00Z", "e"), missing, s.NotFound)
    assert dump_all(conn) == before
    mine = s.create_shipment(conn, B2, "SHP-1", "c")["shipment_id"]                    # the same number, in B2
    rejected(lambda: s.add_shipment_line(conn, B2, mine, rid, 1, "l"), f"no reservation {rid} in business {B2}", s.NotFound)


def test_bad_shipment_input_is_rejected_before_anything_is_written():
    conn, _ = stocked_db("shipment_bad_input")
    rid = reserve(conn, 5, "order:1")
    sid, _ = new_shipment(conn, "SHP-1", [(rid, 2)])
    before = dump_all(conn)
    for number in ("", "  ", None, 7):
        rejected(lambda: s.create_shipment(conn, B1, number, "k"), "shipment_number must be", ValueError)
    for qty in (0, -1, 1.5, "2"):
        rejected(lambda: s.add_shipment_line(conn, B1, sid, rid, qty, "k"), "quantity must be", ValueError)
        rejected(lambda: s.open_stock(conn, B1, ITEM, qty, "count", "k"), "quantity must be", ValueError)
        rejected(lambda: s.receive_and_reserve(conn, B1, ITEM, 5, "po", "order", "k", reserve_quantity=qty),
                 "reserve_quantity must be", ValueError)
    rejected(lambda: s.add_shipment_line(conn, B1, 0, rid, 1, "k"), "shipment_id must be", ValueError)
    rejected(lambda: s.receive_and_reserve(conn, B1, ITEM, 5, "po", "", "k"), "order_ref must be", ValueError)
    rejected(lambda: s.open_stock(conn, B1, ITEM, 1, "", "k"), "source_ref must be", ValueError)
    rejected(lambda: s.dispatch(conn, B1, sid, None, "k", TODAY, label_ref="L"), "carrier must be", ValueError)
    rejected(lambda: s.record_tracking_event(conn, B1, sid, "in_transit", "2026-10-23T12:00:00Z", "k", detail=""),
             "detail must be", ValueError)
    assert dump_all(conn) == before
    assert not conn.in_transaction
    s.create_shipment(conn, B1, "SHP-2", "k")                                          # the connection is still usable


if __name__ == "__main__":
    print(f"Python {sys.version.split()[0]}, SQLite {sqlite3.sqlite_version}")
    tests = [(n, f) for n, f in globals().items() if n.startswith("test_") and callable(f)]
    for name, fn in tests:
        fn()
        print(f"PASS {name}")
    print(f"\n{len(tests)} tests passed")
