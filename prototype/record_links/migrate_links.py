"""
Moves link values from entity_attributes into record_links.

records.py stores a many_to_one or one_to_one link value as one EAV row on
the source record: entity_type is the link's `from` entity key, entity_id the
source record id, attribute_name the link key, and value the target record id
as JSON. Which rows are links, fields or neither is read from each business's
spec, never from key prefixes. precheck() sorts every row and writes nothing.
migrate() copies the good link rows.

Standalone on purpose: plain sqlite3 and spec dicts, no project imports.
Connections are expected in autocommit mode (isolation_level=None).
"""

import json
from pathlib import Path

MIGRATION_SQL = Path(__file__).resolve().parent / "migration.sql"
STORED_CARDINALITIES = ("many_to_one", "one_to_one")
CATEGORIES = ("good", "bad", "undeclared", "fields", "other")


def apply_migration(conn):
    conn.executescript(MIGRATION_SQL.read_text())


def _target_problem(conn, business_id: int, link: dict, target_id: int) -> str | None:
    row = conn.execute("SELECT business_id, entity_type FROM entities WHERE id = ?", (target_id,)).fetchone()
    if row is None:
        return f"target record {target_id} does not exist"
    if row[0] != business_id:
        return f"target record {target_id} belongs to another business"
    if row[1] != link["to"]:
        return f"target record {target_id} is a {row[1]}, the link expects {link['to']}"
    return None


def _link_problem(conn, business_id, link, entity_type, entity_id, source_exists, value):
    """Returns (target_id, reason); reason is None when the row can move."""
    if not source_exists:
        return None, f"no {entity_type} record with id {entity_id}"
    if link["from"] != entity_type:
        return None, f"link belongs to {link['from']} records, not {entity_type}"
    if link["cardinality"] not in STORED_CARDINALITIES:
        return None, f"{link['cardinality']} links are not stored on records"
    try:
        target_id = json.loads(value)
    except ValueError:
        target_id = None
    if isinstance(target_id, bool) or not isinstance(target_id, int):
        return None, "value is not a record id"
    return target_id, _target_problem(conn, business_id, link, target_id)


def precheck(conn, specs: dict) -> dict:
    """Sorts every entity_attributes row into exactly one list:

      good        link values that can move to record_links
      bad         link values that cannot, each with a `reason`
      undeclared  rows whose attribute_name is neither a declared field of the
                  record's entity nor a link in its business's spec, each with
                  a `reason`
      fields      values of declared fields, which this migration leaves alone
      other       rows not on a spec-driven record, such as the legacy
                  'contact' / 'follow_up' rows, whose entity_id points at
                  other tables

    `specs` maps business_id to that business's spec ("entities" with their
    "fields", and "links"). Archived entities, fields and links count as
    declared, because archiving keeps their values. Reads only."""
    report = {c: [] for c in CATEGORIES}
    declared_by = {}
    for business_id, spec in specs.items():
        for e in spec["entities"]:
            declared_by.setdefault(e["key"], set()).add(business_id)

    rows = conn.execute(
        """SELECT a.entity_type, a.entity_id, a.attribute_name, a.value, e.business_id, e.entity_type
           FROM entity_attributes a
           LEFT JOIN entities e ON e.id = a.entity_id
           ORDER BY a.entity_id, a.attribute_name""").fetchall()
    for entity_type, entity_id, name, value, business_id, actual_type in rows:
        row = {"entity_type": entity_type, "entity_id": entity_id, "link_key": name, "value": value}
        source_exists = actual_type == entity_type
        if not source_exists:
            if entity_type not in declared_by:
                report["other"].append({**row, "business_id": None})
                continue
            # The record is gone, so its business comes from whichever spec
            # declares the entity key. Keys are generated per business, so one
            # owner is the norm; with several, the business is unknown.
            owners = declared_by[entity_type]
            business_id = next(iter(owners)) if len(owners) == 1 else None
        row["business_id"] = business_id

        spec = specs.get(business_id)
        if spec is None:
            reason = (f"no spec for business {business_id}" if source_exists
                      else f"no {entity_type} record with id {entity_id}, and its business is unknown")
            report["undeclared"].append({**row, "reason": reason})
            continue
        entity = next((e for e in spec["entities"] if e["key"] == entity_type), None)
        link = next((l for l in spec["links"] if l["key"] == name), None)
        if link is not None:
            target_id, reason = _link_problem(conn, business_id, link, entity_type, entity_id,
                                              source_exists, value)
            if reason:
                report["bad"].append({**row, "reason": reason})
            else:
                report["good"].append({**row, "cardinality": link["cardinality"], "from_type": link["from"],
                                       "to_id": target_id, "to_type": link["to"]})
        elif entity is None:
            report["undeclared"].append({**row, "reason": f"entity type {entity_type} is not in the spec"})
        elif any(f["key"] == name for f in entity["fields"]):
            report["fields"].append(row)
        else:
            report["undeclared"].append(
                {**row, "reason": f"not a field of {entity_type} or a link in the spec"})

    # one_to_one: when several records claim one target, none of them moves.
    # Which claim is right is a business decision, not a migration one.
    claims = {}
    for r in report["good"]:
        if r["cardinality"] == "one_to_one":
            claims.setdefault((r["business_id"], r["link_key"], r["to_id"]), []).append(r)
    for rows in claims.values():
        if len(rows) > 1:
            ids = ", ".join(str(r["entity_id"]) for r in rows)
            for r in rows:
                report["good"].remove(r)
                report["bad"].append({**r, "reason": f"one_to_one target {r['to_id']} is claimed by records {ids}"})
    report["bad"].sort(key=lambda r: (r["entity_id"], r["link_key"]))
    return report


def migrate(conn, specs: dict) -> dict:
    """Takes the write lock, reruns precheck() and copies every good link row
    into record_links, all in one transaction. Returns the precheck report.
    EAV rows are left in place."""
    conn.execute("BEGIN IMMEDIATE")
    try:
        report = precheck(conn, specs)
        conn.executemany(
            """INSERT INTO record_links (business_id, link_key, cardinality, from_id, from_type, to_id, to_type)
               VALUES (?, ?, ?, ?, ?, ?, ?)""",
            [(r["business_id"], r["link_key"], r["cardinality"], r["entity_id"], r["from_type"],
              r["to_id"], r["to_type"]) for r in report["good"]],
        )
    except Exception:
        conn.execute("ROLLBACK")
        raise
    conn.execute("COMMIT")
    return report
