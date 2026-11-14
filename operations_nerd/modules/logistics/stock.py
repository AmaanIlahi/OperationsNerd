"""PostgreSQL ledger port of Jasmeet's stock engine.

All commands use the shared cross-module request registry. Internal helpers use
one caller-owned transaction; quantities are projections, never balance fields.
See docs/purchasing/stock-provenance.md for source and deliberate differences.
"""
from __future__ import annotations

from datetime import date, datetime

from .db import run_command
from .errors import Conflict, InsufficientStock, NotFound, ValidationError


# Compatibility names for the original stock API's domain exceptions.
InvalidState = Conflict
OverShipment = InsufficientStock
LotMismatch = ValidationError
LotUnavailable = InsufficientStock


def _int(name, value):
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        raise ValidationError(f"{name} must be a positive integer")
    return value


def _text(name, value):
    if not isinstance(value, str) or not value.strip():
        raise ValidationError(f"{name} must be nonempty text")
    return value


def _date(name, value):
    if value is None:
        return None
    if isinstance(value, datetime):
        raise ValidationError(f"{name} must be an ISO date")
    if isinstance(value, date):
        return value.isoformat()
    try:
        parsed = date.fromisoformat(value)
        if parsed.isoformat() != value:
            raise ValueError()
    except (TypeError, ValueError):
        raise ValidationError(f"{name} must be an ISO date") from None
    return value


def _row(conn, sql, args=()):
    return conn.execute(sql, args).fetchone()


def _id(conn, sql, args=()):
    return _row(conn, sql + " RETURNING id", args)["id"]


def _location(conn, location_id):
    if location_id is not None:
        return _int("location_id", location_id)
    rows = conn.execute("SELECT id FROM logistics.core_bindings WHERE kind='location' AND valid_to IS NULL ORDER BY id LIMIT 2").fetchall()
    if len(rows) != 1:
        raise ValidationError("location_id is required unless exactly one core location is bound")
    return rows[0]["id"]


def _lock_resources(conn, resources):
    for item_id, location_id in sorted(set(resources)):
        conn.execute("SELECT logistics.lock_stock_resource(%s,%s)", (item_id, location_id))


def _demand(conn, demand_line_id=None, order_ref=None, item_id=None, lock=True):
    if demand_line_id is not None:
        _int("demand_line_id", demand_line_id)
        row = _row(conn, "SELECT * FROM logistics.demand_lines WHERE id=%s" + (" FOR UPDATE" if lock else ""), (demand_line_id,))
        if row is None:
            raise NotFound("customer order line not found")
    elif order_ref is not None:
        row = _row(conn, "SELECT * FROM logistics.demand_lines WHERE external_key=%s" + (" FOR UPDATE" if lock else ""), (order_ref,))
    else:
        row = None
    if row is not None and item_id is not None and row["item_id"] != item_id:
        raise ValidationError("customer line item does not match stock item")
    return row


def _tracked(conn, item_id):
    return _row(conn, "SELECT 1 FROM logistics.stock_lot_tracking WHERE item_id=%s", (item_id,)) is not None


def _lot(conn, lot_id):
    row = _row(conn, "SELECT * FROM logistics.stock_lot_levels WHERE lot_id=%s", (lot_id,))
    if row is None:
        raise NotFound("lot not found")
    return row


def _receipt_lot(conn, item_id, lot_number, expiry_date):
    tracked = _tracked(conn, item_id)
    if tracked != (lot_number is not None):
        raise ValidationError("lot_number is required exactly when an item is lot-tracked")
    if lot_number is None:
        if expiry_date is not None:
            raise ValidationError("expiry_date requires a lot")
        return None
    _text("lot_number", lot_number)
    row = _row(conn, "SELECT * FROM logistics.stock_lots WHERE item_id=%s AND lot_number=%s", (item_id, lot_number))
    if row:
        existing = row["expiry_date"].isoformat() if row["expiry_date"] else None
        if existing != expiry_date:
            raise ValidationError("a lot's expiry date cannot change")
        return row["id"]
    return _id(conn, "INSERT INTO logistics.stock_lots(item_id,lot_number,expiry_date) VALUES(%s,%s,%s)", (item_id, lot_number, expiry_date))


def _movement(conn, *, item_id, location_id, quantity, movement_type, source_ref,
              key, lot_id=None, as_of=None, receipt_line_id=None,
              reservation_id=None, shipment_id=None, shipment_line_id=None):
    return _id(conn, """INSERT INTO logistics.stock_movements
        (item_id,location_id,quantity,movement_type,source_ref,request_key,lot_id,as_of,
         receipt_line_id,reservation_id,shipment_id,shipment_line_id)
        VALUES(%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)""",
        (item_id, location_id, quantity, movement_type, source_ref, key, lot_id, as_of,
         receipt_line_id, reservation_id, shipment_id, shipment_line_id))


