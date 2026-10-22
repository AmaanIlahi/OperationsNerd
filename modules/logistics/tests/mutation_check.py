"""
Checks that the stock tests aren't vacuous: removes each protection in turn
from a temp copy of modules/logistics/, runs every test against it, and reports
which tests fail. Nothing in the real folder is changed. Run from the repo
root with the venv (takes about a minute):

    python -B modules/logistics/tests/mutation_check.py

Each removal ends up in one of three outcomes:
  caught     a test fails because a bad write was accepted or a command misbehaved
  redundant  tests only fail because another rule now rejects the write, with a
             different message: the protection is implied by the others
  survived   no test fails: the protection is untested

Exits non-zero if the unchanged copy fails, anything survives, or the
redundant set differs from EXPECTED_REDUNDANT.
"""

import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

SRC = Path(__file__).resolve().parents[1]
S, A = "schema.sql", "stock.py"
WRONG_REASON = "rejected for the wrong reason"

# Implied by the other rules today; kept for the clearer error message (see README).
EXPECTED_REDUNDANT = {"on hand not negative trigger", "reservation quantity > 0 CHECK"}

HARNESS = r'''
import sys
sys.path.insert(0, sys.argv[1] + "/tests")
import test_stock as t
for name, fn in [(n, f) for n, f in vars(t).items() if n.startswith("test_") and callable(f)]:
    try:
        fn()
    except BaseException as e:
        print("FAIL", name, "|", type(e).__name__, (str(e).splitlines() or [""])[0][:120])
'''

NO_TRANSACTION = [('    conn.execute("BEGIN IMMEDIATE")\n', ""), ('conn.execute("COMMIT")', "pass")]
NO_RAISE = "BEGIN SELECT 1; END;"

