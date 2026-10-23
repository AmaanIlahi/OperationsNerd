"""
Checks that the stock tests aren't vacuous: removes each protection in turn
from a temp copy of modules/logistics/, runs every test against it, and reports
which tests fail. Nothing in the real folder is changed. Run from the repo
root with the venv (several minutes; removals run JOBS at a time, each in its
own process and temp copy):

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
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

SRC = Path(__file__).resolve().parents[1]
S, A = "schema.sql", "stock.py"
WRONG_REASON = "rejected for the wrong reason"
JOBS = 4

# Implied by the other rules today; kept for the clearer error message (see README).
EXPECTED_REDUNDANT = {"on hand not negative trigger", "reservation quantity > 0 CHECK", "lot on hand not negative trigger",
                      "stock_shipment_dispatches no DELETE trigger"}

HARNESS = r'''
import sys
sys.path.insert(0, sys.argv[1] + "/tests")
import test_stock, test_lots, test_shipments
for t in (test_stock, test_lots, test_shipments):
    for name, fn in [(n, f) for n, f in vars(t).items() if n.startswith("test_") and callable(f)]:
        try:
            fn()
        except BaseException as e:
            print("FAIL", name, "|", type(e).__name__, (str(e).splitlines() or [""])[0][:120])
'''

NO_TRANSACTION = [('    conn.execute("BEGIN IMMEDIATE")\n', ""), ('conn.execute("COMMIT")', "pass")]
NO_RAISE = "BEGIN SELECT 1; END;"
WITHIN_LOT = "BEGIN SELECT RAISE(ABORT, 'allocation exceeds the free stock of its lot'); END;"
# Reads the lots outside the write lock and takes it again to write, so two reserves can read the same free stock.
SPLIT_LOT_RESERVE = [("    plan, left = [], quantity\n", '    conn.execute("COMMIT")\n    plan, left = [], quantity\n'),
                     ("    reserved_now = quantity - left\n", '    conn.execute("BEGIN IMMEDIATE")\n    reserved_now = quantity - left\n')]

# Reads the shipment's status outside the write lock, so two dispatches can both see a draft.
SPLIT_DISPATCH = [('        shp = shipment(conn, business_id, shipment_id)\n        if shp["status"] != "draft":\n'
                   '            raise InvalidState(f"shipment {shipment_id} is already',
                   '        conn.execute("COMMIT")\n        shp = shipment(conn, business_id, shipment_id)\n'
                   '        conn.execute("BEGIN IMMEDIATE")\n        if shp["status"] != "draft":\n'
                   '            raise InvalidState(f"shipment {shipment_id} is already')]
# The dispatch primary key. The two foreign keys to the dispatch row go with it: without the primary key they have
# no parent key, and every insert would fail with "foreign key mismatch" instead of showing what the key protects.
DISPATCH_PK = [("    PRIMARY KEY (business_id, shipment_id),\n    UNIQUE (business_id, idempotency_key),\n    CHECK (label_ref",
                "    UNIQUE (business_id, idempotency_key),\n    CHECK (label_ref"),
               ("    FOREIGN KEY (business_id, shipment_id) REFERENCES stock_shipment_dispatches (business_id, shipment_id)\n"
                "        DEFERRABLE INITIALLY DEFERRED,\n", ""),
               (",\n    FOREIGN KEY (business_id, shipment_id) REFERENCES stock_shipment_dispatches (business_id, shipment_id)\n);",
                "\n);")]
# Every database rule that stops a second dispatch of one shipment.
DISPATCH_ONCE_RULES = DISPATCH_PK + [
                       ("BEGIN SELECT RAISE(ABORT, 'shipment movements are written before their dispatch'); END;", NO_RAISE),
                       ("BEGIN SELECT RAISE(ABORT, 'a dispatch ships every line of its shipment in full'); END;", NO_RAISE)]


def immutable(table, verb):
    """Disables one of a table's no-update / no-delete triggers, which share a message."""
    head = f"BEFORE {verb} ON {table}\nBEGIN SELECT RAISE(ABORT, '{table} rows are immutable'); END;"
    return S, [(head, f"BEFORE {verb} ON {table} WHEN 0\n" + NO_RAISE)]