def stock_summary(conn, item_id, location_id=None):
    _int("item_id", item_id)
    if location_id is None:
        row = _row(conn, "SELECT COALESCE(sum(on_hand),0)::bigint on_hand,COALESCE(sum(reserved),0)::bigint reserved FROM logistics.stock_levels WHERE item_id=%s", (item_id,))
    else:
        _int("location_id", location_id)
        row = _row(conn, "SELECT on_hand,reserved FROM logistics.stock_levels WHERE item_id=%s AND location_id=%s", (item_id, location_id)) or {"on_hand": 0, "reserved": 0}
    return {"item_id": item_id, "location_id": location_id, "on_hand": row["on_hand"], "reserved": row["reserved"], "available": row["on_hand"] - row["reserved"]}


def on_hand(conn, item_id, location_id=None):
    return stock_summary(conn, item_id, location_id)["on_hand"]


def reserved(conn, item_id, location_id=None):
    return stock_summary(conn, item_id, location_id)["reserved"]


def available(conn, item_id, location_id=None):
    return stock_summary(conn, item_id, location_id)["available"]


def track_lots(conn, item_id, idempotency_key):
    _int("item_id", item_id)
    def work(c):
        c.execute("SELECT pg_advisory_xact_lock(%s)", (item_id,))
        if not _tracked(c, item_id):
            c.execute("INSERT INTO logistics.stock_lot_tracking(item_id) VALUES(%s)", (item_id,))
        return {"item_id": item_id, "lot_tracked": True}
    return run_command(conn, "stock.track_lots", idempotency_key, {"item_id": item_id}, work)


def open_stock(conn, item_id, quantity, source_ref, idempotency_key,
               lot_number=None, expiry_date=None, *, location_id=None):
    _int("item_id", item_id); _int("quantity", quantity); _text("source_ref", source_ref)
    expiry_date = _date("expiry_date", expiry_date)
    payload = dict(item_id=item_id, quantity=quantity, source_ref=source_ref,
                   lot_number=lot_number, expiry_date=expiry_date, location_id=location_id)
    def work(c):
        loc = _location(c, location_id)
        _lock_resources(c, [(item_id, loc)])
        lot_id = _receipt_lot(c, item_id, lot_number, expiry_date)
        mid = _movement(c, item_id=item_id, location_id=loc, quantity=quantity,
                        movement_type="opening", source_ref=source_ref, key=idempotency_key, lot_id=lot_id)
        return dict(movement_id=mid, item_id=item_id, location_id=loc, quantity=quantity, received=quantity, lot_id=lot_id, lot_number=lot_number, expiry_date=expiry_date)
    return run_command(conn, "stock.open_stock", idempotency_key, payload, work)


def _record_receipt(c, *, receipt_id, line_number, item_id, location_id, quantity,
                    source_ref, key, lot_number=None, expiry_date=None, po_line_id=None):
    lot_id = _receipt_lot(c, item_id, lot_number, expiry_date)
    line_id = _id(c, """INSERT INTO logistics.receipt_lines
        (receipt_id,line_number,po_line_id,item_id,location_id,quantity,source_ref,lot_id)
        VALUES(%s,%s,%s,%s,%s,%s,%s,%s)""",
        (receipt_id, line_number, po_line_id, item_id, location_id, quantity, source_ref, lot_id))
    movement_id = _movement(c, item_id=item_id, location_id=location_id, quantity=quantity,
                           movement_type="receipt", source_ref=source_ref, key=key, lot_id=lot_id,
                           receipt_line_id=line_id)
    return {"receipt_line_id": line_id, "movement_id": movement_id,
            "item_id": item_id, "location_id": location_id, "quantity": quantity, "received": quantity,
            "lot_id": lot_id, "lot_number": lot_number, "expiry_date": expiry_date, "reservations": []}


