"""
Generic, spec-driven records. A record is a row in `entities` whose
entity_type is the entity key from the spec; its values are rows in
`entity_attributes` named by field key (never by label). Nothing here knows
any particular industry -- it reads the business's current spec and
validates against it.

Links: for many_to_one and one_to_one links, the record of the link's `from`
entity stores the target record's id under the link key. one_to_many and
many_to_many links are not editable yet. A write may carry link keys in the
same `values` object as field keys; reads return link targets separately,
under `links`.

Required is enforced only when a record is created or edited. Existing
records that no longer fit the spec are returned as they are, with the
offending field keys listed in `invalid_fields`.
"""

from agentic import store
from agentic.values import coerce_value, is_empty
from db import db as d

EDITABLE_CARDINALITIES = ("many_to_one", "one_to_one")
DEFAULT_LIMIT = 100
MAX_LIMIT = 500


class RecordError(Exception):
    def __init__(self, status: int, detail):
        self.status = status
        self.detail = detail
        super().__init__(str(detail))


def _entity_in_current_spec(conn, account_id: int, business_id: int, entity_key: str) -> tuple[dict, dict]:
    business = store.get_business_with_spec(conn, account_id, business_id)
    if business is None:
        raise RecordError(404, "Business not found")
    entity = next((e for e in business["spec"]["entities"] if e["key"] == entity_key), None)
    if entity is None:
        raise RecordError(404, "Entity type not found")
    if entity["archived"]:
        raise RecordError(409, f"Entity type '{entity['label']}' is archived; restore it to use its records")
    return business["spec"], entity


def _editable_links(spec: dict, entity_key: str) -> list[dict]:
    return [l for l in spec["links"]
            if not l["archived"] and l["from"] == entity_key and l["cardinality"] in EDITABLE_CARDINALITIES]


def _display(entity: dict, attributes: dict, record_id: int) -> str:
    """A record's name in dropdowns and link columns: its first non-archived
    field that has a value."""
    for f in entity["fields"]:
        value = attributes.get(f["key"])
        if not f["archived"] and not is_empty(value):
            return ", ".join(value) if isinstance(value, list) else str(value)
    return f"#{record_id}"


def _check_link_target(conn, business_id: int, spec: dict, link: dict, raw) -> tuple[int | None, str | None]:
    if is_empty(raw):
        return None, None
    target = next(e for e in spec["entities"] if e["key"] == link["to"])
    problem = f"{link['label']} must be the id of a {target['label']} record"
    if isinstance(raw, bool) or not isinstance(raw, (int, str)):
        return None, problem
    try:
        target_id = int(raw)
    except ValueError:
        return None, problem
    found = conn.execute(
        "SELECT 1 FROM entities WHERE id = ? AND business_id = ? AND entity_type = ?",
        (target_id, business_id, link["to"]),
    ).fetchone()
    return (target_id, None) if found else (None, problem)


def _check_values(conn, business_id: int, spec: dict, entity: dict, values: dict,
                  existing: dict | None) -> tuple[dict, dict]:
    """Returns (cleaned changes, errors). `cleaned` maps field key -> coerced
    value, or None to clear the value. Required fields are checked against
    the result of merging the changes onto `existing`."""
    fields = {f["key"]: f for f in entity["fields"] if not f["archived"]}
    links = {l["key"]: l for l in _editable_links(spec, entity["key"])}
    errors, cleaned = {}, {}
    for key, raw in values.items():
        if key in links:
            target_id, problem = _check_link_target(conn, business_id, spec, links[key], raw)
            if problem:
                errors[key] = problem
            else:
                cleaned[key] = target_id
            continue
        field = fields.get(key)
        if field is None:
            errors[key] = "unknown or archived field or link"
            continue
        ok, coerced, problem = coerce_value(field["type"], field.get("options"), raw)
        if not ok:
            errors[key] = f"{field['label']} {problem}"
        else:
            cleaned[key] = coerced

    merged = dict(existing or {})
    for key, value in cleaned.items():
        if value is None:
            merged.pop(key, None)
        else:
            merged[key] = value
    for key, field in fields.items():
        if field["required"] and is_empty(merged.get(key)):
            errors.setdefault(key, f"{field['label']} is required")
    return cleaned, errors


