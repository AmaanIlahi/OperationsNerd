"""
Generic, spec-driven records. A record is a row in `entities` whose
entity_type is the entity key from the spec; its values are rows in
`entity_attributes` named by field key (never by label). Nothing here knows
any particular industry -- it reads the business's current spec and
validates against it.

Required is enforced only when a record is created or edited. Existing
records that no longer fit the spec are returned as they are, with the
offending field keys listed in `invalid_fields`.
"""

from agentic import store
from agentic.values import coerce_value, is_empty
from db import db as d

DEFAULT_LIMIT = 100
MAX_LIMIT = 500


class RecordError(Exception):
    def __init__(self, status: int, detail):
        self.status = status
        self.detail = detail
        super().__init__(str(detail))


def _entity_in_current_spec(conn, account_id: int, business_id: int, entity_key: str) -> dict:
    business = store.get_business_with_spec(conn, account_id, business_id)
    if business is None:
        raise RecordError(404, "Business not found")
    entity = next((e for e in business["spec"]["entities"] if e["key"] == entity_key), None)
    if entity is None:
        raise RecordError(404, "Entity type not found")
    if entity["archived"]:
        raise RecordError(409, f"Entity type '{entity['label']}' is archived; restore it to use its records")
    return entity


def _check_values(entity: dict, values: dict, existing: dict | None) -> tuple[dict, list[str]]:
    """Returns (cleaned changes, errors). `cleaned` maps field key -> coerced
    value, or None to clear the value. Required fields are checked against
    the result of merging the changes onto `existing`."""
    fields = {f["key"]: f for f in entity["fields"] if not f["archived"]}
    errors, cleaned = {}, {}
    for key, raw in values.items():
        field = fields.get(key)
        if field is None:
            errors[key] = "unknown or archived field"
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


def _view(entity: dict, record: dict) -> dict:
    fields = {f["key"]: f for f in entity["fields"] if not f["archived"]}
    values, invalid = {}, []
    for key, field in fields.items():
        if key in record["attributes"]:
            value = record["attributes"][key]
            values[key] = value
            if not is_empty(value) and not coerce_value(field["type"], field.get("options"), value)[0]:
                invalid.append(key)
    return {"id": record["id"], "entity": entity["key"], "values": values,
            "invalid_fields": invalid,
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


def create_record(account_id: int, business_id: int, entity_key: str, values: dict) -> dict:
    with d.get_conn() as conn:
        entity = _entity_in_current_spec(conn, account_id, business_id, entity_key)
        cleaned, errors = _check_values(entity, values, None)
        if errors:
            raise RecordError(422, errors)
        record_id = d.create_entity(conn, business_id, entity_key)
        _write(conn, entity_key, record_id, cleaned)
        return _view(entity, d.get_entity(conn, record_id))


def update_record(account_id: int, business_id: int, entity_key: str, record_id: int, values: dict) -> dict:
    with d.get_conn() as conn:
        entity = _entity_in_current_spec(conn, account_id, business_id, entity_key)
        record = _owned_record(conn, business_id, entity_key, record_id)
        cleaned, errors = _check_values(entity, values, record["attributes"])
        if errors:
            raise RecordError(422, errors)
        _write(conn, entity_key, record_id, cleaned)
        return _view(entity, d.get_entity(conn, record_id))


def get_record(account_id: int, business_id: int, entity_key: str, record_id: int) -> dict:
    with d.get_conn() as conn:
        entity = _entity_in_current_spec(conn, account_id, business_id, entity_key)
        return _view(entity, _owned_record(conn, business_id, entity_key, record_id))


def list_records(account_id: int, business_id: int, entity_key: str,
                 limit: int = DEFAULT_LIMIT, offset: int = 0) -> dict:
    limit = max(1, min(limit, MAX_LIMIT))
    offset = max(0, offset)
    with d.get_conn() as conn:
        entity = _entity_in_current_spec(conn, account_id, business_id, entity_key)
        records = d.list_entities(conn, business_id, entity_key)
    return {"total": len(records), "limit": limit, "offset": offset,
            "records": [_view(entity, r) for r in records[offset:offset + limit]]}
