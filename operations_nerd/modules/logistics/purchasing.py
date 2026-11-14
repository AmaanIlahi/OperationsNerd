"""Persistent purchasing commands for one business's PostgreSQL database.

All writes use the same command registry and transaction boundary as stock.
Core bindings are adapter projections; this module never creates a catalog.
Prices use integer minor units and quantities use indivisible whole units.
"""
from __future__ import annotations

from datetime import date, datetime, timezone
from uuid import uuid4
import re

from .selection import Quote, select_quotes

from .db import run_command
from .errors import RuleViolation


def _positive(value, label, *, zero=False):
    if type(value) is not int or value < (0 if zero else 1):
        raise RuleViolation(f"{label} must be {'nonnegative' if zero else 'positive'} whole units")


def _currency(value):
    if not isinstance(value, str) or re.fullmatch(r"[A-Z]{3}", value) is None:
        raise RuleViolation("currency must be an uppercase three-letter code")


def _date(value, label):
    if isinstance(value, str):
        try:
            value = date.fromisoformat(value)
        except ValueError:
            raise RuleViolation(f"{label} must be an ISO calendar date") from None
    if type(value) is not date:
        raise RuleViolation(f"{label} must be a calendar date")
    return value


def _instant(value, label):
    if isinstance(value, str):
        try:
            value = datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError:
            raise RuleViolation(f"{label} must be an ISO timestamp") from None
    if not isinstance(value, datetime) or value.utcoffset() is None:
        raise RuleViolation(f"{label} must have a timezone")
    return value


def _json(value):
    if isinstance(value, (date, datetime)):
        return value.isoformat()
    if isinstance(value, dict):
        return {key: _json(entry) for key, entry in value.items()}
    if isinstance(value, (tuple, list)):
        return [_json(entry) for entry in value]
    return value


def _one(conn, sql, args=(), label="Record"):
    row = conn.execute(sql, args).fetchone()
    if row is None:
        raise RuleViolation(f"{label} does not exist in this business database")
    return row


def _binding(conn, binding_id, kind, *, at=None):
    _positive(binding_id, f"{kind}_id")
    # Approval/create-time validation must retain the revision it validated until
    # commit. A concurrent supplier revision closes this same row with UPDATE.
    row = _one(conn, "SELECT * FROM logistics.core_bindings WHERE id=%s" +
               (" FOR SHARE" if at is not None else ""), (binding_id,), kind)
    if row["kind"] != kind:
        raise RuleViolation(f"Reference {binding_id} is not a {kind}")
    if at is not None and (row["valid_from"] > at or
                           (row["valid_to"] is not None and row["valid_to"] <= at)):
        raise RuleViolation(f"{kind} reference is not valid at this time")
    return row


def _draft(conn, po_id, expected_version=None):
    row = _one(conn, "SELECT * FROM logistics.purchase_orders WHERE id=%s FOR UPDATE", (po_id,), "Purchase order")
    if row["status"] != "draft":
        raise RuleViolation("Approved purchase orders cannot be edited")
    if expected_version is not None and row["version"] != expected_version:
        raise RuleViolation("Purchase order version has changed; reload the draft")
    return row


def _advance(conn, po_id):
    conn.execute("UPDATE logistics.purchase_orders SET version=version+1 WHERE id=%s", (po_id,))


def _quote_rule(row, *, line_id="item"):
    return Quote(
        id=str(row["id"]), business_id="private-database", line_id=str(line_id),
        supplier_id=str(row.get("supplier_key", row["supplier_id"])), capacity_quantity=row["capacity_quantity"],
        quantity_priced=row["quantity_priced"], price_cents=row["price_minor"],
        pricing_kind=row["pricing_kind"], currency=row["currency"], source=row["source"],
        generated_at=row["generated_at"], expires_at=row["expires_at"], expected_date=row["expected_date"],
    )