# (label, file, [(old, new), ...], [file, [(old, new), ...]] ...). Each old string must occur exactly once.
MUTATIONS = [
    ("UNIQUE key on stock_commands", S, [("    UNIQUE (business_id, idempotency_key)\n);", "    id2 INTEGER\n);")]),
    ("UNIQUE key on stock_movements", S, [("    UNIQUE (business_id, idempotency_key),\n    FOREIGN KEY", "    FOREIGN KEY")]),
    ("UNIQUE key on stock_reservations", S, [("    UNIQUE (business_id, idempotency_key),\n    UNIQUE (business_id, id)",
                                              "    UNIQUE (business_id, id)")]),
    ("UNIQUE (business_id, id) on stock_reservations", S, [("    UNIQUE (business_id, id),  ", "  ")]),
    ("non-empty key CHECK on stock_commands", S, [("idempotency_key TEXT    NOT NULL CHECK (idempotency_key <> ''),\n    command",
                                                   "idempotency_key TEXT    NOT NULL,\n    command")]),
    ("non-empty key CHECK on stock_movements", S, [("idempotency_key TEXT    NOT NULL CHECK (idempotency_key <> ''),\n    created_at",
                                                    "idempotency_key TEXT    NOT NULL,\n    created_at")]),
    ("non-empty key CHECK on stock_reservations", S, [("idempotency_key  TEXT    NOT NULL CHECK (idempotency_key <> ''),",
                                                       "idempotency_key  TEXT    NOT NULL,")]),
    ("command name CHECK", S, [(" CHECK (command IN ('receive', 'reserve', 'release', 'ship'))", "")]),
    ("non-empty source_ref CHECK", S, [(" CHECK (source_ref <> '')", "")]),
    ("non-empty order_ref CHECK", S, [(" CHECK (order_ref <> '')", "")]),
    ("movement sign/type CHECK", S, [(",\n    CHECK ((movement_type = 'receipt' AND quantity > 0) OR (movement_type = 'shipment' AND quantity < 0))", "")]),
    ("shipment has a reservation, receipt has none CHECK", S, [(",\n    CHECK ((movement_type = 'shipment') = (reservation_id IS NOT NULL))", "")]),
    ("business-scoped FOREIGN KEY to the reservation", S, [(",\n    FOREIGN KEY (business_id, reservation_id) REFERENCES stock_reservations (business_id, id)", "")]),
    ("stock_movements no UPDATE trigger", S, [("BEFORE UPDATE ON stock_movements\nBEGIN SELECT RAISE(ABORT, 'stock_movements rows are immutable'); END;",
                                               "BEFORE UPDATE ON stock_movements WHEN 0\n" + NO_RAISE)]),
    ("stock_movements no DELETE trigger", S, [("BEFORE DELETE ON stock_movements\nBEGIN SELECT RAISE(ABORT, 'stock_movements rows are immutable'); END;",
                                               "BEFORE DELETE ON stock_movements WHEN 0\n" + NO_RAISE)]),
    ("on hand not negative trigger", S, [("BEGIN SELECT RAISE(ABORT, 'on hand would go negative'); END;", NO_RAISE)]),
    ("shipment matches its reservation's item and location trigger", S,
     [("BEGIN SELECT RAISE(ABORT, 'shipment item or location does not match its reservation'); END;", NO_RAISE)]),
    ("shipment moves quantity_shipped trigger", S, [("AFTER INSERT ON stock_movements\nWHEN NEW.movement_type = 'shipment'",
                                                     "AFTER INSERT ON stock_movements\nWHEN 0")]),
    ("last shipment marks fulfilled (in that trigger)", S,
     [(",\n           status = CASE WHEN quantity_shipped - NEW.quantity = quantity THEN 'fulfilled' ELSE status END", "")]),
    ("reservation quantity > 0 CHECK", S, [("quantity         INTEGER NOT NULL CHECK (quantity > 0),", "quantity         INTEGER NOT NULL,")]),
    ("quantity_shipped <= quantity CHECK", S, [(" CHECK (quantity_shipped <= quantity)", "")]),
    ("status values CHECK", S, [(" CHECK (status IN ('active', 'fulfilled', 'released'))", "")]),
    ("fulfilled iff fully shipped CHECK", S, [("    CHECK ((status = 'fulfilled') = (quantity_shipped = quantity))", "    CHECK (1)")]),
    ("reservations within on hand trigger", S, [("BEGIN SELECT RAISE(ABORT, 'reservation exceeds available stock'); END;", NO_RAISE)]),
    ("born active trigger", S, [("BEGIN SELECT RAISE(ABORT, 'a reservation starts active with nothing shipped'); END;", NO_RAISE)]),
    ("quantity_shipped matches the shipments trigger", S,
     [("BEGIN SELECT RAISE(ABORT, 'quantity_shipped must equal the shipments of this reservation'); END;", NO_RAISE)]),
    ("status flow trigger", S, [("BEGIN SELECT RAISE(ABORT, 'reservation is no longer active'); END;", NO_RAISE)]),
    ("fixed columns trigger", S, [("BEGIN SELECT RAISE(ABORT, 'only status and quantity_shipped may change'); END;", NO_RAISE)]),
    ("stock_reservations no DELETE trigger", S, [("BEGIN SELECT RAISE(ABORT, 'stock_reservations rows are kept'); END;", NO_RAISE)]),
    ("stock_commands no UPDATE trigger", S, [("BEFORE UPDATE ON stock_commands\nBEGIN SELECT RAISE(ABORT, 'stock_commands rows are immutable'); END;",
                                              "BEFORE UPDATE ON stock_commands WHEN 0\n" + NO_RAISE)]),
    ("stock_commands no DELETE trigger", S, [("BEFORE DELETE ON stock_commands\nBEGIN SELECT RAISE(ABORT, 'stock_commands rows are immutable'); END;",
                                              "BEFORE DELETE ON stock_commands WHEN 0\n" + NO_RAISE)]),
    # app
    ("foreign keys turned on in connect()", A, [('    conn.execute("PRAGMA foreign_keys = ON")\n', "")]),
    ("BEGIN IMMEDIATE -> deferred BEGIN", A, [('conn.execute("BEGIN IMMEDIATE")', 'conn.execute("BEGIN")')]),
    ("no transaction at all (autocommit)", A, NO_TRANSACTION),
    ("no transaction AND no reservations-within-on-hand trigger", A, NO_TRANSACTION,
     S, [("BEGIN SELECT RAISE(ABORT, 'reservation exceeds available stock'); END;", NO_RAISE)]),
    ("replay lookup skipped", A, [("if row is not None:", "if False:")]),
    ("payload comparison on replay", A, [('if (row["command"], row["payload"]) != (command, body):', "if False:")]),
    ("ROLLBACK on failure", A, [('        if conn.in_transaction:\n            conn.execute("ROLLBACK")', "        pass")]),
    ("reserve caps at available", A, [("min(quantity, max(available(conn, business_id, item_id), 0))", "quantity")]),
    ("ship over-reserved check", A, [("if quantity > remaining:", "if False:")]),
    ("reservation status check", A, [('if r["status"] != "active":', "if False:")]),
    ("business filter on reservation lookup", A, [('"SELECT * FROM stock_reservations WHERE id = ? AND business_id = ?"',
                                                   '"SELECT * FROM stock_reservations WHERE id = ? AND ? > 0"')]),
    ("business filter on on_hand", A, [('FROM stock_movements WHERE business_id = ? AND item_id = ?)"',
                                        'FROM stock_movements WHERE ? > 0 AND item_id = ?)"')]),
    ("business filter on reserved", A, [("WHERE business_id = ? AND item_id = ? AND status = 'active')\"\"\"",
                                         "WHERE ? > 0 AND item_id = ? AND status = 'active')\"\"\"")]),
    ("positive integer validation", A, [("def _positive_int(name, value):\n", "def _positive_int(name, value):\n    return\n")]),
    ("non-empty text validation", A, [("def _text(name, value):\n", "def _text(name, value):\n    return\n")]),
]