def _new_reservation(c, *, item_id, location_id, quantity, order_ref, key,
                     demand_line_id=None, as_of=None, preferred_lot_id=None):
    tracked = _tracked(c, item_id)
    plan = []
    if tracked:
        if as_of is None:
            raise ValidationError("as_of is required for a lot-tracked reservation")
        rows = _usable_lots(c, item_id, location_id, as_of)
        if preferred_lot_id is not None:
            rows = [r for r in rows if r["lot_id"] == preferred_lot_id]
        remaining = quantity
        for lot in rows:
            portion = min(remaining, lot["available"])
            if portion > 0:
                plan.append((lot["lot_id"], portion))
                remaining -= portion
            if remaining == 0:
                break
        if remaining:
            raise InsufficientStock("not enough usable lot stock")
    rid = _id(c, """INSERT INTO logistics.stock_reservations
        (item_id,location_id,order_ref,demand_line_id,quantity,request_key)
        VALUES(%s,%s,%s,%s,%s,%s)""", (item_id, location_id, order_ref, demand_line_id, quantity, key))
    for lot_id, amount in plan:
        c.execute("INSERT INTO logistics.lot_allocations(reservation_id,item_id,lot_id,quantity,as_of) VALUES(%s,%s,%s,%s,%s)", (rid, item_id, lot_id, amount, as_of))
    return rid


def _link_received(c, result, allocation, order_ref, key, as_of):
    rid = _new_reservation(c, item_id=result["item_id"], location_id=result["location_id"],
                          quantity=allocation["quantity"], order_ref=order_ref, key=key,
                          demand_line_id=allocation.get("demand_line_id"), as_of=as_of,
                          preferred_lot_id=result["lot_id"])
    aid = _id(c, """INSERT INTO logistics.receipt_allocations
        (receipt_line_id,purchase_allocation_id,demand_line_id,reservation_id,quantity)
        VALUES(%s,%s,%s,%s,%s)""", (result["receipt_line_id"], allocation.get("purchase_allocation_id"),
                                   allocation.get("demand_line_id"), rid, allocation["quantity"]))
    detail = dict(receipt_allocation_id=aid, reservation_id=rid,
                  purchase_allocation_id=allocation.get("purchase_allocation_id"),
                  demand_line_id=allocation.get("demand_line_id"), quantity=allocation["quantity"])
    result["reservations"].append(detail)
    return rid


def receive(conn, item_id, quantity, source_ref, idempotency_key,
            lot_number=None, expiry_date=None, *, location_id=None, received_on=None):
    return _simple_receive(conn, item_id, quantity, source_ref, idempotency_key,
                           lot_number, expiry_date, location_id=location_id, received_on=received_on)


def receive_and_reserve(conn, item_id, quantity, source_ref, order_ref, idempotency_key,
                        lot_number=None, expiry_date=None, as_of=None, reserve_quantity=None,
                        *, location_id=None, received_on=None, demand_line_id=None):
    _text("order_ref", order_ref)
    to_reserve = quantity if reserve_quantity is None else _int("reserve_quantity", reserve_quantity)
    if to_reserve > quantity:
        raise ValidationError("reserve_quantity exceeds receipt quantity")
    return _simple_receive(conn, item_id, quantity, source_ref, idempotency_key,
                           lot_number, expiry_date, location_id=location_id, received_on=received_on,
                           order_ref=order_ref, reserve_quantity=to_reserve, as_of=as_of,
                           demand_line_id=demand_line_id)


def _simple_receive(conn, item_id, quantity, source_ref, key, lot_number, expiry_date,
                    *, location_id, received_on, order_ref=None, reserve_quantity=None,
                    as_of=None, demand_line_id=None):
    _int("item_id", item_id); _int("quantity", quantity); _text("source_ref", source_ref)
    expiry_date = _date("expiry_date", expiry_date)
    as_of = _date("as_of", as_of)
    received_on = _date("received_on", received_on)
    payload = dict(item_id=item_id, quantity=quantity, source_ref=source_ref, lot_number=lot_number,
                   expiry_date=expiry_date, location_id=location_id, received_on=received_on,
                   order_ref=order_ref, reserve_quantity=reserve_quantity, as_of=as_of,
                   demand_line_id=demand_line_id)
    def work(c):
        demand = _demand(c, demand_line_id, order_ref, item_id) if order_ref else None
        loc = _location(c, location_id)
        _lock_resources(c, [(item_id, loc)])
        receipt_id = _id(c, "INSERT INTO logistics.receipts(delivery_ref,request_key,received_on) VALUES(%s,%s,COALESCE(%s::date,CURRENT_DATE))", ("single:" + key, key, received_on))
        result = _record_receipt(c, receipt_id=receipt_id, line_number=1, item_id=item_id,
                                 location_id=loc, quantity=quantity, source_ref=source_ref, key=key,
                                 lot_number=lot_number, expiry_date=expiry_date)
        result["receipt_id"] = receipt_id
        if order_ref:
            alloc = {"quantity": reserve_quantity, "demand_line_id": demand["id"] if demand else None}
            rid = _link_received(c, result, alloc, order_ref, key, as_of)
            result.update(reservation_id=rid, order_ref=order_ref, reserved=reserve_quantity)
        return result
    operation = "stock.receive_and_reserve" if order_ref else "stock.receive"
    return run_command(conn, operation, key, payload, work)


