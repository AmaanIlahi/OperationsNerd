import importlib.util
import json
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
        a = d.create_business(c, "A", "realestate", "1", {})
        b = d.create_business(c, "B", "healthclub", "1", {})
        ca = d.create_contact(c, a, "A")
        cb = d.create_contact(c, b, "B")
        ea = d.create_event(c, a, "email", "A", ca)
        eb = d.create_event(c, b, "email", "B", cb)
        rejected = 0
        for fn in [
            lambda: d.create_follow_up(c, a, cb, "call"),
            lambda: d.create_event(c, a, "email", "wrong", cb),
            lambda: d.create_drafted_action(c, eb, a, "email", {}),
            lambda: d.create_drafted_action(c, ea, a, "email", {}, cb),
        ]:
            try:
                fn()
            except ValueError:
                rejected += 1
        f = d.create_follow_up(c, a, ca, "call")
        e = d.create_event(c, a, "email", "valid", ca)
        n = d.create_event(c, a, "form", "no contact")
        ac = d.create_drafted_action(c, e, a, "email", {}, ca)
        good = d.get_drafted_action(c, ac)["business_id"] == a and n is not None
    print(
        json.dumps(
            dict(cross_business_rejected=rejected, valid_chain=good), sort_keys=True
        )
    )

assert {"cross_business_rejected": 4, "valid_chain": True} == dict(
    cross_business_rejected=rejected, valid_chain=good
)