def run(folder: Path) -> list[str]:
    out = subprocess.run([sys.executable, "-B", "-c", HARNESS, str(folder)], capture_output=True, text=True, timeout=600)
    if out.returncode != 0:
        return [f"FAIL harness crashed | {(out.stderr.strip().splitlines() or ['?'])[-1]}"]
    return [line for line in out.stdout.splitlines() if line.startswith("FAIL")]


def apply(folder: Path, label: str, edits: list) -> None:
    for fname, pairs in zip(edits[::2], edits[1::2]):
        text = (folder / fname).read_text()
        for old, new in pairs:
            assert text.count(old) == 1, f"{label}: target found {text.count(old)} times in {fname}"
            text = text.replace(old, new)
        (folder / fname).write_text(text)


def main() -> int:
    with tempfile.TemporaryDirectory(prefix="stock_mutation_", ignore_cleanup_errors=True) as tmp:
        base = Path(tmp) / "base"
        shutil.copytree(SRC, base, ignore=shutil.ignore_patterns("__pycache__"))
        baseline = run(base)
        print("unchanged copy:", "all tests pass" if not baseline else baseline)
        outcomes = {}
        for i, (label, *edits) in enumerate(MUTATIONS, 1):
            folder = Path(tmp) / f"m{i}"
            shutil.copytree(base, folder)
            apply(folder, label, edits)
            fails = run(folder)
            if not fails:
                outcome = "SURVIVED"
            elif all(WRONG_REASON in f for f in fails):
                outcome = "redundant"
            else:
                outcome = "caught"
            outcomes[label] = outcome
            print(f"\n[{i:2}] {outcome:9} {label}")
            for f in fails:
                print("      ", f[len("FAIL "):])
        survived = [k for k, v in outcomes.items() if v == "SURVIVED"]
        redundant = {k for k, v in outcomes.items() if v == "redundant"}
        print(f"\n{len(MUTATIONS)} removals: {sum(v == 'caught' for v in outcomes.values())} caught, "
              f"{len(redundant)} redundant, {len(survived)} survived")
        problems = []
        if baseline:
            problems.append("the unchanged copy fails")
        if survived:
            problems.append(f"survived: {survived}")
        if redundant != EXPECTED_REDUNDANT:
            problems.append(f"redundant set changed: now {sorted(redundant)}, expected {sorted(EXPECTED_REDUNDANT)}")
        print("OK" if not problems else "PROBLEMS: " + "; ".join(problems))
        return 1 if problems else 0


if __name__ == "__main__":
    sys.exit(main())
