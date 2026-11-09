"""
Accounting module: the cafe walkthrough (demo_cafe.py) runs end to end on a
throwaway database, its figures are the ones worked out by hand below, and
running it again on the same database replays the same answers instead of
invoicing anyone twice.

    python -B modules/accounting/tests/test_cafe_walkthrough.py
"""

import contextlib
import io
import os
import sqlite3
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parent))
import accounting as a  # noqa: E402
import demo_cafe  # noqa: E402
from test_ledger import dump  # noqa: E402

TMP = tempfile.TemporaryDirectory(prefix="accounting_walkthrough_", ignore_cleanup_errors=True)


def test_the_walkthrough_tells_the_cafes_year_in_cents():
    conn = a.connect(os.path.join(TMP.name, "cafe.db"))
    lines = []
    out = demo_cafe.walkthrough(conn, say=lines.append)

    # 585.00 invoiced, 400.00 in, so 185.00 outstanding and past its 16 March due date by 30 April.
    assert (out["invoice"]["total_cents"], out["invoice"]["amount_paid_cents"]) == (58_500, 40_000)
    assert out["invoice"]["outstanding_cents"] == 18_500 and out["invoice"]["status"] == "sent"
    assert a.invoice(conn, out["invoice"]["invoice_id"], as_of=demo_cafe.TODAY)["effective_status"] == "overdue"
    assert (out["beans"]["total_cents"], out["beans"]["status"]) == (27_000, "sent")
    assert (out["rent"]["total_cents"], out["rent"]["status"]) == (95_000, "paid")
    # The duplicate bill is void, its entry still on file, and the void is the mirror of it.
    assert out["voided"]["status"] == "void"
    reversal = a.journal_entry(conn, out["voided"]["void_entry_id"])
    assert reversal["reverses_id"] == out["voided"]["entry_id"]
    assert reversal["entry_date"] == a.journal_entry(conn, out["voided"]["entry_id"])["entry_date"]
    assert a.account_by_code(conn, "2100")["balance_cents"] == 27_000

    # 5,000.00 capital + 585.00 invoiced + 270.00 and 950.00 billed + the 270.00 void, both ways.
    trial = out["trial_balance"]
    assert trial["balanced"] and trial["total_debit_cents"] == trial["total_credit_cents"] == 869_500
    pl = out["profit_and_loss"]
    assert (pl["total_income_cents"], pl["total_expense_cents"], pl["net_income_cents"]) == (58_500, 122_000,
                                                                                             -63_500)
    sheet = out["balance_sheet"]
    assert sheet["balanced"] and sheet["total_assets_cents"] == 463_500
    assert (sheet["total_liabilities_cents"], sheet["total_equity_cents"]) == (27_000, 500_000)
    assert sheet["retained_earnings_cents"] == -63_500

    # Both agings fall in the bucket their due date puts them in, and total to the control accounts.
    assert out["ar_aging"]["by_customer"] == [
        {"customer_id": 1, "customer": "Northgate Offices", "current": 0, "1_30": 0, "31_60": 18_500,
         "61_90": 0, "over_90": 0, "total_cents": 18_500}]
    assert out["ap_aging"]["by_supplier"] == [
        {"supplier_id": 1, "supplier": "Dockside Roastery", "current": 0, "1_30": 27_000, "31_60": 0,
         "61_90": 0, "over_90": 0, "total_cents": 27_000}]
    as_of = demo_cafe.TODAY
    assert out["ar_aging"]["totals"]["total_cents"] == \
        a.account_balance(conn, a.account_by_code(conn, "1100")["account_id"], as_of=as_of)["balance_cents"]
    assert out["ap_aging"]["totals"]["total_cents"] == \
        a.account_balance(conn, a.account_by_code(conn, "2100")["account_id"], as_of=as_of)["balance_cents"]

    assert any("Nothing calls it yet" in line for line in lines)      # the payroll hook, still unwired

    before = dump(conn)
    again = demo_cafe.walkthrough(conn, say=lambda *_: None)
    assert dump(conn) == before and again["invoice"] == out["invoice"]
    conn.close()


def test_the_script_runs_on_its_own_temp_database():
    out = io.StringIO()
    with contextlib.redirect_stdout(out):
        demo_cafe.main([])
    text = out.getvalue()
    assert "balanced: True" in text and "on 2026-04-30 it reads as: overdue" in text
    assert "income 585.00 - expenses 1220.00 = -635.00" in text
    path = text.strip().splitlines()[-1].removeprefix("Database: ").removesuffix(" (deleted on exit)")
    assert not os.path.exists(path)


def test_money_formats_whole_cents_without_floating_point():
    for cents, text in ((0, "0.00"), (5, "0.05"), (99, "0.99"), (100, "1.00"), (58_500, "585.00"),
                        (-63_500, "-635.00"), (a.MAX_CENTS, "100000000000.00")):
        assert demo_cafe.money(cents) == text


if __name__ == "__main__":
    print(f"Python {sys.version.split()[0]}, SQLite {sqlite3.sqlite_version}")
    tests = [(n, f) for n, f in globals().items() if n.startswith("test_") and callable(f)]
    for name, fn in tests:
        fn()
        print(f"PASS {name}")
    print(f"\n{len(tests)} tests passed")
