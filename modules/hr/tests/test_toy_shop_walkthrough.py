"""
HR module: the toy-shop walkthrough (demo_toy_shop.py) runs end to end on a
throwaway database, and running it again on the same database replays the
same answers instead of writing anything twice.

    python -B modules/hr/tests/test_toy_shop_walkthrough.py
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
import demo_toy_shop  # noqa: E402
import hr as h  # noqa: E402
from test_employees import dump  # noqa: E402

TMP = tempfile.TemporaryDirectory(prefix="hr_walkthrough_", ignore_cleanup_errors=True)


def test_the_walkthrough_approves_one_request_and_refuses_one_for_balance():
    conn = h.connect(os.path.join(TMP.name, "shop.db"))
    lines = []
    out = demo_toy_shop.walkthrough(conn, say=lines.append)
    assert out["approved"]["status"] == "approved"
    assert out["refused"] == "asks for 10.0 'pto' days, employee 3 has 6.0 available (6.0 balance, 0.0 pending)"
    assert {k: (b["accrued"], b["taken"], b["available"]) for k, b in out["balances"].items()} == {
        "Priya": (20, 0, 20), "Sam": (20, 3, 17), "Lee": (6, 0, 6)}
    assert [(e["entry_type"], e["days"]) for e in out["ledger"]] == [("accrual", 20), ("request", 0), ("approve", -3)]
    assert [e["event"] for e in out["history"]] == ["hired", "updated"]
    assert h.leave_requests(conn, employee_id=3) == []
    assert any("refused:" in line for line in lines)

    before = dump(conn)
    again = demo_toy_shop.walkthrough(conn, say=lambda *_: None)
    assert dump(conn) == before and again["approved"] == out["approved"]


def test_the_script_runs_on_its_own_temp_database():
    out = io.StringIO()
    with contextlib.redirect_stdout(out):
        demo_toy_shop.main([])
    text = out.getvalue()
    assert "refused: asks for 10.0 'pto' days" in text and "approved by Priya" in text
    path = text.strip().splitlines()[-1].removeprefix("Database: ").removesuffix(" (deleted on exit)")
    assert not os.path.exists(path)


if __name__ == "__main__":
    print(f"Python {sys.version.split()[0]}, SQLite {sqlite3.sqlite_version}")
    tests = [(n, f) for n, f in globals().items() if n.startswith("test_") and callable(f)]
    for name, fn in tests:
        fn()
        print(f"PASS {name}")
    print(f"\n{len(tests)} tests passed")