def create_quote(conn, *, supplier_id, item_id, quantity_priced, capacity_quantity,
                 price_minor, pricing_kind, currency, expires_at, expected_date,
                 idempotency_key, quote_ref=None, source="supplier", generated_at=None):
    """Append immutable supplier terms. Reusing quote_ref appends a new version.

    ``generated_at=None`` means command time and is omitted from replay identity;
    retrying an omitted server timestamp therefore returns the original result.
    """
    payload = _json(dict(supplier_id=supplier_id, item_id=item_id, quantity_priced=quantity_priced,
                        capacity_quantity=capacity_quantity, price_minor=price_minor,
                        pricing_kind=pricing_kind, currency=currency, expires_at=expires_at,
                        expected_date=expected_date, quote_ref=quote_ref, source=source,
                        generated_at=generated_at))

    def work(tx):
        created = _instant(generated_at, "generated_at") if generated_at is not None else datetime.now(timezone.utc)
        expires = _instant(expires_at, "expires_at")
        expected = _date(expected_date, "expected_date")
        _binding(tx, supplier_id, "supplier", at=created)
        _binding(tx, item_id, "item", at=created)
        ref = quote_ref if quote_ref is not None else str(uuid4())
        if not isinstance(ref, str) or not ref.strip():
            raise RuleViolation("quote_ref must be a nonempty identifier")
        _positive(quantity_priced, "quantity_priced")
        _positive(capacity_quantity, "capacity_quantity", zero=True)
        _positive(price_minor, "price_minor", zero=True)
        try:
            Quote(id="new", business_id="private-database", line_id="item", supplier_id=str(supplier_id),
                  capacity_quantity=capacity_quantity, quantity_priced=quantity_priced,
                  price_cents=price_minor, pricing_kind=pricing_kind, currency=currency,
                  source=source, generated_at=created, expires_at=expires, expected_date=expected)
        except ValueError as error:
            raise RuleViolation(str(error)) from error
        # Serialize revisions even when no previous version exists.
        tx.execute("SELECT pg_advisory_xact_lock(hashtextextended(%s,0))", ("quote:" + ref,))
        previous = tx.execute("SELECT * FROM logistics.quote_versions WHERE quote_ref=%s ORDER BY version DESC LIMIT 1", (ref,)).fetchone()
        if previous:
            old_supplier = _binding(tx, previous["supplier_id"], "supplier")
            new_supplier = _binding(tx, supplier_id, "supplier")
            if old_supplier["external_key"] != new_supplier["external_key"] or previous["item_id"] != item_id:
                raise RuleViolation("Quote revisions must retain the same supplier and item")
        row = tx.execute("""INSERT INTO logistics.quote_versions
            (quote_ref,version,supplier_id,item_id,quantity_priced,capacity_quantity,price_minor,
             pricing_kind,currency,source,generated_at,expires_at,expected_date,supersedes_id)
            VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s) RETURNING *""",
            (ref, previous["version"] + 1 if previous else 1, supplier_id, item_id,
             quantity_priced, capacity_quantity, price_minor, pricing_kind, currency, source,
             created, expires, expected, previous["id"] if previous else None)).fetchone()
        return _json(row)

    return run_command(conn, "purchasing.create_quote", idempotency_key, payload, work)


def create_purchase_order(conn, *, supplier_id, currency, idempotency_key):
    """Create a draft supplier PO. This changes no stock."""
    def work(tx):
        _currency(currency)
        _binding(tx, supplier_id, "supplier", at=datetime.now(timezone.utc))
        return _json(tx.execute("INSERT INTO logistics.purchase_orders(supplier_id,currency) VALUES (%s,%s) RETURNING *", (supplier_id, currency)).fetchone())
    return run_command(conn, "purchasing.create_purchase_order", idempotency_key,
                       dict(supplier_id=supplier_id, currency=currency), work)


def add_purchase_line(conn, *, purchase_order_id, quote_version_id, quantity,
                      idempotency_key, expected_version=None):
    """Add one exact quote version to a draft; a version converts only once."""
    def work(tx):
        po = _draft(tx, purchase_order_id, expected_version)
        _positive(quantity, "quantity")
        quote = _one(tx, "SELECT * FROM logistics.quote_versions WHERE id=%s", (quote_version_id,), "Quote version")
        supplier = _binding(tx, po["supplier_id"], "supplier")
        quoted_supplier = _binding(tx, quote["supplier_id"], "supplier")
        if supplier["external_key"] != quoted_supplier["external_key"] or po["currency"] != quote["currency"]:
            raise RuleViolation("Quote supplier and currency must match the purchase order")
        try:
            _quote_rule(quote).cost(quantity)
        except ValueError as error:
            raise RuleViolation(str(error)) from error
        if tx.execute("SELECT id FROM logistics.purchase_order_lines WHERE quote_version_id=%s", (quote_version_id,)).fetchone():
            raise RuleViolation("This exact quote version is already used by a purchase line")
        row = tx.execute("""INSERT INTO logistics.purchase_order_lines
            (purchase_order_id,quote_version_id,item_id,quantity) VALUES (%s,%s,%s,%s) RETURNING *""",
            (purchase_order_id, quote_version_id, quote["item_id"], quantity)).fetchone()
        _advance(tx, purchase_order_id)
        return _json(row)
    return run_command(conn, "purchasing.add_purchase_line", idempotency_key,
                       dict(purchase_order_id=purchase_order_id, quote_version_id=quote_version_id,
                            quantity=quantity, expected_version=expected_version), work)


