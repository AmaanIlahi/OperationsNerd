"""Real PostgreSQL core tests, only against newly created disposable databases.

Set OPERATIONS_TEST_DATABASE_URL to a local PostgreSQL admin/fixture URL.
The suite creates random operations_test_* databases, never resets the supplied
database, and drops only those databases it created. Without that setting the
normal offline test runner skips this file. No external/model calls occur.
"""

import json
import os
import sqlite3
import sys
import threading
import unittest
import uuid
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from db import db as d
from db.runtime import database_scope


ADMIN_URL = os.environ.get("OPERATIONS_TEST_DATABASE_URL")


@unittest.skipUnless(ADMIN_URL, "Set OPERATIONS_TEST_DATABASE_URL for disposable PostgreSQL tests")
class PostgresFoundationTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        import psycopg
        from psycopg import sql
        from psycopg.conninfo import make_conninfo
        cls.admin = psycopg.connect(ADMIN_URL, autocommit=True)
        cls.names = []
        try:
            for _ in range(2):
                name = "operations_test_foundation_" + uuid.uuid4().hex[:12]
                cls.admin.execute(sql.SQL("CREATE DATABASE {}").format(sql.Identifier(name)))
                cls.names.append(name)
            cls.control, cls.business = [make_conninfo(ADMIN_URL, dbname=name) for name in cls.names]
            # db runtime accepts URLs, not arbitrary psycopg keyword conninfo.
            from urllib.parse import quote, urlencode, urlsplit, urlunsplit, parse_qsl
            def database_url(name):
                parts = urlsplit(ADMIN_URL)
                query = dict(parse_qsl(parts.query))
                query.pop("dbname", None)
                return urlunsplit((parts.scheme, parts.netloc, "/" + quote(name), urlencode(query), ""))
            cls.control, cls.business = map(database_url, cls.names)
            d.init_db(cls.control)
            d.init_db(cls.business)
        except Exception:
            cls._cleanup()
            raise

    @classmethod
    def _cleanup(cls):
        from psycopg import sql
        for name in cls.names:
            cls.admin.execute(sql.SQL("DROP DATABASE IF EXISTS {} WITH (FORCE)").format(sql.Identifier(name)))
        cls.admin.close()

    @classmethod
    def tearDownClass(cls):
        cls._cleanup()

    def seed_business(self):
        from agentic import store
        spec = {"business": {"name": "Race", "industry": None}, "entities": [
            {"key": "crm_person", "label": "Person", "archived": False, "fields": [
                {"key": "f_name", "label": "Name", "type": "text", "required": True, "archived": False}]},
            {"key": "crm_passport", "label": "Passport", "archived": False, "fields": [
                {"key": "f_number", "label": "Number", "type": "text", "required": True, "archived": False}]}
        ], "links": [{"key": "l_passport", "label": "Passport", "from": "crm_person", "to": "crm_passport",
                       "cardinality": "one_to_one", "archived": False}]}
        with d.get_conn(self.business) as conn:
            # Actor lives in control, not a duplicate business-side accounts row.
            return store.create_business_with_version_1(conn, 4242, "Race", None, spec)

    def test_aggregated_eav_and_identity_rollback(self):
        with d.get_conn(self.business) as conn:
            bid = d.create_business(conn, "EAV", "test", "1", {})
            rid = d.create_entity(conn, bid, "crm_roundtrip", {"f_int": 9, "f_bool": True, "f_list": ["a", "é"]})
            self.assertIsInstance(rid, int)
            row = d.list_entities(conn, bid, "crm_roundtrip")[0]
            self.assertEqual(row["attributes"], {"f_int": 9, "f_bool": True, "f_list": ["a", "é"]})
            contact = d.create_contact(conn, bid, "Contact", extension_data={"rate": 3.5})
            self.assertEqual(d.list_contacts(conn, bid)[0]["extension_data"], {"rate": 3.5})
        with self.assertRaises(RuntimeError):
            with d.get_conn(self.business) as conn:
                d.create_contact(conn, bid, "rollback sentinel")
                raise RuntimeError("abort after write")
        with d.get_conn(self.business) as conn:
            self.assertEqual(len(d.list_contacts(conn, bid)), 1)
            self.assertEqual(d.get_contact(conn, contact)["name"], "Contact")

    def test_control_session_data_is_not_routed_to_business(self):
        from agentic import store
        with patch.dict(os.environ, {"OPERATIONS_DATABASE_URL": self.control}):
            with database_scope(self.business):
                with d.control_conn() as conn:
                    account = store.create_account(conn, uuid.uuid4().hex + "@example.com", "test-hash")
                with d.get_conn() as conn:
                    self.assertEqual(conn.execute("SELECT count(*) FROM accounts").fetchone()[0], 0)
                with d.control_conn() as conn:
                    self.assertEqual(conn.execute("SELECT id FROM accounts WHERE id = ?", (account,)).fetchone()["id"], account)

    def test_duplicate_email_matches_existing_store_error(self):
        from agentic import store
        email = uuid.uuid4().hex + "@example.com"
        with d.get_conn(self.control) as conn:
            store.create_account(conn, email, "hash")
        with self.assertRaises(store.EmailTaken):
            with d.get_conn(self.control) as conn:
                store.create_account(conn, email, "hash")
        with d.get_conn(self.control) as conn:
            self.assertEqual(conn.execute("SELECT count(*) FROM accounts WHERE email = ?", (email,)).fetchone()[0], 1)

    def test_chat_approval_audit_and_usage_across_control_business(self):
        from agentic import chat, store
        from agentic.llm import LLMError, LLMResult
        from agentic.spec import empty_spec
        with patch.dict(os.environ, {"OPERATIONS_DATABASE_URL": self.control, "OPERATIONS_UMBRELLA_ENABLED": "0"}):
            with d.control_conn() as conn:
                account = store.create_account(conn, uuid.uuid4().hex + "@example.com", "test-hash")
            with d.get_conn(self.business) as conn:
                bid = store.create_business_with_version_1(conn, account, "Chat", None, empty_spec("Chat"))
            operations = [{"op": "create_entity_type", "label": "Supplier", "ref": "supplier"},
                          {"op": "add_field", "entity": "supplier", "label": "Name", "type": "text", "required": True}]
            answer = LLMResult(json.dumps({"reply": "Proposed supplier records", "operations": operations}), "stub", 1, 1, 1)
            with database_scope(self.business), patch.object(chat, "run_llm", return_value=answer):
                proposal = chat.chat(account, bid, [{"role": "user", "content": "Track suppliers"}])
                approved = chat.approve(account, proposal["proposal_id"])
                self.assertEqual(approved["version"], 2)
                with self.assertRaises(chat.ChangeError):
                    chat.approve(account, proposal["proposal_id"])
                with d.get_conn() as conn:
                    audit = store.get_version(conn, account, bid, 2)
                    self.assertEqual(audit["approved_by"], account)
                    self.assertEqual(audit["proposal_id"], proposal["proposal_id"])
                with patch.object(chat, "run_llm", side_effect=LLMError("fixture unavailable")):
                    with self.assertRaises(chat.ChangeError):
                        chat.chat(account, bid, [{"role": "user", "content": "Another change"}])
            with d.control_conn() as conn:
                self.assertEqual(store.chat_turns_today(conn, account), 1)
            with d.get_conn(self.business) as conn:
                self.assertEqual(conn.execute("SELECT count(*) FROM chat_usage").fetchone()[0], 0)

    def test_two_writers_cannot_claim_one_target(self):
        from agentic import records
        bid = self.seed_business()
        def race(target):
            barrier = threading.Barrier(2)
            def create(name):
                with database_scope(self.business):
                    barrier.wait(timeout=10)
                    try:
                        records.create_record(4242, bid, "crm_person", {"f_name": name, "l_passport": target})
                        return 201
                    except records.RecordError as exc:
                        return exc.status
            with ThreadPoolExecutor(max_workers=2) as executor:
                return sorted(executor.map(create, ["one", "two"]))
        with database_scope(self.business):
            for trial in range(12):
                target = records.create_record(4242, bid, "crm_passport", {"f_number": str(trial)})["id"]
                self.assertEqual(race(target), [201, 422])
            view = records.list_records(4242, bid, "crm_person")
            targets = [r["links"]["l_passport"]["id"] for r in view["records"]]
            self.assertEqual(len(targets), len(set(targets)))
            # A failed validation must release the advisory lock and roll back.
            with self.assertRaises(records.RecordError):
                records.create_record(4242, bid, "crm_person", {"f_name": "bad", "l_passport": -1})
            self.assertEqual(records.create_record(4242, bid, "crm_person", {"f_name": "after failure"})["values"]["f_name"], "after failure")

    def test_two_updates_cannot_claim_one_target(self):
        from agentic import records
        bid = self.seed_business()
        with database_scope(self.business):
            for trial in range(8):
                target = records.create_record(4242, bid, "crm_passport", {"f_number": "update" + str(trial)})["id"]
                people = [records.create_record(4242, bid, "crm_person", {"f_name": name})["id"]
                          for name in ("a", "b")]
                barrier = threading.Barrier(2)
                def change(record_id):
                    with database_scope(self.business):
                        barrier.wait(timeout=10)
                        try:
                            records.update_record(4242, bid, "crm_person", record_id, {"l_passport": target})
                            return 200
                        except records.RecordError as exc:
                            return exc.status
                with ThreadPoolExecutor(max_workers=2) as executor:
                    self.assertEqual(sorted(executor.map(change, people)), [200, 422])

    def test_spec_version_compare_and_set_is_atomic_across_connections(self):
        from agentic import store
        bid = self.seed_business()
        barrier = threading.Barrier(2)
        def append(_):
            try:
                with d.get_conn(self.business) as conn:
                    current = store.get_business_with_spec(conn, 4242, bid)
                    barrier.wait(timeout=10)
                    version = store.append_version(conn, bid, current["current_version"], current["spec"],
                                                   [], "agent", 4242)
                    return str(version)
            except store.StaleVersion:
                return "stale"
        with ThreadPoolExecutor(max_workers=2) as executor:
            self.assertEqual(sorted(executor.map(append, range(2))), ["2", "stale"])
        with d.get_conn(self.business) as conn:
            current = store.get_business_with_spec(conn, 4242, bid)
            self.assertEqual(current["current_version"], 2)
            self.assertEqual([v["version"] for v in store.list_versions(conn, 4242, bid)], [1, 2])

    def test_safe_reinitialization_does_not_remove_records(self):
        with d.get_conn(self.business) as conn:
            bid = d.create_business(conn, "Preserved", "test", "1", {})
        d.init_db(self.business)
        with d.get_conn(self.business) as conn:
            self.assertEqual(d.get_business(conn, bid)["name"], "Preserved")
        with patch.dict(os.environ, {"OPERATIONS_ALLOW_DATABASE_RESET": "0"}):
            with self.assertRaises(ValueError):
                d.init_db(self.business, reset=True)
        with d.get_conn(self.business) as conn:
            self.assertEqual(d.get_business(conn, bid)["name"], "Preserved")


if __name__ == "__main__":
    unittest.main()
