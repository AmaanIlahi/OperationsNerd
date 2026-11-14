"""Offline SQL adapter and request-routing regressions. No model or live DB."""

import asyncio
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from db import db as d
from db import runtime
from db.postgres import Row, translate


class RoutingTests(unittest.TestCase):
    def test_nested_scopes_restore_even_on_failure(self):
        with patch.dict(os.environ, {"OPERATIONS_DATABASE_URL": "control"}):
            self.assertEqual(runtime.current_database_url(), "control")
            with runtime.database_scope("outer"):
                self.assertEqual(runtime.current_database_url(), "outer")
                with self.assertRaises(RuntimeError):
                    with runtime.database_scope("inner"):
                        self.assertEqual(runtime.current_database_url(), "inner")
                        raise RuntimeError("leave scope")
                self.assertEqual(runtime.current_database_url(), "outer")
            self.assertEqual(runtime.current_database_url(), "control")

    def test_concurrent_async_tasks_do_not_share_context(self):
        async def run():
            async def read(value):
                with runtime.database_scope(value):
                    await asyncio.sleep(0)
                    return runtime.current_database_url()
            return await asyncio.gather(read("one"), read("two"))
        self.assertEqual(asyncio.run(run()), ["one", "two"])

    def test_control_connection_bypasses_business_scope(self):
        with tempfile.TemporaryDirectory() as folder:
            control, business = (str(Path(folder) / name) for name in ("control.db", "business.db"))
            d.init_db(control)
            d.init_db(business)
            with patch.dict(os.environ, {"OPERATIONS_DATABASE_URL": control}):
                with runtime.database_scope(business):
                    with d.get_conn() as conn:
                        d.create_business(conn, "Business only", "test", "1", {})
                    with d.control_conn() as conn:
                        self.assertEqual(conn.execute("SELECT count(*) FROM businesses").fetchone()[0], 0)
                with d.get_conn() as conn:
                    self.assertEqual(conn.execute("SELECT count(*) FROM businesses").fetchone()[0], 0)
            with d.get_conn(business) as conn:
                self.assertEqual(conn.execute("SELECT name FROM businesses").fetchone()[0], "Business only")

    def test_dynamic_sqlite_default_and_rollback(self):
        with tempfile.TemporaryDirectory() as folder:
            path = str(Path(folder) / "default.db")
            with patch.object(d, "DB_PATH", path), patch.dict(os.environ, {}, clear=True):
                d.init_db()
                with self.assertRaises(ValueError):
                    with d.get_conn() as conn:
                        d.create_business(conn, "Rolled back", "test", "1", {})
                        raise ValueError("fail entire unit of work")
                with d.get_conn() as conn:
                    self.assertEqual(conn.execute("SELECT count(*) FROM businesses").fetchone()[0], 0)


class AdapterTests(unittest.TestCase):
    def test_qmarks_are_not_replaced_in_literals_or_comments(self):
        sql = "SELECT ?, '?', \"?\", $$?$$, $body$?$body$ /* ? /* ? */ */ -- ?\n, ?"
        converted, identity = translate(sql, has_parameters=True)
        self.assertEqual(converted,
                         "SELECT %s, '?', \"?\", $$?$$, $body$?$body$ /* ? /* ? */ */ -- ?\n, %s")
        self.assertFalse(identity)

    def test_percent_literals_remain_literal_under_parameter_binding(self):
        sql, _ = translate("SELECT ? WHERE '100%' LIKE '%0%'", has_parameters=True)
        self.assertEqual(sql, "SELECT %s WHERE '100%%' LIKE '%%0%%'")

    def test_known_dialect_changes_preserve_json_text(self):
        sql, _ = translate("SELECT json_group_object(attribute_name, value), datetime('now'), MAX(turns - 1, 0)")
        self.assertIn("json_object_agg(attribute_name, value)::text", sql)
        self.assertIn("statement_timestamp()", sql)
        self.assertIn("GREATEST(turns - 1, 0)", sql)

    def test_conflict_and_identity_are_only_added_when_needed(self):
        sql, identity = translate("INSERT INTO entities(business_id, entity_type) VALUES (?, ?)", has_parameters=True)
        self.assertTrue(identity)
        self.assertTrue(sql.endswith("RETURNING id"))
        sql, identity = translate("INSERT OR IGNORE INTO chat_usage(account_id, day, turns) VALUES (?, ?, 0)", has_parameters=True)
        self.assertFalse(identity)
        self.assertTrue(sql.endswith("ON CONFLICT DO NOTHING"))
        sql, identity = translate("INSERT INTO accounts(email,password_hash) VALUES (?, ?) RETURNING id", has_parameters=True)
        self.assertFalse(identity)
        self.assertEqual(sql.count("RETURNING"), 1)

    def test_rows_support_core_mapping_and_index_access(self):
        row = Row({"id": 11, "name": "Toy company"})
        self.assertEqual(row[0], row["id"])
        self.assertEqual(row[-1], "Toy company")
        self.assertEqual(dict(row), {"id": 11, "name": "Toy company"})


if __name__ == "__main__":
    unittest.main()
