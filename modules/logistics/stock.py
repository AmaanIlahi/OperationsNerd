"""
LogisticsNerd stock engine, v3: v1, plus lots and expiry (v2), plus opening stock,
receive-and-reserve and shipment records (v3). Standalone: stdlib sqlite3 only,
nothing from operations_nerd/, no LLM calls.

Stock is a ledger. on_hand is the sum of stock_movements, reserved is the
unshipped part of active stock_reservations, and available is on_hand minus
reserved. No caller edits a running total: a reservation's quantity_shipped
is moved by a database trigger when its shipment is written, and can never
differ from the sum of its shipments.

Every command runs in one BEGIN IMMEDIATE transaction and takes an idempotency
key that is unique per business. Replaying a key with the same payload returns
the original result and writes nothing; the same key with a different payload
raises IdempotencyConflict. A command that fails records nothing, so its key
stays free.

Lots are opt-in per item (track_lots). An item without them behaves exactly as
in v1. For a tracked item every receipt names a lot, reserve allocates lots
earliest expiry first and skips held and expired ones, and ship ships from the
reservation's allocations lot by lot. Expiry is judged against an as_of date
the caller passes in ('YYYY-MM-DD'), never the clock. A lot is expired on any
date after its expiry_date.

A shipment is a record: create_shipment, add_shipment_line for each reservation
it carries, then dispatch once, which writes the stock movements and the
dispatch together. Its status (draft, dispatched, delivered) is derived from the
dispatch and the tracking events, which only append. ship still ships a
reservation directly, without a record.

Open connections with connect(); it sets autocommit mode, which the explicit
BEGIN IMMEDIATE relies on, and turns on the foreign keys that tie each
shipment to its reservation and each allocation to its lot.
"""

import datetime
import json
import sqlite3
from pathlib import Path

SCHEMA = Path(__file__).resolve().with_name("schema.sql")
LOCATION_ID = 1             # v1 has a single location; the column is there for later
BUSY_TIMEOUT_SECONDS = 10   # how long a command waits for another writer to finish


class StockError(Exception):
    pass


class IdempotencyConflict(StockError):
    """The key was already used by this business for a different command or payload."""


class NotFound(StockError):
    """No such reservation or lot in this business."""


class InvalidState(StockError):
    """The reservation is no longer active, the lot's hold is already as asked, or lots came too late for the item."""


class OverShipment(StockError):
    """The shipment is larger than what is still reserved."""


class LotMismatch(StockError):
    """A lot was given for an item without lots, none for a lot-tracked item, or a lot's expiry differs."""


class LotUnavailable(StockError):
    """The shipment needs stock from allocated lots that are held or expired."""


def connect(path: str) -> sqlite3.Connection:
    conn = sqlite3.connect(path, isolation_level=None, timeout=BUSY_TIMEOUT_SECONDS)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA journal_mode=WAL")
    return conn


def init_db(conn: sqlite3.Connection) -> None:
    conn.executescript(SCHEMA.read_text())


# ---------------------------------------------------------------- commands

def track_lots(conn, business_id: int, item_id: int, idempotency_key: str) -> dict:
    """Turns on lot tracking for an item. Only before the item has any stock; it can't be turned off.

    Tracking an item that is already tracked changes nothing.
    """
    _positive_int("business_id", business_id)
    _positive_int("item_id", item_id)

    def work():
        if not _lot_tracked(conn, business_id, item_id):
            if conn.execute("SELECT 1 FROM stock_movements WHERE business_id = ? AND item_id = ? LIMIT 1",
                            (business_id, item_id)).fetchone():
                raise InvalidState(f"item {item_id} already has stock without lots")
            conn.execute("INSERT INTO stock_lot_tracking (business_id, item_id) VALUES (?, ?)", (business_id, item_id))
        return {"item_id": item_id, "lot_tracked": True}

    return _run(conn, business_id, idempotency_key, "track_lots", {"item_id": item_id}, work)


def receive(conn, business_id: int, item_id: int, quantity: int, source_ref: str, idempotency_key: str,
            lot_number: str | None = None, expiry_date: str | None = None) -> dict:
    """Adds stock. The handoff from the buying side: source_ref is the purchase order line received.

    A lot-tracked item needs a lot_number, and any other item takes none. The first receipt of a lot
    creates it with its expiry_date (None: it does not expire); later receipts of the same lot must
    give the same expiry_date. One purchase order line can be received as several lots, one call each.
    """
    _check_stock_input(business_id, item_id, quantity, source_ref, lot_number, expiry_date)

    def work():
        return _add_stock(conn, business_id, item_id, quantity, "receipt", source_ref, idempotency_key, lot_number,
                          expiry_date, "receive")

    payload = _stock_payload(item_id, quantity, source_ref, lot_number, expiry_date)
    return _run(conn, business_id, idempotency_key, "receive", payload, work)


