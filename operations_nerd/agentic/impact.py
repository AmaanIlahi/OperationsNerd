"""
Impact check: for each operation in a proposal, what would it do to the
business's real records? Returns structured numbers only; the UI words them.

Each operation is measured against the spec as it stood just before that
operation, so a field added earlier in the same proposal has no data yet.
Records live in `entities` (one row each, entity_type = the entity key) and
`entity_attributes` (attribute_name = the field key, value JSON-encoded).
Nothing here writes.
"""

import json

from agentic.values import coerce_value, is_empty

SAMPLE_LIMIT = 5


def _find(items, key):
    return next((i for i in items if i["key"] == key), None)


def _record_count(conn, business_id: int, entity_key: str) -> int:
    return conn.execute(
        "SELECT COUNT(*) FROM entities WHERE business_id = ? AND entity_type = ?",
        (business_id, entity_key),
    ).fetchone()[0]


def _field_values(conn, business_id: int, entity_key: str, field_key: str) -> list[tuple[int, object]]:
    """(record id, decoded value) for every record of the entity that has a
    stored value for the field."""
    rows = conn.execute(
        """SELECT e.id, a.value FROM entities e
           JOIN entity_attributes a
             ON a.entity_type = e.entity_type AND a.entity_id = e.id AND a.attribute_name = ?
           WHERE e.business_id = ? AND e.entity_type = ?
           ORDER BY e.id""",
        (field_key, business_id, entity_key),
    ).fetchall()
    return [(r["id"], json.loads(r["value"])) for r in rows]


def _non_empty(values):
    return [(rid, v) for rid, v in values if not is_empty(v)]


def _uses_option(value, option) -> bool:
    return option in value if isinstance(value, list) else value == option


def compute_impact(conn, business_id: int, applied: dict) -> dict:
    """`applied` is one entry from operations.apply_each()["applied"]."""
    op, resolved, before = applied["op"], applied["resolved"], applied["spec_before"]
    name = op["op"]
    out = {"index": applied["index"], "op": name}

    entity_key = resolved.get("entity")
    if entity_key:
        out["entity"] = entity_key
        out["entity_label"] = resolved.get("entity_label")
    if resolved.get("field"):
        out["field"] = resolved["field"]
        out["field_label"] = resolved.get("field_label")
    if resolved.get("link"):
        out["link"] = resolved["link"]
        out["link_label"] = resolved.get("link_label")

    # Links carry no record data in v1, so they change no records.
    if name in ("add_link", "archive_link", "restore_link"):
        out["records_affected"] = 0
        return out

    records = _record_count(conn, business_id, entity_key)
    out["records_affected"] = records

    if name == "create_entity_type":
        return out

    if name == "archive_entity_type":
        out["records_hidden"] = records
        out["links_archived"] = len(resolved["cascaded_links"])
        return out

    if name == "restore_entity_type":
        out["records_restored"] = records
        out["links_restored"] = len(resolved["restored_links"])
        return out

    if name == "add_field":
        out["records_missing_required"] = records if op.get("required") else 0
        return out

    if name == "update_entity_type":
        return out

    entity_before = _find(before["entities"], entity_key)
    field_key = resolved["field"]
    field_before = _find(entity_before["fields"], field_key)
    values = _non_empty(_field_values(conn, business_id, entity_key, field_key))

    if name == "archive_field":
        out["values_hidden"] = len(values)
        return out

    if name == "restore_field":
        out["values_restored"] = len(values)
        return out

    if name == "change_field_type":
        new_type = op["type"]
        new_options = op.get("options") if op.get("options") is not None else field_before.get("options")
        clean, unclean = 0, []
        for rid, value in values:
            ok, _, reason = coerce_value(new_type, new_options, value)
            if ok:
                clean += 1
            else:
                unclean.append({"record_id": rid, "value": value, "problem": reason})
        out["values_total"] = len(values)
        out["convert_cleanly"] = clean
        out["not_convertible"] = len(unclean)
        out["not_convertible_samples"] = unclean[:SAMPLE_LIMIT]
        return out

    if name == "update_field":
        if op.get("required") is True and not field_before["required"]:
            out["records_missing_required"] = records - len(values)
        if "options" in op and field_before["type"] in ("select", "multiselect"):
            new_options = {o.strip() for o in op["options"]}
            removed = [o for o in (field_before.get("options") or []) if o not in new_options]
            per_option = {o: sum(1 for _, v in values if _uses_option(v, o)) for o in removed}
            out["removed_options"] = per_option
            out["records_using_removed_options"] = sum(
                1 for _, v in values if any(_uses_option(v, o) for o in removed)
            )
        return out

    return out


def compute_impacts(conn, business_id: int, applied_ops: list[dict]) -> list[dict]:
    return [compute_impact(conn, business_id, a) for a in applied_ops]