def _view(entity: dict, record: dict, links: dict | None = None) -> dict:
    fields = {f["key"]: f for f in entity["fields"] if not f["archived"]}
    values, invalid = {}, []
    for key, field in fields.items():
        if key in record["attributes"]:
            value = record["attributes"][key]
            values[key] = value
            if not is_empty(value) and not coerce_value(field["type"], field.get("options"), value)[0]:
                invalid.append(key)
    return {"id": record["id"], "entity": entity["key"],
            "display": _display(entity, record["attributes"], record["id"]),
            "values": values, "links": links or {}, "invalid_fields": invalid,
            "created_at": record["created_at"], "updated_at": record["updated_at"]}


def _write(conn, entity_key: str, record_id: int, cleaned: dict):
    to_set = {k: v for k, v in cleaned.items() if v is not None}
    d.set_entity_attributes(conn, entity_key, record_id, to_set)
    for key, value in cleaned.items():
        if value is None:
            conn.execute(
                "DELETE FROM entity_attributes WHERE entity_type = ? AND entity_id = ? AND attribute_name = ?",
                (entity_key, record_id, key),
            )
    conn.execute("UPDATE entities SET updated_at = datetime('now') WHERE id = ?", (record_id,))


def _owned_record(conn, business_id: int, entity_key: str, record_id: int) -> dict:
    row = conn.execute(
        "SELECT id FROM entities WHERE id = ? AND business_id = ? AND entity_type = ?",
        (record_id, business_id, entity_key),
    ).fetchone()
    if row is None:
        raise RecordError(404, "Record not found")
    return d.get_entity(conn, record_id)


def _link_views(conn, business_id: int, spec: dict, entity: dict, records: list[dict]) -> list[dict]:
    """For each record, {link_key: {"id", "label"}} for its editable links."""
    links = _editable_links(spec, entity["key"])
    entities = {e["key"]: e for e in spec["entities"]}
    labels = {}
    for l in links:
        target = entities[l["to"]]
        labels[l["key"]] = {r["id"]: _display(target, r["attributes"], r["id"])
                            for r in d.list_entities(conn, business_id, l["to"])}
    out = []
    for r in records:
        view = {}
        for l in links:
            target_id = r["attributes"].get(l["key"])
            if target_id is not None:
                view[l["key"]] = {"id": target_id, "label": labels[l["key"]].get(target_id)}
        out.append(view)
    return out


def create_record(account_id: int, business_id: int, entity_key: str, values: dict) -> dict:
    with d.get_conn() as conn:
        spec, entity = _entity_in_current_spec(conn, account_id, business_id, entity_key)
        cleaned, errors = _check_values(conn, business_id, spec, entity, values, None)
        if errors:
            raise RecordError(422, errors)
        record_id = d.create_entity(conn, business_id, entity_key)
        _write(conn, entity_key, record_id, cleaned)
        record = d.get_entity(conn, record_id)
        return _view(entity, record, _link_views(conn, business_id, spec, entity, [record])[0])


def update_record(account_id: int, business_id: int, entity_key: str, record_id: int, values: dict) -> dict:
    with d.get_conn() as conn:
        spec, entity = _entity_in_current_spec(conn, account_id, business_id, entity_key)
        record = _owned_record(conn, business_id, entity_key, record_id)
        cleaned, errors = _check_values(conn, business_id, spec, entity, values, record["attributes"])
        if errors:
            raise RecordError(422, errors)
        _write(conn, entity_key, record_id, cleaned)
        record = d.get_entity(conn, record_id)
        return _view(entity, record, _link_views(conn, business_id, spec, entity, [record])[0])


def get_record(account_id: int, business_id: int, entity_key: str, record_id: int) -> dict:
    with d.get_conn() as conn:
        spec, entity = _entity_in_current_spec(conn, account_id, business_id, entity_key)
        record = _owned_record(conn, business_id, entity_key, record_id)
        return _view(entity, record, _link_views(conn, business_id, spec, entity, [record])[0])


def list_records(account_id: int, business_id: int, entity_key: str,
                 limit: int = DEFAULT_LIMIT, offset: int = 0) -> dict:
    limit = max(1, min(limit, MAX_LIMIT))
    offset = max(0, offset)
    with d.get_conn() as conn:
        spec, entity = _entity_in_current_spec(conn, account_id, business_id, entity_key)
        records = d.list_entities(conn, business_id, entity_key)
        page = records[offset:offset + limit]
        link_views = _link_views(conn, business_id, spec, entity, page)
    return {"total": len(records), "limit": limit, "offset": offset,
            "records": [_view(entity, r, lv) for r, lv in zip(page, link_views)]}