def receive_delivery(conn, delivery_ref, lines, idempotency_key, received_on, as_of=None):
    """Atomically receive a physical delivery and its explicit purchase allocations.

    Purchasing validation runs inside this command's transaction. Quantities in
    line portions describe physical goods exactly once; allocation quantities may
    distribute that line across multiple waiting customer lines.
    """
    _text("delivery_ref", delivery_ref)
    received_on = _date("received_on", received_on)
    if received_on is None:
        raise ValidationError("received_on is required")
    as_of = _date("as_of", as_of) or received_on
    if not isinstance(lines, list) or not lines:
        raise ValidationError("a delivery needs at least one receipt line")
    allowed = {"po_line_id", "item_id", "location_id", "quantity", "lot_number", "expiry_date", "allocations"}
    normalized = []
    for line in lines:
        if not isinstance(line, dict) or set(line) - allowed:
            raise ValidationError("invalid delivery line fields")
        row = {name: _int(name, line.get(name)) for name in ("po_line_id", "item_id", "location_id", "quantity")}
        row.update(lot_number=line.get("lot_number"), expiry_date=_date("expiry_date", line.get("expiry_date")), allocations=[])
        allocations = line.get("allocations", [])
        if not isinstance(allocations, list):
            raise ValidationError("allocations must be a list")
        for allocation in allocations:
            if not isinstance(allocation, dict) or set(allocation) != {"purchase_allocation_id", "demand_line_id", "quantity"}:
                raise ValidationError("allocation requires purchase_allocation_id, demand_line_id and quantity")
            row["allocations"].append({name: _int(name, allocation[name]) for name in allocation})
        if sum(a["quantity"] for a in row["allocations"]) > row["quantity"]:
            raise ValidationError("allocation portions exceed physical receipt quantity")
        normalized.append(row)
    payload = dict(delivery_ref=delivery_ref, lines=normalized, received_on=received_on, as_of=as_of)
    def work(c):
        # Import lazily: purchasing's receiving caller imports this module too.
        from .purchasing import _validate_receipt
        _validate_receipt(c, normalized)
        for did in sorted({a["demand_line_id"] for l in normalized for a in l["allocations"]}):
            _demand(c, did)
        _lock_resources(c, [(l["item_id"], l["location_id"]) for l in normalized])
        if _row(c, "SELECT 1 FROM logistics.receipts WHERE delivery_ref=%s", (delivery_ref,)):
            raise Conflict("this physical delivery has already been recorded")
        receipt_id = _id(c, "INSERT INTO logistics.receipts(delivery_ref,request_key,received_on) VALUES(%s,%s,%s)", (delivery_ref, idempotency_key, received_on))
        results = []
        for index, line in enumerate(normalized, 1):
            result = _record_receipt(c, receipt_id=receipt_id, line_number=index,
                                     item_id=line["item_id"], location_id=line["location_id"],
                                     quantity=line["quantity"], source_ref=str(line["po_line_id"]),
                                     key=idempotency_key, lot_number=line["lot_number"],
                                     expiry_date=line["expiry_date"], po_line_id=line["po_line_id"])
            for allocation in line["allocations"]:
                demand = _demand(c, allocation["demand_line_id"], item_id=line["item_id"])
                _link_received(c, result, allocation, demand["external_key"], idempotency_key, as_of)
            results.append(result)
        return {"receipt_id": receipt_id, "delivery_ref": delivery_ref, "lines": results}
    return run_command(conn, "stock.receive_delivery", idempotency_key, payload, work)


def _usable_lots(conn, item_id, location_id, as_of):
    return conn.execute("""SELECT l.lot_id,l.expiry_date,
       (SELECT COALESCE(sum(m.quantity),0)::bigint FROM logistics.stock_movements m WHERE m.lot_id=l.lot_id AND m.location_id=%s)
       -(SELECT COALESCE(sum(a.quantity),0)::bigint FROM logistics.lot_allocations a JOIN logistics.stock_reservations r ON r.id=a.reservation_id WHERE a.lot_id=l.lot_id AND r.location_id=%s AND r.status='active')
       -(SELECT COALESCE(sum(m.quantity),0)::bigint FROM logistics.stock_movements m JOIN logistics.stock_reservations r ON r.id=m.reservation_id WHERE m.lot_id=l.lot_id AND r.location_id=%s AND r.status='active') AS available
       FROM logistics.stock_lot_levels l WHERE l.item_id=%s AND NOT l.on_hold
       AND (l.expiry_date IS NULL OR l.expiry_date>=%s::date)
       ORDER BY l.expiry_date NULLS LAST,l.lot_id""", (location_id, location_id, location_id, item_id, as_of)).fetchall()