def allocate_purchase_line(conn, *, po_line_id, demand_line_id, quantity,
                           idempotency_key, cost_share_minor=None, expected_version=None):
    """Create a draft purchase allocation; explicit cost shares avoid invented policy."""
    def work(tx):
        line = _one(tx, "SELECT * FROM logistics.purchase_order_lines WHERE id=%s", (po_line_id,), "Purchase line")
        _draft(tx, line["purchase_order_id"], expected_version)
        # A concurrent draft edit may have changed quantity while we waited for
        # its header lock. Validate against the committed current line.
        line = _one(tx, "SELECT * FROM logistics.purchase_order_lines WHERE id=%s", (po_line_id,), "Purchase line")
        _positive(quantity, "quantity")
        if cost_share_minor is not None:
            _positive(cost_share_minor, "cost_share_minor", zero=True)
        demand = _one(tx, "SELECT * FROM logistics.demand_lines WHERE id=%s", (demand_line_id,), "Customer line")
        if line["item_id"] != demand["item_id"]:
            raise RuleViolation("Purchase and customer lines must refer to the same item")
        allocated = tx.execute("SELECT COALESCE(sum(quantity),0) AS quantity FROM logistics.purchase_allocations WHERE po_line_id=%s", (po_line_id,)).fetchone()["quantity"]
        if allocated + quantity > line["quantity"]:
            raise RuleViolation("Allocations exceed purchase line quantity")
        if tx.execute("SELECT id FROM logistics.purchase_allocations WHERE po_line_id=%s AND demand_line_id=%s", (po_line_id, demand_line_id)).fetchone():
            raise RuleViolation("This customer line already has an allocation on the purchase line; edit it")
        row = tx.execute("""INSERT INTO logistics.purchase_allocations
            (po_line_id,demand_line_id,quantity,cost_share_minor) VALUES (%s,%s,%s,%s) RETURNING *""",
            (po_line_id, demand_line_id, quantity, cost_share_minor)).fetchone()
        _advance(tx, line["purchase_order_id"])
        return _json(row)
    return run_command(conn, "purchasing.allocate_purchase_line", idempotency_key,
                       dict(po_line_id=po_line_id, demand_line_id=demand_line_id, quantity=quantity,
                            cost_share_minor=cost_share_minor, expected_version=expected_version), work)


def edit_purchase_line(conn, *, po_line_id, quantity, idempotency_key, expected_version=None):
    """Change draft quantity only. Existing allocations must still fit."""
    def work(tx):
        line = _one(tx, "SELECT * FROM logistics.purchase_order_lines WHERE id=%s", (po_line_id,), "Purchase line")
        _draft(tx, line["purchase_order_id"], expected_version)
        line = _one(tx, "SELECT * FROM logistics.purchase_order_lines WHERE id=%s", (po_line_id,), "Purchase line")
        _positive(quantity, "quantity")
        quote = _one(tx, "SELECT * FROM logistics.quote_versions WHERE id=%s", (line["quote_version_id"],), "Quote version")
        try:
            _quote_rule(quote).cost(quantity)
        except ValueError as error:
            raise RuleViolation(str(error)) from error
        allocated = tx.execute("SELECT COALESCE(sum(quantity),0) AS quantity FROM logistics.purchase_allocations WHERE po_line_id=%s", (po_line_id,)).fetchone()["quantity"]
        if allocated > quantity:
            raise RuleViolation("Existing allocations exceed the new purchase quantity")
        row = tx.execute("UPDATE logistics.purchase_order_lines SET quantity=%s WHERE id=%s RETURNING *", (quantity, po_line_id)).fetchone()
        _advance(tx, line["purchase_order_id"])
        return _json(row)
    return run_command(conn, "purchasing.edit_purchase_line", idempotency_key,
                       dict(po_line_id=po_line_id, quantity=quantity, expected_version=expected_version), work)


