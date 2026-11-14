"""Purchasing contracts against real PostgreSQL, including independent writers."""
from concurrent.futures import ThreadPoolExecutor
from datetime import date, datetime, timedelta, timezone
from threading import Barrier
from queue import Queue
import time

import psycopg

from .. import core, purchasing as p, stock
from ..errors import LogisticsError, IdempotencyConflict
from .common import PostgresCase


class PurchasingTests(PostgresCase):
    def setUp(self):
        super().setUp()
        self.item = self.bind("item", "chair")
        self.other_item = self.bind("item", "table")
        self.supplier = self.bind("supplier", "supplier-A", {"name": "Supplier A", "address": "Old address"})
        self.supplier_b = self.bind("supplier", "supplier-B")
        self.location = self.bind("location", "warehouse")
        self.actor = self.bind("actor", "owner", {"can_approve_purchases": True})
        self.deadline = date.today() + timedelta(days=15)

    def quote(self, *, quantity=10, supplier=None, price=100, **options):
        values = dict(supplier_id=supplier or self.supplier, item_id=self.item,
                      quantity_priced=quantity, capacity_quantity=quantity,
                      price_minor=price, pricing_kind="unit", currency="USD",
                      expires_at=datetime.now(timezone.utc) + timedelta(days=7),
                      expected_date=date.today() + timedelta(days=2),
                      idempotency_key=self.key("quote"))
        values.update(options)
        return p.create_quote(self.conn, **values)

    def purchase(self, *, quantity=10, supplier=None, demand=None, budget=None, **quote_options):
        supplier = supplier or self.supplier
        demand = demand or self.demand(self.item, quantity, budget_minor=budget, required_by=self.deadline)
        quote = self.quote(quantity=quantity, supplier=supplier, **quote_options)
        po = p.create_purchase_order(self.conn, supplier_id=supplier, currency="USD", idempotency_key=self.key("po"))
        line = p.add_purchase_line(self.conn, purchase_order_id=po["id"], quote_version_id=quote["id"],
                                   quantity=quantity, idempotency_key=self.key("line"))
        allocation = p.allocate_purchase_line(self.conn, po_line_id=line["id"], demand_line_id=demand,
                                             quantity=quantity, idempotency_key=self.key("allocation"))
        return po, line, allocation, quote

    def approve(self, po, **options):
        return p.approve_purchase_order(self.conn, purchase_order_id=po["id"], actor_id=self.actor,
                                        idempotency_key=self.key("approve"), **options)

    def receive(self, line, allocation, quantity, **options):
        values = dict(delivery_ref=self.key("delivery"), lines=[dict(po_line_id=line["id"],
            item_id=self.item, location_id=self.location, quantity=quantity,
            allocations=[dict(purchase_allocation_id=allocation["id"],
                              demand_line_id=allocation["demand_line_id"], quantity=quantity)])],
            received_on=date.today(), idempotency_key=self.key("receive"))
        values.update(options)
        return stock.receive_delivery(self.conn, **values)

    def test_approval_freezes_terms_and_replays_without_stock(self):
        po, line, allocation, quote = self.purchase(budget=1000)
        version = p.get_purchase_order(self.conn, purchase_order_id=po["id"])["version"]
        kwargs = dict(purchase_order_id=po["id"], actor_id=self.actor,
                      expected_version=version, idempotency_key="approval")
        approved = p.approve_purchase_order(self.conn, **kwargs)
        self.assertEqual(approved, p.approve_purchase_order(self.conn, **kwargs))
        self.assertEqual(approved["status"], "approved")
        self.assertEqual(approved["total_minor"], 1000)
        self.assertEqual(approved["lines"][0]["approved_terms"]["quote_version"]["id"], quote["id"])
        self.assertEqual(approved["lines"][0]["allocations"][0]["cost_share_minor"], 1000)
        self.assertEqual(self.conn.execute("SELECT count(*) AS n FROM logistics.stock_movements").fetchone()["n"], 0)
        self.assertEqual(stock.on_hand(self.conn, self.item), 0)

    def test_quote_revisions_are_immutable_and_old_supplier_snapshot_survives(self):
        po, line, allocation, quote = self.purchase(quote_ref="offer-A")
        approved = self.approve(po)
        revised = self.quote(quote_ref="offer-A", price=130)
        self.assertEqual(revised["version"], 2)
        self.assertEqual(revised["supersedes_id"], quote["id"])
        self.assertEqual(p.get_quote(self.conn, quote_version_id=quote["id"])["price_minor"], 100)
        with self.assertRaises(psycopg.Error):
            self.conn.execute("UPDATE logistics.quote_versions SET price_minor=99 WHERE id=%s", (quote["id"],))
        core.bind_reference(self.conn, "supplier", "supplier-A",
                            {"name": "Supplier A", "address": "New address"}, self.key("revision"))
        current = p.get_purchase_order(self.conn, purchase_order_id=po["id"])
        self.assertEqual(current["approved_supplier_snapshot"]["address"], "Old address")
        self.assertEqual(current["lines"][0]["approved_terms"], approved["lines"][0]["approved_terms"])

    def test_quote_replay_uses_original_server_timestamp(self):
        kwargs = dict(supplier_id=self.supplier, item_id=self.item, quantity_priced=2,
                      capacity_quantity=2, price_minor=15, pricing_kind="unit", currency="USD",
                      expires_at=datetime.now(timezone.utc) + timedelta(days=1),
                      expected_date=date.today(), idempotency_key="same-quote")
        first = p.create_quote(self.conn, **kwargs)
        self.assertEqual(first, p.create_quote(self.conn, **kwargs))
        with self.assertRaises(IdempotencyConflict):
            p.create_quote(self.conn, **dict(kwargs, price_minor=16))
        with self.assertRaises(IdempotencyConflict):
            p.create_purchase_order(self.conn, supplier_id=self.supplier, currency="USD", idempotency_key="same-quote")

    def test_approved_documents_reject_api_and_direct_database_edits(self):
        po, line, allocation, _ = self.purchase()
        self.approve(po)
        for mutation in (
            lambda: p.edit_purchase_line(self.conn, po_line_id=line["id"], quantity=9, idempotency_key=self.key()),
            lambda: p.edit_purchase_allocation(self.conn, allocation_id=allocation["id"], quantity=9, idempotency_key=self.key()),
            lambda: p.edit_purchase_order(self.conn, purchase_order_id=po["id"], supplier_id=self.supplier,
                                          currency="USD", idempotency_key=self.key()),
            lambda: p.remove_purchase_allocation(self.conn, allocation_id=allocation["id"], idempotency_key=self.key()),
        ):
            with self.assertRaises(LogisticsError):
                mutation()
        for sql, args in (
            ("UPDATE logistics.purchase_order_lines SET quantity=9 WHERE id=%s", (line["id"],)),
            ("DELETE FROM logistics.purchase_allocations WHERE id=%s", (allocation["id"],)),
            ("UPDATE logistics.purchase_orders SET currency='EUR' WHERE id=%s", (po["id"],)),
        ):
            with self.assertRaises(psycopg.Error):
                self.conn.execute(sql, args)

    def test_draft_edits_version_checks_and_removal(self):
        po, line, allocation, _ = self.purchase()
        with self.assertRaises(LogisticsError):
            p.edit_purchase_line(self.conn, po_line_id=line["id"], quantity=10,
                                 expected_version=1, idempotency_key=self.key())
        p.edit_purchase_allocation(self.conn, allocation_id=allocation["id"], quantity=6, idempotency_key=self.key())
        edited = p.edit_purchase_line(self.conn, po_line_id=line["id"], quantity=6, idempotency_key=self.key())
        self.assertEqual(edited["quantity"], 6)
        with self.assertRaises(LogisticsError):
            p.remove_purchase_line(self.conn, po_line_id=line["id"], idempotency_key=self.key())
        p.remove_purchase_allocation(self.conn, allocation_id=allocation["id"], idempotency_key=self.key())
        p.remove_purchase_line(self.conn, po_line_id=line["id"], idempotency_key=self.key())
        self.assertEqual(p.get_purchase_order(self.conn, purchase_order_id=po["id"])["lines"], [])

    def test_same_exact_quote_cannot_convert_twice(self):
        po, line, allocation, quote = self.purchase()
        second = p.create_purchase_order(self.conn, supplier_id=self.supplier, currency="USD", idempotency_key=self.key())
        with self.assertRaises(LogisticsError):
            p.add_purchase_line(self.conn, purchase_order_id=second["id"], quote_version_id=quote["id"], quantity=1, idempotency_key=self.key())
        self.assertEqual(p.get_purchase_order(self.conn, purchase_order_id=second["id"])["lines"], [])

    def test_split_suppliers_approve_only_uncovered_demand(self):
        demand = self.demand(self.item, 10, budget_minor=1100)
        first = self.purchase(quantity=6, demand=demand)
        second = self.purchase(quantity=4, supplier=self.supplier_b, demand=demand)
        self.approve(first[0])
        self.approve(second[0])
        extra = self.purchase(quantity=1, demand=demand, quote_ref="extra")
        with self.assertRaises(LogisticsError):
            self.approve(extra[0])
        self.assertEqual(p.get_purchase_order(self.conn, purchase_order_id=extra[0]["id"])["status"], "draft")

    def test_one_purchase_line_serves_two_customer_lines_with_explicit_costs(self):
        da = self.demand(self.item, 6, order_ref="A", budget_minor=600)
        db = self.demand(self.item, 4, order_ref="B", budget_minor=400)
        quote = self.quote()
        po = p.create_purchase_order(self.conn, supplier_id=self.supplier, currency="USD", idempotency_key=self.key())
        line = p.add_purchase_line(self.conn, purchase_order_id=po["id"], quote_version_id=quote["id"], quantity=10, idempotency_key=self.key())
        aa = p.allocate_purchase_line(self.conn, po_line_id=line["id"], demand_line_id=da, quantity=6, cost_share_minor=600, idempotency_key=self.key())
        ab = p.allocate_purchase_line(self.conn, po_line_id=line["id"], demand_line_id=db, quantity=4, cost_share_minor=400, idempotency_key=self.key())
        self.approve(po)
        receipt = stock.receive_delivery(self.conn, delivery_ref="two-customers", received_on=date.today(), idempotency_key=self.key(),
            lines=[dict(po_line_id=line["id"], item_id=self.item, location_id=self.location, quantity=10,
                        allocations=[dict(purchase_allocation_id=a["id"], demand_line_id=a["demand_line_id"], quantity=a["quantity"])
                                     for a in (aa, ab)])])
        self.assertEqual(len(receipt["lines"][0]["reservations"]), 2)
        progress = p.purchase_progress(self.conn, purchase_order_id=po["id"])
        self.assertEqual(progress["lines"][0]["outstanding_quantity"], 0)
        self.assertEqual([a["received_quantity"] for a in progress["lines"][0]["allocations"]], [6, 4])

    def test_ambiguous_multi_demand_budget_shares_are_rejected(self):
        da = self.demand(self.item, 5, budget_minor=500)
        db = self.demand(self.item, 5, budget_minor=500)
        quote = self.quote()
        po = p.create_purchase_order(self.conn, supplier_id=self.supplier, currency="USD", idempotency_key=self.key())
        line = p.add_purchase_line(self.conn, purchase_order_id=po["id"], quote_version_id=quote["id"], quantity=10, idempotency_key=self.key())
        for demand in (da, db):
            p.allocate_purchase_line(self.conn, po_line_id=line["id"], demand_line_id=demand, quantity=5, idempotency_key=self.key())
        with self.assertRaisesRegex(LogisticsError, "cost shares"):
            self.approve(po)
        self.assertEqual(p.get_purchase_order(self.conn, purchase_order_id=po["id"])["status"], "draft")

    def test_partial_receipts_derive_progress_and_replay_without_duplicate_stock(self):
        po, line, allocation, _ = self.purchase()
        self.approve(po)
        kwargs = dict(delivery_ref="part-1", idempotency_key="receive-part-1")
        first = self.receive(line, allocation, 3, **kwargs)
        self.assertEqual(first, self.receive(line, allocation, 3, **kwargs))
        progress = p.purchase_progress(self.conn, purchase_order_id=po["id"])["lines"][0]
        self.assertEqual((progress["received_quantity"], progress["outstanding_quantity"]), (3, 7))
        self.assertEqual(progress["allocations"][0]["received_quantity"], 3)
        self.receive(line, allocation, 7)
        self.assertEqual(stock.on_hand(self.conn, self.item), 10)
        with self.assertRaises(LogisticsError):
            self.receive(line, allocation, 1)
        self.assertEqual(stock.on_hand(self.conn, self.item), 10)

    def test_repeated_portions_are_aggregated_before_receiving(self):
        po, line, allocation, _ = self.purchase(quantity=10)
        self.approve(po)
        portion = dict(po_line_id=line["id"], item_id=self.item, location_id=self.location, quantity=6,
                       allocations=[dict(purchase_allocation_id=allocation["id"], demand_line_id=allocation["demand_line_id"], quantity=6)])
        with self.assertRaises(LogisticsError):
            stock.receive_delivery(self.conn, delivery_ref="too-many", lines=[portion, portion], received_on=date.today(), idempotency_key="failed-delivery")
        self.assertEqual(stock.on_hand(self.conn, self.item), 0)
        self.assertIsNone(self.conn.execute("SELECT * FROM logistics.command_requests WHERE idempotency_key='failed-delivery'").fetchone())
        self.assertEqual(self.conn.execute("SELECT count(*) AS n FROM logistics.receipts").fetchone()["n"], 0)

    def test_approval_rejects_budget_deadline_expiry_estimates_and_wrong_actor(self):
        po, line, allocation, quote = self.purchase(quantity=2, budget=199)
        with self.assertRaisesRegex(LogisticsError, "budget"):
            self.approve(po)
        actor = self.bind("actor", "viewer", {"can_approve_purchases": False})
        with self.assertRaisesRegex(LogisticsError, "authorized"):
            p.approve_purchase_order(self.conn, purchase_order_id=po["id"], actor_id=actor, idempotency_key=self.key())
        for options in (dict(source="ai_estimate"), dict(expected_date=self.deadline + timedelta(days=1))):
            other = self.purchase(quantity=2, **options)
            with self.assertRaises(LogisticsError):
                self.approve(other[0])
        later = datetime.fromisoformat(quote["expires_at"]) + timedelta(seconds=1)
        with self.assertRaisesRegex(LogisticsError, "expired"):
            self.approve(po, now=later)
        self.assertEqual(p.get_purchase_order(self.conn, purchase_order_id=po["id"])["status"], "draft")

    def test_exact_total_price_wrong_quantity_and_wrong_item_are_rejected(self):
        quote = self.quote(pricing_kind="total", price=999)
        po = p.create_purchase_order(self.conn, supplier_id=self.supplier, currency="USD", idempotency_key=self.key())
        with self.assertRaises(LogisticsError):
            p.add_purchase_line(self.conn, purchase_order_id=po["id"], quote_version_id=quote["id"], quantity=9, idempotency_key=self.key())
        line = p.add_purchase_line(self.conn, purchase_order_id=po["id"], quote_version_id=quote["id"], quantity=10, idempotency_key=self.key())
        demand = self.demand(self.other_item, 10)
        with self.assertRaises(LogisticsError):
            p.allocate_purchase_line(self.conn, po_line_id=line["id"], demand_line_id=demand, quantity=10, idempotency_key=self.key())

    def test_reserve_available_stock_before_purchasing_shortage(self):
        po, line, allocation, _ = self.purchase(quantity=3)
        stock.open_stock(self.conn, self.item, 2, "opening", self.key(), location_id=self.location)
        with self.assertRaisesRegex(LogisticsError, "reserve"):
            self.approve(po)

    def test_stock_only_receipt_does_not_create_negative_purchase_inbound(self):
        demand = self.demand(self.item, 3)
        external = self.conn.execute("SELECT external_key FROM logistics.demand_lines WHERE id=%s", (demand,)).fetchone()["external_key"]
        stock.receive_and_reserve(self.conn, self.item, 3, "outside-purchase", external,
                                  self.key(), location_id=self.location, demand_line_id=demand)
        po, _, _, _ = self.purchase(quantity=3, demand=demand)
        with self.assertRaisesRegex(LogisticsError, "shortage"):
            self.approve(po)
        self.assertEqual(p.compare_suppliers(self.conn, demand_line_id=demand)["quantity_to_purchase"], 0)

    def test_comparison_uses_remaining_budget_and_has_no_purchase_side_effect(self):
        demand = self.demand(self.item, 10, budget_minor=1200)
        self.quote(quantity=6, quote_ref="A")
        self.quote(quantity=4, supplier=self.supplier_b, price=120, quote_ref="B")
        result = p.compare_suppliers(self.conn, demand_line_id=demand)
        self.assertTrue(result["feasible"])
        self.assertEqual(result["total_minor"], 1080)
        self.assertEqual(sum(a["quantity"] for a in result["allocations"]), 10)
        self.assertEqual(p.list_purchase_orders(self.conn), [])

    def test_comparison_cannot_stack_capacity_across_supplier_revisions(self):
        demand = self.demand(self.item, 10)
        self.quote(quantity=5, quote_ref="old-offer")
        revision = core.bind_reference(self.conn, "supplier", "supplier-A",
                                       {"address": "New address"}, self.key("revision"))
        self.quote(quantity=5, supplier=revision["id"], quote_ref="another-offer")
        with self.assertRaisesRegex(LogisticsError, "one current offer per supplier"):
            p.compare_suppliers(self.conn, demand_line_id=demand)

    def test_two_approvals_cannot_purchase_same_last_demand(self):
        demand = self.demand(self.item, 5)
        a = self.purchase(quantity=5, demand=demand)
        b = self.purchase(quantity=5, demand=demand, supplier=self.supplier_b)
        barrier = Barrier(2)
        def attempt(po_id, key):
            with self.newconn() as conn:
                barrier.wait()
                try:
                    p.approve_purchase_order(conn, purchase_order_id=po_id, actor_id=self.actor, idempotency_key=key)
                    return True
                except LogisticsError:
                    return False
        with ThreadPoolExecutor(max_workers=2) as pool:
            one = pool.submit(attempt, a[0]["id"], "race-a")
            two = pool.submit(attempt, b[0]["id"], "race-b")
            self.assertEqual(sorted([one.result(), two.result()]), [False, True])
        self.assertEqual(len(p.list_purchase_orders(self.conn, status="approved")), 1)

    def test_allocation_reloads_quantity_after_waiting_for_a_draft_edit(self):
        demand = self.demand(self.item, 10)
        quote = self.quote(quantity=10)
        po = p.create_purchase_order(self.conn, supplier_id=self.supplier, currency="USD", idempotency_key=self.key())
        line = p.add_purchase_line(self.conn, purchase_order_id=po["id"], quote_version_id=quote["id"], quantity=10, idempotency_key=self.key())
        pid_queue = Queue()
        def allocate_after_edit():
            with self.newconn() as conn:
                pid_queue.put(conn.info.backend_pid)
                try:
                    p.allocate_purchase_line(conn, po_line_id=line["id"], demand_line_id=demand,
                                             quantity=8, idempotency_key="racing-draft-allocation")
                    return True
                except LogisticsError:
                    return False
        with ThreadPoolExecutor(max_workers=1) as pool:
            with self.conn.transaction():
                # The DB child guard locks the draft header. Other connections
                # still see the old quantity until this transaction commits.
                self.conn.execute("UPDATE logistics.purchase_order_lines SET quantity=5 WHERE id=%s", (line["id"],))
                future = pool.submit(allocate_after_edit)
                pid = pid_queue.get(timeout=2)
                timeout = time.monotonic() + 3
                while time.monotonic() < timeout:
                    waiting = self.conn.execute("SELECT wait_event_type FROM pg_stat_activity WHERE pid=%s", (pid,)).fetchone()
                    if waiting and waiting["wait_event_type"] == "Lock":
                        break
                    time.sleep(0.01)
                else:
                    self.fail("Allocation did not wait for the concurrent draft edit")
            self.assertFalse(future.result(timeout=3))
        self.assertEqual(p.get_purchase_order(self.conn, purchase_order_id=po["id"])["lines"][0]["allocations"], [])

    def test_two_receipts_cannot_exceed_last_purchase_quantity(self):
        po, line, allocation, _ = self.purchase(quantity=5)
        self.approve(po)
        barrier = Barrier(2)
        payload = [dict(po_line_id=line["id"], item_id=self.item, location_id=self.location, quantity=5,
                        allocations=[dict(purchase_allocation_id=allocation["id"], demand_line_id=allocation["demand_line_id"], quantity=5)])]
        def attempt(ref):
            with self.newconn() as conn:
                barrier.wait()
                try:
                    stock.receive_delivery(conn, delivery_ref=ref, lines=payload, received_on=date.today(), idempotency_key=ref)
                    return True
                except LogisticsError:
                    return False
        with ThreadPoolExecutor(max_workers=2) as pool:
            one = pool.submit(attempt, "receipt-race-a")
            two = pool.submit(attempt, "receipt-race-b")
            self.assertEqual(sorted([one.result(), two.result()]), [False, True])
        self.assertEqual(stock.on_hand(self.conn, self.item), 5)