def open_stock(conn, business_id: int, item_id: int, quantity: int, source_ref: str, idempotency_key: str,
               lot_number: str | None = None, expiry_date: str | None = None) -> dict:
    """Records stock that was there before the system: an 'opening' movement, not a supplier receipt.

    Only before any receipt or shipment of the item; several opening movements are fine (one per lot,
    say). source_ref says where the figure came from, for example a stock count. Lots work as in receive,
    so a lot-tracked item needs track_lots first.
    """
    _check_stock_input(business_id, item_id, quantity, source_ref, lot_number, expiry_date)

    def work():
        if conn.execute("""SELECT 1 FROM stock_movements WHERE business_id = ? AND item_id = ?
                           AND movement_type <> 'opening' LIMIT 1""", (business_id, item_id)).fetchone():
            raise InvalidState(f"item {item_id} already has receipts or shipments: opening stock comes first")
        return _add_stock(conn, business_id, item_id, quantity, "opening", source_ref, idempotency_key, lot_number,
                          expiry_date, "open_stock")

    payload = _stock_payload(item_id, quantity, source_ref, lot_number, expiry_date)
    return _run(conn, business_id, idempotency_key, "open_stock", payload, work)


def receive_and_reserve(conn, business_id: int, item_id: int, quantity: int, source_ref: str, order_ref: str,
                        idempotency_key: str, lot_number: str | None = None, expiry_date: str | None = None,
                        as_of: str | None = None, reserve_quantity: int | None = None) -> dict:
    """Receives stock and reserves it for a waiting order line, in one transaction.

    source_ref is the purchase reference, order_ref the customer order line. reserve_quantity (default:
    all of quantity) is how much of the receipt goes to that line; the rest is free stock. For a
    lot-tracked item as_of is required and the reservation is allocated from the lot just received, not
    earliest expiry first: these are the goods bought for that order. If that lot is held or expired on
    as_of it raises LotUnavailable and writes nothing, receipt included.
    """
    _check_stock_input(business_id, item_id, quantity, source_ref, lot_number, expiry_date)
    _text("order_ref", order_ref)
    if as_of is not None:
        _date("as_of", as_of)
    if reserve_quantity is not None:
        _positive_int("reserve_quantity", reserve_quantity)
        if reserve_quantity > quantity:
            raise ValueError(f"reserve_quantity {reserve_quantity} is more than the {quantity} received")
    to_reserve = quantity if reserve_quantity is None else reserve_quantity

    def work():
        if _lot_tracked(conn, business_id, item_id) and as_of is None:
            raise ValueError(f"item {item_id} is lot-tracked: receive_and_reserve needs as_of")
        result = _add_stock(conn, business_id, item_id, quantity, "receipt", source_ref, idempotency_key, lot_number,
                            expiry_date, "receive_and_reserve")
        if "lot_id" in result:
            lot = _lot(conn, business_id, result["lot_id"])
            if lot["on_hold"] or _expired(lot["expiry_date"], as_of):
                state = "held" if lot["on_hold"] else f"expired on {as_of}"
                raise LotUnavailable(f"lot {lot_number!r} is {state}: nothing received or reserved")
            reservation_id = _insert_lot_reservation(conn, business_id, item_id, order_ref, idempotency_key,
                                                     [(lot, to_reserve)], as_of)
        else:
            reservation_id = conn.execute(
                """INSERT INTO stock_reservations (business_id, item_id, location_id, order_ref, quantity, idempotency_key)
                   VALUES (?, ?, ?, ?, ?, ?)""",
                (business_id, item_id, LOCATION_ID, order_ref, to_reserve, idempotency_key)).lastrowid
        result.update(reservation_id=reservation_id, order_ref=order_ref, reserved=to_reserve)
        return result

    payload = _stock_payload(item_id, quantity, source_ref, lot_number, expiry_date)
    payload.update(order_ref=order_ref, reserve_quantity=to_reserve)
    if as_of is not None:
        payload["as_of"] = as_of
    return _run(conn, business_id, idempotency_key, "receive_and_reserve", payload, work)


def reserve(conn, business_id: int, item_id: int, quantity: int, order_ref: str, idempotency_key: str,
            as_of: str | None = None) -> dict:
    """Reserves up to what is available. shortage is the part that could not be reserved, for the buying side.

    With nothing available no reservation row is written and reservation_id is None.

    For a lot-tracked item as_of is required. Only usable lots count: not held, not expired on as_of,
    with free stock. They are allocated earliest expiry first (lots without expiry last), and the
    result lists the allocations. as_of is ignored for other items.
    """
    _positive_int("business_id", business_id)
    _positive_int("item_id", item_id)
    _positive_int("quantity", quantity)
    _text("order_ref", order_ref)
    if as_of is not None:
        _date("as_of", as_of)

    def work():
        if _lot_tracked(conn, business_id, item_id):
            return _reserve_lots(conn, business_id, item_id, quantity, order_ref, idempotency_key, as_of)
        reserved_now = min(quantity, max(available(conn, business_id, item_id), 0))
        reservation_id = None
        if reserved_now:
            reservation_id = conn.execute(
                """INSERT INTO stock_reservations (business_id, item_id, location_id, order_ref, quantity, idempotency_key)
                   VALUES (?, ?, ?, ?, ?, ?)""",
                (business_id, item_id, LOCATION_ID, order_ref, reserved_now, idempotency_key),
            ).lastrowid
        return {"reservation_id": reservation_id, "item_id": item_id, "requested": quantity,
                "reserved": reserved_now, "shortage": quantity - reserved_now}

    payload = {"item_id": item_id, "quantity": quantity, "order_ref": order_ref}
    if as_of is not None:
        payload["as_of"] = as_of
    return _run(conn, business_id, idempotency_key, "reserve", payload, work)