def reserve(conn, item_id, quantity, order_ref, idempotency_key, as_of=None,
            *, location_id=None, demand_line_id=None):
    _int("item_id", item_id); _int("quantity", quantity); _text("order_ref", order_ref)
    as_of = _date("as_of", as_of)
    payload = dict(item_id=item_id, quantity=quantity, order_ref=order_ref, as_of=as_of,
                   location_id=location_id, demand_line_id=demand_line_id)
    def work(c):
        demand = _demand(c, demand_line_id, order_ref, item_id)
        loc = _location(c, location_id)
        _lock_resources(c, [(item_id, loc)])
        if _tracked(c, item_id):
            if as_of is None:
                raise ValidationError("as_of is required for a lot-tracked reservation")
            free = sum(max(0, l["available"]) for l in _usable_lots(c, item_id, loc, as_of))
        else:
            free = available(c, item_id, loc)
        amount = min(quantity, free)
        rid = None
        if amount:
            rid = _new_reservation(c, item_id=item_id, location_id=loc, quantity=amount,
                                   order_ref=order_ref, key=idempotency_key,
                                   demand_line_id=demand["id"] if demand else None, as_of=as_of)
        result = dict(reservation_id=rid, item_id=item_id, location_id=loc, order_ref=order_ref,
                      requested=quantity, reserved=amount, shortage=quantity-amount)
        if rid is not None and _tracked(c, item_id):
            result["allocations"] = [dict(r) for r in c.execute("SELECT lot_id,quantity FROM logistics.lot_allocations WHERE reservation_id=%s ORDER BY id", (rid,)).fetchall()]
        return result
    return run_command(conn, "stock.reserve", idempotency_key, payload, work)


def _reservation(conn, reservation_id, active=True):
    row = _row(conn, "SELECT * FROM logistics.stock_reservations WHERE id=%s", (reservation_id,))
    if row is None:
        raise NotFound("reservation not found")
    if active and row["status"] != "active":
        raise Conflict("reservation is no longer active")
    return row


def release(conn, reservation_id, idempotency_key):
    _int("reservation_id", reservation_id)
    def work(c):
        r = _reservation(c, reservation_id)
        _lock_resources(c, [(r["item_id"], r["location_id"])])
        r = _reservation(c, reservation_id)
        c.execute("UPDATE logistics.stock_reservations SET status='released' WHERE id=%s", (reservation_id,))
        return {"reservation_id": reservation_id, "released": r["quantity"]-r["quantity_shipped"], "status": "released"}
    return run_command(conn, "stock.release", idempotency_key, {"reservation_id": reservation_id}, work)


def _ship(c, r, quantity, key, as_of, shipment_id=None, shipment_line_id=None):
    if quantity > r["quantity"] - r["quantity_shipped"]:
        raise InsufficientStock("shipment exceeds reservation remainder")
    plan = [(None, quantity)]
    if _tracked(c, r["item_id"]):
        if as_of is None:
            raise ValidationError("as_of is required for a lot shipment")
        plan = []
        left = quantity
        rows = c.execute("""SELECT a.lot_id,a.quantity,
            (SELECT COALESCE(-sum(quantity),0) FROM logistics.stock_movements WHERE reservation_id=a.reservation_id AND lot_id=a.lot_id) AS sent
            FROM logistics.lot_allocations a JOIN logistics.stock_lots l ON l.id=a.lot_id
            WHERE a.reservation_id=%s ORDER BY l.expiry_date NULLS LAST,l.id""", (r["id"],)).fetchall()
        for a in rows:
            amount = min(left, a["quantity"]-a["sent"])
            if amount:
                lot = _lot(c, a["lot_id"])
                if lot["on_hold"] or (lot["expiry_date"] and as_of > lot["expiry_date"].isoformat()):
                    continue
                plan.append((a["lot_id"], amount))
                left -= amount
            if left == 0:
                break
        if left:
            raise InsufficientStock("lot allocation remainder is insufficient")
    ids = []
    for lot_id, amount in plan:
        ids.append(_movement(c, item_id=r["item_id"], location_id=r["location_id"],
                             quantity=-amount, movement_type="shipment", source_ref=r["order_ref"],
                             key=key, lot_id=lot_id, as_of=as_of, reservation_id=r["id"],
                             shipment_id=shipment_id, shipment_line_id=shipment_line_id))
    return ids


