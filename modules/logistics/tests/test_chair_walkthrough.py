"""
Harsh's reference test of Somaditya's schema, replayed with the stock engine's real commands.

A customer orders 15 chairs. 4 are in stock. Supplier A supplies 6 and supplier B supplies 5, each
in two deliveries, and each delivery is received and reserved for the order as it arrives. Two
shipments carry 6 and then 9. After every step on hand, reserved and sent must match Harsh's table.
His "to receive" column is left out: what is still due from suppliers belongs to purchasing.

Also checked: the order line is fully shipped only after both dispatches, delivery events change no
stock, and replaying any step with its key changes nothing. Harsh's contention check ran one reserve
after the other; here two connections race for the last 3 chairs at the same time. Run from the
repo root with the venv:

    python -B modules/logistics/tests/test_chair_walkthrough.py
"""

import sqlite3
import sys
import threading
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parent))
import stock as s
from test_stock import TRIALS, new_db
from test_shipments import dump_all, stock_rows

CHAIR = 301
LINE = "order:1/line:1"          # the customer's order line for the chairs
ORDERED = 15
TODAY = "2026-10-23"

REFERENCE = [                    # Harsh's table: step, on_hand, reserved, sent
    ("opening stock 4", 4, 0, 0),
    ("reserve 4", 4, 4, 0),
    ("receive A 3 and reserve", 7, 7, 0),
    ("receive A 3 and reserve", 10, 10, 0),
    ("dispatch shipment one (6)", 4, 4, 6),
    ("receive B 2 and reserve", 6, 6, 6),
    ("receive B 3 and reserve", 9, 9, 6),
    ("dispatch shipment two (9)", 0, 0, 15),
]


def state(conn, item=CHAIR) -> tuple[int, int, int]:
    """on_hand, reserved and sent. Sent is every shipment movement of the item, read from the ledger."""
    sent = conn.execute("""SELECT COALESCE(-SUM(quantity), 0) FROM stock_movements
                           WHERE item_id = ? AND movement_type = 'shipment'""", (item,)).fetchone()[0]
    return s.on_hand(conn, item), s.reserved(conn, item), sent


def line_shipped(conn) -> tuple[int, bool]:
    """How much of the order line has shipped, and whether it is fully shipped: all of it, every reservation fulfilled."""
    rows = conn.execute("SELECT quantity_shipped, status FROM stock_reservations WHERE order_ref = ?", (LINE,)).fetchall()
    shipped = sum(r["quantity_shipped"] for r in rows)
    return shipped, shipped == ORDERED and all(r["status"] == "fulfilled" for r in rows)


def run(conn, log, command, *args, **kwargs):
    """Runs a command, replays it at once with its key, and logs it so the whole walkthrough can be replayed."""
    result = command(conn, *args, **kwargs)
    log.append((command, args, kwargs, result))
    assert_replays_change_nothing(conn, log[-1:])
    return result


def assert_replays_change_nothing(conn, calls) -> None:
    before = dump_all(conn)
    for command, args, kwargs, result in calls:
        assert command(conn, *args, **kwargs) == result, (command.__name__, args, kwargs)
    assert dump_all(conn) == before


def ship(conn, log, number, lines, as_of) -> int:
    """Creates a shipment with lines [(reservation_id, quantity)] and dispatches it. Returns its id."""
    sid = run(conn, log, s.create_shipment, number, f"create-{number}")["shipment_id"]
    for rid, quantity in lines:
        run(conn, log, s.add_shipment_line, sid, rid, quantity, f"{number}/reservation:{rid}")
    run(conn, log, s.dispatch, sid, "DHL", f"dispatch-{number}", as_of, tracking_ref=f"TRK-{number}")
    return sid


def deliver(conn, log, sid, day) -> None:
    """The carrier's events up to delivery. They change no stock."""
    levels, rows = state(conn), stock_rows(conn)
    run(conn, log, s.record_tracking_event, sid, "in_transit", f"{day}T08:00:00Z", f"carrier-{sid}-1")
    out = run(conn, log, s.record_tracking_event, sid, "delivered", f"{day}T15:00:00Z", f"carrier-{sid}-2")
    assert out["status"] == "delivered", out
    assert state(conn) == levels and stock_rows(conn) == rows


