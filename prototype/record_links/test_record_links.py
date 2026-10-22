"""
Proves the record_links design (migration.sql) against the current schema.
Every insert is raw SQL that bypasses records.py, so whatever is rejected
here is rejected by the database itself. Each test gets its own throwaway
database under a temp directory. Run from the repo root with the venv:

    python prototype/record_links/test_record_links.py
"""

import hashlib
import json
import os
import sqlite3
import sys
import tempfile
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import migrate_links as m

SCHEMA = HERE.parents[1] / "operations_nerd" / "db" / "schema.sql"
TMP = tempfile.TemporaryDirectory(prefix="record_links_", ignore_cleanup_errors=True)

WORKS_AT = "l_works_at"      # crm_contact -> crm_account, many_to_one
CONVERTED = "converted"      # crm_lead -> crm_contact, one_to_one; no l_ prefix on purpose
LINK_ERROR = "cardinality or types differ"


def new_db(name: str) -> tuple[sqlite3.Connection, str]:
    path = os.path.join(TMP.name, name + ".db")
    conn = sqlite3.connect(path, isolation_level=None)
    conn.execute("PRAGMA foreign_keys = ON")
    conn.executescript(SCHEMA.read_text())
    assert conn.execute("PRAGMA foreign_keys").fetchone()[0] == 1
    return conn, path


def business(conn, name: str) -> int:
    return conn.execute("INSERT INTO businesses (name, industry_pack, pack_version) VALUES (?, 'crm', '1')",
                        (name,)).lastrowid


def entity(conn, business_id: int, entity_type: str) -> int:
    return conn.execute("INSERT INTO entities (business_id, entity_type) VALUES (?, ?)",
                        (business_id, entity_type)).lastrowid


def link(conn, business_id, link_key, cardinality, from_id, from_type, to_id, to_type) -> int:
    return conn.execute(
        """INSERT INTO record_links (business_id, link_key, cardinality, from_id, from_type, to_id, to_type)
           VALUES (?, ?, ?, ?, ?, ?, ?)""",
        (business_id, link_key, cardinality, from_id, from_type, to_id, to_type),
    ).lastrowid


def works_at(conn, b, contact_id, account_id, **override):
    args = {"cardinality": "many_to_one", "from_type": "crm_contact", "to_type": "crm_account", **override}
    return link(conn, b, WORKS_AT, args["cardinality"], contact_id, args["from_type"], account_id, args["to_type"])


def converted(conn, b, lead_id, contact_id):
    return link(conn, b, CONVERTED, "one_to_one", lead_id, "crm_lead", contact_id, "crm_contact")


def rejected(fn, expected: str) -> str:
    try:
        fn()
    except sqlite3.IntegrityError as e:
        assert expected in str(e), f"rejected for the wrong reason: {e}"
        return str(e)
    raise AssertionError(f"accepted, expected rejection containing {expected!r}")


def linked_db(name: str):
    """A migrated database with two businesses and a few records in each."""
    conn, _ = new_db(name)
    m.apply_migration(conn)
    b, other = business(conn, "Acme"), business(conn, "Other Co")
    r = {
        "b": b, "other": other,
        "acct1": entity(conn, b, "crm_account"), "acct2": entity(conn, b, "crm_account"),
        "c1": entity(conn, b, "crm_contact"), "c2": entity(conn, b, "crm_contact"),
        "c3": entity(conn, b, "crm_contact"),
        "lead1": entity(conn, b, "crm_lead"), "lead2": entity(conn, b, "crm_lead"),
        "other_acct": entity(conn, other, "crm_account"), "other_c": entity(conn, other, "crm_contact"),
    }
    return conn, r


def count(conn) -> int:
    return conn.execute("SELECT COUNT(*) FROM record_links").fetchone()[0]


# ---- rejected by the database ---------------------------------------------

def test_rejects_missing_target():
    conn, r = linked_db("missing_target")
    rejected(lambda: works_at(conn, r["b"], r["c1"], 9999), "FOREIGN KEY")
    assert count(conn) == 0


def test_rejects_wrong_target_type():
    conn, r = linked_db("wrong_target_type")
    # lead1 is a crm_lead; the row claims it is a crm_account
    rejected(lambda: works_at(conn, r["b"], r["c1"], r["lead1"]), "FOREIGN KEY")


def test_rejects_wrong_source_type():
    conn, r = linked_db("wrong_source_type")
    # lead1 is a crm_lead; the row claims it is a crm_contact
    rejected(lambda: works_at(conn, r["b"], r["lead1"], r["acct1"]), "FOREIGN KEY")