def release(conn, business_id: int, reservation_id: int, idempotency_key: str) -> dict:
    """Frees the unshipped remainder of an active reservation. What was already shipped stays shipped.

    For a lot-tracked item this also frees what is left of each allocation; the allocation rows stay.
    """
    _positive_int("business_id", business_id)
    _positive_int("reservation_id", reservation_id)

    def work():
        r = _active_reservation(conn, business_id, reservation_id)
        conn.execute("UPDATE stock_reservations SET status = 'released' WHERE id = ?", (reservation_id,))
        return {"reservation_id": reservation_id, "released": r["quantity"] - r["quantity_shipped"],
                "status": "released"}

    return _run(conn, business_id, idempotency_key, "release", {"reservation_id": reservation_id}, work)


def ship(conn, business_id: int, reservation_id: int, quantity: int, idempotency_key: str,
         as_of: str | None = None) -> dict:
    """Ships part or all of an active reservation by writing one negative movement linked to it.

    The database trigger on that insert raises quantity_shipped and marks the reservation
    fulfilled once everything reserved has shipped.

    For a lot-tracked item as_of is required. It ships from the reservation's allocations, earliest
    expiry first, one movement per lot, and passes over allocations whose lot is held or expired on
    as_of. If what is left in usable allocations is less than quantity it raises LotUnavailable and
    ships nothing. The result has lots (one entry per movement) instead of movement_id.
    """
    _positive_int("business_id", business_id)
    _positive_int("reservation_id", reservation_id)
    _positive_int("quantity", quantity)
    if as_of is not None:
        _date("as_of", as_of)

    def work():
        r = _active_reservation(conn, business_id, reservation_id)
        remaining = r["quantity"] - r["quantity_shipped"]
        if quantity > remaining:
            raise OverShipment(f"cannot ship {quantity}: reservation {reservation_id} has {remaining} left to ship")
        lots_shipped = None
        if _lot_tracked(conn, business_id, r["item_id"]):
            lots_shipped = _ship_lots(conn, business_id, r, quantity, idempotency_key, as_of)
        else:
            movement_id = _insert_movement(conn, business_id, r["item_id"], r["location_id"], -quantity, "shipment",
                                           f"reservation:{reservation_id}", idempotency_key, reservation_id)
        after = conn.execute("SELECT quantity, quantity_shipped, status FROM stock_reservations WHERE id = ?",
                             (reservation_id,)).fetchone()
        result = {"reservation_id": reservation_id, "shipped": quantity,
                  "quantity_shipped": after["quantity_shipped"], "remaining": after["quantity"] - after["quantity_shipped"],
                  "status": after["status"]}
        if lots_shipped is None:
            result["movement_id"] = movement_id
        else:
            result["lots"] = lots_shipped
        return result

    payload = {"reservation_id": reservation_id, "quantity": quantity}
    if as_of is not None:
        payload["as_of"] = as_of
    return _run(conn, business_id, idempotency_key, "ship", payload, work)


def hold_lot(conn, business_id: int, lot_id: int, reason: str, idempotency_key: str) -> dict:
    """Puts a lot on hold: it gets no new allocations and nothing ships from it until release_hold.

    Allocations already on the lot stay, and still count as reserved. allocated in the result is
    how much of the lot they still hold, so the caller can decide to release those reservations
    and reserve again from other lots.
    """
    return _hold_event(conn, business_id, lot_id, reason, idempotency_key, "hold")


def release_hold(conn, business_id: int, lot_id: int, reason: str, idempotency_key: str) -> dict:
    """Takes a lot off hold. Its allocations can ship again, and its free stock can be allocated."""
    return _hold_event(conn, business_id, lot_id, reason, idempotency_key, "release")


def create_shipment(conn, business_id: int, shipment_number: str, idempotency_key: str) -> dict:
    """Starts a draft shipment. shipment_number is unique per business."""
    _positive_int("business_id", business_id)
    _text("shipment_number", shipment_number)

    def work():
        taken = conn.execute("SELECT id FROM stock_shipments WHERE business_id = ? AND shipment_number = ?",
                             (business_id, shipment_number)).fetchone()
        if taken:
            raise InvalidState(f"shipment number {shipment_number!r} is already shipment {taken['id']}")
        shipment_id = conn.execute("""INSERT INTO stock_shipments (business_id, shipment_number, idempotency_key)
                                      VALUES (?, ?, ?)""", (business_id, shipment_number, idempotency_key)).lastrowid
        return {"shipment_id": shipment_id, "shipment_number": shipment_number, "status": "draft"}

    return _run(conn, business_id, idempotency_key, "create_shipment", {"shipment_number": shipment_number}, work)


