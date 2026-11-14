"""Real same-business PostgreSQL accounting, explicit bills and fund regressions."""
from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta
import unittest

import psycopg
from psycopg.rows import dict_row

from tests_integration import test_umbrella_workflow as flow


class AccountingTests(flow.UmbrellaHTTPTests):
    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        from db import db as d
        from umbrella.businesses import initialize_registry
        d.init_db()
        initialize_registry()

    def setUp(self):
        super().setUp()
        self.accounting_base = self.base + "/accounting"
        self.accounting_bindings = {
            "customer": {"entity": self.entities["Customer"]["key"], "fields": {
                "name": self.fields["Customer"]["Name"], "address": self.fields["Customer"]["Shipping address"]}, "links": {}},
            "supplier": {"entity": self.entities["Supplier"]["key"], "fields": {"name": self.fields["Supplier"]["Name"]}, "links": {}},
        }
        proposed = self.response(self.owner.post(self.base + "/nerds/accounting/proposals", json={
            "base_version": self.toy["current_version"], "bindings": self.accounting_bindings}), 201)
        self.response(self.owner.post(self.base + f"/nerds/accounting/proposals/{proposed['id']}/approve", json={}))
        workspace = self.finance()
        self.accounts = {row["code"]: row["id"] for row in workspace["accounts"]}
        self.general = next(row["id"] for row in workspace["funds"] if row["is_default"])

    def finance(self):
        return self.response(self.owner.get(self.accounting_base + "/workspace"))

    def accounting(self, operation, values, key=None, expected=200):
        from uuid import uuid4
        return self.response(self.owner.post(self.accounting_base + "/commands/" + operation,
            json={**values, "idempotency_key": key or uuid4().hex}), expected)

    def journal(self, lines, *, fund=None, dated=None, key=None, expected=200):
        return self.accounting("post_journal_entry", {"entry_date": dated or self.today.isoformat(), "memo": "Fictional fixture entry",
            "currency": "USD", "lines": [{**line, **({"fund_id": fund} if fund else {})} for line in lines]}, key, expected)

    def create_fund(self, code, permanent=False):
        return self.accounting("create_fund", {"code": code, "name": code + " fund", "purpose": "Fictional program",
            "restriction_type": "permanently_restricted" if permanent else "temporarily_restricted"})["id"]

    def grant(self, fund, amount=10000, dated=None):
        return self.accounting("grant_receipt", {"fund_id": fund, "amount_minor": amount, "cash_account_id": self.accounts["1000"],
            "revenue_account_id": self.accounts["4100"], "entry_date": dated or self.today.isoformat(), "memo": "Fictional grant"})

    def fund(self, identifier):
        return next(row for row in self.finance()["funds"] if row["id"] == identifier)

    def bill(self, amount=10000):
        bill = self.accounting("create_bill", {"number": "BILL-001", "party_record_id": self.supplier["id"],
            "issue_date": self.today.isoformat(), "due_date": (self.today + timedelta(days=10)).isoformat(), "currency": "USD",
            "lines": [{"description": "Toy inventory", "quantity": 1, "unit_price_minor": amount, "account_id": self.accounts["1200"]}]})
        return self.accounting("post_bill", {"document_id": bill["id"]})

    def test_explicit_bills_invoices_payments_same_core_parties_and_history(self):
        gym_before = self.fixture.core_snapshot(self.gym["id"])
        self.assertIsNone(self.fixture.scalar(self.bid, "SELECT to_regnamespace('logistics')"))
        self.journal([{"account_id": self.accounts["1000"], "debit_minor": 20000},
                      {"account_id": self.accounts["3000"], "credit_minor": 20000}])
        bill = self.bill()
        self.assertEqual((bill["status"], bill["remaining_minor"]), ("posted", 10000))
        self.assertEqual(self.finance()["ap_aging"]["total_minor"], 10000)
        payment = {"document_id": bill["id"], "amount_minor": 4000, "cash_account_id": self.accounts["1000"],
                   "payment_date": self.today.isoformat(), "reference": "Fixture payment one"}
        paid = self.accounting("record_bill_payment", payment, "bill-payment-one")
        self.assertEqual((paid["paid_minor"], paid["remaining_minor"]), (4000, 6000))
        self.assertEqual(paid, self.accounting("record_bill_payment", payment, "bill-payment-one"))
        complete = self.accounting("record_bill_payment", {**payment, "amount_minor": 6000, "reference": "Fixture payment two"})
        self.assertEqual((complete["status"], complete["remaining_minor"]), ("paid", 0))
        self.accounting("record_bill_payment", {**payment, "amount_minor": 1}, expected=409)
        invoice = self.accounting("create_invoice", {"number": "INV-001", "party_record_id": self.customer["id"],
            "issue_date": self.today.isoformat(), "due_date": (self.today + timedelta(days=15)).isoformat(), "currency": "USD",
            "lines": [{"description": "Toy order", "quantity": 10, "unit_price_minor": 2000, "account_id": self.accounts["4000"]}]})
        self.accounting("post_invoice", {"document_id": invoice["id"]})
        self.assertEqual(self.finance()["ar_aging"]["total_minor"], 20000)
        self.accounting("record_invoice_payment", {"document_id": invoice["id"], "amount_minor": 20000,
            "cash_account_id": self.accounts["1000"], "payment_date": self.today.isoformat(), "reference": "Customer paid"})
        report = self.finance()
        cash = next(row for row in report["accounts"] if row["id"] == self.accounts["1000"])
        self.assertEqual(cash["balance_minor"], 30000)
        self.assertEqual((report["ap_aging"]["total_minor"], report["ar_aging"]["total_minor"]), (0, 0))
        self.assertEqual(report["balance_sheet"]["net_assets_minor"], 40000)
        self.assertEqual(report["trial_balance"]["total_debits_minor"], report["trial_balance"]["total_credits_minor"])
        self.response(self.owner.patch(self.base + f"/records/{self.entities['Supplier']['key']}/{self.supplier['id']}",
            json={"values": {self.fields["Supplier"]["Name"]: "Renamed supplier"}}))
        updated = self.finance()
        self.assertEqual(updated["bills"][0]["party_name"], "Toy Supplier")
        self.assertEqual(updated["references"]["suppliers"][0]["name"], "Renamed supplier")
        self.assertIsNone(self.fixture.scalar(self.bid, "SELECT to_regclass('accounting.suppliers')"))
        self.response(self.stranger.get(self.accounting_base + "/workspace"), 404)
        self.response(self.stranger.post(self.accounting_base + "/commands/post_bill",
            json={"document_id": bill["id"], "idempotency_key": "stranger"}), 404)
        self.assertEqual(self.fixture.core_snapshot(self.gym["id"]), gym_before)

    def test_four_fund_regressions_capital_release_permanent_and_backdating(self):
        temporary = self.create_fund("TEMP")
        self.grant(temporary)
        self.assertEqual(self.fund(temporary)["net_assets_minor"], 10000)
        self.assertEqual(self.fund(temporary)["available_minor"], 10000)
        capital = [{"account_id": self.accounts["1500"], "debit_minor": 4000},
                   {"account_id": self.accounts["1000"], "credit_minor": 4000}]
        self.journal(capital, fund=temporary)
        self.assertEqual(self.fund(temporary)["available_minor"], 6000)
        self.assertEqual(self.fund(temporary)["net_assets_minor"], 10000)
        self.journal([{**capital[0], "debit_minor": 7000}, {**capital[1], "credit_minor": 7000}], fund=temporary, expected=422)
        self.assertEqual(self.fund(temporary)["available_minor"], 6000)
        release = {"fund_id": temporary, "amount_minor": 1000, "entry_date": self.today.isoformat(), "memo": "Purpose completed"}
        self.accounting("release_fund", release)
        self.assertEqual(self.fund(temporary)["available_minor"], 5000)
        self.assertEqual(self.fund(temporary)["net_assets_minor"], 9000)
        permanent = self.create_fund("PERM", permanent=True)
        self.grant(permanent)
        self.accounting("release_fund", {**release, "fund_id": permanent}, expected=409)
        self.assertEqual(self.fund(permanent)["available_minor"], 10000)
        backdated = self.create_fund("BACK")
        self.grant(backdated, dated=(self.today - timedelta(days=10)).isoformat())
        expense = lambda value: [{"account_id": self.accounts["5000"], "debit_minor": value},
                                 {"account_id": self.accounts["1000"], "credit_minor": value}]
        self.journal(expense(8000), fund=backdated, dated=(self.today - timedelta(days=1)).isoformat())
        self.journal(expense(5000), fund=backdated, dated=(self.today - timedelta(days=2)).isoformat(), key="backdated", expected=422)
        self.assertEqual(self.fund(backdated)["available_minor"], 2000)
        self.journal(expense(1000), fund=backdated, dated=(self.today - timedelta(days=2)).isoformat(), key="backdated")
        self.assertEqual(self.fund(backdated)["available_minor"], 1000)
        self.accounting("create_account", {"code": "1900", "name": "Unclassified asset", "account_type": "asset"}, expected=422)
        self.accounting("create_account", {"code": "1900", "name": "Explicit equipment", "account_type": "asset", "is_capital": True})

    def test_database_guards_immutability_balance_per_fund_and_raw_restricted_capacity(self):
        journal = self.journal([{"account_id": self.accounts["1000"], "debit_minor": 100},
                                {"account_id": self.accounts["3000"], "credit_minor": 100}])
        with psycopg.connect(self.fixture.business_url(self.bid), autocommit=True, row_factory=dict_row) as conn:
            for statement in ("UPDATE accounting.journal_entries SET memo='changed' WHERE id=%s",
                              "DELETE FROM accounting.journal_entries WHERE id=%s"):
                with self.assertRaises(psycopg.errors.CheckViolation):
                    conn.execute(statement, (journal["id"],))
            with self.assertRaises(psycopg.errors.CheckViolation):
                conn.execute("""INSERT INTO accounting.journal_lines(entry_id,line_no,account_id,fund_id,debit_minor)
                    VALUES(%s,3,%s,%s,1)""", (journal["id"], self.accounts["1000"], self.general))
            with self.assertRaises(psycopg.errors.CheckViolation):
                with conn.transaction():
                    conn.execute("INSERT INTO accounting.command_requests(idempotency_key,operation,payload,result) VALUES('raw-unbalanced','raw','{}','{}')")
                    eid = conn.execute("""INSERT INTO accounting.journal_entries(entry_date,memo,source,currency,total_minor,actor_id,request_key)
                        VALUES(%s,'raw','manual','USD',10,1,'raw-unbalanced') RETURNING id""", (self.today,)).fetchone()["id"]
                    for number, account, debit, credit in ((1, self.accounts["1000"], 10, 0), (2, self.accounts["3000"], 0, 9)):
                        conn.execute("""INSERT INTO accounting.journal_lines(entry_id,line_no,account_id,fund_id,debit_minor,credit_minor)
                            VALUES(%s,%s,%s,%s,%s,%s)""", (eid, number, account, self.general, debit, credit))
            self.assertEqual(conn.execute("SELECT count(*) AS n FROM accounting.command_requests WHERE idempotency_key='raw-unbalanced'").fetchone()["n"], 0)
        fund = self.create_fund("RAW")
        self.grant(fund, amount=100)
        with psycopg.connect(self.fixture.business_url(self.bid), autocommit=True, row_factory=dict_row) as conn:
            # Bypass the Python ledger entirely: a balanced capital purchase
            # still cannot exceed the restricted fund's available capacity.
            with self.assertRaisesRegex(psycopg.errors.CheckViolation, "Restricted fund capacity"):
                with conn.transaction():
                    conn.execute("INSERT INTO accounting.command_requests(idempotency_key,operation,payload,result) VALUES('raw-capital','raw','{}','{}')")
                    eid = conn.execute("""INSERT INTO accounting.journal_entries(entry_date,memo,source,currency,total_minor,actor_id,request_key)
                        VALUES(%s,'raw capital','manual','USD',101,1,'raw-capital') RETURNING id""", (self.today,)).fetchone()["id"]
                    for number, account, debit, credit in ((1, self.accounts["1500"], 101, 0), (2, self.accounts["1000"], 0, 101)):
                        conn.execute("""INSERT INTO accounting.journal_lines(entry_id,line_no,account_id,fund_id,debit_minor,credit_minor)
                            VALUES(%s,%s,%s,%s,%s,%s)""", (eid, number, account, fund, debit, credit))
            self.assertEqual(conn.execute("SELECT count(*) AS n FROM accounting.command_requests WHERE idempotency_key='raw-capital'").fetchone()["n"], 0)
        self.assertEqual(self.fund(fund)["available_minor"], 100)
        # Overall balance is insufficient: each fund must also balance.
        self.journal([{"account_id": self.accounts["1000"], "debit_minor": 1, "fund_id": fund},
                      {"account_id": self.accounts["3000"], "credit_minor": 1, "fund_id": self.general}], expected=422)
        self.accounting("post_journal_entry", {"entry_date": self.today.isoformat(), "memo": "Bad actor", "actor_id": 123,
            "lines": [{"account_id": self.accounts["1000"], "debit_minor": 10}, {"account_id": self.accounts["3000"], "credit_minor": 10}]}, expected=422)

    def test_concurrent_retries_and_payments_cannot_double_post_or_overpay(self):
        lines = [{"account_id": self.accounts["1000"], "debit_minor": 20000},
                 {"account_id": self.accounts["3000"], "credit_minor": 20000}]
        with ThreadPoolExecutor(max_workers=2) as executor:
            results = list(executor.map(lambda _: self.journal(lines, key="same-opening"), range(2)))
        self.assertEqual(results[0], results[1])
        self.assertEqual(len(self.finance()["journal"]), 1)
        bill = self.bill()
        payload = {"document_id": bill["id"], "amount_minor": 6000, "cash_account_id": self.accounts["1000"],
                   "payment_date": self.today.isoformat(), "reference": "Racing payment"}
        def pay(key):
            return self.owner.post(self.accounting_base + "/commands/record_bill_payment", json={**payload, "idempotency_key": key})
        with ThreadPoolExecutor(max_workers=2) as executor:
            responses = list(executor.map(pay, ["payment-a", "payment-b"]))
        self.assertEqual(sorted(response.status_code for response in responses), [200, 409])
        self.assertEqual(self.finance()["bills"][0]["remaining_minor"], 4000)

    def test_void_reversal_and_explicit_currency_and_party_validation(self):
        draft = self.accounting("create_bill", {"number": "VOID-DRAFT", "party_record_id": self.supplier["id"],
            "issue_date": self.today.isoformat(), "due_date": self.today.isoformat(), "currency": "USD",
            "lines": [{"description": "Expense", "quantity": 1, "unit_price_minor": 100, "account_id": self.accounts["5000"]}]})
        self.accounting("void_bill", {"document_id": draft["id"], "entry_date": self.today.isoformat(), "reason": "Wrong draft"})
        posted = self.bill()
        self.accounting("void_bill", {"document_id": posted["id"], "entry_date": self.today.isoformat(), "reason": "Duplicate bill"})
        self.assertEqual(self.finance()["ap_aging"]["total_minor"], 0)
        self.assertEqual(self.finance()["trial_balance"]["total_debits_minor"], 0)
        self.accounting("create_bill", {"number": "WRONG-PARTY", "party_record_id": self.item["id"],
            "issue_date": self.today.isoformat(), "due_date": self.today.isoformat(), "currency": "USD",
            "lines": [{"description": "Expense", "quantity": 1, "unit_price_minor": 100, "account_id": self.accounts["5000"]}]}, expected=404)
        self.accounting("post_journal_entry", {"entry_date": self.today.isoformat(), "memo": "Wrong currency", "currency": "EUR",
            "lines": [{"account_id": self.accounts["1000"], "debit_minor": 10}, {"account_id": self.accounts["3000"], "credit_minor": 10}]}, expected=422)

    def test_payment_timeliness_uses_actual_partial_payment_dates_and_raw_denominators(self):
        def invoice(number, total, due):
            draft = self.accounting("create_invoice", {"number": number, "party_record_id": self.customer["id"],
                "issue_date": (self.today - timedelta(days=10)).isoformat(), "due_date": due.isoformat(), "currency": "USD",
                "lines": [{"description": "Fictional toys", "quantity": 1, "unit_price_minor": total, "account_id": self.accounts["4000"]}]})
            return self.accounting("post_invoice", {"document_id": draft["id"]})
        overdue = invoice("TIMELY-1", 10000, self.today - timedelta(days=4))
        invoice("TIMELY-2", 4000, self.today + timedelta(days=4))
        for amount, days, reference in ((2000, -5, "Before due"), (3000, -1, "Three days late"), (1000, 1, "Future recorded payment")):
            self.accounting("record_invoice_payment", {"document_id": overdue["id"], "amount_minor": amount,
                "cash_account_id": self.accounts["1000"], "payment_date": (self.today + timedelta(days=days)).isoformat(), "reference": reference})
        counts = self.fixture.scalar(self.bid, "SELECT count(*) FROM accounting.command_requests")
        report = self.finance()
        timely = report["payment_timeliness"]
        self.assertEqual((timely["invoice_count"], timely["invoice_total_minor"]), (2, 14000))
        self.assertEqual((timely["paid_minor"], timely["eligible_paid_minor"], timely["on_time_paid_minor"], timely["late_paid_minor"]), (5000, 5000, 2000, 3000))
        self.assertEqual((timely["payment_portions_count"], timely["unknown_date_paid_minor"], timely["on_time_basis_points"]), (2, 0, 4000))
        self.assertEqual((timely["unpaid_minor"], timely["overdue_unpaid_minor"]), (9000, 5000))
        self.assertEqual([row["days_late"] for row in timely["rows"]], [0, 3])
        self.assertEqual(timely["overdue_unpaid"][0]["remaining_minor"], 5000)
        self.assertEqual(timely["customers"][0]["on_time_basis_points"], 4000)
        self.assertEqual(timely["quality"]["missing_due_date_invoice_count"], 0)
        self.assertEqual(self.fixture.scalar(self.bid, "SELECT count(*) FROM accounting.command_requests"), counts)
        self.assertEqual(sum(row["trial_debit_minor"] for row in report["trial_balance"]["accounts"]), report["trial_balance"]["total_debits_minor"])
        from modules.accounting.ledger import payment_timeliness
        unknown = payment_timeliness([{"id": 1, "status": "posted", "issue_date": self.today.isoformat(),
            "due_date": None, "party_record_id": 1, "party_name": "Legacy example", "number": "LEGACY", "total_minor": 100,
            "payments": [{"id": 1, "amount_minor": 40, "payment_date": None}]}], self.today)
        self.assertEqual((unknown["paid_minor"], unknown["unknown_date_paid_minor"], unknown["eligible_paid_minor"]), (40, 40, 0))
        self.assertIsNone(unknown["on_time_basis_points"])
        self.assertEqual(unknown["unpaid_minor"], 60)

    def test_database_document_payment_linkage_and_account_fund_updates(self):
        self.accounting("update_account", {"account_id": self.accounts["1500"], "name": "Equipment renamed"})
        fund = self.create_fund("UPDATES")
        self.accounting("update_fund", {"fund_id": fund, "name": "Program renamed", "purpose": "Updated purpose"})
        permanent = self.create_fund("NO-RECLASS", permanent=True)
        self.grant(permanent, amount=100)
        # The manual journal API cannot disguise the prohibited release.
        self.journal([
            {"account_id": self.accounts["3100"], "debit_minor": 10, "fund_id": permanent},
            {"account_id": self.accounts["1300"], "credit_minor": 10, "fund_id": permanent},
            {"account_id": self.accounts["1300"], "debit_minor": 10, "fund_id": self.general},
            {"account_id": self.accounts["3200"], "credit_minor": 10, "fund_id": self.general}], expected=422)
        draft = self.accounting("create_bill", {"number": "RAW-DOC", "party_record_id": self.supplier["id"],
            "issue_date": self.today.isoformat(), "due_date": self.today.isoformat(), "currency": "USD",
            "lines": [{"description": "Inventory", "quantity": 1, "unit_price_minor": 100, "account_id": self.accounts["1200"]}]})
        with psycopg.connect(self.fixture.business_url(self.bid), autocommit=True, row_factory=dict_row) as conn:
            with self.assertRaisesRegex(psycopg.errors.CheckViolation, "Document journal accounts"):
                with conn.transaction():
                    conn.execute("INSERT INTO accounting.command_requests(idempotency_key,operation,payload,result) VALUES('raw-document','raw','{}','{}')")
                    eid = conn.execute("""INSERT INTO accounting.journal_entries(entry_date,memo,source,source_ref,currency,total_minor,actor_id,request_key)
                        VALUES(%s,'wrong accounts','bill',%s,'USD',100,1,'raw-document') RETURNING id""", (self.today, str(draft["id"]))).fetchone()["id"]
                    # Correct header/total, wrong asset account: must be rejected.
                    for number, account, debit, credit in ((1, self.accounts["1000"], 100, 0), (2, self.accounts["2000"], 0, 100)):
                        conn.execute("""INSERT INTO accounting.journal_lines(entry_id,line_no,account_id,fund_id,debit_minor,credit_minor)
                            VALUES(%s,%s,%s,%s,%s,%s)""", (eid, number, account, self.general, debit, credit))
                    conn.execute("UPDATE accounting.documents SET status='posted',posted_entry_id=%s WHERE id=%s", (eid, draft["id"]))
            self.assertEqual(conn.execute("SELECT status FROM accounting.documents WHERE id=%s", (draft["id"],)).fetchone()["status"], "draft")
        posted = self.accounting("post_bill", {"document_id": draft["id"]})
        opening = self.journal([{"account_id": self.accounts["1000"], "debit_minor": 100}, {"account_id": self.accounts["3000"], "credit_minor": 100}])
        with psycopg.connect(self.fixture.business_url(self.bid), autocommit=True, row_factory=dict_row) as conn:
            with self.assertRaisesRegex(psycopg.errors.CheckViolation, "Payment must match"):
                with conn.transaction():
                    conn.execute("INSERT INTO accounting.command_requests(idempotency_key,operation,payload,result) VALUES('raw-payment','raw','{}','{}')")
                    conn.execute("""INSERT INTO accounting.payments(document_id,amount_minor,cash_account_id,payment_date,reference,entry_id,actor_id,request_key)
                        VALUES(%s,10,%s,%s,'wrong journal',%s,1,'raw-payment')""", (posted["id"], self.accounts["1000"], self.today, opening["id"]))
            self.assertEqual(conn.execute("SELECT count(*) AS n FROM accounting.payments WHERE document_id=%s", (posted["id"],)).fetchone()["n"], 0)

    def test_historical_void_reconciliation_and_strict_calendar_dates(self):
        draft = self.accounting("create_bill", {"number": "HISTORICAL-VOID", "party_record_id": self.supplier["id"],
            "issue_date": (self.today - timedelta(days=2)).isoformat(), "due_date": self.today.isoformat(), "currency": "USD",
            "lines": [{"description": "Inventory", "quantity": 1, "unit_price_minor": 100, "account_id": self.accounts["1200"]}]})
        self.accounting("post_bill", {"document_id": draft["id"]})
        self.accounting("void_bill", {"document_id": draft["id"], "entry_date": self.today.isoformat(), "reason": "Duplicate"})
        from modules.accounting.ledger import financial_reports
        with psycopg.connect(self.fixture.business_url(self.bid), autocommit=True, row_factory=dict_row) as conn:
            with conn.transaction():
                conn.execute("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ READ ONLY")
                past = financial_reports(conn, self.today - timedelta(days=1))
                self.assertEqual((past["ap_aging"]["total_minor"], past["ap_aging"]["control_balance_minor"], past["ap_aging"]["difference_minor"]), (100, 100, 0))
                current = financial_reports(conn, self.today)
                self.assertEqual((current["ap_aging"]["total_minor"], current["ap_aging"]["control_balance_minor"]), (0, 0))
        lines = [{"account_id": self.accounts["1000"], "debit_minor": 10}, {"account_id": self.accounts["3000"], "credit_minor": 10}]
        for value in (0, "2026-1-02", self.today.isoformat() + "T00:00:00Z"):
            self.accounting("post_journal_entry", {"entry_date": value, "memo": "Invalid calendar format", "lines": lines}, expected=422)
        self.journal([{**lines[0], "debit_minor": True}, lines[1]], expected=422)


def load_tests(loader, tests, pattern):
    return unittest.TestSuite(AccountingTests(name) for name, method in AccountingTests.__dict__.items()
                              if name.startswith("test_") and callable(method))


if __name__ == "__main__":
    unittest.main()