def test_rejects_target_in_another_business():
    conn, r = linked_db("cross_business")
    rejected(lambda: works_at(conn, r["b"], r["c1"], r["other_acct"]), "FOREIGN KEY")
    rejected(lambda: works_at(conn, r["b"], r["other_c"], r["acct1"]), "FOREIGN KEY")   # source side too


def test_rejects_second_value_for_same_record_and_link():
    conn, r = linked_db("second_value")
    works_at(conn, r["b"], r["c1"], r["acct1"])
    rejected(lambda: works_at(conn, r["b"], r["c1"], r["acct2"]),
             "UNIQUE constraint failed: record_links.business_id, record_links.link_key, record_links.from_id")


def test_rejects_taken_one_to_one_target():
    conn, r = linked_db("one_to_one_taken")
    converted(conn, r["b"], r["lead1"], r["c1"])
    rejected(lambda: converted(conn, r["b"], r["lead2"], r["c1"]),
             "UNIQUE constraint failed: record_links.business_id, record_links.link_key, record_links.to_id")


def test_rejects_cardinality_mismatch_within_link():
    conn, r = linked_db("cardinality_mismatch")
    works_at(conn, r["b"], r["c1"], r["acct1"])
    rejected(lambda: works_at(conn, r["b"], r["c2"], r["acct2"], cardinality="one_to_one"), LINK_ERROR)


def test_rejects_from_type_mismatch_within_link():
    conn, r = linked_db("from_type_mismatch")
    works_at(conn, r["b"], r["c1"], r["acct1"])
    # an honest row (lead1 really is a crm_lead), but this link is from crm_contact
    rejected(lambda: works_at(conn, r["b"], r["lead1"], r["acct1"], from_type="crm_lead"), LINK_ERROR)


def test_rejects_to_type_mismatch_within_link():
    conn, r = linked_db("to_type_mismatch")
    works_at(conn, r["b"], r["c1"], r["acct1"])
    rejected(lambda: works_at(conn, r["b"], r["c2"], r["lead1"], to_type="crm_lead"), LINK_ERROR)


def test_rejects_update_that_breaks_link_consistency():
    conn, r = linked_db("update_mismatch")
    works_at(conn, r["b"], r["c1"], r["acct1"])
    row = works_at(conn, r["b"], r["c2"], r["acct1"])
    rejected(lambda: conn.execute("UPDATE record_links SET cardinality = 'one_to_one' WHERE id = ?", (row,)),
             LINK_ERROR)


def test_rejects_cardinality_that_is_not_writable():
    conn, r = linked_db("not_writable")
    rejected(lambda: works_at(conn, r["b"], r["c1"], r["acct1"], cardinality="one_to_many"), "CHECK")


def test_rejects_null_type():
    conn, r = linked_db("null_type")
    # without NOT NULL, a NULL in the key would skip the foreign key check entirely
    rejected(lambda: works_at(conn, r["b"], r["c1"], 9999, to_type=None), "NOT NULL")


def test_blocks_deleting_a_linked_record():
    conn, r = linked_db("delete_linked")
    works_at(conn, r["b"], r["c1"], r["acct1"])
    rejected(lambda: conn.execute("DELETE FROM entities WHERE id = ?", (r["acct1"],)), "FOREIGN KEY")
    conn.execute("DELETE FROM entities WHERE id = ?", (r["acct2"],))    # unlinked: fine


# ---- accepted -------------------------------------------------------------

def test_accepts_valid_many_to_one():
    conn, r = linked_db("valid_many_to_one")
    works_at(conn, r["b"], r["c1"], r["acct1"])
    assert count(conn) == 1


def test_accepts_two_records_sharing_many_to_one_target():
    conn, r = linked_db("shared_target")
    works_at(conn, r["b"], r["c1"], r["acct1"])
    works_at(conn, r["b"], r["c2"], r["acct1"])
    assert count(conn) == 2


def test_accepts_valid_one_to_one():
    conn, r = linked_db("valid_one_to_one")
    converted(conn, r["b"], r["lead1"], r["c1"])
    converted(conn, r["b"], r["lead2"], r["c2"])
    assert count(conn) == 2


def test_accepts_one_to_one_target_after_old_row_deleted():
    conn, r = linked_db("one_to_one_reuse")
    old = converted(conn, r["b"], r["lead1"], r["c1"])
    conn.execute("DELETE FROM record_links WHERE id = ?", (old,))
    converted(conn, r["b"], r["lead2"], r["c1"])
    assert count(conn) == 1