def add_shipment_line(conn, business_id: int, shipment_id: int, reservation_id: int, quantity: int,
                      idempotency_key: str) -> dict:
    """Puts part or all of an active reservation on a draft shipment. Nothing moves until dispatch.

    A reservation can be on several shipments, once each. quantity can't exceed what is left to ship
    minus what is already on other draft shipments.
    """
    _positive_int("business_id", business_id)
    _positive_int("shipment_id", shipment_id)
    _positive_int("reservation_id", reservation_id)
    _positive_int("quantity", quantity)

    def work():
        shp = shipment(conn, business_id, shipment_id)
        if shp["status"] != "draft":
            raise InvalidState(f"shipment {shipment_id} is {shp['status']}: lines are added before dispatch")
        if any(line["reservation_id"] == reservation_id for line in shp["lines"]):
            raise InvalidState(f"reservation {reservation_id} is already on shipment {shipment_id}")
        r = _active_reservation(conn, business_id, reservation_id)
        planned = conn.execute(
            """SELECT COALESCE(SUM(l.quantity), 0) FROM stock_shipment_lines l
               WHERE l.business_id = ? AND l.reservation_id = ?
                 AND NOT EXISTS (SELECT 1 FROM stock_shipment_dispatches d
                                 WHERE d.business_id = l.business_id AND d.shipment_id = l.shipment_id)""",
            (business_id, reservation_id)).fetchone()[0]
        left = r["quantity"] - r["quantity_shipped"] - planned
        if quantity > left:
            raise OverShipment(f"cannot add {quantity}: reservation {reservation_id} has {left} left to ship "
                               f"that is not already on a draft shipment")
        line_id = conn.execute("""INSERT INTO stock_shipment_lines (business_id, shipment_id, reservation_id, quantity,
                                                                    idempotency_key)
                                  VALUES (?, ?, ?, ?, ?)""",
                               (business_id, shipment_id, reservation_id, quantity, idempotency_key)).lastrowid
        return {"line_id": line_id, "shipment_id": shipment_id, "reservation_id": reservation_id, "quantity": quantity}

    payload = {"shipment_id": shipment_id, "reservation_id": reservation_id, "quantity": quantity}
    return _run(conn, business_id, idempotency_key, "add_shipment_line", payload, work)


def dispatch(conn, business_id: int, shipment_id: int, carrier: str, idempotency_key: str, as_of: str,
             label_ref: str | None = None, tracking_ref: str | None = None) -> dict:
    """Dispatches a draft shipment, once: writes every line's shipment movements and the dispatch record together.

    Needs a carrier and a label_ref or tracking_ref. as_of is the dispatch date. Each line ships as
    ship does: one movement for an item without lots, or one per lot for a lot-tracked item, by the
    same rules (earliest expiry first, past held and expired lots, LotUnavailable if they can't cover
    the line). If any line can't ship, nothing is written. Returns the shipment, as shipment() does.
    """
    _positive_int("business_id", business_id)
    _positive_int("shipment_id", shipment_id)
    _text("carrier", carrier)
    _date("as_of", as_of)
    for name, value in (("label_ref", label_ref), ("tracking_ref", tracking_ref)):
        if value is not None:
            _text(name, value)
    if label_ref is None and tracking_ref is None:
        raise ValueError("dispatch needs a label_ref or a tracking_ref")

    def work():
        shp = shipment(conn, business_id, shipment_id)
        if shp["status"] != "draft":
            raise InvalidState(f"shipment {shipment_id} is already {shp['status']}")
        if not shp["lines"]:
            raise InvalidState(f"shipment {shipment_id} has no lines")
        source_ref = f"shipment:{shp['shipment_number']}"
        for line in shp["lines"]:
            r = _active_reservation(conn, business_id, line["reservation_id"])
            remaining = r["quantity"] - r["quantity_shipped"]
            if line["quantity"] > remaining:
                raise OverShipment(f"cannot dispatch line {line['line_id']}: reservation {r['id']} has {remaining} "
                                   f"left to ship, the line has {line['quantity']}")
            key = f"{idempotency_key}/line:{line['line_id']}"
            if _lot_tracked(conn, business_id, r["item_id"]):
                _ship_lots(conn, business_id, r, line["quantity"], key, as_of, source_ref, shipment_id, line["line_id"])
            else:
                _insert_movement(conn, business_id, r["item_id"], r["location_id"], -line["quantity"], "shipment",
                                 source_ref, key, r["id"], as_of=as_of, shipment_id=shipment_id,
                                 shipment_line_id=line["line_id"])
        conn.execute("""INSERT INTO stock_shipment_dispatches (business_id, shipment_id, carrier, label_ref, tracking_ref,
                                                               as_of, idempotency_key)
                        VALUES (?, ?, ?, ?, ?, ?, ?)""",
                     (business_id, shipment_id, carrier, label_ref, tracking_ref, as_of, idempotency_key))
        return shipment(conn, business_id, shipment_id)

    payload = {"shipment_id": shipment_id, "carrier": carrier, "label_ref": label_ref, "tracking_ref": tracking_ref,
               "as_of": as_of}
    return _run(conn, business_id, idempotency_key, "dispatch", payload, work)