def ship(conn, reservation_id, quantity, idempotency_key, as_of=None):
    _int("reservation_id", reservation_id); _int("quantity", quantity)
    as_of = _date("as_of", as_of)
    def work(c):
        r = _reservation(c, reservation_id)
        _lock_resources(c, [(r["item_id"], r["location_id"])])
        r = _reservation(c, reservation_id)
        ids = _ship(c, r, quantity, idempotency_key, as_of)
        after = _reservation(c, reservation_id, active=False)
        result = {"reservation_id": reservation_id, "shipped": quantity, "movement_ids": ids,
                  "quantity_shipped": after["quantity_shipped"],
                  "remaining": after["quantity"]-after["quantity_shipped"], "status": after["status"]}
        if _tracked(c, r["item_id"]):
            result["lots"] = [dict(row) for row in c.execute("SELECT m.id movement_id,m.lot_id,l.lot_number,-m.quantity quantity FROM logistics.stock_movements m JOIN logistics.stock_lots l ON l.id=m.lot_id WHERE m.id=ANY(%s) ORDER BY m.id", (ids,)).fetchall()]
        else:
            result["movement_id"] = ids[0]
        return result
    return run_command(conn, "stock.ship", idempotency_key, dict(reservation_id=reservation_id, quantity=quantity, as_of=as_of), work)


def _hold(conn, lot_id, reason, key, event):
    _int("lot_id", lot_id); _text("reason", reason)
    def work(c):
        l = _lot(c, lot_id)
        c.execute("SELECT pg_advisory_xact_lock(%s)", (l["item_id"],))
        l = _lot(c, lot_id)
        if l["on_hold"] == (event == "hold"):
            raise Conflict("lot hold is already in the requested state")
        hid = _id(c, "INSERT INTO logistics.stock_lot_holds(lot_id,event,reason,request_key) VALUES(%s,%s,%s,%s)", (lot_id, event, reason, key))
        return {"lot_id": lot_id, "lot_number": l["lot_number"], "allocated": l["allocated"], "on_hold": event == "hold", "hold_event_id": hid}
    return run_command(conn, "stock." + ("hold_lot" if event == "hold" else "release_hold"), key, dict(lot_id=lot_id, reason=reason), work)


def hold_lot(conn, lot_id, reason, idempotency_key):
    return _hold(conn, lot_id, reason, idempotency_key, "hold")


def release_hold(conn, lot_id, reason, idempotency_key):
    return _hold(conn, lot_id, reason, idempotency_key, "release")


def create_shipment(conn, shipment_number, idempotency_key, *, order_ref=None):
    _text("shipment_number", shipment_number)
    if order_ref is not None:
        _text("order_ref", order_ref)
    def work(c):
        sid = _id(c, "INSERT INTO logistics.shipments(shipment_number,order_ref,request_key) VALUES(%s,%s,%s)", (shipment_number, order_ref, idempotency_key))
        return {"shipment_id": sid, "shipment_number": shipment_number, "status": "draft"}
    return run_command(conn, "stock.create_shipment", idempotency_key, dict(shipment_number=shipment_number, order_ref=order_ref), work)


def add_shipment_line(conn, shipment_id, reservation_id, quantity, idempotency_key):
    for name, value in (("shipment_id", shipment_id), ("reservation_id", reservation_id), ("quantity", quantity)):
        _int(name, value)
    def work(c):
        c.execute("SELECT pg_advisory_xact_lock(hashtextextended(%s,0))", ("shipment:" + str(shipment_id),))
        r = _reservation(c, reservation_id)
        _lock_resources(c, [(r["item_id"], r["location_id"])])
        r = _reservation(c, reservation_id)
        planned = _row(c, "SELECT COALESCE(sum(l.quantity),0)::bigint quantity FROM logistics.shipment_lines l WHERE l.reservation_id=%s AND NOT EXISTS(SELECT 1 FROM logistics.shipment_dispatches d WHERE d.shipment_id=l.shipment_id)", (reservation_id,))["quantity"]
        if quantity > r["quantity"]-r["quantity_shipped"]-planned:
            raise InsufficientStock("shipment line exceeds reservation remainder not already on a draft")
        if not _row(c, "SELECT 1 FROM logistics.shipments WHERE id=%s FOR UPDATE", (shipment_id,)):
            raise NotFound("shipment not found")
        lid = _id(c, "INSERT INTO logistics.shipment_lines(shipment_id,reservation_id,quantity) VALUES(%s,%s,%s)", (shipment_id, reservation_id, quantity))
        return {"line_id": lid, "shipment_line_id": lid, "shipment_id": shipment_id, "reservation_id": reservation_id, "quantity": quantity}
    return run_command(conn, "stock.add_shipment_line", idempotency_key, dict(shipment_id=shipment_id, reservation_id=reservation_id, quantity=quantity), work)