def test_limit_accepts_link_key_not_in_spec():
    # The database knows records, not the spec: the first row of any link key
    # is accepted as long as its records are real, and it sets that key's types.
    conn, r = linked_db("not_in_spec")
    link(conn, r["b"], "l_not_in_spec", "many_to_one", r["acct1"], "crm_account", r["lead1"], "crm_lead")
    assert count(conn) == 1


def test_limit_trigger_blocks_changing_a_whole_links_cardinality():
    # The trigger fires per row, so the first row updated disagrees with the
    # rows not yet updated. Changing a link's cardinality (no spec operation
    # does this today) would need delete and reinsert instead of UPDATE.
    conn, r = linked_db("bulk_change")
    works_at(conn, r["b"], r["c1"], r["acct1"])
    works_at(conn, r["b"], r["c2"], r["acct2"])
    rejected(lambda: conn.execute("UPDATE record_links SET cardinality = 'one_to_one' WHERE link_key = ?",
                                  (WORKS_AT,)), LINK_ERROR)


# ---- migration from EAV ---------------------------------------------------

def field(key: str) -> dict:
    return {"key": key, "label": key, "type": "text", "required": False, "archived": False}


def seed_eav(conn) -> tuple[dict, dict, dict, dict]:
    """Records and values written the way records.py writes them today:
    entity_attributes(entity key, record id, field or link key, json.dumps(value)),
    where a link value is the target record id stored on the link's `from` record.
    Business B uses keys without the crm_ / l_ prefixes to show nothing relies on them.
    Returns (specs, records, expected bad, expected undeclared), each expected
    map being {(entity_id, attribute_name): reason fragment}."""
    a, b = business(conn, "Acme"), business(conn, "Other Co")
    specs = {
        a: {"entities": [
                {"key": "crm_account", "label": "Account", "archived": False, "fields": [field("f_industry")]},
                {"key": "crm_contact", "label": "Contact", "archived": False, "fields": [field("f_name")]},
                {"key": "crm_lead", "label": "Lead", "archived": False, "fields": []},
            ],
            "links": [
                {"key": WORKS_AT, "label": "Works at", "from": "crm_contact", "to": "crm_account",
                 "cardinality": "many_to_one", "archived": False},
                {"key": CONVERTED, "label": "Converted to", "from": "crm_lead", "to": "crm_contact",
                 "cardinality": "one_to_one", "archived": False},
                {"key": "l_old_owner", "label": "Old owner", "from": "crm_contact", "to": "crm_account",
                 "cardinality": "many_to_one", "archived": True},
                {"key": "l_partners", "label": "Partners", "from": "crm_account", "to": "crm_account",
                 "cardinality": "many_to_many", "archived": False},
            ]},
        b: {"entities": [
                {"key": "company", "label": "Company", "archived": False, "fields": []},
                {"key": "person", "label": "Person", "archived": False, "fields": []},
            ],
            "links": [
                {"key": "employer", "label": "Employer", "from": "person", "to": "company",
                 "cardinality": "many_to_one", "archived": False},
            ]},
    }
    r = {"a": a, "b": b}
    for name, biz, entity_type in [
        ("acct1", a, "crm_account"), ("acct2", a, "crm_account"),
        ("c1", a, "crm_contact"), ("c2", a, "crm_contact"), ("c3", a, "crm_contact"),
        ("c4", a, "crm_contact"), ("c5", a, "crm_contact"), ("c6", a, "crm_contact"),
        ("lead1", a, "crm_lead"), ("lead2", a, "crm_lead"), ("lead3", a, "crm_lead"),
        ("b_company", b, "company"), ("b_person", b, "person"),
    ]:
        r[name] = entity(conn, biz, entity_type)
    # A legacy questionnaire contact: its EAV row's entity_id points at contacts, not entities.
    r["legacy"] = conn.execute("INSERT INTO contacts (business_id, name) VALUES (?, 'Jordan')", (a,)).lastrowid

    def put(entity_type, entity_id, key, value):
        conn.execute("INSERT INTO entity_attributes (entity_type, entity_id, attribute_name, value) VALUES (?, ?, ?, ?)",
                     (entity_type, entity_id, key, json.dumps(value)))

    # declared fields and legacy rows: left alone
    put("crm_contact", r["c1"], "f_name", "Ann")
    put("crm_account", r["acct1"], "f_industry", "Retail")
    put("contact", r["legacy"], "property_type", "condo")
    # good links
    put("crm_contact", r["c1"], WORKS_AT, r["acct1"])
    put("crm_contact", r["c2"], WORKS_AT, r["acct1"])          # shares c1's target
    put("crm_contact", r["c3"], WORKS_AT, r["acct2"])
    put("crm_lead", r["lead1"], CONVERTED, r["c1"])
    put("crm_contact", r["c4"], "l_old_owner", r["acct2"])     # archived link: still moves
    put("person", r["b_person"], "employer", r["b_company"])
    # bad links
    put("crm_contact", r["c4"], WORKS_AT, 9999)                # dangling target
    put("crm_lead", r["lead2"], CONVERTED, r["c2"])            # duplicate one_to_one target ...
    put("crm_lead", r["lead3"], CONVERTED, r["c2"])            # ... claimed twice
    put("crm_contact", r["c5"], WORKS_AT, r["lead1"])          # target is a lead, not an account
    put("crm_contact", r["c6"], WORKS_AT, r["b_company"])      # target in another business
    put("crm_contact", 9999, WORKS_AT, r["acct1"])             # source record gone
    # undeclared: neither a field of the record's entity nor a link in the spec
    put("crm_contact", r["c1"], "l_deadbeef", r["acct1"])      # looks like a link key, isn't one
    put("crm_contact", r["c3"], "f_industry", "Retail")        # an account field, on a contact
    expected_bad = {
        (r["c4"], WORKS_AT): "target record 9999 does not exist",
        (r["lead2"], CONVERTED): f"one_to_one target {r['c2']} is claimed by records",
        (r["lead3"], CONVERTED): f"one_to_one target {r['c2']} is claimed by records",
        (r["c5"], WORKS_AT): "is a crm_lead, the link expects crm_account",
        (r["c6"], WORKS_AT): "belongs to another business",
        (9999, WORKS_AT): "no crm_contact record with id 9999",
    }
    expected_undeclared = {
        (r["c1"], "l_deadbeef"): "not a field of crm_contact or a link in the spec",
        (r["c3"], "f_industry"): "not a field of crm_contact or a link in the spec",
    }
    return specs, r, expected_bad, expected_undeclared


