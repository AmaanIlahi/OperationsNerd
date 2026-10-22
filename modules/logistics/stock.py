"""
LogisticsNerd stock engine, v1. Standalone: stdlib sqlite3 only, nothing from
operations_nerd/, no LLM calls.

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

Open connections with connect(); it sets autocommit mode, which the explicit
BEGIN IMMEDIATE relies on, and turns on the foreign keys that tie each
shipment to its reservation.
"""

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
    """No such reservation in this business."""


class InvalidState(StockError):
    """The reservation is no longer active."""


class OverShipment(StockError):
    """The shipment is larger than what is still reserved."""


def connect(path: str) -> sqlite3.Connection:
    conn = sqlite3.connect(path, isolation_level=None, timeout=BUSY_TIMEOUT_SECONDS)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA journal_mode=WAL")
    return conn


def init_db(conn: sqlite3.Connection) -> None:
    conn.executescript(SCHEMA.read_text())


# ---------------------------------------------------------------- commands

def receive(conn, business_id: int, item_id: int, quantity: int, source_ref: str, idempotency_key: str) -> dict:
    """Adds stock. The handoff from the buying side: source_ref is the purchase order line received."""
    _positive_int("business_id", business_id)
    _positive_int("item_id", item_id)
    _positive_int("quantity", quantity)
    _text("source_ref", source_ref)

    def work():
        movement_id = _insert_movement(conn, business_id, item_id, LOCATION_ID, quantity, "receipt", source_ref,
                                       idempotency_key)
        return {"movement_id": movement_id, "item_id": item_id, "received": quantity}

    payload = {"item_id": item_id, "quantity": quantity, "source_ref": source_ref}
    return _run(conn, business_id, idempotency_key, "receive", payload, work)


def reserve(conn, business_id: int, item_id: int, quantity: int, order_ref: str, idempotency_key: str) -> dict:
    """Reserves up to what is available. shortage is the part that could not be reserved, for the buying side.

    With nothing available no reservation row is written and reservation_id is None.
    """
    _positive_int("business_id", business_id)
    _positive_int("item_id", item_id)
    _positive_int("quantity", quantity)
    _text("order_ref", order_ref)

    def work():
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
    return _run(conn, business_id, idempotency_key, "reserve", payload, work)


def release(conn, business_id: int, reservation_id: int, idempotency_key: str) -> dict:
    """Frees the unshipped remainder of an active reservation. What was already shipped stays shipped."""
    _positive_int("business_id", business_id)
    _positive_int("reservation_id", reservation_id)

    def work():
        r = _active_reservation(conn, business_id, reservation_id)
        conn.execute("UPDATE stock_reservations SET status = 'released' WHERE id = ?", (reservation_id,))
        return {"reservation_id": reservation_id, "released": r["quantity"] - r["quantity_shipped"],
                "status": "released"}

    return _run(conn, business_id, idempotency_key, "release", {"reservation_id": reservation_id}, work)


def ship(conn, business_id: int, reservation_id: int, quantity: int, idempotency_key: str) -> dict:
    """Ships part or all of an active reservation by writing one negative movement linked to it.

    The database trigger on that insert raises quantity_shipped and marks the reservation
    fulfilled once everything reserved has shipped.
    """
    _positive_int("business_id", business_id)
    _positive_int("reservation_id", reservation_id)
    _positive_int("quantity", quantity)

    def work():
        r = _active_reservation(conn, business_id, reservation_id)
        remaining = r["quantity"] - r["quantity_shipped"]
        if quantity > remaining:
            raise OverShipment(f"cannot ship {quantity}: reservation {reservation_id} has {remaining} left to ship")
        movement_id = _insert_movement(conn, business_id, r["item_id"], r["location_id"], -quantity, "shipment",
                                       f"reservation:{reservation_id}", idempotency_key, reservation_id)
        after = conn.execute("SELECT quantity, quantity_shipped, status FROM stock_reservations WHERE id = ?",
                             (reservation_id,)).fetchone()
        return {"reservation_id": reservation_id, "movement_id": movement_id, "shipped": quantity,
                "quantity_shipped": after["quantity_shipped"], "remaining": after["quantity"] - after["quantity_shipped"],
                "status": after["status"]}

    payload = {"reservation_id": reservation_id, "quantity": quantity}
    return _run(conn, business_id, idempotency_key, "ship", payload, work)


# ---------------------------------------------------------------- queries

def on_hand(conn, business_id: int, item_id: int) -> int:
    return conn.execute(f"SELECT {_ON_HAND}", (business_id, item_id)).fetchone()[0]


def reserved(conn, business_id: int, item_id: int) -> int:
    return conn.execute(f"SELECT {_RESERVED}", (business_id, item_id)).fetchone()[0]


def available(conn, business_id: int, item_id: int) -> int:
    """on_hand minus reserved, read in one statement so both sides come from the same snapshot."""
    return conn.execute(f"SELECT ({_ON_HAND}) - ({_RESERVED})", (business_id, item_id, business_id, item_id)).fetchone()[0]


_ON_HAND = "(SELECT COALESCE(SUM(quantity), 0) FROM stock_movements WHERE business_id = ? AND item_id = ?)"
_RESERVED = """(SELECT COALESCE(SUM(quantity - quantity_shipped), 0) FROM stock_reservations
               WHERE business_id = ? AND item_id = ? AND status = 'active')"""


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
                     reservation_id=None):
    return conn.execute(
        """INSERT INTO stock_movements (business_id, item_id, location_id, quantity, movement_type, reservation_id,
                                        source_ref, idempotency_key)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
        (business_id, item_id, location_id, quantity, movement_type, reservation_id, source_ref, idempotency_key),
    ).lastrowid


def _active_reservation(conn, business_id, reservation_id):
    r = conn.execute("SELECT * FROM stock_reservations WHERE id = ? AND business_id = ?",
                     (reservation_id, business_id)).fetchone()
    if r is None:
        raise NotFound(f"no reservation {reservation_id} in business {business_id}")
    if r["status"] != "active":
        raise InvalidState(f"reservation {reservation_id} is {r['status']}")
    return r


def _positive_int(name, value):
    if type(value) is not int or value <= 0:
        raise ValueError(f"{name} must be a positive integer, got {value!r}")


def _text(name, value):
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{name} must be a non-empty string, got {value!r}")
