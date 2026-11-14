"""Read-only operational reports with explicit quantities and date evidence."""
from datetime import date, datetime, timezone

from modules.logistics import db
from modules.logistics.errors import ValidationError
from umbrella import logistics_adapter as adapter


def build_reports(account_id, business_id, bindings, as_of=None):
    if as_of is None:
        cutoff = datetime.now(timezone.utc).date()
    elif type(as_of) is date:
        cutoff = as_of
    else:
        try:
            cutoff = date.fromisoformat(as_of)
            if cutoff.isoformat() != as_of:
                raise ValueError()
        except (TypeError, ValueError):
            raise ValidationError("as_of requires YYYY-MM-DD") from None
    with adapter._connection(account_id, business_id, bindings) as (conn, clean, _):
        with conn.transaction():
            conn.execute("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ READ ONLY")
            display = adapter._HostDisplay(business_id, clean)
            references = display.decorate_references(conn, [dict(row) for row in conn.execute(
                "SELECT * FROM logistics.core_bindings ORDER BY id").fetchall()])
            names = {row["id"]: row["snapshot"].get("name", row["external_key"]) for row in references}
            inventory = [dict(row) for row in conn.execute("""SELECT item_id,location_id,on_hand,reserved,
                on_hand-reserved AS available FROM logistics.stock_levels ORDER BY item_id,location_id""").fetchall()]
            for row in inventory:
                row.update(item_name=names.get(row["item_id"]), location_name=names.get(row["location_id"]))
            outstanding = [dict(row) for row in conn.execute("""SELECT p.id AS purchase_order_id,
                l.id AS po_line_id,p.supplier_id,l.item_id,l.quantity AS ordered_quantity,
                COALESCE(r.received_quantity,0)::bigint AS received_quantity,
                (l.quantity-COALESCE(r.received_quantity,0))::bigint AS outstanding_quantity,q.expected_date
                FROM logistics.purchase_order_lines l JOIN logistics.purchase_orders p ON p.id=l.purchase_order_id
                JOIN logistics.quote_versions q ON q.id=l.quote_version_id
                LEFT JOIN (SELECT po_line_id,sum(quantity) AS received_quantity FROM logistics.receipt_lines
                           GROUP BY po_line_id) r ON r.po_line_id=l.id
                WHERE p.status='approved' AND l.quantity>COALESCE(r.received_quantity,0) ORDER BY p.id,l.id""").fetchall()]
            for row in outstanding:
                row.update(supplier_name=names.get(row["supplier_id"]), item_name=names.get(row["item_id"]))
            shipments = [dict(row) for row in conn.execute("""SELECT s.*,COALESCE(l.quantity,0)::bigint AS total_quantity
                FROM logistics.shipment_status s LEFT JOIN
                (SELECT shipment_id,sum(quantity) AS quantity FROM logistics.shipment_lines GROUP BY shipment_id) l
                ON l.shipment_id=s.shipment_id ORDER BY s.shipment_id""").fetchall()]
            overdue = [dict(row) for row in conn.execute("""SELECT f.demand_line_id,f.order_ref,f.item_id,
                d.required_by,f.quantity,f.shipped AS shipped_quantity,f.delivered AS delivered_quantity,
                (f.quantity-f.delivered)::bigint AS remaining_quantity
                FROM logistics.demand_fulfillment f JOIN logistics.demand_lines d ON d.id=f.demand_line_id
                WHERE d.required_by<%s AND f.delivered<f.quantity ORDER BY d.required_by,f.demand_line_id""", (cutoff,)).fetchall()]
            labels = display.order_labels(conn, sorted({row["order_ref"] for row in shipments + overdue if row["order_ref"]}))
            for row in shipments + overdue:
                row.update(labels.get(row["order_ref"], {"order_number": row["order_ref"], "customer_name": None}))
            for row in overdue:
                row.update(item_name=names.get(row["item_id"]), days_overdue=(cutoff - row["required_by"]).days)
            portions = [dict(row) for row in conn.execute("""SELECT r.id AS receipt_id,rl.id AS receipt_line_id,
                rl.po_line_id,rl.item_id,rl.quantity,p.supplier_id,s.external_key AS supplier_key,
                q.expected_date AS promised_date,r.received_on,
                CASE WHEN q.expected_date IS NULL OR r.received_on IS NULL THEN NULL
                     ELSE r.received_on<=q.expected_date END AS on_time
                FROM logistics.receipt_lines rl JOIN logistics.receipts r ON r.id=rl.receipt_id
                LEFT JOIN logistics.purchase_order_lines pl ON pl.id=rl.po_line_id
                LEFT JOIN logistics.purchase_orders p ON p.id=pl.purchase_order_id
                LEFT JOIN logistics.quote_versions q ON q.id=pl.quote_version_id
                LEFT JOIN logistics.core_bindings s ON s.id=p.supplier_id ORDER BY r.id,rl.id""").fetchall()]
            totals = {"known_date_quantity": 0, "on_time_quantity": 0, "missing_date_quantity": 0,
                      "receipt_portions": len(portions)}
            for row in portions:
                row.update(supplier_name=names.get(row["supplier_id"]), item_name=names.get(row["item_id"]))
                if row["on_time"] is None:
                    totals["missing_date_quantity"] += row["quantity"]
                else:
                    totals["known_date_quantity"] += row["quantity"]
                    if row["on_time"]:
                        totals["on_time_quantity"] += row["quantity"]
            result = db.json_value({"as_of": cutoff, "inventory": inventory, "outstanding_purchases": outstanding,
                "shipments": shipments, "overdue_orders": overdue,
                "supplier_timeliness": {"portions": portions, "totals": totals},
                "limitations": [
                    "Supplier timeliness uses received physical quantities, not a count of suppliers or orders; unknown promised/receipt dates are shown separately.",
                    "Balances and shipment states reflect current committed data; as_of sets the overdue cutoff and is not a historical balance reconstruction."]})
    # The host composes independently owned module views. Logistics never calls
    # Accounting or infers paid invoices from purchases, receipts, or shipments.
    from umbrella import lifecycle, registry
    accounting = next((module for module in lifecycle.describe(account_id, business_id)["nerds"]
                       if module["id"] == "accounting" and module["status"] == "enabled"), None)
    if accounting:
        financial = registry.adapter("accounting").workspace(account_id, business_id, accounting["bindings"])
        result["customer_payment_timeliness"] = financial["payment_timeliness"]
        result["limitations"].append("Customer payment timing is a separate current Accounting snapshot with its own as_of date; the selected Logistics cutoff does not change it.")
    else:
        result["customer_payment_timeliness"] = None
        result["limitations"].append("Enable Accounting and explicitly record invoices and completed payments to include customer payment timing.")
    return result