def fingerprint(conn) -> str:
    return hashlib.sha256("\n".join(conn.iterdump()).encode()).hexdigest()


def read_only(path: str) -> sqlite3.Connection:
    return sqlite3.connect(Path(path).as_uri() + "?mode=ro", uri=True, isolation_level=None)


def reasons(rows: list[dict]) -> dict:
    return {(row["entity_id"], row["link_key"]): row["reason"] for row in rows}


def test_precheck_reports_every_bad_row_and_changes_nothing():
    conn, path = new_db("precheck")
    specs, r, expected_bad, expected_undeclared = seed_eav(conn)
    before = fingerprint(conn)
    ro = read_only(path)                     # a write here would raise
    report = m.precheck(ro, specs)
    ro.close()
    print("  pre-check report:")
    for category in ("bad", "undeclared"):
        for row in report[category]:
            print(f"    {category:<10}  business {row['business_id']}  {row['entity_type']} {row['entity_id']}  "
                  f"{row['link_key']} = {row['value']}: {row['reason']}")
    assert fingerprint(conn) == before
    for category, expected in (("bad", expected_bad), ("undeclared", expected_undeclared)):
        found = reasons(report[category])
        assert found.keys() == expected.keys(), (category, sorted(found))
        for key, fragment in expected.items():
            assert fragment in found[key], (key, found[key])
    assert len(report["good"]) == 6
    assert {(row["entity_id"], row["link_key"]) for row in report["fields"]} == {
        (r["c1"], "f_name"), (r["acct1"], "f_industry")}
    assert [(row["entity_type"], row["entity_id"]) for row in report["other"]] == [("contact", r["legacy"])]


def test_precheck_reports_records_outside_the_spec():
    conn, _ = new_db("outside_spec")
    specs, r, _, _ = seed_eav(conn)
    ghost = entity(conn, r["a"], "crm_ghost")     # an entity type the spec does not declare
    conn.execute("INSERT INTO entity_attributes VALUES ('crm_ghost', ?, 'f_x', '1')", (ghost,))
    del specs[r["b"]]                             # a business with records but no spec
    found = reasons(m.precheck(conn, specs)["undeclared"])
    assert "entity type crm_ghost is not in the spec" in found[(ghost, "f_x")]
    assert f"no spec for business {r['b']}" in found[(r["b_person"], "employer")]