TRACKING_EVENTS = ("in_transit", "out_for_delivery", "delivery_failed", "delivered", "exception")


def record_tracking_event(conn, business_id: int, shipment_id: int, event: str, occurred_at: str,
                          idempotency_key: str, detail: str | None = None) -> dict:
    """Appends a carrier tracking event to a dispatched shipment. It never changes stock.

    occurred_at is the carrier's time, 'YYYY-MM-DDTHH:MM:SSZ'. A delivered event makes the shipment
    delivered for good: events that arrive later, whatever their time, don't move it back.
    """
    _positive_int("business_id", business_id)
    _positive_int("shipment_id", shipment_id)
    if event not in TRACKING_EVENTS:
        raise ValueError(f"event must be one of {', '.join(TRACKING_EVENTS)}, got {event!r}")
    _timestamp("occurred_at", occurred_at)
    if detail is not None:
        _text("detail", detail)

    def work():
        if shipment(conn, business_id, shipment_id)["status"] == "draft":
            raise InvalidState(f"shipment {shipment_id} is not dispatched yet")
        event_id = conn.execute("""INSERT INTO stock_shipment_events (business_id, shipment_id, event, occurred_at, detail,
                                                                      idempotency_key)
                                   VALUES (?, ?, ?, ?, ?, ?)""",
                                (business_id, shipment_id, event, occurred_at, detail, idempotency_key)).lastrowid
        return {"event_id": event_id, "shipment_id": shipment_id, "event": event,
                "status": shipment(conn, business_id, shipment_id)["status"]}

    payload = {"shipment_id": shipment_id, "event": event, "occurred_at": occurred_at, "detail": detail}
    return _run(conn, business_id, idempotency_key, "record_tracking_event", payload, work)


# ---------------------------------------------------------------- queries

def on_hand(conn, business_id: int, item_id: int) -> int:
    return conn.execute(f"SELECT {_ON_HAND}", (business_id, item_id)).fetchone()[0]


def reserved(conn, business_id: int, item_id: int) -> int:
    return conn.execute(f"SELECT {_RESERVED}", (business_id, item_id)).fetchone()[0]


def available(conn, business_id: int, item_id: int) -> int:
    """on_hand minus reserved, read in one statement so both sides come from the same snapshot.

    For a lot-tracked item this includes held and expired lots; lots() says what is usable.
    """
    return conn.execute(f"SELECT ({_ON_HAND}) - ({_RESERVED})", (business_id, item_id, business_id, item_id)).fetchone()[0]


_ON_HAND = "(SELECT COALESCE(SUM(quantity), 0) FROM stock_movements WHERE business_id = ? AND item_id = ?)"
_RESERVED = """(SELECT COALESCE(SUM(quantity - quantity_shipped), 0) FROM stock_reservations
               WHERE business_id = ? AND item_id = ? AND status = 'active')"""


def lots(conn, business_id: int, item_id: int, as_of: str | None = None) -> list[dict]:
    """The item's lots, earliest expiry first and lots without expiry last.

    Each has on_hand, allocated (reserved and not yet shipped), free (on_hand - allocated) and
    on_hold. With as_of, each also has expired and usable (not held, not expired, something free).
    """
    if as_of is not None:
        _date("as_of", as_of)
    rows = conn.execute(f"""SELECT {_LOT_COLUMNS} FROM stock_lot_levels WHERE business_id = ? AND item_id = ?
                            ORDER BY expiry_date IS NULL, expiry_date, lot_id""", (business_id, item_id))
    out = []
    for row in rows:
        lot = _lot_dict(row)
        if as_of is not None:
            lot["expired"] = _expired(lot["expiry_date"], as_of)
            lot["usable"] = not lot["on_hold"] and not lot["expired"] and lot["free"] > 0
        out.append(lot)
    return out


