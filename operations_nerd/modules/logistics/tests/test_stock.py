"""Real PostgreSQL acceptance tests: no mocked transactions or ledger balances."""
from concurrent.futures import ThreadPoolExecutor
from datetime import date, datetime, timedelta, timezone
from uuid import uuid4
import unittest

import psycopg

from .. import stock, purchasing
from ..db import run_command
from ..errors import Conflict, IdempotencyConflict, LogisticsError, ValidationError
from .common import PostgresCase


class StockTests(PostgresCase):
    def setUp(self):
        super().setUp()
        self.item = self.bind("item", "chair", {"name": "Chair"})
        self.location = self.bind("location", "warehouse", {"name": "Warehouse"})
        self.actor = self.bind("actor", "manager", {"can_approve_purchases": True})

    def _demand(self, quantity, order="customer-1"):
        did = self.demand(self.item, quantity, order_ref=order)
        row = self.conn.execute("SELECT * FROM logistics.demand_lines WHERE id=%s", (did,)).fetchone()
        return row

    def _po(self, quantity, allocations):
        sid = self.bind("supplier", str(uuid4()), {"name": "Supplier"})
        q = purchasing.create_quote(self.conn, supplier_id=sid, item_id=self.item,
            quantity_priced=quantity, capacity_quantity=quantity, price_minor=100,
            pricing_kind="unit", currency="USD", expires_at=datetime.now(timezone.utc)+timedelta(days=2),
            expected_date=date.today()+timedelta(days=1), idempotency_key=str(uuid4()))
        p = purchasing.create_purchase_order(self.conn, supplier_id=sid, currency="USD", idempotency_key=str(uuid4()))
        l = purchasing.add_purchase_line(self.conn, purchase_order_id=p["id"], quote_version_id=q["id"], quantity=quantity, idempotency_key=str(uuid4()))
        # add_purchase_line returns either the inserted line or current PO projection.
        lid = l.get("po_line_id", l.get("id"))
        if "lines" in l:
            lid = l["lines"][-1]["id"]
        result = []
        for did, amount in allocations:
            a = purchasing.allocate_purchase_line(self.conn, po_line_id=lid, demand_line_id=did,
                quantity=amount, cost_share_minor=amount*100, idempotency_key=str(uuid4()))
            aid = a.get("allocation_id", a.get("id"))
            if "lines" in a:
                aid = a["lines"][-1]["allocations"][-1]["id"]
            result.append((aid, did, amount))
        purchasing.approve_purchase_order(self.conn, purchase_order_id=p["id"], actor_id=self.actor, idempotency_key=str(uuid4()))
        return p["id"], lid, result

    def _line(self, lid, quantity, allocations=(), **extra):
        return dict(po_line_id=lid, item_id=self.item, location_id=self.location, quantity=quantity,
                    allocations=[dict(purchase_allocation_id=a, demand_line_id=d, quantity=q) for a,d,q in allocations], **extra)

    def test_partial_reservation_ship_and_release_derive_balances(self):
        stock.open_stock(self.conn,self.item,5,"count","opening")
        r = stock.reserve(self.conn,self.item,8,"line-1","reserve")
        self.assertEqual((r["reserved"],r["shortage"]),(5,3))
        stock.ship(self.conn,r["reservation_id"],2,"ship")
        self.assertEqual(stock.stock_summary(self.conn,self.item)["on_hand"],3)
        self.assertEqual(stock.reserved(self.conn,self.item),3)
        stock.release(self.conn,r["reservation_id"],"release")
        self.assertEqual(stock.available(self.conn,self.item),3)
        self.assertEqual(self.conn.execute("SELECT count(*) n FROM logistics.stock_movements").fetchone()["n"],2)
        with self.assertRaises(Conflict):
            stock.ship(self.conn,r["reservation_id"],1,"late-ship")

    def test_keys_are_shared_and_changed_content_rejected(self):
        result = stock.receive(self.conn,self.item,4,"source","same")
        self.assertEqual(stock.receive(self.conn,self.item,4,"source","same"),result)
        with self.assertRaises(IdempotencyConflict):
            stock.receive(self.conn,self.item,5,"source","same")
        with self.assertRaises(IdempotencyConflict):
            stock.reserve(self.conn,self.item,1,"order","same")
        self.assertEqual(stock.on_hand(self.conn,self.item),4)

    def test_failed_receive_reserve_rolls_back_every_row_and_frees_key(self):
        stock.track_lots(self.conn,self.item,"tracking")
        with self.assertRaises(LogisticsError):
            stock.receive_and_reserve(self.conn,self.item,4,"PO","order","receive-fail",
                lot_number="expired",expiry_date="2026-01-01",as_of="2026-10-27")
        self.assertEqual(stock.on_hand(self.conn,self.item),0)
        self.assertEqual(self.conn.execute("SELECT count(*) n FROM logistics.receipts").fetchone()["n"],0)
        self.assertIsNone(self.conn.execute("SELECT 1 FROM logistics.command_requests WHERE idempotency_key='receive-fail'").fetchone())
        stock.receive_and_reserve(self.conn,self.item,4,"PO","order","receive-fail",
                lot_number="valid",expiry_date=None,as_of="2026-10-27")
        self.assertEqual(stock.reserved(self.conn,self.item),4)

    def test_lots_use_fefo_skip_held_expired_and_recall_shipped_portions(self):
        stock.track_lots(self.conn,self.item,"tracking")
        expired=stock.receive(self.conn,self.item,5,"PO","expired",lot_number="expired",expiry_date="2026-01-01")
        early=stock.receive(self.conn,self.item,3,"PO","early",lot_number="early",expiry_date="2026-11-01")
        held=stock.receive(self.conn,self.item,4,"PO","held",lot_number="held",expiry_date="2026-11-02")
        late=stock.receive(self.conn,self.item,5,"PO","late",lot_number="no-expiry")
        stock.hold_lot(self.conn,held["lot_id"],"recall","hold")
        r=stock.reserve(self.conn,self.item,7,"order","reserve",as_of="2026-10-27")
        self.assertEqual(r["allocations"],[{"lot_id":early["lot_id"],"quantity":3},{"lot_id":late["lot_id"],"quantity":4}])
        stock.ship(self.conn,r["reservation_id"],4,"ship",as_of="2026-10-27")
        report=stock.recall(self.conn,early["lot_id"])
        self.assertEqual(sum(x["quantity"] for x in report["shipments"]),3)
        self.assertEqual(report["on_hand"],0)
        stock.release_hold(self.conn,held["lot_id"],"checked","unhold")
        self.assertFalse(stock.recall(self.conn,held["lot_id"])["on_hold"])
        self.assertEqual(stock.recall(self.conn,expired["lot_id"])["on_hand"],5)

    def test_lot_tracking_and_expiry_cannot_change_after_receipt(self):
        stock.receive(self.conn,self.item,1,"PO","receive")
        with self.assertRaises(LogisticsError):
            stock.track_lots(self.conn,self.item,"tracking")
        tracked=self.bind("item","tracked")
        stock.track_lots(self.conn,tracked,"tracking2")
        stock.receive(self.conn,tracked,1,"PO","r2",lot_number="batch",expiry_date="2026-11-01")
        with self.assertRaises(ValidationError):
            stock.receive(self.conn,tracked,1,"PO","r3",lot_number="batch",expiry_date="2026-11-02")

    def test_raw_sql_cannot_edit_ledger_or_overdraw_reservation(self):
        stock.receive(self.conn,self.item,2,"PO","receive")
        r=stock.reserve(self.conn,self.item,2,"order","reserve")
        with self.assertRaises(psycopg.errors.CheckViolation):
            self.conn.execute("UPDATE logistics.stock_movements SET quantity=100")
        with self.assertRaises(psycopg.errors.CheckViolation):
            self.conn.execute("DELETE FROM logistics.stock_movements")
        def bad(c):
            c.execute("""INSERT INTO logistics.stock_movements(item_id,location_id,quantity,movement_type,reservation_id,source_ref,request_key)
                       VALUES(%s,%s,-3,'shipment',%s,'order','raw-overdraw')""",(self.item,self.location,r["reservation_id"]))
            return {}
        with self.assertRaises(ValidationError):
            run_command(self.conn,"test.raw","raw-overdraw",{},bad)
        self.assertEqual(stock.on_hand(self.conn,self.item),2)
        self.assertEqual(stock.reserved(self.conn,self.item),2)

    def test_raw_sql_reservation_quantity_must_match_shipment_ledger(self):
        stock.receive(self.conn,self.item,2,"PO","receive")
        r=stock.reserve(self.conn,self.item,2,"order","reserve")
        with self.assertRaises(psycopg.errors.CheckViolation):
            self.conn.execute("UPDATE logistics.stock_reservations SET quantity_shipped=1 WHERE id=%s",(r["reservation_id"],))
        self.assertEqual(stock.reserved(self.conn,self.item),2)

    def test_two_orders_cannot_both_reserve_last_unit(self):
        stock.receive(self.conn,self.item,1,"PO","receive")
        def claim(n):
            with self.newconn() as conn:
                return stock.reserve(conn,self.item,1,"order-"+str(n),"reserve-"+str(n))["reserved"]
        with ThreadPoolExecutor(max_workers=2) as pool:
            results=list(pool.map(claim,[1,2]))
        self.assertEqual(sorted(results),[0,1])
        self.assertEqual(stock.reserved(self.conn,self.item),1)

    def test_duplicate_requests_racing_receive_once(self):
        def receive(_):
            with self.newconn() as conn:
                return stock.receive(conn,self.item,3,"PO","same-request")
        with ThreadPoolExecutor(max_workers=2) as pool:
            results=list(pool.map(receive,[1,2]))
        self.assertEqual(results[0],results[1])
        self.assertEqual(stock.on_hand(self.conn,self.item),3)

    def test_delivery_multiple_customers_explicit_allocation_links_and_retry(self):
        d1=self._demand(2,"order-A"); d2=self._demand(3,"order-B")
        pid,lid,allocs=self._po(5,[(d1["id"],2),(d2["id"],3)])
        self.assertEqual(stock.on_hand(self.conn,self.item),0)
        lines=[self._line(lid,5,allocs)]
        first=stock.receive_delivery(self.conn,"delivery-1",lines,"receive","2026-10-27")
        self.assertEqual(stock.receive_delivery(self.conn,"delivery-1",lines,"receive","2026-10-27"),first)
        self.assertEqual(len(first["lines"][0]["reservations"]),2)
        self.assertEqual(stock.on_hand(self.conn,self.item),5)
        self.assertEqual(stock.reserved(self.conn,self.item),5)
        progress=purchasing.purchase_progress(self.conn,purchase_order_id=pid)
        self.assertEqual(progress["lines"][0]["outstanding_quantity"],0)
        self.assertEqual([a["received_quantity"] for a in progress["lines"][0]["allocations"]],[2,3])
        with self.assertRaises(LogisticsError):
            stock.receive_delivery(self.conn,"delivery-1",lines,"new-key","2026-10-27")

    def test_delivery_bad_later_line_rolls_back_whole_delivery(self):
        demand=self._demand(5)
        _,lid,allocs=self._po(5,[(demand["id"],5)])
        lines=[self._line(lid,2,[(allocs[0][0],demand["id"],2)]),
               self._line(lid,3,[(allocs[0][0],demand["id"],3)],lot_number="unexpected-lot")]
        with self.assertRaises(ValidationError):
            stock.receive_delivery(self.conn,"bad-delivery",lines,"delivery","2026-10-27")
        self.assertEqual(stock.on_hand(self.conn,self.item),0)
        for table in ("receipts","receipt_lines","receipt_allocations","stock_reservations","stock_movements"):
            self.assertEqual(self.conn.execute(f"SELECT count(*) n FROM logistics.{table}").fetchone()["n"],0)

    def test_partial_receipts_and_concurrent_purchase_limit(self):
        demand=self._demand(5)
        pid,lid,allocs=self._po(5,[(demand["id"],5)])
        aid=allocs[0][0]
        def receipt(n):
            try:
                with self.newconn() as conn:
                    return stock.receive_delivery(conn,"delivery-"+str(n),[self._line(lid,4,[(aid,demand["id"],4)])],"receive-"+str(n),"2026-10-27")
            except LogisticsError:
                return None
        with ThreadPoolExecutor(max_workers=2) as pool:
            results=list(pool.map(receipt,[1,2]))
        self.assertEqual(sum(x is not None for x in results),1)
        self.assertEqual(stock.on_hand(self.conn,self.item),4)
        stock.receive_delivery(self.conn,"remainder",[self._line(lid,1,[(aid,demand["id"],1)])],"remainder","2026-10-27")
        self.assertEqual(purchasing.purchase_progress(self.conn,purchase_order_id=pid)["lines"][0]["outstanding_quantity"],0)

    def test_shipment_five_states_delivery_terminal_and_no_stock_change_from_events(self):
        stock.receive(self.conn,self.item,4,"PO","receive")
        r=stock.reserve(self.conn,self.item,4,"order","reserve")
        s=stock.create_shipment(self.conn,"S1","shipment")
        stock.add_shipment_line(self.conn,s["shipment_id"],r["reservation_id"],4,"line")
        self.assertEqual(stock.shipment(self.conn,s["shipment_id"])["status"],"draft")
        stock.dispatch(self.conn,s["shipment_id"],"Carrier","dispatch","2026-10-27",tracking_ref="tracking")
        self.assertEqual(stock.shipment(self.conn,s["shipment_id"])["status"],"dispatched")
        self.assertEqual(stock.on_hand(self.conn,self.item),0)
        for event,stamp,state in [("in_transit","2026-10-27T10:00:00Z","in_transit"),("delivery_failed","2026-10-27T11:00:00Z","exception"),("delivered","2026-10-27T12:00:00Z","delivered"),("exception","2026-10-27T13:00:00Z","delivered")]:
            stock.record_tracking_event(self.conn,s["shipment_id"],event,stamp,"event-"+stamp)
            self.assertEqual(stock.shipment(self.conn,s["shipment_id"])["status"],state)
            self.assertEqual(stock.on_hand(self.conn,self.item),0)
        with self.assertRaises(Conflict):
            stock.dispatch(self.conn,s["shipment_id"],"Carrier","dispatch-again","2026-10-27",tracking_ref="tracking")

    def test_shipment_rejects_mixed_customer_orders(self):
        d1=self._demand(1,"order-A"); d2=self._demand(1,"order-B")
        stock.receive(self.conn,self.item,2,"PO","receive")
        a=stock.reserve(self.conn,self.item,1,d1["external_key"],"reserve-A")
        b=stock.reserve(self.conn,self.item,1,d2["external_key"],"reserve-B")
        s=stock.create_shipment(self.conn,"S1","shipment")
        stock.add_shipment_line(self.conn,s["shipment_id"],a["reservation_id"],1,"line-A")
        with self.assertRaises(ValidationError):
            stock.add_shipment_line(self.conn,s["shipment_id"],b["reservation_id"],1,"line-B")

    def test_demand_cannot_be_reserved_twice(self):
        d=self._demand(1)
        stock.receive(self.conn,self.item,3,"PO","receive")
        stock.reserve(self.conn,self.item,1,d["external_key"],"first")
        with self.assertRaises(ValidationError):
            stock.reserve(self.conn,self.item,1,d["external_key"],"second")
        self.assertEqual(stock.reserved(self.conn,self.item),1)

    def test_fifteen_chairs_split_sources_partial_receipts_two_shipments(self):
        d=self._demand(15)
        stock.open_stock(self.conn,self.item,4,"count","opening")
        opening=stock.reserve(self.conn,self.item,15,d["external_key"],"reserve-opening")
        pa,la,aa=self._po(6,[(d["id"],6)])
        pb,lb,ab=self._po(5,[(d["id"],5)])
        snapshots=[]
        def check(expected):
            received=self.conn.execute("SELECT COALESCE(sum(quantity),0)::bigint n FROM logistics.receipt_lines WHERE po_line_id IN (%s,%s)",(la,lb)).fetchone()["n"]
            shipped=self.conn.execute("SELECT COALESCE(-sum(quantity),0)::bigint n FROM logistics.stock_movements WHERE movement_type='shipment'").fetchone()["n"]
            actual=(stock.on_hand(self.conn,self.item),stock.reserved(self.conn,self.item),11-received,shipped)
            snapshots.append(actual)
            self.assertEqual(actual,expected)
        def receive(lid,aid,amount,key):
            response=stock.receive_delivery(self.conn,key,[self._line(lid,amount,[(aid,d["id"],amount)])],key,"2026-10-27")
            return response["lines"][0]["reservations"][0]["reservation_id"]
        check((4,4,11,0))
        a1=receive(la,aa[0][0],3,"A-first"); check((7,7,8,0))
        a2=receive(la,aa[0][0],3,"A-second"); check((10,10,5,0))
        one=stock.create_shipment(self.conn,"S1","shipment-1")["shipment_id"]
        stock.add_shipment_line(self.conn,one,opening["reservation_id"],4,"s1-opening")
        stock.add_shipment_line(self.conn,one,a1,2,"s1-a1")
        stock.dispatch(self.conn,one,"Carrier","dispatch-1","2026-10-27",tracking_ref="T1"); check((4,4,5,6))
        b1=receive(lb,ab[0][0],2,"B-first"); check((6,6,3,6))
        b2=receive(lb,ab[0][0],3,"B-second"); check((9,9,0,6))
        two=stock.create_shipment(self.conn,"S2","shipment-2")["shipment_id"]
        for rid,amount in [(a1,1),(a2,3),(b1,2),(b2,3)]:
            stock.add_shipment_line(self.conn,two,rid,amount,"s2-"+str(rid))
        stock.dispatch(self.conn,two,"Carrier","dispatch-2","2026-10-27",tracking_ref="T2"); check((0,0,0,15))
        status=lambda:self.conn.execute("SELECT status FROM logistics.demand_fulfillment WHERE demand_line_id=%s",(d["id"],)).fetchone()["status"]
        self.assertEqual(status(),"shipped")
        stock.record_tracking_event(self.conn,one,"delivered","2026-10-27T12:00:00Z","delivered-1")
        self.assertEqual(status(),"shipped")
        stock.record_tracking_event(self.conn,two,"delivered","2026-10-27T13:00:00Z","delivered-2")
        self.assertEqual(status(),"delivered")
        self.assertEqual(len(snapshots),7)

    def test_shipping_passes_held_lot_when_other_allocated_stock_is_usable(self):
        stock.track_lots(self.conn,self.item,"tracking")
        early=stock.receive(self.conn,self.item,3,"PO","early",lot_number="early",expiry_date="2026-11-01")
        late=stock.receive(self.conn,self.item,3,"PO","late",lot_number="late",expiry_date="2026-11-02")
        r=stock.reserve(self.conn,self.item,6,"order","reserve",as_of="2026-10-27")
        stock.hold_lot(self.conn,early["lot_id"],"recall","hold")
        sent=stock.ship(self.conn,r["reservation_id"],2,"ship",as_of="2026-10-27")
        self.assertEqual(sent["lots"][0]["lot_id"],late["lot_id"])
        self.assertEqual(stock.recall(self.conn,early["lot_id"])["on_hand"],3)
        self.assertEqual(stock.recall(self.conn,late["lot_id"])["on_hand"],1)
        with self.assertRaises(LogisticsError):
            stock.ship(self.conn,r["reservation_id"],2,"blocked",as_of="2026-10-27")
        self.assertEqual(stock.on_hand(self.conn,self.item),4)

    def test_draft_shipments_cannot_plan_same_reserved_units_twice(self):
        stock.receive(self.conn,self.item,3,"PO","receive")
        r=stock.reserve(self.conn,self.item,3,"order","reserve")
        a=stock.create_shipment(self.conn,"S1","s1")["shipment_id"]
        b=stock.create_shipment(self.conn,"S2","s2")["shipment_id"]
        stock.add_shipment_line(self.conn,a,r["reservation_id"],2,"line1")
        with self.assertRaises(LogisticsError):
            stock.add_shipment_line(self.conn,b,r["reservation_id"],2,"line2")
        stock.add_shipment_line(self.conn,b,r["reservation_id"],1,"line3")

    def test_raw_sql_competing_reservations_share_resource_lock(self):
        stock.receive(self.conn,self.item,1,"PO","receive")
        def claim(n):
            try:
                with self.newconn() as conn:
                    def write(c):
                        c.execute("INSERT INTO logistics.stock_reservations(item_id,location_id,order_ref,quantity,request_key) VALUES(%s,%s,%s,1,%s)",(self.item,self.location,"order-"+str(n),"raw-"+str(n)))
                        return {}
                    run_command(conn,"raw.reserve","raw-"+str(n),{},write)
                return True
            except ValidationError:
                return False
        with ThreadPoolExecutor(max_workers=2) as pool:
            results=list(pool.map(claim,[1,2]))
        self.assertEqual(sum(results),1)
        self.assertEqual(stock.reserved(self.conn,self.item),1)

    def test_stock_isolated_by_location(self):
        other=self.bind("location","other")
        stock.receive(self.conn,self.item,3,"PO","receive",location_id=self.location)
        result=stock.reserve(self.conn,self.item,2,"order","reserve",location_id=other)
        self.assertEqual(result["reserved"],0)
        self.assertEqual(stock.on_hand(self.conn,self.item,self.location),3)
        self.assertEqual(stock.on_hand(self.conn,self.item,other),0)
        with self.assertRaises(ValidationError):
            stock.receive(self.conn,self.item,1,"PO","ambiguous-location")


if __name__ == "__main__":
    unittest.main()