def test_chairs_match_the_reference_table_after_every_step():
    conn, _ = new_db("chairs")
    log, table, fully = [], [], []

    def step(name):
        table.append((name, *state(conn)))
        fully.append(line_shipped(conn)[1])
        assert table[-1] == REFERENCE[len(table) - 1], (table[-1], REFERENCE[len(table) - 1])

    run(conn, log, s.open_stock, CHAIR, 4, "count:go-live", "open-chairs")
    step("opening stock 4")
    stock = run(conn, log, s.reserve, CHAIR, ORDERED, LINE, "reserve-line")
    assert (stock["reserved"], stock["shortage"]) == (4, 11), stock          # 11 is what purchasing has to buy
    step("reserve 4")
    a1 = run(conn, log, s.receive_and_reserve, CHAIR, 3, "po:A/delivery:1", LINE, "receive-A-1")
    step("receive A 3 and reserve")
    a2 = run(conn, log, s.receive_and_reserve, CHAIR, 3, "po:A/delivery:2", LINE, "receive-A-2")
    step("receive A 3 and reserve")
    one = ship(conn, log, "SHP-1", [(stock["reservation_id"], 4), (a1["reservation_id"], 2)], TODAY)
    step("dispatch shipment one (6)")
    assert line_shipped(conn) == (6, False)
    deliver(conn, log, one, "2026-10-24")
    b1 = run(conn, log, s.receive_and_reserve, CHAIR, 2, "po:B/delivery:1", LINE, "receive-B-1")
    step("receive B 2 and reserve")
    b2 = run(conn, log, s.receive_and_reserve, CHAIR, 3, "po:B/delivery:2", LINE, "receive-B-2")
    step("receive B 3 and reserve")
    two = ship(conn, log, "SHP-2", [(a1["reservation_id"], 1), (a2["reservation_id"], 3), (b1["reservation_id"], 2),
                                    (b2["reservation_id"], 3)], "2026-10-27")
    step("dispatch shipment two (9)")
    deliver(conn, log, two, "2026-10-28")

    assert table == REFERENCE
    assert fully == [False] * 7 + [True]                                    # fully shipped only after both dispatches
    assert line_shipped(conn) == (ORDERED, True)
    assert [len(s.shipment(conn, sid)["lines"]) for sid in (one, two)] == [2, 4]
    assert state(conn) == (0, 0, 15)
    assert_replays_change_nothing(conn, log)                                 # every step again, in order, with its key


def test_two_connections_racing_for_the_last_3_chairs_leave_exactly_one_winner():
    setup, path = new_db("chair_race")
    original = s.available

    def slow_available(conn, item_id):
        n = original(conn, item_id)
        time.sleep(0.05)                                                     # widen the gap between the check and the write
        return n
    s.available = slow_available
    try:
        for trial in range(TRIALS):
            item = CHAIR + 1000 + trial
            s.receive(setup, item, 10, f"po:{trial}/line:1", f"rcv-{trial}")
            s.reserve(setup, item, 7, f"order:{trial}-0", f"res-{trial}-0")   # 3 chairs left
            barrier, results, errors = threading.Barrier(2), [], []

            def go(n):
                conn = s.connect(path)                                       # one connection per racer
                try:
                    barrier.wait()
                    results.append(s.reserve(conn, item, 3, f"order:{trial}-{n}", f"res-{trial}-{n}"))
                except Exception as e:
                    errors.append(e)
                finally:
                    conn.close()
            threads = [threading.Thread(target=go, args=(n,)) for n in (1, 2)]
            [t.start() for t in threads]
            [t.join() for t in threads]
            assert not errors, (trial, errors)
            outcomes = sorted((r["reserved"], r["shortage"], r["reservation_id"] is None) for r in results)
            assert outcomes == [(0, 3, True), (3, 0, False)], (trial, results)   # one gets all 3, the other none
            assert state(setup, item) == (10, 10, 0), (trial, state(setup, item))
            assert s.available(setup, item) == 0
    finally:
        s.available = original


if __name__ == "__main__":
    print(f"Python {sys.version.split()[0]}, SQLite {sqlite3.sqlite_version}")
    tests = [(n, f) for n, f in globals().items() if n.startswith("test_") and callable(f)]
    for name, fn in tests:
        fn()
        print(f"PASS {name}")
    print(f"\n{len(tests)} tests passed")
