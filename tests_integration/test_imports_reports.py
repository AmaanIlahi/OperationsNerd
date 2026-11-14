"""CSV atomicity/deduplication and operational report evidence on local PG."""
import csv
import io
import unittest
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from unittest.mock import patch

from tests_integration import test_umbrella_workflow as flow


CUSTOMER_HEADERS = ["external_key", "name", "address"]
ORDER_HEADERS = ["external_key", "line_key", "order_number", "customer_record_id", "currency", "order_date",
                 "item_record_id", "quantity", "unit_price", "required_by", "budget"]


def csv_text(headers, rows):
    stream = io.StringIO(newline="")
    writer = csv.writer(stream, lineterminator="\n")
    writer.writerow(headers)
    writer.writerows(rows)
    return stream.getvalue()


class ImportsReportsTests(flow.UmbrellaHTTPTests):
    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        # unittest discovery may already have imported main for another fixture.
        # Initialize only this class's newly created control database in that case.
        from db import db as d
        from umbrella.businesses import initialize_registry
        d.init_db()
        initialize_registry()

    def preview(self, kind, text):
        return self.response(self.owner.post(self.base + f"/logistics/imports/{kind}/preview", json={"csv_text": text}))

    def commit(self, kind, text, preview, key, expected=200):
        return self.response(self.owner.post(self.base + f"/logistics/imports/{kind}/commit", json={
            "csv_text": text, "base_version": preview["base_version"], "digest": preview["digest"], "idempotency_key": key}), expected)

    def test_customer_preview_is_read_only_and_batches_replay_without_duplicates(self):
        self.enable()
        before = self.fixture.core_snapshot(self.bid)
        text = csv_text(CUSTOMER_HEADERS, [["customer-1", "CSV Customer A", "100 Main St, New York"],
                                           ["customer-2", "CSV Customer B", ""]])
        preview = self.preview("customers", text)
        self.assertTrue(preview["summary"]["valid"], preview["errors"])
        self.assertEqual(preview["summary"]["create_count"], 2)
        self.assertEqual(self.fixture.core_snapshot(self.bid), before)
        first = self.commit("customers", text, preview, "customers-batch")
        self.assertEqual(first["created_count"], 2)
        self.assertEqual(first, self.commit("customers", text, preview, "customers-batch"))
        again = self.preview("customers", text)
        self.assertEqual(again["summary"]["skip_count"], 2)
        skipped = self.commit("customers", text, again, "customers-new-request")
        self.assertEqual((skipped["created_count"], skipped["skipped_count"]), (0, 2))
        current = self.response(self.owner.get(self.base + f"/records/{self.entities['Customer']['key']}"))
        self.assertEqual(current["total"], 3)  # One original plus two imported customers.
        bad = csv_text(CUSTOMER_HEADERS, [["new-one", "Would be valid", ""], ["new-two", "", ""]])
        invalid = self.preview("customers", bad)
        self.assertFalse(invalid["summary"]["valid"])
        self.assertTrue(any(error["row"] == 3 and error["field"] == "name" for error in invalid["errors"]))
        self.commit("customers", bad, invalid, "invalid-batch", 422)
        self.assertEqual(self.response(self.owner.get(self.base + f"/records/{self.entities['Customer']['key']}"))["total"], 3)
        changed = csv_text(CUSTOMER_HEADERS, [["customer-1", "Different name", "100 Main St, New York"]])
        conflict = self.preview("customers", changed)
        self.assertFalse(conflict["summary"]["valid"])
        self.assertTrue(any("different content" in error["message"] for error in conflict["errors"]))
        self.commit("customers", changed, conflict, "changed-existing", 422)
        self.commit("customers", text + "customer-3,Extra,\n", preview, "tampered-preview", 409)

    def test_fifty_multiline_orders_exact_money_and_stale_schema(self):
        self.enable()
        train = self.record("Item", {"Name": "Toy Train", "SKU": "CSV-TRAIN", "Unit": "each"})
        self.command("sync_records", {}, "sync-import-items")
        rows = []
        for number in range(50):
            for line_key, item_id, quantity, price, budget in (("A", self.item["id"], 1, "10.00", "20.00"),
                                                            ("B", train["id"], 2, "2.50", "5.00")):
                rows.append([f"order-{number}", line_key, f"CSV-{number}", self.customer["id"], "USD", "",
                             item_id, quantity, price, self.deadline, budget])
        text = csv_text(ORDER_HEADERS, rows)
        preview = self.preview("orders", text)
        self.assertTrue(preview["summary"]["valid"], preview["errors"])
        self.assertEqual((preview["summary"]["total_rows"], preview["summary"]["total_orders"]), (100, 50))
        self.assertEqual({row["unit_price_minor"] for row in preview["rows"]}, {1000, 250})
        renamed = self.chat_operations([{"op": "update_entity_type", "entity": self.bindings["order"]["entity"], "label": "Orders"}])
        self.response(self.owner.post(f"/api/proposals/{renamed['proposal_id']}/approve", headers={"X-Business-Id": str(self.bid)}))
        self.commit("orders", text, preview, "stale-orders", 409)
        preview = self.preview("orders", text)
        imported = self.commit("orders", text, preview, "fifty-orders")
        self.assertEqual(imported["created_count"], 50)
        self.assertTrue(all(len(order["line_record_ids"]) == 2 for order in imported["created"]))
        self.assertEqual(self.fixture.scalar(self.bid, "SELECT count(*) FROM logistics.demand_lines"), 0)
        orders = self.response(self.owner.get(self.base + f"/records/{self.entities['Customer order']['key']}"))
        lines = self.response(self.owner.get(self.base + f"/records/{self.entities['Order line']['key']}?limit=500"))
        self.assertEqual((orders["total"], lines["total"]), (51, 101))
        self.assertEqual(imported, self.commit("orders", text, preview, "fifty-orders"))
        reversed_text = csv_text(ORDER_HEADERS, reversed(rows))
        reversed_preview = self.preview("orders", reversed_text)
        self.assertTrue(reversed_preview["summary"]["valid"], reversed_preview["errors"])
        replay = self.commit("orders", reversed_text, reversed_preview, "fifty-orders-new-key")
        self.assertEqual((replay["created_count"], replay["skipped_count"]), (0, 50))
        # Imported orders remain drafts until the owner invokes ordinary submission.
        first = imported["created"][0]
        submitted = self.response(self.owner.post(self.base + f"/logistics/orders/{first['record_id']}/submit",
            json={"idempotency_key": "submit-imported-order"}))
        self.assertEqual(len(submitted["demands"]), 2)

    def test_bad_numeric_reference_and_duplicate_rows_are_rejected(self):
        self.enable()
        good = ["order-x", "line-a", "ORDER-X", self.customer["id"], "USD", "", self.item["id"], 1, "10.00", self.deadline, ""]
        cases = [(7, "1e3", "quantity"), (7, "1.5", "quantity"), (8, "1,5", "unit_price"),
                 (8, "1e3", "unit_price"), (6, self.customer["id"], "record"), (3, 999999, "record")]
        for column, value, field in cases:
            row = list(good)
            row[column] = value
            with self.subTest(field=field, value=value):
                preview = self.preview("orders", csv_text(ORDER_HEADERS, [row]))
                self.assertFalse(preview["summary"]["valid"])
                self.assertTrue(any(error["field"] == field for error in preview["errors"]), preview["errors"])
        duplicate = self.preview("orders", csv_text(ORDER_HEADERS, [good, good]))
        self.assertFalse(duplicate["summary"]["valid"])
        self.assertTrue(any(error["field"] == "line_key" for error in duplicate["errors"]))
        unknown = self.preview("customers", "external_key,name,unexpected\n1,Name,x\n")
        self.assertFalse(unknown["summary"]["valid"])
        self.assertTrue(any(error["field"] == "headers" for error in unknown["errors"]))
        from umbrella.imports import _read
        _, errors = _read("customers", "external_key,name\n" + "too,many,cells\n" * 5001)
        self.assertTrue(any(error["field"] == "rows" and "5000" in error["message"] for error in errors))

    def test_late_commit_failure_rolls_back_earlier_orders_and_request_key(self):
        self.enable()
        rows = [["atomic-A", "one", "ATOMIC-A", self.customer["id"], "USD", "", self.item["id"], 1, "10.00", self.deadline, ""],
                ["atomic-B", "one", "ATOMIC-B", self.customer["id"], "USD", "", self.item["id"], 1, "10.00", self.deadline, ""]]
        text = csv_text(ORDER_HEADERS, rows)
        preview = self.preview("orders", text)
        before = self.fixture.core_snapshot(self.bid)
        from umbrella import imports
        original = imports._create
        calls = 0
        def interrupted(*args, **kwargs):
            nonlocal calls
            calls += 1
            if calls == 4:
                raise RuntimeError("injected later line failure")
            return original(*args, **kwargs)
        with patch.object(imports, "_create", side_effect=interrupted):
            self.commit("orders", text, preview, "atomic-import", 503)
        self.assertEqual(self.fixture.core_snapshot(self.bid), before)
        result = self.commit("orders", text, preview, "atomic-import")
        self.assertEqual(result["created_count"], 2)

    def test_concurrent_batches_with_different_keys_create_each_external_identity_once(self):
        self.enable()
        text = csv_text(CUSTOMER_HEADERS, [["simultaneous-A", "Concurrent customer A", ""],
                                           ["simultaneous-B", "Concurrent customer B", ""]])
        preview = self.preview("customers", text)
        with ThreadPoolExecutor(max_workers=2) as executor:
            results = list(executor.map(lambda key: self.commit("customers", text, preview, key),
                                        ["simultaneous-one", "simultaneous-two"]))
        self.assertEqual(sorted(result["created_count"] for result in results), [0, 2])
        self.assertEqual(sorted(result["skipped_count"] for result in results), [0, 2])
        records = self.response(self.owner.get(self.base + f"/records/{self.entities['Customer']['key']}"))
        self.assertEqual(records["total"], 3)

    def test_reports_use_actual_portions_and_do_not_mutate_data(self):
        self.enable()
        submitted = self.response(self.owner.post(self.base + f"/logistics/orders/{self.order['id']}/submit",
            json={"idempotency_key": "report-order"}))
        demand = submitted["demands"][0]
        workspace = self.workspace()
        supplier = self.reference(workspace, "supplier", "Toy Supplier")["id"]
        item = self.reference(workspace, "item", "Toy Robot")["id"]
        location = self.reference(workspace, "location", "Toy Warehouse")["id"]
        promised = self.today + timedelta(days=2)
        quote = self.command("create_quote", {"supplier_id": supplier, "item_id": item, "quantity_priced": 10,
            "capacity_quantity": 10, "price_minor": 1000, "pricing_kind": "unit", "currency": "USD",
            "expires_at": (datetime.now(timezone.utc) + timedelta(days=7)).isoformat(), "expected_date": promised.isoformat()})
        po = self.command("create_purchase_order", {"supplier_id": supplier, "currency": "USD"})
        line = self.command("add_purchase_line", {"purchase_order_id": po["id"], "quote_version_id": quote["id"], "quantity": 10})
        allocation = self.command("allocate_purchase_line", {"po_line_id": line["id"], "demand_line_id": demand["id"], "quantity": 10,
                                                              "cost_share_minor": 10000})
        self.command("approve_purchase_order", {"purchase_order_id": po["id"]})
        receipts = []
        for number, amount, received in ((1, 3, self.today + timedelta(days=1)), (2, 4, self.today + timedelta(days=4))):
            receipts.append(self.command("receive_delivery", {"delivery_ref": f"REPORT-{number}", "received_on": received.isoformat(),
                "lines": [{"po_line_id": line["id"], "item_id": item, "location_id": location, "quantity": amount,
                           "allocations": [{"purchase_allocation_id": allocation["id"], "demand_line_id": demand["id"], "quantity": amount}]}]}))
        # A valid non-PO receipt has no supplier promise and belongs in the
        # explicit unknown-date denominator, not the on-time numerator.
        from db import db as d
        from db.runtime import database_scope
        from modules.logistics import db as logistics_db, stock
        with database_scope(self.fixture.business_url(self.bid)), d.raw_postgres_conn() as conn:
            logistics_db.prepare_connection(conn, f"business:{self.bid}")
            stock.receive(conn, item, 2, "unattributed receipt", "report-unknown", location_id=location,
                          received_on=self.today.isoformat())
        reservation = receipts[0]["lines"][0]["reservations"][0]["reservation_id"]
        shipment = self.command("create_shipment", {"shipment_number": "REPORT-SHIP", "order_ref": demand["order_ref"]})
        self.command("add_shipment_line", {"shipment_id": shipment["shipment_id"], "reservation_id": reservation, "quantity": 3})
        self.command("dispatch", {"shipment_id": shipment["shipment_id"], "carrier": "Fixture", "tracking_ref": "REPORT-TRACK",
                                  "as_of": self.today.isoformat()})
        self.command("record_tracking_event", {"shipment_id": shipment["shipment_id"], "event": "delivered",
                                               "occurred_at": datetime.now(timezone.utc).isoformat()})
        before = self.fixture.core_snapshot(self.bid)
        count = self.fixture.scalar(self.bid, "SELECT count(*) FROM logistics.command_requests")
        report = self.response(self.owner.get(self.base + "/logistics/reports", params={"as_of": (self.today + timedelta(days=15)).isoformat()}))
        self.assertEqual(report["outstanding_purchases"][0]["outstanding_quantity"], 3)
        self.assertEqual((report["inventory"][0]["on_hand"], report["inventory"][0]["reserved"], report["inventory"][0]["available"]), (6, 4, 2))
        self.assertEqual(report["shipments"][0]["status"], "delivered")
        overdue = report["overdue_orders"][0]
        self.assertEqual((overdue["quantity"], overdue["shipped_quantity"], overdue["delivered_quantity"], overdue["remaining_quantity"]), (10, 3, 3, 7))
        totals = report["supplier_timeliness"]["totals"]
        self.assertEqual(totals, {"known_date_quantity": 7, "on_time_quantity": 3, "missing_date_quantity": 2, "receipt_portions": 3})
        self.assertEqual([row["on_time"] for row in report["supplier_timeliness"]["portions"]], [True, False, None])
        self.assertTrue(any("payment" in limitation.lower() for limitation in report["limitations"]))
        self.assertEqual(self.fixture.core_snapshot(self.bid), before)
        self.assertEqual(self.fixture.scalar(self.bid, "SELECT count(*) FROM logistics.command_requests"), count)
        self.response(self.stranger.get(self.base + "/logistics/reports"), 404)


def load_tests(loader, tests, pattern):
    # Reuse the HTTP fixture/helpers, not the base class's independent workflows.
    return unittest.TestSuite(ImportsReportsTests(name) for name, method in ImportsReportsTests.__dict__.items()
                              if name.startswith("test_") and callable(method))


if __name__ == "__main__":
    unittest.main()