def dispatch(conn, shipment_id, carrier, idempotency_key, as_of, label_ref=None, tracking_ref=None):
    _int("shipment_id", shipment_id); _text("carrier", carrier)
    as_of = _date("as_of", as_of)
    if as_of is None or not (label_ref or tracking_ref):
        raise ValidationError("dispatch requires as_of and a label or tracking reference")
    if label_ref is not None:
        _text("label_ref", label_ref)
    if tracking_ref is not None:
        _text("tracking_ref", tracking_ref)
    def work(c):
        # Serialize draft line additions before taking resource locks.
        c.execute("SELECT pg_advisory_xact_lock(hashtextextended(%s,0))", ("shipment:" + str(shipment_id),))
        rows = c.execute("""SELECT l.id AS shipment_line_id,l.quantity AS line_quantity,r.*
            FROM logistics.shipment_lines l JOIN logistics.stock_reservations r ON r.id=l.reservation_id
            WHERE l.shipment_id=%s ORDER BY r.item_id,r.location_id,r.id""", (shipment_id,)).fetchall()
        _lock_resources(c, [(r["item_id"], r["location_id"]) for r in rows])
        if not _row(c, "SELECT 1 FROM logistics.shipments WHERE id=%s FOR UPDATE", (shipment_id,)):
            raise NotFound("shipment not found")
        if _row(c, "SELECT 1 FROM logistics.shipment_dispatches WHERE shipment_id=%s", (shipment_id,)):
            raise Conflict("shipment already dispatched")
        # Reload after the lock: draft lines may have been added while waiting.
        rows = c.execute("""SELECT l.id AS shipment_line_id,l.quantity AS line_quantity,r.*
            FROM logistics.shipment_lines l JOIN logistics.stock_reservations r ON r.id=l.reservation_id
            WHERE l.shipment_id=%s ORDER BY r.item_id,r.location_id,r.id""", (shipment_id,)).fetchall()
        if not rows:
            raise ValidationError("shipment has no lines")
        _lock_resources(c, [(r["item_id"], r["location_id"]) for r in rows])
        mids = []
        for row in rows:
            r = _reservation(c, row["id"])
            mids.extend(_ship(c, r, row["line_quantity"], idempotency_key, as_of, shipment_id, row["shipment_line_id"]))
        c.execute("INSERT INTO logistics.shipment_dispatches(shipment_id,carrier,label_ref,tracking_ref,as_of,request_key) VALUES(%s,%s,%s,%s,%s,%s)", (shipment_id, carrier, label_ref, tracking_ref, as_of, idempotency_key))
        return {"shipment_id": shipment_id, "status": "dispatched", "movement_ids": mids}
    return run_command(conn, "stock.dispatch", idempotency_key,
                       dict(shipment_id=shipment_id, carrier=carrier, as_of=as_of, label_ref=label_ref, tracking_ref=tracking_ref), work)


def record_tracking_event(conn, shipment_id, event, occurred_at, idempotency_key, detail=None):
    _int("shipment_id", shipment_id)
    if event not in {"in_transit", "out_for_delivery", "delivery_failed", "delivered", "exception"}:
        raise ValidationError("invalid carrier event")
    try:
        parsed = datetime.fromisoformat(occurred_at.replace("Z", "+00:00"))
        if parsed.tzinfo is None:
            raise ValueError()
    except (AttributeError, TypeError, ValueError):
        raise ValidationError("occurred_at requires an ISO timestamp with timezone") from None
    def work(c):
        if not _row(c, "SELECT 1 FROM logistics.shipment_dispatches WHERE shipment_id=%s", (shipment_id,)):
            raise Conflict("shipment has not been dispatched")
        eid = _id(c, "INSERT INTO logistics.shipment_events(shipment_id,event,occurred_at,detail,request_key) VALUES(%s,%s,%s,%s,%s)", (shipment_id, event, occurred_at, detail, idempotency_key))
        return {"shipment_id": shipment_id, "event_id": eid, "status": shipment(c, shipment_id)["status"]}
    return run_command(conn, "stock.record_tracking_event", idempotency_key,
                       dict(shipment_id=shipment_id, event=event, occurred_at=occurred_at, detail=detail), work)


