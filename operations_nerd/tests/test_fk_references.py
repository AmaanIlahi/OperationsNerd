import importlib.util
import json
import sqlite3
import tempfile
from pathlib import Path

spec = importlib.util.spec_from_file_location(
    "dbmod", str(Path(__file__).resolve().parents[1] / "db/db.py")
)
d = importlib.util.module_from_spec(spec)
spec.loader.exec_module(d)
with tempfile.TemporaryDirectory() as td:
    p = td + "/data.db"
    d.init_db(p)
    with d.get_conn(p) as c:
        enabled = c.execute("PRAGMA foreign_keys").fetchone()[0]
        rejected = 0
        for fn in [
            lambda: d.create_contact(c, 999, "bad"),
            lambda: d.create_event(c, 999, "email", "bad"),
            lambda: d.create_follow_up(c, 999, 999, "call"),
            lambda: d.create_drafted_action(c, 999, 999, "email", {}),
        ]:
            try:
                fn()
            except sqlite3.IntegrityError:
                rejected += 1
        b = d.create_business(c, "Good", "realestate", "1", {})
        contact = d.create_contact(c, b, "Good")
        e = d.create_event(c, b, "email", "good", contact)
        a = d.create_drafted_action(c, e, b, "email", {}, contact)
        good = d.get_drafted_action(c, a)["business_id"] == b
    print(
        json.dumps(
            dict(foreign_keys=enabled, invalid_rejected=rejected, valid_chain=good),
            sort_keys=True,
        )
    )

assert {"foreign_keys": 1, "invalid_rejected": 4, "valid_chain": True} == dict(
    foreign_keys=enabled, invalid_rejected=rejected, valid_chain=good
)