def edit_purchase_allocation(conn, *, allocation_id, quantity, idempotency_key,
                             cost_share_minor=None, expected_version=None):
    """Change draft allocation quantity/cost share; approved allocations are frozen."""
    def work(tx):
        allocation = _one(tx, "SELECT * FROM logistics.purchase_allocations WHERE id=%s", (allocation_id,), "Purchase allocation")
        line = _one(tx, "SELECT * FROM logistics.purchase_order_lines WHERE id=%s", (allocation["po_line_id"],), "Purchase line")
        _draft(tx, line["purchase_order_id"], expected_version)
        allocation = _one(tx, "SELECT * FROM logistics.purchase_allocations WHERE id=%s", (allocation_id,), "Purchase allocation")
        line = _one(tx, "SELECT * FROM logistics.purchase_order_lines WHERE id=%s", (line["id"],), "Purchase line")
        _positive(quantity, "quantity")
        if cost_share_minor is not None:
            _positive(cost_share_minor, "cost_share_minor", zero=True)
        other = tx.execute("SELECT COALESCE(sum(quantity),0) AS quantity FROM logistics.purchase_allocations WHERE po_line_id=%s AND id<>%s", (line["id"], allocation_id)).fetchone()["quantity"]
        if quantity + other > line["quantity"]:
            raise RuleViolation("Allocations exceed purchase line quantity")
        row = tx.execute("UPDATE logistics.purchase_allocations SET quantity=%s,cost_share_minor=%s WHERE id=%s RETURNING *", (quantity, cost_share_minor, allocation_id)).fetchone()
        _advance(tx, line["purchase_order_id"])
        return _json(row)
    return run_command(conn, "purchasing.edit_purchase_allocation", idempotency_key,
                       dict(allocation_id=allocation_id, quantity=quantity, cost_share_minor=cost_share_minor,
                            expected_version=expected_version), work)


def edit_purchase_order(conn, *, purchase_order_id, supplier_id, currency,
                        idempotency_key, expected_version=None):
    """Edit a draft header, including selecting a newer supplier revision.

    Every existing quote must still identify this supplier and currency. A
    supplier revision may change its address without rewriting an old quote.
    """
    def work(tx):
        _draft(tx, purchase_order_id, expected_version)
        _currency(currency)
        supplier = _binding(tx, supplier_id, "supplier", at=datetime.now(timezone.utc))
        rows = tx.execute("""SELECT q.currency,b.external_key FROM logistics.purchase_order_lines l
            JOIN logistics.quote_versions q ON q.id=l.quote_version_id
            JOIN logistics.core_bindings b ON b.id=q.supplier_id
            WHERE l.purchase_order_id=%s""", (purchase_order_id,)).fetchall()
        if any(row["currency"] != currency or row["external_key"] != supplier["external_key"] for row in rows):
            raise RuleViolation("Existing purchase lines do not match the new supplier/currency")
        row = tx.execute("""UPDATE logistics.purchase_orders SET supplier_id=%s,currency=%s,version=version+1
            WHERE id=%s RETURNING *""", (supplier_id, currency, purchase_order_id)).fetchone()
        return _json(row)
    return run_command(conn, "purchasing.edit_purchase_order", idempotency_key,
                       dict(purchase_order_id=purchase_order_id, supplier_id=supplier_id, currency=currency,
                            expected_version=expected_version), work)


def remove_purchase_allocation(conn, *, allocation_id, idempotency_key, expected_version=None):
    """Delete an unapproved draft allocation only; approved history is retained."""
    def work(tx):
        row = _one(tx, """SELECT a.*,l.purchase_order_id FROM logistics.purchase_allocations a
            JOIN logistics.purchase_order_lines l ON l.id=a.po_line_id WHERE a.id=%s""", (allocation_id,), "Purchase allocation")
        _draft(tx, row["purchase_order_id"], expected_version)
        tx.execute("DELETE FROM logistics.purchase_allocations WHERE id=%s", (allocation_id,))
        _advance(tx, row["purchase_order_id"])
        return dict(removed_allocation_id=allocation_id)
    return run_command(conn, "purchasing.remove_purchase_allocation", idempotency_key,
                       dict(allocation_id=allocation_id, expected_version=expected_version), work)


