"""Complete HTTP proof against disposable local PostgreSQL databases.

Run from the fork root:
  OPERATIONS_TEST_DATABASE_URL=postgresql://... .venv/bin/python -m unittest \
      tests_integration.test_umbrella_workflow -v

The explicitly supplied URL must be local. Tests create a random control DB,
exercise real HTTP handlers and real PostgreSQL transactions, and delete only
their newly created databases. Models are stubbed; no carrier or supplier calls
are made. Direct SQL is read-only and used to verify invariants, not seed data.
"""

import json
import os
import sys
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch
from uuid import uuid4

from tests_integration.local_postgres import LocalDatabaseFixture


ROOT = Path(__file__).resolve().parents[1]
APP_ROOT = ROOT / "operations_nerd"
ADMIN_URL = os.environ.get("OPERATIONS_TEST_DATABASE_URL")


@unittest.skipUnless(ADMIN_URL, "Set OPERATIONS_TEST_DATABASE_URL to a local disposable PostgreSQL fixture")
class UmbrellaHTTPTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.fixture = LocalDatabaseFixture(ADMIN_URL)
        cls.environment = patch.dict(os.environ, {
            "OPERATIONS_DATABASE_URL": cls.fixture.url,
            "OPERATIONS_ADMIN_DATABASE_URL": ADMIN_URL,
            "OPERATIONS_UMBRELLA_ENABLED": "1",
            "APP_ENV": "production",
            "SESSION_SECRET": "local-http-acceptance-fixture-secret-000000000000",
            "LLM_PROVIDER": "stub",
            "SIGNUP_INVITE_CODE": "test",
            "RATE_LIMIT_ENABLED": "0",
        })
        cls.environment.start()
        cls.old_cwd = os.getcwd()
        os.chdir(APP_ROOT)
        sys.path.insert(0, str(APP_ROOT))
        try:
            from main import app
            cls.app = app
        except Exception:
            os.chdir(cls.old_cwd)
            cls.environment.stop()
            cls.fixture.close()
            raise

    @classmethod
    def tearDownClass(cls):
        try:
            cls.fixture.close()
        finally:
            os.chdir(cls.old_cwd)
            cls.environment.stop()

    def response(self, response, expected=200):
        self.assertEqual(response.status_code, expected,
                         f"{response.request.method} {response.request.url.path}: {response.status_code} {response.text[:1500]}")
        return response.json()

    def signup(self):
        from fastapi.testclient import TestClient
        client = TestClient(self.app, base_url="https://testserver")
        email = uuid4().hex + "@example.com"
        body = {"email": email, "password": "integration fixture password", "invite_code": "test"}
        self.response(client.post("/api/auth/signup", json=body), 201)
        self.response(client.post("/api/auth/logout"))
        self.response(client.post("/api/auth/login", json={"email": email, "password": body["password"]}))
        return client

    def setUp(self):
        self.owner = self.signup()
        self.stranger = self.signup()
        self.toy = self.response(self.owner.post("/api/businesses", json={"name": "Playful Planet Toys", "template": "toy_store"}), 201)
        self.gym = self.response(self.owner.post("/api/businesses", json={"name": "Neighborhood Gym", "template": "health_club"}), 201)
        self.bid = self.toy["id"]
        self.base = f"/api/businesses/{self.bid}"
        self.entities = {entity["label"]: entity for entity in self.toy["spec"]["entities"]}
        self.fields = {label: {field["label"]: field["key"] for field in entity["fields"]}
                       for label, entity in self.entities.items()}
        self.links = {(link["from"], link["label"]): link["key"] for link in self.toy["spec"]["links"]}
        self.today = datetime.now(timezone.utc).date()
        self.deadline = (self.today + timedelta(days=10)).isoformat()
        self.supplier = self.record("Supplier", {"Name": "Toy Supplier", "Email": "supplier@example.com"})
        self.customer = self.record("Customer", {"Name": "Toy Customer", "Shipping address": "10 Customer Lane"})
        self.item = self.record("Item", {"Name": "Toy Robot", "SKU": "ROBOT-001", "Unit": "each"})
        self.location = self.record("Location", {"Name": "Toy Warehouse", "Address": "20 Warehouse Street"})
        self.order = self.record("Customer order", {"Order number": "ORDER-001", "Currency": "USD", "Order date": self.today.isoformat()},
                                 {"Customer": self.customer["id"]})
        self.order_line = self.record("Order line", {"Quantity": 10, "Unit price (minor units)": 2000,
            "Required by": self.deadline, "Budget (minor units)": 10000}, {"Order": self.order["id"], "Item": self.item["id"]})
        member = next(entity for entity in self.gym["spec"]["entities"] if entity["label"] == "Member")
        full_name = next(field["key"] for field in member["fields"] if field["label"] == "Full name")
        self.response(self.owner.post(f"/api/businesses/{self.gym['id']}/records/{member['key']}",
                                      json={"values": {full_name: "Gym member stays unchanged"}}), 201)
        self.bindings = self.role_bindings()

    def tearDown(self):
        self.owner.close()
        self.stranger.close()

    def record(self, label, fields, links=None):
        entity = self.entities[label]
        values = {self.fields[label][name]: value for name, value in fields.items()}
        values.update({self.links[(entity["key"], name)]: value for name, value in (links or {}).items()})
        return self.response(self.owner.post(f"{self.base}/records/{entity['key']}", json={"values": values}), 201)

    def role_bindings(self):
        choices = {
            "supplier": ("Supplier", {"name": "Name"}, {}),
            "customer": ("Customer", {"name": "Name", "address": "Shipping address"}, {}),
            "item": ("Item", {"name": "Name", "sku": "SKU", "unit": "Unit"}, {}),
            "location": ("Location", {"name": "Name", "address": "Address"}, {}),
            "order": ("Customer order", {"number": "Order number", "currency": "Currency", "date": "Order date"}, {"customer": "Customer"}),
            "order_line": ("Order line", {"quantity": "Quantity", "unit_price_minor": "Unit price (minor units)",
                "required_by": "Required by", "budget_minor": "Budget (minor units)"}, {"order": "Order", "item": "Item"}),
        }
        return {role: {"entity": self.entities[label]["key"],
                       "fields": {semantic: self.fields[label][field] for semantic, field in fields.items()},
                       "links": {semantic: self.links[(self.entities[label]["key"], link)] for semantic, link in links.items()}}
                for role, (label, fields, links) in choices.items()}

    def enable(self):
        proposed = self.response(self.owner.post(self.base + "/nerds/logistics/proposals",
            json={"base_version": self.toy["current_version"], "bindings": self.bindings}), 201)
        approved = self.response(self.owner.post(self.base + f"/nerds/logistics/proposals/{proposed['id']}/approve", json={}))
        self.assertEqual(approved["status"], "enabled")
        replay = self.response(self.owner.post(self.base + f"/nerds/logistics/proposals/{proposed['id']}/approve", json={}))
        self.assertEqual(replay["status"], "enabled")

    def workspace(self):
        return self.response(self.owner.get(self.base + "/logistics/workspace"))

    def command(self, name, payload, key=None):
        body = {**payload, "idempotency_key": key or uuid4().hex}
        return self.response(self.owner.post(self.base + "/logistics/commands/" + name, json=body))

    def reference(self, workspace, kind, name):
        matches = [row for row in workspace["references"] if row["kind"] == kind and not row.get("valid_to")
                   and row["snapshot"].get("name") == name]
        self.assertEqual(len(matches), 1, f"Expected one {kind} reference named {name}")
        return matches[0]

    def chat_operations(self, operations):
        from agentic import chat
        from agentic.llm import LLMResult
        with patch.object(chat, "run_llm", return_value=LLMResult(
                json.dumps({"reply": "Review these fixture changes", "operations": operations}), "stub", 1, 1, 1)):
            return self.response(self.owner.post(self.base + "/chat", json={"messages": [
                {"role": "user", "content": "Apply the fixture's schema change"}]}))

    def test_complete_purchase_delivery_shipping_and_isolation(self):
        gym_before = self.fixture.core_snapshot(self.gym["id"])
        self.assertNotEqual(self.fixture.business_url(self.bid), self.fixture.business_url(self.gym["id"]))
        self.enable()
        self.assertIsNone(self.fixture.scalar(self.gym["id"], "SELECT to_regnamespace('logistics')"))
        # Authenticated ownership is checked before database selection.
        for suffix in ("", "/nerds", "/logistics/workspace"):
            self.response(self.stranger.get(self.base + suffix), 404)
        self.response(self.stranger.post(self.base + "/logistics/commands/sync_records",
                                        json={"idempotency_key": "stranger"}), 404)
        self.response(self.owner.get(f"/api/businesses/0{self.bid}/logistics/workspace"), 400)

        submitted = self.response(self.owner.post(self.base + f"/logistics/orders/{self.order['id']}/submit",
                                                   json={"idempotency_key": "submit-order"}))
        self.assertEqual(submitted, self.response(self.owner.post(self.base + f"/logistics/orders/{self.order['id']}/submit",
                                                                   json={"idempotency_key": "submit-order"})))
        self.response(self.owner.patch(self.base + f"/records/{self.entities['Customer order']['key']}/{self.order['id']}",
            json={"values": {self.fields["Customer order"]["Currency"]: "EUR"}}), 409)
        self.response(self.owner.patch(self.base + f"/records/{self.entities['Order line']['key']}/{self.order_line['id']}",
            json={"values": {self.fields["Order line"]["Quantity"]: 5}}), 409)
        workspace = self.workspace()
        self.assertEqual(len(workspace["demands"]), 1)
        demand = workspace["demands"][0]
        supplier = self.reference(workspace, "supplier", "Toy Supplier")["id"]
        item = self.reference(workspace, "item", "Toy Robot")["id"]
        location = self.reference(workspace, "location", "Toy Warehouse")["id"]
        self.assertEqual(demand["quantity"], 10)
        self.assertEqual(demand["budget_minor"], 10000)
        quote_payload = {"supplier_id": supplier, "item_id": item, "quantity_priced": 10, "capacity_quantity": 10,
            "price_minor": 1000, "pricing_kind": "unit", "currency": "USD",
            "expires_at": (datetime.now(timezone.utc) + timedelta(days=7)).isoformat(),
            "expected_date": (self.today + timedelta(days=2)).isoformat(), "quote_ref": "QUOTE-001"}
        quote = self.command("create_quote", quote_payload, "quote")
        self.assertEqual(quote, self.command("create_quote", quote_payload, "quote"))
        self.response(self.owner.post(self.base + "/logistics/commands/create_quote",
            json={**quote_payload, "price_minor": 1001, "idempotency_key": "quote"}), 409)
        self.response(self.owner.post(self.base + "/logistics/commands/create_purchase_order",
            json={"supplier_id": supplier, "currency": "USD", "idempotency_key": "quote"}), 409)
        po = self.command("create_purchase_order", {"supplier_id": supplier, "currency": "USD"})
        line = self.command("add_purchase_line", {"purchase_order_id": po["id"], "quote_version_id": quote["id"], "quantity": 10})
        allocation = self.command("allocate_purchase_line", {"po_line_id": line["id"], "demand_line_id": demand["id"],
            "quantity": 10, "cost_share_minor": 10000})
        self.response(self.owner.post(self.base + "/logistics/commands/approve_purchase_order",
            json={"purchase_order_id": po["id"], "actor_id": 999999, "idempotency_key": "untrusted-actor"}), 422)
        approved = self.command("approve_purchase_order", {"purchase_order_id": po["id"]}, "approve")
        self.assertEqual(approved["status"], "approved")
        self.assertEqual(approved["total_minor"], 10000)
        self.assertEqual(approved, self.command("approve_purchase_order", {"purchase_order_id": po["id"]}, "approve"))
        self.assertEqual(self.fixture.scalar(self.bid, "SELECT count(*) FROM logistics.stock_movements"), 0)

        # A core supplier edit updates display data while frozen terms retain history.
        self.response(self.owner.patch(self.base + f"/records/{self.entities['Supplier']['key']}/{self.supplier['id']}",
            json={"values": {self.fields["Supplier"]["Name"]: "Renamed Toy Supplier"}}))
        self.command("sync_records", {}, "sync-renamed-supplier")
        self.reference(self.workspace(), "supplier", "Renamed Toy Supplier")
        self.assertEqual(approved["approved_supplier_snapshot"]["name"], "Toy Supplier")
        self.assertEqual(self.fixture.scalar(self.bid,
            "SELECT approved_supplier_snapshot->>'name' FROM logistics.purchase_orders WHERE id=%s", (po["id"],)),
            "Toy Supplier")

        receipt_line = {"po_line_id": line["id"], "item_id": item, "location_id": location, "quantity": 10,
            "allocations": [{"purchase_allocation_id": allocation["id"], "demand_line_id": demand["id"], "quantity": 10}]}
        bad_lines = [{**receipt_line, "quantity": 5, "allocations": [{**receipt_line["allocations"][0], "quantity": 5}]},
                     {**receipt_line, "quantity": 5, "lot_number": "invalid-untracked-lot",
                      "allocations": [{**receipt_line["allocations"][0], "quantity": 5}]}]
        bad = self.owner.post(self.base + "/logistics/commands/receive_delivery", json={"delivery_ref": "DELIVERY-001",
            "received_on": self.today.isoformat(), "lines": bad_lines, "idempotency_key": "receive"})
        self.assertIn(bad.status_code, (409, 422), bad.text)
        for table in ("receipts", "receipt_lines", "stock_movements", "stock_reservations"):
            self.assertEqual(self.fixture.scalar(self.bid, f"SELECT count(*) FROM logistics.{table}"), 0)
        receipt_payload = {"delivery_ref": "DELIVERY-001", "received_on": self.today.isoformat(), "lines": [receipt_line]}
        received = self.command("receive_delivery", receipt_payload, "receive")
        self.assertEqual(received, self.command("receive_delivery", receipt_payload, "receive"))
        self.assertEqual(self.fixture.scalar(self.bid, "SELECT count(*) FROM logistics.receipts"), 1)
        self.assertEqual(self.fixture.scalar(self.bid, "SELECT sum(on_hand) FROM logistics.stock_levels"), 10)
        self.assertEqual(self.fixture.scalar(self.bid, "SELECT sum(reserved) FROM logistics.stock_levels"), 10)
        reservation = received["lines"][0]["reservations"][0]["reservation_id"]
        shipment = self.command("create_shipment", {"shipment_number": "SHIP-001", "order_ref": demand["order_ref"]})
        sid = shipment["shipment_id"]
        self.command("add_shipment_line", {"shipment_id": sid, "reservation_id": reservation, "quantity": 10})
        dispatch = {"shipment_id": sid, "carrier": "Fixture Carrier", "tracking_ref": "TRACK-001", "as_of": self.today.isoformat()}
        dispatched = self.command("dispatch", dispatch, "dispatch")
        self.assertEqual(dispatched, self.command("dispatch", dispatch, "dispatch"))
        self.assertEqual(self.fixture.scalar(self.bid, "SELECT count(*) FROM logistics.stock_movements"), 2)
        self.assertEqual(self.fixture.scalar(self.bid, "SELECT sum(on_hand) FROM logistics.stock_levels"), 0)
        for index, (event, expected) in enumerate((("in_transit", "in_transit"), ("delivery_failed", "exception"),
                                                     ("delivered", "delivered"), ("exception", "delivered"))):
            payload = {"shipment_id": sid, "event": event,
                "occurred_at": (datetime.now(timezone.utc) + timedelta(minutes=index)).isoformat()}
            result = self.command("record_tracking_event", payload, "tracking-" + str(index))
            self.assertEqual(result["status"], expected)
            self.assertEqual(result, self.command("record_tracking_event", payload, "tracking-" + str(index)))
            self.assertEqual(self.fixture.scalar(self.bid, "SELECT count(*) FROM logistics.stock_movements"), 2)
        fulfillment = self.workspace()["fulfillment"]
        self.assertEqual(fulfillment["shipments"][0]["status"], "delivered")
        self.assertEqual(fulfillment["orders"][0]["customer_name"], "Toy Customer")
        self.assertEqual(fulfillment["orders"][0]["lines"][0]["delivered"], 10)
        self.assertEqual(self.fixture.core_snapshot(self.gym["id"]), gym_before)

    def test_permanent_bindings_schema_protection_and_later_item(self):
        self.enable()
        original_item_key = self.bindings["item"]["entity"]
        sku_key = self.bindings["item"]["fields"]["sku"]
        rename = self.chat_operations([
            {"op": "update_entity_type", "entity": original_item_key, "label": "Products"},
            {"op": "update_field", "entity": original_item_key, "field": sku_key, "label": "Catalog code"}])
        self.response(self.owner.post(f"/api/proposals/{rename['proposal_id']}/approve",
                                     headers={"X-Business-Id": str(self.bid)}))
        renamed = self.response(self.owner.get(self.base))
        self.assertEqual(next(entity for entity in renamed["spec"]["entities"] if entity["key"] == original_item_key)["label"], "Products")
        self.command("sync_records", {}, "sync-after-label-change")
        self.reference(self.workspace(), "item", "Toy Robot")
        protected = self.chat_operations([{"op": "archive_field", "entity": original_item_key, "field": sku_key}])
        if protected["status"] == "pending":
            self.response(self.owner.post(f"/api/proposals/{protected['proposal_id']}/approve",
                headers={"X-Business-Id": str(self.bid)}), 409)
        else:
            self.assertTrue(protected["rejected_operations"], "An incompatible change must explain its rejection")
        current = self.response(self.owner.get(self.base))
        entity = next(entity for entity in current["spec"]["entities"] if entity["key"] == original_item_key)
        self.assertFalse(next(field for field in entity["fields"] if field["key"] == sku_key)["archived"])
        self.assertEqual(current["current_version"], renamed["current_version"])
        self.record("Item", {"Name": "Toy Train", "SKU": "TRAIN-002", "Unit": "each"})
        self.command("sync_records", {}, "sync-later-item")
        self.reference(self.workspace(), "item", "Toy Train")
        nerd = next(row for row in self.response(self.owner.get(self.base + "/nerds"))["nerds"] if row["id"] == "logistics")
        self.assertEqual(nerd["status"], "enabled")

    def test_stale_enablement_and_unqualified_proposal_routes_fail_safely(self):
        proposal = self.response(self.owner.post(self.base + "/nerds/logistics/proposals",
            json={"base_version": self.toy["current_version"], "bindings": self.bindings}), 201)
        rename = self.chat_operations([{"op": "update_entity_type", "entity": self.bindings["item"]["entity"], "label": "Products"}])
        self.response(self.owner.post(f"/api/proposals/{rename['proposal_id']}/approve"), 400)
        self.response(self.stranger.post(f"/api/proposals/{rename['proposal_id']}/approve",
            headers={"X-Business-Id": str(self.bid)}), 404)
        self.response(self.owner.post(f"/api/proposals/{rename['proposal_id']}/approve",
            headers={"X-Business-Id": str(self.bid)}))
        self.response(self.owner.post(self.base + f"/nerds/logistics/proposals/{proposal['id']}/approve", json={}), 409)
        self.assertIsNone(self.fixture.scalar(self.bid, "SELECT to_regnamespace('logistics')"))
        current = self.response(self.owner.get(self.base))
        self.toy["current_version"] = current["current_version"]
        self.enable()

    def test_failed_installation_retains_data_and_retries_same_proposal(self):
        from umbrella import logistics_adapter
        gym_before = self.fixture.core_snapshot(self.gym["id"])
        proposed = self.response(self.owner.post(self.base + "/nerds/logistics/proposals",
            json={"base_version": self.toy["current_version"], "bindings": self.bindings}), 201)
        approve_path = self.base + f"/nerds/logistics/proposals/{proposed['id']}/approve"
        with patch.object(logistics_adapter, "sync_records", side_effect=RuntimeError("injected interrupted setup")):
            self.response(self.owner.post(approve_path, json={}), 503)
        self.assertEqual(self.fixture.scalar(self.bid,
            "SELECT status FROM core.nerd_installations WHERE nerd_id='logistics'"), "failed")
        self.assertEqual(self.fixture.scalar(self.bid, "SELECT count(*) FROM logistics.schema_migrations"), 3)
        self.response(self.owner.get(self.base + "/logistics/workspace"), 409)
        businesses = self.response(self.owner.get("/api/businesses"))
        self.assertEqual({business["id"] for business in businesses}, {self.bid, self.gym["id"]})
        result = self.response(self.owner.post(approve_path, json={}))
        self.assertEqual(result["status"], "enabled")
        self.assertEqual(self.fixture.scalar(self.bid,
            "SELECT status FROM core.nerd_proposals WHERE id=%s", (proposed["id"],)), "approved")
        self.assertEqual(self.fixture.scalar(self.bid, "SELECT count(*) FROM logistics.schema_migrations"), 3)
        self.reference(self.workspace(), "item", "Toy Robot")
        self.assertEqual(self.fixture.core_snapshot(self.gym["id"]), gym_before)


if __name__ == "__main__":
    unittest.main()