def recall(conn, business_id: int, lot_id: int) -> dict:
    """Everything a lot went to: each reservation it was allocated to, with order_ref, status, how much
    was allocated from the lot and how much of that has shipped. Released reservations are included.
    """
    _positive_int("business_id", business_id)
    _positive_int("lot_id", lot_id)
    lot = _lot(conn, business_id, lot_id)
    received = conn.execute("""SELECT COALESCE(SUM(quantity), 0) FROM stock_movements
                               WHERE business_id = ? AND lot_id = ? AND movement_type IN ('receipt', 'opening')""",
                            (business_id, lot_id)).fetchone()[0]
    rows = conn.execute(
        """SELECT a.reservation_id, r.order_ref, r.status, a.quantity AS allocated,
                  COALESCE((SELECT -SUM(m.quantity) FROM stock_movements m
                            WHERE m.business_id = a.business_id AND m.reservation_id = a.reservation_id
                              AND m.lot_id = a.lot_id), 0) AS shipped
           FROM stock_allocations a
           JOIN stock_reservations r ON r.business_id = a.business_id AND r.id = a.reservation_id
           WHERE a.business_id = ? AND a.lot_id = ?
           ORDER BY a.reservation_id""", (business_id, lot_id))
    return {"lot_id": lot_id, "item_id": lot["item_id"], "lot_number": lot["lot_number"],
            "expiry_date": lot["expiry_date"], "on_hold": lot["on_hold"], "received": received,
            "on_hand": lot["on_hand"], "reservations": [dict(r) for r in rows]}


_LOT_COLUMNS = "lot_id, item_id, lot_number, expiry_date, on_hand, allocated, on_hold"


def shipment(conn, business_id: int, shipment_id: int) -> dict:
    """A shipment: number, derived status (draft, dispatched, delivered), dispatch details, lines with
    the movements each one created, and tracking events in the order they were recorded."""
    row = conn.execute("SELECT * FROM stock_shipment_status WHERE shipment_id = ? AND business_id = ?",
                       (shipment_id, business_id)).fetchone()
    if row is None:
        raise NotFound(f"no shipment {shipment_id} in business {business_id}")
    lines = []
    for line in conn.execute("""SELECT l.id AS line_id, l.reservation_id, r.order_ref, r.item_id, l.quantity
                                FROM stock_shipment_lines l
                                JOIN stock_reservations r ON r.business_id = l.business_id AND r.id = l.reservation_id
                                WHERE l.business_id = ? AND l.shipment_id = ? ORDER BY l.id""",
                             (business_id, shipment_id)).fetchall():
        moves = conn.execute("""SELECT m.id AS movement_id, m.lot_id, lot.lot_number, -m.quantity AS quantity
                                FROM stock_movements m LEFT JOIN stock_lots lot ON lot.id = m.lot_id
                                WHERE m.business_id = ? AND m.shipment_line_id = ? ORDER BY m.id""",
                             (business_id, line["line_id"]))
        lines.append(dict(line) | {"movements": [dict(m) for m in moves]})
    events = conn.execute("""SELECT id AS event_id, event, occurred_at, detail FROM stock_shipment_events
                             WHERE business_id = ? AND shipment_id = ? ORDER BY id""", (business_id, shipment_id))
    out = {k: row[k] for k in ("shipment_id", "shipment_number", "status", "carrier", "label_ref", "tracking_ref",
                               "dispatched_on")}
    return out | {"lines": lines, "events": [dict(e) for e in events]}


# ---------------------------------------------------------------- internals

def _run(conn, business_id, idempotency_key, command, payload, work):
    """Runs work() in one BEGIN IMMEDIATE transaction, or returns the stored result of an earlier run of this key."""
    _text("idempotency_key", idempotency_key)
    body = json.dumps(payload, sort_keys=True)
    conn.execute("BEGIN IMMEDIATE")
    try:
        row = conn.execute("SELECT command, payload, result FROM stock_commands WHERE business_id = ? AND idempotency_key = ?",
                           (business_id, idempotency_key)).fetchone()
        if row is not None:
            if (row["command"], row["payload"]) != (command, body):
                raise IdempotencyConflict(f"key {idempotency_key!r} was already used for {row['command']} {row['payload']}")
            result = row["result"]
        else:
            result = json.dumps(work(), sort_keys=True)
            conn.execute("""INSERT INTO stock_commands (business_id, idempotency_key, command, payload, result)
                            VALUES (?, ?, ?, ?, ?)""", (business_id, idempotency_key, command, body, result))
        conn.execute("COMMIT")
    except BaseException:
        if conn.in_transaction:
            conn.execute("ROLLBACK")
        raise
    return json.loads(result)