def remove_purchase_line(conn, *, po_line_id, idempotency_key, expected_version=None):
    """Delete an empty draft line. Remove its draft allocations first."""
    def work(tx):
        row = _one(tx, "SELECT * FROM logistics.purchase_order_lines WHERE id=%s", (po_line_id,), "Purchase line")
        _draft(tx, row["purchase_order_id"], expected_version)
        if tx.execute("SELECT id FROM logistics.purchase_allocations WHERE po_line_id=%s LIMIT 1", (po_line_id,)).fetchone():
            raise RuleViolation("Remove draft allocations before removing this purchase line")
        tx.execute("DELETE FROM logistics.purchase_order_lines WHERE id=%s", (po_line_id,))
        _advance(tx, row["purchase_order_id"])
        return dict(removed_po_line_id=po_line_id)
    return run_command(conn, "purchasing.remove_purchase_line", idempotency_key,
                       dict(po_line_id=po_line_id, expected_version=expected_version), work)


def _demand_position(conn, demand_id):
    """No balances: stock + approved allocations − explicit accepted portions."""
    row = conn.execute("""SELECT
        COALESCE((SELECT sum(quantity_shipped) FROM logistics.stock_reservations WHERE demand_line_id=%s),0) AS shipped,
        COALESCE((SELECT sum(quantity-quantity_shipped) FROM logistics.stock_reservations WHERE demand_line_id=%s AND status='active'),0) AS reserved,
        COALESCE((SELECT sum(a.quantity) FROM logistics.purchase_allocations a
          JOIN logistics.purchase_order_lines l ON l.id=a.po_line_id
          JOIN logistics.purchase_orders p ON p.id=l.purchase_order_id
          WHERE a.demand_line_id=%s AND p.status='approved'),0)
        - COALESCE((SELECT sum(ra.quantity) FROM logistics.receipt_allocations ra
          JOIN logistics.purchase_allocations a ON a.id=ra.purchase_allocation_id
          JOIN logistics.purchase_order_lines l ON l.id=a.po_line_id
          JOIN logistics.purchase_orders p ON p.id=l.purchase_order_id
          WHERE ra.demand_line_id=%s AND p.status='approved'),0) AS inbound,
        COALESCE((SELECT sum(a.cost_share_minor) FROM logistics.purchase_allocations a
          JOIN logistics.purchase_order_lines l ON l.id=a.po_line_id
          JOIN logistics.purchase_orders p ON p.id=l.purchase_order_id
          WHERE a.demand_line_id=%s AND p.status='approved'),0) AS committed_minor""",
        (demand_id,) * 5).fetchone()
    return {key: int(value) for key, value in row.items()}