# (label, file, [(old, new), ...], [file, [(old, new), ...]] ...). Each old string must occur exactly once.
MUTATIONS = [
    ("UNIQUE key on stock_commands", S, [("    UNIQUE (business_id, idempotency_key)\n);", "    id2 INTEGER\n);")]),
    ("UNIQUE key on stock_movements", S, [("    UNIQUE (business_id, idempotency_key),\n    FOREIGN KEY (business_id, reservation_id)",
                                           "    FOREIGN KEY (business_id, reservation_id)")]),
    ("UNIQUE key on stock_reservations", S, [("    UNIQUE (business_id, idempotency_key),\n    UNIQUE (business_id, id),",
                                              "    UNIQUE (business_id, id),")]),
    ("UNIQUE (business_id, id) on stock_reservations", S, [("    UNIQUE (business_id, id),                -- the parent key for stock_movements'",
                                                            "                -- the parent key for stock_movements'")]),
    ("non-empty key CHECK on stock_commands", S, [("idempotency_key TEXT    NOT NULL CHECK (idempotency_key <> ''),\n    command",
                                                   "idempotency_key TEXT    NOT NULL,\n    command")]),
    ("non-empty key CHECK on stock_movements", S, [("CHECK (source_ref <> ''),\n    idempotency_key TEXT    NOT NULL CHECK (idempotency_key <> ''),",
                                                    "CHECK (source_ref <> ''),\n    idempotency_key TEXT    NOT NULL,")]),
    ("non-empty key CHECK on stock_reservations", S, [("idempotency_key  TEXT    NOT NULL CHECK (idempotency_key <> ''),",
                                                       "idempotency_key  TEXT    NOT NULL,")]),
    ("command name CHECK", S, [("CHECK (command IN (", "CHECK (1 OR command IN (")]),
    ("non-empty source_ref CHECK", S, [(" CHECK (source_ref <> '')", "")]),
    ("non-empty order_ref CHECK", S, [(" CHECK (order_ref <> '')", "")]),
    ("movement sign/type CHECK", S, [(",\n    CHECK ((movement_type IN ('receipt', 'opening') AND quantity > 0) OR (movement_type = 'shipment' AND quantity < 0))", "")]),
    ("shipment has a reservation, receipt has none CHECK", S, [(",\n    CHECK ((movement_type = 'shipment') = (reservation_id IS NOT NULL))", "")]),
    ("business-scoped FOREIGN KEY to the reservation", S,
     [("    FOREIGN KEY (business_id, reservation_id) REFERENCES stock_reservations (business_id, id),\n", "")]),
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

    # ------------------------------------------------ v2: lots and expiry, database
    ("lot tracking PRIMARY KEY", S, [("    PRIMARY KEY (business_id, item_id)\n", "    x INTEGER\n")]),
    ("lot tracking only before any stock trigger", S, [("BEGIN SELECT RAISE(ABORT, 'item already has stock without lots'); END;", NO_RAISE)]),
    ("stock_lot_tracking no UPDATE trigger", *immutable("stock_lot_tracking", "UPDATE")),
    ("stock_lot_tracking no DELETE trigger", *immutable("stock_lot_tracking", "DELETE")),
    ("lot number UNIQUE per business and item", S, [("    UNIQUE (business_id, item_id, lot_number),\n", "")]),
    ("UNIQUE (business_id, id) on stock_lots", S, [("    UNIQUE (business_id, id),                -- the parent key for stock_lot_holds\n", "")]),
    ("UNIQUE (business_id, item_id, id) on stock_lots", S, [("    UNIQUE (business_id, item_id, id),       -- the parent key for movements",
                                                             "    -- the parent key for movements")]),
    ("lots only for tracked items FOREIGN KEY", S,
     [("    FOREIGN KEY (business_id, item_id) REFERENCES stock_lot_tracking (business_id, item_id)\n", "    CHECK (1)\n")]),
    ("non-empty lot_number CHECK", S, [(" CHECK (lot_number <> '')", "")]),
    ("expiry_date is a date CHECK", S, [(" CHECK (expiry_date IS date(expiry_date))", "")]),
    ("stock_lots no UPDATE trigger", *immutable("stock_lots", "UPDATE")),
    ("stock_lots no DELETE trigger", *immutable("stock_lots", "DELETE")),
    ("hold event values CHECK", S, [(" CHECK (event IN ('hold', 'release'))", "")]),
    ("non-empty hold reason CHECK", S, [(" CHECK (reason <> '')", "")]),
    ("non-empty key CHECK on stock_lot_holds", S, [("CHECK (reason <> ''),\n    idempotency_key TEXT    NOT NULL CHECK (idempotency_key <> ''),",
                                                    "CHECK (reason <> ''),\n    idempotency_key TEXT    NOT NULL,")]),
    ("UNIQUE key on stock_lot_holds", S, [("    UNIQUE (business_id, idempotency_key),\n    FOREIGN KEY (business_id, lot_id)",
                                           "    FOREIGN KEY (business_id, lot_id)")]),
    ("hold's business-scoped FOREIGN KEY to its lot", S, [(",\n    FOREIGN KEY (business_id, lot_id) REFERENCES stock_lots (business_id, id)", "")]),
    ("holds alternate trigger", S, [("BEGIN SELECT RAISE(ABORT, 'a lot is held and released in turn'); END;", NO_RAISE)]),
    ("stock_lot_holds no UPDATE trigger", *immutable("stock_lot_holds", "UPDATE")),
    ("stock_lot_holds no DELETE trigger", *immutable("stock_lot_holds", "DELETE")),
    ("allocation quantity > 0 CHECK", S, [("quantity       INTEGER NOT NULL CHECK (quantity > 0),", "quantity       INTEGER NOT NULL,")]),
    ("allocation as_of is a date CHECK", S, [("as_of          TEXT    NOT NULL CHECK (as_of IS date(as_of)),", "as_of          TEXT    NOT NULL,")]),
    ("one allocation per reservation and lot UNIQUE", S, [("    UNIQUE (business_id, reservation_id, lot_id),", "    ")]),
    ("allocation FOREIGN KEY to its reservation", S,
     [("    FOREIGN KEY (business_id, reservation_id) REFERENCES stock_reservations (business_id, id) DEFERRABLE INITIALLY DEFERRED,\n", "")]),
    ("that FOREIGN KEY made immediate instead of deferred", S,
     [("REFERENCES stock_reservations (business_id, id) DEFERRABLE INITIALLY DEFERRED", "REFERENCES stock_reservations (business_id, id)")]),
    ("allocation FOREIGN KEY to its lot and item", S,
     [(",\n    FOREIGN KEY (business_id, item_id, lot_id) REFERENCES stock_lots (business_id, item_id, id)\n);", "\n);")]),
    ("pending allocations count against the lot (in the view)", S, [("(r.id IS NULL OR r.status = 'active')", "r.status = 'active'")]),
    ("allocations before their reservation trigger", S,
     [("BEGIN SELECT RAISE(ABORT, 'allocations are written before their reservation'); END;", NO_RAISE)]),
    ("no allocation from a held lot trigger", S, [("BEGIN SELECT RAISE(ABORT, 'cannot allocate from a held lot'); END;", NO_RAISE)]),
    ("no allocation from an expired lot trigger", S, [("BEGIN SELECT RAISE(ABORT, 'cannot allocate from an expired lot'); END;", NO_RAISE)]),
    ("allocation within the lot's free stock trigger", S, [(WITHIN_LOT, NO_RAISE)]),
    ("stock_allocations no UPDATE trigger", *immutable("stock_allocations", "UPDATE")),
    ("stock_allocations no DELETE trigger", *immutable("stock_allocations", "DELETE")),
    ("allocations match the reservation trigger", S,
     [("BEGIN SELECT RAISE(ABORT, 'allocations must match the reservation''s item and quantity'); END;", NO_RAISE)]),
    ("the item part of that trigger", S, [("\n  OR EXISTS (SELECT 1 FROM stock_allocations\n"
                                           "              WHERE business_id = NEW.business_id AND reservation_id = NEW.id AND item_id <> NEW.item_id)", "")]),
    ("movement FOREIGN KEY to its lot and item", S,
     [("    FOREIGN KEY (business_id, item_id, lot_id) REFERENCES stock_lots (business_id, item_id, id),\n", "")]),
    ("lot shipment FOREIGN KEY to its allocation", S,
     [("    FOREIGN KEY (business_id, reservation_id, lot_id) REFERENCES stock_allocations (business_id, reservation_id, lot_id),\n", "")]),
    ("lot shipment carries as_of CHECK", S, [(",\n    CHECK (movement_type <> 'shipment' OR lot_id IS NULL OR as_of IS NOT NULL)", "")]),
    ("movement as_of is a date CHECK", S, [("as_of           TEXT    CHECK (as_of IS date(as_of)),", "as_of           TEXT,")]),
    ("movements of a tracked item need a lot trigger", S, [("BEGIN SELECT RAISE(ABORT, 'movements of a lot-tracked item need a lot'); END;", NO_RAISE)]),
    ("shipment within its allocation trigger", S, [("BEGIN SELECT RAISE(ABORT, 'shipment exceeds its allocation'); END;", NO_RAISE)]),
    ("no shipment from a held lot trigger", S, [("BEGIN SELECT RAISE(ABORT, 'cannot ship from a held lot'); END;", NO_RAISE)]),
    ("no shipment from an expired lot trigger", S, [("BEGIN SELECT RAISE(ABORT, 'cannot ship from an expired lot'); END;", NO_RAISE)]),
    ("lot on hand not negative trigger", S, [("BEGIN SELECT RAISE(ABORT, 'lot on hand would go negative'); END;", NO_RAISE)]),
    # ------------------------------------------------ v2: lots and expiry, app
    ("track_lots refuses an item with stock", A, [('raise InvalidState(f"item {item_id} already has stock without lots")', "pass")]),
    ("track_lots on a tracked item changes nothing", A, [("        if not _lot_tracked(conn, business_id, item_id):\n            if conn.execute",
                                                         "        if True:\n            if conn.execute")]),
    ("receive: a tracked item needs a lot", A, [("if tracked and lot_number is None:", "if False:")]),
    ("receive: an item without lots takes none", A, [("if not tracked and lot_number is not None:", "if False:")]),
    ("receive: expiry must match the lot's", A, [('if lot["expiry_date"] != expiry_date:', "if False:")]),
    ("reserve takes the lot path for tracked items", A, [("        if _lot_tracked(conn, business_id, item_id):\n            return _reserve_lots",
                                                         "        if False:\n            return _reserve_lots")]),
    ("earliest expiry first", A, [("ORDER BY expiry_date IS NULL, expiry_date, lot_id", "ORDER BY lot_id")]),
    ("lots without expiry last", A, [("ORDER BY expiry_date IS NULL, expiry_date, lot_id", "ORDER BY expiry_date, lot_id")]),
    ("reserve passes over held lots", A, [('lot["usable"] = not lot["on_hold"] and not lot["expired"]', 'lot["usable"] = not lot["expired"]')]),
    ("reserve passes over expired lots", A, [('not lot["on_hold"] and not lot["expired"] and', 'not lot["on_hold"] and')]),
    ("a lot is usable on its expiry date", A, [("return expiry_date is not None and as_of > expiry_date",
                                                "return expiry_date is not None and as_of >= expiry_date")]),
    ("reserve needs as_of for a tracked item", A, [('raise ValueError(f"item {item_id} is lot-tracked: reserve needs as_of")', "pass")]),
    ("ship takes the lot path for tracked items", A, [('lots_shipped = None\n        if _lot_tracked(conn, business_id, r["item_id"]):',
                                                      "lots_shipped = None\n        if False:")]),
    ("ship passes over held and expired lots", A, [('if a["on_hold"] or _expired(a["expiry_date"], as_of):', "if False:")]),
    ("ship rejects what usable allocations can't cover", A, [("    if need:\n        raise LotUnavailable", "    if False:\n        raise LotUnavailable")]),
    ("ship needs as_of for a tracked item", A, [("raise ValueError(f\"item {r['item_id']} is lot-tracked: ship needs as_of\")", "pass")]),
    ("hold state check", A, [('if lot["on_hold"] == (event == "hold"):', "if False:")]),
    ("business filter on lot lookup", A, [('FROM stock_lot_levels WHERE lot_id = ? AND business_id = ?"',
                                           'FROM stock_lot_levels WHERE lot_id = ? AND ? > 0"')]),
    ("business filter on lots()", A, [("FROM stock_lot_levels WHERE business_id = ? AND item_id = ?",
                                       "FROM stock_lot_levels WHERE ? > 0 AND item_id = ?")]),
    ("date validation", A, [("def _date(name, value):\n", "def _date(name, value):\n    return\n")]),
    ("lots read outside the write lock", A, SPLIT_LOT_RESERVE),
    ("lots read outside the write lock AND no within-lot trigger", A, SPLIT_LOT_RESERVE, S, [(WITHIN_LOT, NO_RAISE)]),

    # ------------------------------------------------ v3: opening stock and shipments, database
    ("opening stock first trigger", S, [("BEGIN SELECT RAISE(ABORT, 'opening stock comes before any other movement of the item'); END;", NO_RAISE)]),
    ("non-empty shipment_number CHECK", S, [(" CHECK (shipment_number <> '')", "")]),
    ("non-empty key CHECK on stock_shipments", S, [("CHECK (shipment_number <> ''),\n    idempotency_key TEXT    NOT NULL CHECK (idempotency_key <> ''),",
                                                    "CHECK (shipment_number <> ''),\n    idempotency_key TEXT    NOT NULL,")]),
    ("shipment number UNIQUE per business", S, [("    UNIQUE (business_id, shipment_number),\n", "")]),
    ("UNIQUE key on stock_shipments", S, [("    UNIQUE (business_id, idempotency_key),\n    UNIQUE (business_id, id)                 -- the parent key for lines",
                                           "    UNIQUE (business_id, id)                 -- the parent key for lines")]),
    ("UNIQUE (business_id, id) on stock_shipments", S, [(",\n    UNIQUE (business_id, id)                 -- the parent key for lines and the dispatch",
                                                         "                 -- the parent key for lines and the dispatch")]),
    ("stock_shipments no UPDATE trigger", *immutable("stock_shipments", "UPDATE")),
    ("stock_shipments no DELETE trigger", *immutable("stock_shipments", "DELETE")),
    ("line quantity > 0 CHECK", S, [("quantity        INTEGER NOT NULL CHECK (quantity > 0),", "quantity        INTEGER NOT NULL,")]),
    ("non-empty key CHECK on stock_shipment_lines", S, [("CHECK (quantity > 0),\n    idempotency_key TEXT    NOT NULL CHECK (idempotency_key <> ''),",
                                                         "CHECK (quantity > 0),\n    idempotency_key TEXT    NOT NULL,")]),
    ("a reservation once per shipment UNIQUE", S, [("    UNIQUE (business_id, shipment_id, reservation_id),", "   ")]),
    ("UNIQUE parent key on stock_shipment_lines", S, [("    UNIQUE (business_id, shipment_id, id, reservation_id),", "   ")]),
    ("UNIQUE key on stock_shipment_lines", S, [("    UNIQUE (business_id, idempotency_key),\n    FOREIGN KEY (business_id, shipment_id) REFERENCES stock_shipments",
                                                "    FOREIGN KEY (business_id, shipment_id) REFERENCES stock_shipments")]),
    ("line FOREIGN KEY to its shipment", S, [("    FOREIGN KEY (business_id, shipment_id) REFERENCES stock_shipments (business_id, id),\n", "")]),
    ("line FOREIGN KEY to its reservation", S,
     [(",\n    FOREIGN KEY (business_id, reservation_id) REFERENCES stock_reservations (business_id, id)\n);", "\n);")]),
    ("lines only before dispatch trigger", S, [("BEGIN SELECT RAISE(ABORT, 'lines are added before dispatch'); END;", NO_RAISE)]),
    ("stock_shipment_lines no UPDATE trigger", *immutable("stock_shipment_lines", "UPDATE")),
    ("stock_shipment_lines no DELETE trigger", *immutable("stock_shipment_lines", "DELETE")),
    ("non-empty carrier CHECK", S, [(" CHECK (carrier <> '')", "")]),
    ("non-empty label_ref CHECK", S, [(" CHECK (label_ref <> '')", "")]),
    ("non-empty tracking_ref CHECK", S, [(" CHECK (tracking_ref <> '')", "")]),
    ("label or tracking reference CHECK", S, [(",\n    CHECK (label_ref IS NOT NULL OR tracking_ref IS NOT NULL)", "")]),
    ("dispatch as_of is a date CHECK", S, [("NOT NULL CHECK (as_of IS date(as_of)),   -- the dispatch date", "NOT NULL,   -- the dispatch date")]),
    ("non-empty key CHECK on stock_shipment_dispatches", S,
     [("-- the dispatch date\n    idempotency_key TEXT    NOT NULL CHECK (idempotency_key <> ''),",
       "-- the dispatch date\n    idempotency_key TEXT    NOT NULL,")]),
    ("dispatch once PRIMARY KEY (and the foreign keys that use it)", S, DISPATCH_PK),
    ("UNIQUE key on stock_shipment_dispatches", S, [("    UNIQUE (business_id, idempotency_key),\n    CHECK (label_ref", "    CHECK (label_ref")]),
    ("dispatch ships every line in full trigger", S,
     [("BEGIN SELECT RAISE(ABORT, 'a dispatch ships every line of its shipment in full'); END;", NO_RAISE)]),
    ("the no-lines part of that trigger", S,
     [("WHEN NOT EXISTS (SELECT 1 FROM stock_shipment_lines WHERE business_id = NEW.business_id AND shipment_id = NEW.shipment_id)\n  OR EXISTS",
       "WHEN EXISTS")]),
    ("stock_shipment_dispatches no UPDATE trigger", *immutable("stock_shipment_dispatches", "UPDATE")),
    ("stock_shipment_dispatches no DELETE trigger", *immutable("stock_shipment_dispatches", "DELETE")),
    ("tracking event values CHECK", S, [(" CHECK (event IN ('in_transit', 'out_for_delivery', 'delivery_failed',\n"
                                         "                                                      'delivered', 'exception'))", "")]),
    ("occurred_at is a UTC time CHECK", S, [(" CHECK (occurred_at IS strftime('%Y-%m-%dT%H:%M:%SZ', occurred_at))", "")]),
    ("non-empty key CHECK on stock_shipment_events", S,
     [("    detail          TEXT,\n    idempotency_key TEXT    NOT NULL CHECK (idempotency_key <> ''),",
       "    detail          TEXT,\n    idempotency_key TEXT    NOT NULL,")]),
    ("UNIQUE key on stock_shipment_events", S, [("    UNIQUE (business_id, idempotency_key),\n    FOREIGN KEY (business_id, shipment_id) REFERENCES stock_shipment_dispatches",
                                                 "    FOREIGN KEY (business_id, shipment_id) REFERENCES stock_shipment_dispatches")]),
    ("events only for a dispatched shipment FOREIGN KEY", S,
     [(",\n    FOREIGN KEY (business_id, shipment_id) REFERENCES stock_shipment_dispatches (business_id, shipment_id)\n);", "\n);")]),
    ("stock_shipment_events no UPDATE trigger", *immutable("stock_shipment_events", "UPDATE")),
    ("stock_shipment_events no DELETE trigger", *immutable("stock_shipment_events", "DELETE")),
    ("shipment movement FOREIGN KEY to its line", S,
     [("    FOREIGN KEY (business_id, shipment_id, shipment_line_id, reservation_id)\n"
       "        REFERENCES stock_shipment_lines (business_id, shipment_id, id, reservation_id),\n", "")]),
    ("shipment movement FOREIGN KEY to its dispatch", S,
     [("    FOREIGN KEY (business_id, shipment_id) REFERENCES stock_shipment_dispatches (business_id, shipment_id)\n"
       "        DEFERRABLE INITIALLY DEFERRED,\n", "")]),
    ("the dispatch FOREIGN KEY made immediate instead of deferred", S,
     [("REFERENCES stock_shipment_dispatches (business_id, shipment_id)\n        DEFERRABLE INITIALLY DEFERRED,",
       "REFERENCES stock_shipment_dispatches (business_id, shipment_id),")]),
    ("shipment and line set together CHECK", S, [(",\n    CHECK ((shipment_id IS NULL) = (shipment_line_id IS NULL))", "")]),
    ("only shipments carry a line CHECK", S, [(",\n    CHECK (shipment_line_id IS NULL OR movement_type = 'shipment')", "")]),
    ("shipment movements before their dispatch trigger", S,
     [("BEGIN SELECT RAISE(ABORT, 'shipment movements are written before their dispatch'); END;", NO_RAISE)]),
    ("delivered stays delivered (status from the latest event instead)", S,
     [("AND e.shipment_id = s.id AND e.event = 'delivered')",
       "AND e.shipment_id = s.id AND e.event = 'delivered'\n                            AND e.id = (SELECT MAX(x.id) FROM stock_shipment_events x"
       " WHERE x.business_id = s.business_id AND x.shipment_id = s.id))")]),
    # ------------------------------------------------ v3: opening stock and shipments, app
    ("open_stock refuses an item with receipts", A,
     [('raise InvalidState(f"item {item_id} already has receipts or shipments: opening stock comes first")', "pass")]),
    ("receive_and_reserve: the lot must be usable", A, [('if lot["on_hold"] or _expired(lot["expiry_date"], as_of):', "if False:")]),
    ("receive_and_reserve needs as_of for a tracked item", A,
     [('raise ValueError(f"item {item_id} is lot-tracked: receive_and_reserve needs as_of")', "pass")]),
    ("reserve_quantity at most what was received", A, [("        if reserve_quantity > quantity:\n            raise ValueError",
                                                        "        if False:\n            raise ValueError")]),
    ("create_shipment: number not taken", A, [("        if taken:\n", "        if False:\n")]),
    ("add_shipment_line: draft only", A, [("raise InvalidState(f\"shipment {shipment_id} is {shp['status']}: lines are added before dispatch\")", "pass")]),
    ("add_shipment_line: a reservation once per shipment", A,
     [('if any(line["reservation_id"] == reservation_id for line in shp["lines"]):', "if False:")]),
    ("add_shipment_line: not more than is left to ship", A, [("        if quantity > left:\n", "        if False:\n")]),
    ("dispatch: draft only", A, [("raise InvalidState(f\"shipment {shipment_id} is already {shp['status']}\")", "pass")]),
    ("dispatch: needs lines", A, [('        if not shp["lines"]:\n', "        if False:\n")]),
    ("dispatch: each line within its reservation", A, [('if line["quantity"] > remaining:', "if False:")]),
    ("dispatch takes the lot path for tracked items", A, [('            if _lot_tracked(conn, business_id, r["item_id"]):\n                _ship_lots',
                                                           "            if False:\n                _ship_lots")]),
    ("dispatch: label or tracking reference", A, [("    if label_ref is None and tracking_ref is None:\n", "    if False:\n")]),
    ("tracking events only after dispatch", A, [('if shipment(conn, business_id, shipment_id)["status"] == "draft":', "if False:")]),
    ("tracking event values", A, [("    if event not in TRACKING_EVENTS:\n", "    if False:\n")]),
    ("timestamp validation", A, [("def _timestamp(name, value):\n", "def _timestamp(name, value):\n    return\n")]),
    ("business filter on shipment()", A, [('"SELECT * FROM stock_shipment_status WHERE shipment_id = ? AND business_id = ?"',
                                           '"SELECT * FROM stock_shipment_status WHERE shipment_id = ? AND ? > 0"')]),
    ("shipment status read outside the write lock", A, SPLIT_DISPATCH),
    ("status read outside the lock AND no once-only protections", A, SPLIT_DISPATCH, S, DISPATCH_ONCE_RULES),
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


def report(i: int, label: str, fails: list[str]) -> str:
    if not fails:
        outcome = "SURVIVED"
    elif all(WRONG_REASON in f for f in fails):
        outcome = "redundant"
    else:
        outcome = "caught"
    print(f"\n[{i:3}] {outcome:9} {label}", flush=True)
    for f in fails:
        print("      ", f[len("FAIL "):])
    return outcome


def main() -> int:
    with tempfile.TemporaryDirectory(prefix="stock_mutation_", ignore_cleanup_errors=True) as tmp:
        base = Path(tmp) / "base"
        shutil.copytree(SRC, base, ignore=shutil.ignore_patterns("__pycache__"))
        baseline = run(base)
        print("unchanged copy:", "all tests pass" if not baseline else baseline)
        folders = []
        for i, (label, *edits) in enumerate(MUTATIONS, 1):
            folder = Path(tmp) / f"m{i}"
            shutil.copytree(base, folder)
            apply(folder, label, edits)
            folders.append(folder)
        outcomes = {}
        with ThreadPoolExecutor(JOBS) as pool:
            results = pool.map(run, folders)                        # in order, so the report reads top to bottom
            for i, ((label, *_), fails) in enumerate(zip(MUTATIONS, results), 1):
                outcomes[label] = report(i, label, fails)
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