def test_precheck_reports_other_link_problems():
    conn, _ = new_db("other_problems")
    specs, r, _, _ = seed_eav(conn)
    c7 = entity(conn, r["a"], "crm_contact")
    for entity_type, entity_id, key, value in [
        ("crm_lead", r["lead1"], WORKS_AT, r["acct1"]),        # a contact link stored on a lead
        ("crm_account", r["acct1"], "l_partners", r["acct2"]),  # many_to_many values are not stored on records
        ("crm_contact", c7, WORKS_AT, "Acme"),                  # not a record id
    ]:
        conn.execute("INSERT INTO entity_attributes VALUES (?, ?, ?, ?)", (entity_type, entity_id, key, json.dumps(value)))
    # once two specs declare crm_contact, the business of the gone record 9999 is unknown
    specs[r["b"]]["entities"].append({"key": "crm_contact", "label": "Contact", "archived": False, "fields": []})
    report = m.precheck(conn, specs)
    bad, undeclared = reasons(report["bad"]), reasons(report["undeclared"])
    assert "link belongs to crm_contact records, not crm_lead" in bad[(r["lead1"], WORKS_AT)]
    assert "many_to_many links are not stored on records" in bad[(r["acct1"], "l_partners")]
    assert "value is not a record id" in bad[(c7, WORKS_AT)]
    assert "its business is unknown" in undeclared[(9999, WORKS_AT)]


def test_migration_moves_good_rows_and_counts_match():
    conn, _ = new_db("migrate")
    specs, r, expected_bad, expected_undeclared = seed_eav(conn)
    eav_before = conn.execute("SELECT COUNT(*) FROM entity_attributes").fetchone()[0]
    m.apply_migration(conn)
    report = m.migrate(conn, specs)
    sizes = {c: len(report[c]) for c in m.CATEGORIES}
    migrated = conn.execute(
        "SELECT business_id, link_key, cardinality, from_id, from_type, to_id, to_type FROM record_links").fetchall()
    print(f"  EAV rows {eav_before} = " + " + ".join(f"{c} {n}" for c, n in sizes.items())
          + f"; migrated {len(migrated)}")
    assert sum(sizes.values()) == eav_before                  # every row lands in exactly one category
    assert sizes == {"good": 6, "bad": len(expected_bad), "undeclared": len(expected_undeclared),
                     "fields": 2, "other": 1}, sizes
    assert len(migrated) == sizes["good"]
    assert set(migrated) == {(g["business_id"], g["link_key"], g["cardinality"], g["entity_id"],
                              g["from_type"], g["to_id"], g["to_type"]) for g in report["good"]}
    per_link = dict(conn.execute("SELECT link_key, COUNT(*) FROM record_links GROUP BY link_key").fetchall())
    assert per_link == {WORKS_AT: 3, CONVERTED: 1, "l_old_owner": 1, "employer": 1}, per_link
    assert conn.execute("SELECT COUNT(*) FROM entity_attributes").fetchone()[0] == eav_before   # EAV untouched


def test_database_alone_would_let_some_bad_rows_in():
    # Force every bad link row, and every undeclared row holding a record id,
    # into record_links (spec types where the link is known) to see which ones
    # the database catches without the pre-check.
    conn, _ = new_db("forced")
    specs, r, _, _ = seed_eav(conn)
    m.apply_migration(conn)
    report = m.migrate(conn, specs)
    links = {l["key"]: l for l in specs[r["a"]]["links"]}
    candidates = report["bad"] + [row for row in report["undeclared"] if isinstance(json.loads(row["value"]), int)]
    outcomes = {}
    conn.execute("BEGIN")
    for row in candidates:
        target = json.loads(row["value"])
        known = links.get(row["link_key"])
        target_type = known["to"] if known else conn.execute(
            "SELECT entity_type FROM entities WHERE id = ?", (target,)).fetchone()[0]
        try:
            link(conn, r["a"], row["link_key"], known["cardinality"] if known else "many_to_one",
                 row["entity_id"], row["entity_type"], target, target_type)
            outcome = "ACCEPTED"
        except sqlite3.IntegrityError as e:
            outcome = f"rejected ({e})"
        outcomes[(row["entity_id"], row["link_key"])] = outcome
        print(f"    {row['reason']:<62} -> {outcome}")
    conn.execute("ROLLBACK")
    accepted = {k for k, v in outcomes.items() if v == "ACCEPTED"}
    # Only the unknown link key and the first of the two one_to_one claimants get in.
    assert accepted == {(r["c1"], "l_deadbeef"), (r["lead2"], CONVERTED)}, accepted
    assert "UNIQUE" in outcomes[(r["lead3"], CONVERTED)]


if __name__ == "__main__":
    print(f"SQLite {sqlite3.sqlite_version}, Python {sys.version.split()[0]}")
    tests = [(n, f) for n, f in globals().items() if n.startswith("test_") and callable(f)]
    for name, fn in tests:
        fn()
        print(f"PASS {name}")
    print(f"\n{len(tests)} tests passed")