def approve_purchase_order(conn, *, purchase_order_id, actor_id, idempotency_key,
                           expected_version=None, now=None):
    """Revalidate and freeze purchase terms/allocations; create no stock.

    The server supplies an authenticated actor binding with explicit purchasing
    approval permission. Passing an arbitrary actor ID is not authentication.
    Multi-demand cost sharing is explicit when any affected demand has a budget;
    no automatic cost policy is assumed before the team agrees one.
    """
    def work(tx):
        at = _instant(now, "now") if now is not None else datetime.now(timezone.utc)
        po = _draft(tx, purchase_order_id, expected_version)
        actor = _binding(tx, actor_id, "actor", at=at)
        if actor["snapshot"].get("can_approve_purchases") is not True:
            raise RuleViolation("Actor is not authorized to approve purchases")
        supplier = _binding(tx, po["supplier_id"], "supplier", at=at)
        lines = tx.execute("SELECT * FROM logistics.purchase_order_lines WHERE purchase_order_id=%s ORDER BY id FOR UPDATE", (purchase_order_id,)).fetchall()
        if not lines:
            raise RuleViolation("A purchase order must contain at least one line")
        line_ids = [line["id"] for line in lines]
        allocations = tx.execute("SELECT * FROM logistics.purchase_allocations WHERE po_line_id=ANY(%s) ORDER BY id FOR UPDATE", (line_ids,)).fetchall()
        demand_ids = sorted({a["demand_line_id"] for a in allocations})
        demands = {row["id"]: row for row in tx.execute("SELECT * FROM logistics.demand_lines WHERE id=ANY(%s) ORDER BY id FOR UPDATE", (demand_ids,)).fetchall()}
        # Stable item mutexes also serialize reserve/receive/ship against approval.
        for item in sorted({line["item_id"] for line in lines}):
            tx.execute("SELECT pg_advisory_xact_lock(%s)", (item,))
        from .stock import usable_available
        for item in sorted({line["item_id"] for line in lines}):
            if usable_available(tx, item, at.astimezone(timezone.utc).date().isoformat()) > 0:
                raise RuleViolation("Usable free stock remains; reserve available stock before approving a shortage purchase")
        cost_by_demand = {}
        quantity_by_demand = {}
        total = 0
        from psycopg.types.json import Jsonb
        for line in lines:
            _binding(tx, line["item_id"], "item", at=at)
            quote = _one(tx, "SELECT * FROM logistics.quote_versions WHERE id=%s", (line["quote_version_id"],), "Quote version")
            quoted_supplier = _binding(tx, quote["supplier_id"], "supplier")
            if quoted_supplier["external_key"] != supplier["external_key"] or quote["item_id"] != line["item_id"]:
                raise RuleViolation("Quote supplier/item does not match the purchase line")
            if quote["source"] != "supplier":
                raise RuleViolation("Estimates are not supplier commitments and cannot be approved")
            portions = [a for a in allocations if a["po_line_id"] == line["id"]]
            if not portions or sum(a["quantity"] for a in portions) != line["quantity"]:
                raise RuleViolation("Purchase quantity must be fully allocated to customer demand")
            rule = _quote_rule(quote)
            try:
                cost = rule.cost(line["quantity"])
                for allocation in portions:
                    demand = demands[allocation["demand_line_id"]]
                    if demand["item_id"] != line["item_id"] or demand["currency"] != po["currency"]:
                        raise RuleViolation("Customer demand item/currency does not match the purchase")
                    rule.validate_at(at, demand["required_by"], po["currency"])
            except ValueError as error:
                raise RuleViolation(str(error)) from error
            if len(portions) == 1 and portions[0]["cost_share_minor"] is None:
                portions[0]["cost_share_minor"] = cost
                tx.execute("UPDATE logistics.purchase_allocations SET cost_share_minor=%s WHERE id=%s", (cost, portions[0]["id"]))
            shares = [a["cost_share_minor"] for a in portions]
            budgeted = any(demands[a["demand_line_id"]]["budget_minor"] is not None for a in portions)
            if (budgeted or any(share is not None for share in shares)) and (
                    any(share is None for share in shares) or sum(shares) != cost):
                raise RuleViolation("Explicit allocation cost shares must sum to the line cost; multi-demand budget policy is not implicit")
            for allocation in portions:
                demand_id = allocation["demand_line_id"]
                quantity_by_demand[demand_id] = quantity_by_demand.get(demand_id, 0) + allocation["quantity"]
                cost_by_demand[demand_id] = cost_by_demand.get(demand_id, 0) + (allocation["cost_share_minor"] or 0)
            terms = _json(dict(quote_version=quote, quantity=line["quantity"], total_minor=cost,
                               supplier_snapshot=supplier["snapshot"], approved_at=at,
                               allocations=[dict(id=a["id"], demand_line_id=a["demand_line_id"],
                                                 quantity=a["quantity"], cost_share_minor=a["cost_share_minor"])
                                            for a in portions]))
            tx.execute("UPDATE logistics.purchase_order_lines SET approved_terms=%s WHERE id=%s", (Jsonb(terms), line["id"]))
            total += cost
        for demand_id, quantity in quantity_by_demand.items():
            demand = demands[demand_id]
            position = _demand_position(tx, demand_id)
            uncovered = demand["quantity"] - position["shipped"] - position["reserved"] - position["inbound"]
            if quantity > uncovered:
                raise RuleViolation("Purchase allocation exceeds the remaining customer shortage")
            if demand["budget_minor"] is not None and position["committed_minor"] + cost_by_demand[demand_id] > demand["budget_minor"]:
                raise RuleViolation("Combined purchase cost exceeds the customer line budget")
        tx.execute("""UPDATE logistics.purchase_orders SET status='approved',version=version+1,
            approved_at=%s,approved_by=%s,approved_supplier_snapshot=%s,total_minor=%s WHERE id=%s""",
            (at, actor_id, Jsonb(supplier["snapshot"]), total, purchase_order_id))
        return get_purchase_order(tx, purchase_order_id=purchase_order_id)
    return run_command(conn, "purchasing.approve_purchase_order", idempotency_key,
                       _json(dict(purchase_order_id=purchase_order_id, actor_id=actor_id,
                                  expected_version=expected_version, now=now)), work)