def shipment(conn, shipment_id):
    row = _row(conn, "SELECT * FROM logistics.shipment_status WHERE shipment_id=%s", (shipment_id,))
    if row is None:
        raise NotFound("shipment not found")
    result = dict(row)
    if result["dispatched_on"] is not None:
        result["dispatched_on"] = result["dispatched_on"].isoformat()
    result["lines"] = []
    for row in conn.execute("SELECT l.id line_id,l.id shipment_line_id,l.reservation_id,r.order_ref,r.item_id,l.quantity FROM logistics.shipment_lines l JOIN logistics.stock_reservations r ON r.id=l.reservation_id WHERE l.shipment_id=%s ORDER BY l.id", (shipment_id,)).fetchall():
        line = dict(row)
        line["movements"] = [dict(m) for m in conn.execute("SELECT m.id movement_id,m.lot_id,l.lot_number,-m.quantity quantity FROM logistics.stock_movements m LEFT JOIN logistics.stock_lots l ON l.id=m.lot_id WHERE m.shipment_line_id=%s ORDER BY m.id", (row["line_id"],)).fetchall()]
        result["lines"].append(line)
    result["events"] = [dict(e) for e in conn.execute("SELECT id event_id,event,occurred_at,detail FROM logistics.shipment_events WHERE shipment_id=%s ORDER BY id", (shipment_id,)).fetchall()]
    for event in result["events"]:
        event["occurred_at"] = event["occurred_at"].isoformat()
    return result


def lots(conn, item_id, as_of=None):
    _int("item_id", item_id)
    as_of = _date("as_of", as_of)
    results = []
    for row in conn.execute("SELECT * FROM logistics.stock_lot_levels WHERE item_id=%s ORDER BY expiry_date NULLS LAST,lot_id", (item_id,)).fetchall():
        result = dict(row)
        expiry = row["expiry_date"].isoformat() if row["expiry_date"] else None
        result.update(expiry_date=expiry, available=row["on_hand"]-row["allocated"], free=row["on_hand"]-row["allocated"],
                      expired=bool(expiry and as_of and as_of > expiry))
        if as_of is not None:
            result["usable"] = not row["on_hold"] and not result["expired"] and result["free"] > 0
        results.append(result)
    return results


def recall(conn, lot_id):
    l = _lot(conn, lot_id)
    return {"lot_id": lot_id, "item_id": l["item_id"], "lot_number": l["lot_number"],
            "on_hand": l["on_hand"], "on_hold": l["on_hold"],
            "expiry_date": l["expiry_date"].isoformat() if l["expiry_date"] else None,
            "received": _row(conn, "SELECT COALESCE(sum(quantity),0)::bigint quantity FROM logistics.stock_movements WHERE lot_id=%s AND movement_type IN ('receipt','opening')", (lot_id,))["quantity"],
            "reservations": [dict(row) for row in conn.execute("SELECT a.reservation_id,r.order_ref,r.status,a.quantity allocated,(SELECT COALESCE(-sum(m.quantity),0)::bigint FROM logistics.stock_movements m WHERE m.reservation_id=a.reservation_id AND m.lot_id=a.lot_id) shipped FROM logistics.lot_allocations a JOIN logistics.stock_reservations r ON r.id=a.reservation_id WHERE a.lot_id=%s ORDER BY a.reservation_id", (lot_id,)).fetchall()],
            "shipments": [dict(r) for r in conn.execute("""SELECT m.id AS movement_id,m.reservation_id,
                r.order_ref,m.shipment_id,m.shipment_line_id,-m.quantity AS quantity
                FROM logistics.stock_movements m JOIN logistics.stock_reservations r ON r.id=m.reservation_id
                WHERE m.lot_id=%s AND m.movement_type='shipment' ORDER BY m.id""", (lot_id,)).fetchall()]}


def usable_available(conn, item_id, as_of, location_id=None):
    """Available stock eligible on the supplied business date, across locations."""
    _int("item_id", item_id)
    as_of = _date("as_of", as_of)
    if not _tracked(conn, item_id):
        return available(conn, item_id, location_id)
    if as_of is None:
        raise ValidationError("as_of is required for usable lot stock")
    locations = [location_id] if location_id is not None else [r["location_id"] for r in conn.execute("SELECT location_id FROM logistics.stock_resources WHERE item_id=%s ORDER BY location_id", (item_id,)).fetchall()]
    return sum(max(0, row["available"]) for loc in locations for row in _usable_lots(conn, item_id, loc, as_of))