def _insert_movement(conn, business_id, item_id, location_id, quantity, movement_type, source_ref, idempotency_key,
                     reservation_id=None, lot_id=None, as_of=None, shipment_id=None, shipment_line_id=None):
    return conn.execute(
        """INSERT INTO stock_movements (business_id, item_id, location_id, quantity, movement_type, reservation_id,
                                        lot_id, as_of, shipment_id, shipment_line_id, source_ref, idempotency_key)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        (business_id, item_id, location_id, quantity, movement_type, reservation_id, lot_id, as_of, shipment_id,
         shipment_line_id, source_ref, idempotency_key),
    ).lastrowid


def _check_stock_input(business_id, item_id, quantity, source_ref, lot_number, expiry_date):
    _positive_int("business_id", business_id)
    _positive_int("item_id", item_id)
    _positive_int("quantity", quantity)
    _text("source_ref", source_ref)
    if lot_number is not None:
        _text("lot_number", lot_number)
    if expiry_date is not None:
        _date("expiry_date", expiry_date)
        if lot_number is None:
            raise ValueError("expiry_date needs a lot_number")


def _stock_payload(item_id, quantity, source_ref, lot_number, expiry_date) -> dict:
    payload = {"item_id": item_id, "quantity": quantity, "source_ref": source_ref}
    if lot_number is not None:
        payload.update(lot_number=lot_number, expiry_date=expiry_date)
    return payload


def _add_stock(conn, business_id, item_id, quantity, movement_type, source_ref, idempotency_key, lot_number,
               expiry_date, command) -> dict:
    """Writes one positive movement (a receipt or opening stock), into its lot if the item is lot-tracked."""
    tracked = _lot_tracked(conn, business_id, item_id)
    if tracked and lot_number is None:
        raise LotMismatch(f"item {item_id} is lot-tracked: {command} needs a lot_number")
    if not tracked and lot_number is not None:
        raise LotMismatch(f"item {item_id} is not lot-tracked: {command} takes no lot_number")
    lot_id = _lot_for_receipt(conn, business_id, item_id, lot_number, expiry_date) if tracked else None
    movement_id = _insert_movement(conn, business_id, item_id, LOCATION_ID, quantity, movement_type, source_ref,
                                   idempotency_key, lot_id=lot_id)
    result = {"movement_id": movement_id, "item_id": item_id, "received": quantity}
    if tracked:
        result.update(lot_id=lot_id, lot_number=lot_number, expiry_date=expiry_date)
    return result


def _active_reservation(conn, business_id, reservation_id):
    r = conn.execute("SELECT * FROM stock_reservations WHERE id = ? AND business_id = ?",
                     (reservation_id, business_id)).fetchone()
    if r is None:
        raise NotFound(f"no reservation {reservation_id} in business {business_id}")
    if r["status"] != "active":
        raise InvalidState(f"reservation {reservation_id} is {r['status']}")
    return r


def _lot_tracked(conn, business_id, item_id) -> bool:
    return conn.execute("SELECT 1 FROM stock_lot_tracking WHERE business_id = ? AND item_id = ?",
                        (business_id, item_id)).fetchone() is not None


def _lot_for_receipt(conn, business_id, item_id, lot_number, expiry_date) -> int:
    """The lot's id, creating the lot on its first receipt. A later receipt must agree on the expiry date."""
    lot = conn.execute("SELECT id, expiry_date FROM stock_lots WHERE business_id = ? AND item_id = ? AND lot_number = ?",
                       (business_id, item_id, lot_number)).fetchone()
    if lot is None:
        return conn.execute("INSERT INTO stock_lots (business_id, item_id, lot_number, expiry_date) VALUES (?, ?, ?, ?)",
                            (business_id, item_id, lot_number, expiry_date)).lastrowid
    if lot["expiry_date"] != expiry_date:
        raise LotMismatch(f"lot {lot_number!r} of item {item_id} expires {lot['expiry_date'] or 'never'}, "
                          f"not {expiry_date or 'never'}")
    return lot["id"]


def _reserve_lots(conn, business_id, item_id, quantity, order_ref, idempotency_key, as_of):
    """Allocates usable lots earliest expiry first, then writes the allocations and, last, their reservation."""
    if as_of is None:
        raise ValueError(f"item {item_id} is lot-tracked: reserve needs as_of")
    plan, left = [], quantity
    for lot in lots(conn, business_id, item_id, as_of):
        if left and lot["usable"]:
            take = min(left, lot["free"])
            plan.append((lot, take))
            left -= take
    reserved_now = quantity - left
    reservation_id = None
    if reserved_now:
        reservation_id = _insert_lot_reservation(conn, business_id, item_id, order_ref, idempotency_key, plan, as_of)
    return {"reservation_id": reservation_id, "item_id": item_id, "requested": quantity,
            "reserved": reserved_now, "shortage": left,
            "allocations": [{"lot_id": lot["lot_id"], "lot_number": lot["lot_number"],
                             "expiry_date": lot["expiry_date"], "quantity": take} for lot, take in plan]}


def _insert_lot_reservation(conn, business_id, item_id, order_ref, idempotency_key, plan, as_of) -> int:
    """Writes the allocations [(lot, quantity)] and, last, their reservation for the plan's total."""
    # The allocations reference the reservation before it exists (a deferred foreign key), so its id is
    # chosen here. BEGIN IMMEDIATE holds the write lock, so no other writer can take it meanwhile.
    reservation_id = conn.execute("SELECT COALESCE(MAX(id), 0) + 1 FROM stock_reservations").fetchone()[0]
    for lot, take in plan:
        conn.execute("""INSERT INTO stock_allocations (business_id, reservation_id, item_id, lot_id, quantity, as_of)
                        VALUES (?, ?, ?, ?, ?, ?)""", (business_id, reservation_id, item_id, lot["lot_id"], take, as_of))
    conn.execute("""INSERT INTO stock_reservations (id, business_id, item_id, location_id, order_ref, quantity,
                                                    idempotency_key)
                    VALUES (?, ?, ?, ?, ?, ?, ?)""",
                 (reservation_id, business_id, item_id, LOCATION_ID, order_ref, sum(take for _, take in plan),
                  idempotency_key))
    return reservation_id


def _ship_lots(conn, business_id, r, quantity, idempotency_key, as_of, source_ref=None, shipment_id=None,
               shipment_line_id=None):
    """Ships from the reservation's allocations earliest expiry first, one movement per lot, passing over
    held and expired lots. Each movement's key is the given key plus the lot, since one command writes several.
    """
    if as_of is None:
        raise ValueError(f"item {r['item_id']} is lot-tracked: ship needs as_of")
    allocations = conn.execute(
        """SELECT a.lot_id, v.lot_number, v.expiry_date, v.on_hold,
                  a.quantity + COALESCE((SELECT SUM(m.quantity) FROM stock_movements m
                                         WHERE m.business_id = a.business_id AND m.reservation_id = a.reservation_id
                                           AND m.lot_id = a.lot_id), 0) AS left_to_ship
           FROM stock_allocations a JOIN stock_lot_levels v ON v.lot_id = a.lot_id
           WHERE a.business_id = ? AND a.reservation_id = ?
           ORDER BY v.expiry_date IS NULL, v.expiry_date, a.lot_id""", (business_id, r["id"])).fetchall()
    shipped, blocked, need = [], [], quantity
    for a in allocations:
        if not need or not a["left_to_ship"]:
            continue
        if a["on_hold"] or _expired(a["expiry_date"], as_of):
            blocked.append(f"{a['lot_number']} ({'held' if a['on_hold'] else 'expired'})")
            continue
        take = min(need, a["left_to_ship"])
        movement_id = _insert_movement(conn, business_id, r["item_id"], r["location_id"], -take, "shipment",
                                       source_ref or f"reservation:{r['id']}", f"{idempotency_key}/lot:{a['lot_id']}",
                                       r["id"], lot_id=a["lot_id"], as_of=as_of, shipment_id=shipment_id,
                                       shipment_line_id=shipment_line_id)
        shipped.append({"lot_id": a["lot_id"], "lot_number": a["lot_number"], "quantity": take,
                        "movement_id": movement_id})
        need -= take
    if need:
        raise LotUnavailable(f"cannot ship {quantity} from reservation {r['id']} on {as_of}: {need} of it is "
                             f"allocated in lots that are held or expired: {', '.join(blocked)}")
    return shipped


def _hold_event(conn, business_id, lot_id, reason, idempotency_key, event):
    _positive_int("business_id", business_id)
    _positive_int("lot_id", lot_id)
    _text("reason", reason)

    def work():
        lot = _lot(conn, business_id, lot_id)
        if lot["on_hold"] == (event == "hold"):
            raise InvalidState(f"lot {lot_id} is {'already' if lot['on_hold'] else 'not'} on hold")
        conn.execute("""INSERT INTO stock_lot_holds (business_id, lot_id, event, reason, idempotency_key)
                        VALUES (?, ?, ?, ?, ?)""", (business_id, lot_id, event, reason, idempotency_key))
        return {"lot_id": lot_id, "lot_number": lot["lot_number"], "on_hold": event == "hold",
                "allocated": lot["allocated"]}

    command = "hold_lot" if event == "hold" else "release_hold"
    return _run(conn, business_id, idempotency_key, command, {"lot_id": lot_id, "reason": reason}, work)


def _lot(conn, business_id, lot_id) -> dict:
    row = conn.execute(f"SELECT {_LOT_COLUMNS} FROM stock_lot_levels WHERE lot_id = ? AND business_id = ?",
                       (lot_id, business_id)).fetchone()
    if row is None:
        raise NotFound(f"no lot {lot_id} in business {business_id}")
    return _lot_dict(row)


def _lot_dict(row) -> dict:
    lot = dict(row)
    lot["on_hold"] = bool(lot["on_hold"])
    lot["free"] = lot["on_hand"] - lot["allocated"]
    return lot


def _expired(expiry_date, as_of) -> bool:
    return expiry_date is not None and as_of > expiry_date


def _positive_int(name, value):
    if type(value) is not int or value <= 0:
        raise ValueError(f"{name} must be a positive integer, got {value!r}")


def _text(name, value):
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{name} must be a non-empty string, got {value!r}")


def _timestamp(name, value):
    try:
        ok = isinstance(value, str) and \
            datetime.datetime.strptime(value, "%Y-%m-%dT%H:%M:%SZ").strftime("%Y-%m-%dT%H:%M:%SZ") == value
    except ValueError:
        ok = False
    if not ok:
        raise ValueError(f"{name} must be a UTC time as 'YYYY-MM-DDTHH:MM:SSZ', got {value!r}")


def _date(name, value):
    try:
        ok = isinstance(value, str) and datetime.date.fromisoformat(value).isoformat() == value
    except ValueError:
        ok = False
    if not ok:
        raise ValueError(f"{name} must be a date as 'YYYY-MM-DD', got {value!r}")