def _validate_receipt(conn, lines):
    """Stock-private helper. Validate a complete payload inside stock's transaction.

    Locks purchase lines, allocations and demands in the shared lock order.
    Aggregate repeated PO/allocation portions before testing outstanding limits.
    Never commits and never writes a purchasing receipt counter.
    """
    totals, allocation_totals, allocation_demand = {}, {}, {}
    for line in lines:
        po_line_id = line["po_line_id"]
        _positive(po_line_id, "po_line_id")
        _positive(line["quantity"], "quantity")
        totals[po_line_id] = totals.get(po_line_id, 0) + line["quantity"]
        apportioned = 0
        for allocation in line.get("allocations", []):
            aid = allocation["purchase_allocation_id"]
            _positive(aid, "purchase_allocation_id")
            _positive(allocation["quantity"], "allocation quantity")
            allocation_totals[aid] = allocation_totals.get(aid, 0) + allocation["quantity"]
            pair = (po_line_id, allocation["demand_line_id"])
            if aid in allocation_demand and allocation_demand[aid] != pair:
                raise RuleViolation("An allocation cannot refer to several purchase/customer lines")
            allocation_demand[aid] = pair
            apportioned += allocation["quantity"]
        if apportioned > line["quantity"]:
            raise RuleViolation("Reserved receipt portions exceed physical received quantity")
    locked_lines = {row["id"]: row for row in conn.execute("""SELECT l.*,p.status
        FROM logistics.purchase_order_lines l JOIN logistics.purchase_orders p ON p.id=l.purchase_order_id
        WHERE l.id=ANY(%s) ORDER BY l.id FOR UPDATE OF l""", (sorted(totals),)).fetchall()}
    if len(locked_lines) != len(totals):
        raise RuleViolation("Receipt refers to an unknown purchase line")
    locked_allocations = {row["id"]: row for row in conn.execute("SELECT * FROM logistics.purchase_allocations WHERE id=ANY(%s) ORDER BY id FOR UPDATE", (sorted(allocation_totals),)).fetchall()}
    if len(locked_allocations) != len(allocation_totals):
        raise RuleViolation("Receipt refers to an unknown purchase allocation")
    demand_ids = sorted({a["demand_line_id"] for a in locked_allocations.values()})
    conn.execute("SELECT id FROM logistics.demand_lines WHERE id=ANY(%s) ORDER BY id FOR UPDATE", (demand_ids,)).fetchall()
    for line in lines:
        po_line = locked_lines[line["po_line_id"]]
        if po_line["status"] != "approved" or po_line["item_id"] != line["item_id"]:
            raise RuleViolation("Receiving requires an approved purchase line for the same item")
    for po_line_id, quantity in totals.items():
        received = conn.execute("SELECT COALESCE(sum(quantity),0) AS quantity FROM logistics.receipt_lines WHERE po_line_id=%s", (po_line_id,)).fetchone()["quantity"]
        if received + quantity > locked_lines[po_line_id]["quantity"]:
            raise RuleViolation("Receipt exceeds outstanding purchase line quantity")
    for aid, quantity in allocation_totals.items():
        allocation = locked_allocations[aid]
        if (allocation["po_line_id"], allocation["demand_line_id"]) != allocation_demand[aid]:
            raise RuleViolation("Receipt allocation does not match its purchase and customer lines")
        received = conn.execute("SELECT COALESCE(sum(quantity),0) AS quantity FROM logistics.receipt_allocations WHERE purchase_allocation_id=%s", (aid,)).fetchone()["quantity"]
        if received + quantity > allocation["quantity"]:
            raise RuleViolation("Receipt exceeds outstanding purchase allocation quantity")


def get_purchase_order(conn, *, purchase_order_id):
    """Read a PO and its lines/allocations; this creates no replay record."""
    po = dict(_one(conn, "SELECT * FROM logistics.purchase_orders WHERE id=%s", (purchase_order_id,), "Purchase order"))
    lines = conn.execute("SELECT * FROM logistics.purchase_order_lines WHERE purchase_order_id=%s ORDER BY id", (purchase_order_id,)).fetchall()
    po["lines"] = []
    for source in lines:
        line = dict(source)
        line["allocations"] = conn.execute("SELECT * FROM logistics.purchase_allocations WHERE po_line_id=%s ORDER BY id", (line["id"],)).fetchall()
        po["lines"].append(line)
    return _json(po)


def list_purchase_orders(conn, *, status=None):
    if status is not None and status not in ("draft", "approved"):
        raise RuleViolation("Unknown purchase order status")
    rows = conn.execute("SELECT * FROM logistics.purchase_orders WHERE (%s::text IS NULL OR status=%s) ORDER BY id", (status, status)).fetchall()
    return _json(rows)


def get_quote(conn, *, quote_version_id):
    return _json(_one(conn, "SELECT * FROM logistics.quote_versions WHERE id=%s", (quote_version_id,), "Quote version"))


def list_quotes(conn, *, item_id=None, supplier_id=None):
    rows = conn.execute("""SELECT * FROM logistics.quote_versions
        WHERE (%s::bigint IS NULL OR item_id=%s) AND (%s::bigint IS NULL OR supplier_id=%s)
        ORDER BY quote_ref,version""", (item_id, item_id, supplier_id, supplier_id)).fetchall()
    return _json(rows)


def purchase_progress(conn, *, purchase_order_id):
    """Calculate received/outstanding quantities from immutable receipt records."""
    po = get_purchase_order(conn, purchase_order_id=purchase_order_id)
    for line in po["lines"]:
        received = conn.execute("SELECT COALESCE(sum(quantity),0) AS quantity FROM logistics.receipt_lines WHERE po_line_id=%s", (line["id"],)).fetchone()["quantity"]
        line["received_quantity"] = int(received)
        line["outstanding_quantity"] = line["quantity"] - int(received)
        for allocation in line["allocations"]:
            received = conn.execute("SELECT COALESCE(sum(quantity),0) AS quantity FROM logistics.receipt_allocations WHERE purchase_allocation_id=%s", (allocation["id"],)).fetchone()["quantity"]
            allocation["received_quantity"] = int(received)
            allocation["outstanding_quantity"] = allocation["quantity"] - int(received)
    return po


def compare_suppliers(conn, *, demand_line_id, now=None, budget_minor=None):
    """Read-only bounded exact comparison; recommendations never approve stock/POs."""
    at = _instant(now, "now") if now is not None else datetime.now(timezone.utc)
    demand = _one(conn, "SELECT * FROM logistics.demand_lines WHERE id=%s", (demand_line_id,), "Customer line")
    position = _demand_position(conn, demand_line_id)
    needed = max(0, demand["quantity"] - position["shipped"] - position["reserved"] - position["inbound"])
    rows = conn.execute("""SELECT q.*,b.external_key AS supplier_key FROM logistics.quote_versions q
        JOIN logistics.core_bindings b ON b.id=q.supplier_id
        WHERE q.item_id=%s AND NOT EXISTS (SELECT 1 FROM logistics.purchase_order_lines l WHERE l.quote_version_id=q.id)
        AND NOT EXISTS (SELECT 1 FROM logistics.quote_versions newer WHERE newer.quote_ref=q.quote_ref AND newer.version>q.version)
        ORDER BY q.supplier_id,q.id""", (demand["item_id"],)).fetchall()
    # One candidate per supplier: the pure solver cannot stack capacity across
    # offers from one supplier. Require explicit choice rather than discard offers.
    if len({row["supplier_key"] for row in rows}) != len(rows):
        raise RuleViolation("Select one current offer per supplier before comparison")
    limit = budget_minor
    if limit is None and demand["budget_minor"] is not None:
        limit = max(0, demand["budget_minor"] - int(position["committed_minor"]))
    try:
        result = select_quotes(needed, tuple(_quote_rule(row, line_id=demand_line_id) for row in rows),
                               now=at, deadline=demand["required_by"], currency=demand["currency"], budget_cents=limit)
    except ValueError as error:
        raise RuleViolation(str(error)) from error
    return dict(feasible=result.feasible, quantity_to_purchase=int(needed), total_minor=result.total_cents,
                allocations=[dict(quote_version_id=int(a.quote_id), quantity=a.quantity, total_minor=a.total_cents)
                             for a in result.allocations], explanations=list(result.explanations))
