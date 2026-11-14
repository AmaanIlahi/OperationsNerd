"""Strict, previewed CSV imports into authoritative CRM records.

One confirmed batch is one Logistics command transaction, including all core
records, stable external identities and the replay result. Imports create draft
orders only; purchasing/stock transitions still require their ordinary commands.
"""
from __future__ import annotations

import csv
import hashlib
import io
import json
import re
from collections import OrderedDict
from datetime import date

from agentic import records
from db import db as d
from db.postgres import CORE_WRITE_LOCK, Connection
from modules.logistics import db as domain_db
from modules.logistics.errors import Conflict, ValidationError
from umbrella import logistics_adapter as adapter

MAX_BYTES = 1024 * 1024
MAX_ROWS = 5000
MAX_INTEGER = 9223372036854775807
HEADERS = {
    "customers": ("external_key", "name", "address"),
    "orders": ("external_key", "line_key", "order_number", "customer_record_id", "currency", "order_date",
               "item_record_id", "quantity", "unit_price", "required_by", "budget"),
}
OPTIONAL = {"customers": {"address"}, "orders": {"order_date", "budget"}}


def template_csv(kind):
    if kind not in HEADERS:
        raise ValidationError("Choose customers or orders")
    stream = io.StringIO(newline="")
    csv.writer(stream, lineterminator="\n").writerow(HEADERS[kind])
    return stream.getvalue()


def _hash(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode()).hexdigest()


def _error(row, field, message):
    return {"row": row, "field": field, "message": str(message)}


def _text(value, label, maximum=500):
    if not value or len(value) > maximum or "\x00" in value:
        raise ValueError(f"{label} requires nonblank text of at most {maximum} characters")
    return value


def _integer(value, label):
    if not re.fullmatch(r"[1-9][0-9]*", value) or int(value) > MAX_INTEGER:
        raise ValueError(f"{label} requires a positive whole number without separators or exponents")
    return int(value)


def _money(value, label):
    if not re.fullmatch(r"[0-9]+(?:\.[0-9]{1,2})?", value):
        raise ValueError(f"{label} requires a nonnegative amount such as 10.00; commas and exponents are not accepted")
    whole, _, fraction = value.partition(".")
    result = int(whole) * 100 + int(fraction.ljust(2, "0") or "0")
    if result > MAX_INTEGER:
        raise ValueError(f"{label} is too large")
    return result


def _date(value, label):
    try:
        result = date.fromisoformat(value)
        if result.isoformat() != value:
            raise ValueError()
        return value
    except ValueError:
        raise ValueError(f"{label} requires a real date in YYYY-MM-DD format") from None


def _read(kind, text):
    errors, parsed = [], []
    if kind not in HEADERS:
        return [], [_error(None, "kind", "Choose customers or orders")]
    if not isinstance(text, str) or not text or len(text.encode("utf-8")) > MAX_BYTES:
        return [], [_error(None, "csv_text", "Provide a nonempty CSV file of at most 1 MiB")]
    reader = csv.reader(io.StringIO(text.removeprefix("\ufeff"), newline=""), strict=True)
    try:
        headers = [column.strip() for column in next(reader)]
        if len(headers) != len(set(headers)):
            return [], [_error(1, "headers", "Column names must be unique")]
        unknown = set(headers) - set(HEADERS[kind])
        missing = set(HEADERS[kind]) - OPTIONAL[kind] - set(headers)
        if unknown or missing:
            message = "Unknown columns: " + ", ".join(sorted(unknown)) if unknown else ""
            if missing:
                message += (". " if message else "") + "Missing columns: " + ", ".join(sorted(missing))
            return [], [_error(1, "headers", message)]
        previous_line, data_rows = reader.line_num, 0
        for values in reader:
            row_number, previous_line = previous_line + 1, reader.line_num
            if not values or not any(value.strip() for value in values):
                continue
            data_rows += 1
            if data_rows > MAX_ROWS:
                errors.append(_error(row_number, "rows", f"A batch may contain at most {MAX_ROWS} data rows"))
                break
            if len(values) != len(headers):
                errors.append(_error(row_number, "columns", "The row has a different number of cells than the header"))
                continue
            raw = {key: value.strip() for key, value in zip(headers, values)}
            row = {"row": row_number}
            def read(name, parser, *, optional=False):
                value = raw.get(name, "")
                if optional and not value:
                    return None
                try:
                    return parser(value, name)
                except ValueError as error:
                    errors.append(_error(row_number, name, error))
                    return None
            row["external_key"] = read("external_key", lambda value, label: _text(value, label, 200))
            if kind == "customers":
                row["name"] = read("name", _text)
                row["address"] = read("address", lambda value, label: _text(value, label, 20000), optional=True)
            else:
                row.update(line_key=read("line_key", lambda value, label: _text(value, label, 200)),
                    order_number=read("order_number", _text), customer_record_id=read("customer_record_id", _integer),
                    item_record_id=read("item_record_id", _integer), quantity=read("quantity", _integer),
                    unit_price_minor=read("unit_price", _money), budget_minor=read("budget", _money, optional=True),
                    required_by=read("required_by", _date), order_date=read("order_date", _date, optional=True))
                currency = raw.get("currency", "")
                row["currency"] = currency
                if not re.fullmatch("[A-Z]{3}", currency):
                    errors.append(_error(row_number, "currency", "Use an uppercase three-letter currency code"))
            parsed.append(row)
    except StopIteration:
        errors.append(_error(1, "headers", "The CSV needs a header row"))
    except csv.Error as error:
        errors.append(_error(reader.line_num, "csv_text", "Malformed CSV: " + str(error)))
    if not parsed and not errors:
        errors.append(_error(None, "rows", "The CSV needs at least one data row"))
    return parsed, errors


def _groups(kind, rows, errors):
    groups = OrderedDict()
    for row in rows:
        key = row["external_key"]
        if key is None:
            continue
        if kind == "customers" and key in groups:
            errors.append(_error(row["row"], "external_key", "A customer external_key may appear only once in a batch"))
        groups.setdefault(key, []).append(row)
    if kind == "orders":
        for entries in groups.values():
            seen = set()
            first = entries[0]
            for row in entries:
                if row["line_key"] in seen:
                    errors.append(_error(row["row"], "line_key", "A line_key may appear only once within an order"))
                seen.add(row["line_key"])
                for name in ("order_number", "customer_record_id", "currency", "order_date"):
                    if row[name] != first[name]:
                        errors.append(_error(row["row"], name, "All lines with the same external_key must use the same order header"))
    return groups


def _values(mapping, row, role):
    fields = mapping["fields"]
    if role == "customer":
        result = {fields["name"]: row["name"]}
        optional = {"address": row.get("address")}
    elif role == "order":
        result = {fields["number"]: row["order_number"], fields["currency"]: row["currency"],
                  mapping["links"]["customer"]: row["customer_record_id"]}
        optional = {"date": row.get("order_date")}
    else:
        result = {fields["quantity"]: row["quantity"], fields["unit_price_minor"]: row["unit_price_minor"],
                  fields["required_by"]: row["required_by"], mapping["links"]["item"]: row["item_record_id"]}
        optional = {"budget_minor": row.get("budget_minor")}
    for meaning, value in optional.items():
        if value is not None:
            if meaning not in fields:
                raise ValueError(f"The approved {role} mapping does not include {meaning}; map it or leave that CSV value blank")
            result[fields[meaning]] = value
    return result


def _check(conn, business_id, spec, mapping, values):
    entity = next(entity for entity in spec["entities"] if entity["key"] == mapping["entity"])
    adapter.assert_record_writable(conn, business_id, mapping["entity"], values)
    cleaned, errors = records._check_values(conn, business_id, spec, entity, values, None)
    if errors:
        raise ValueError("; ".join(str(message) for message in errors.values()))
    return cleaned


def _existing(conn, business_id):
    if conn.execute("SELECT to_regclass('core.import_records') AS name").fetchone()["name"] is None:
        return {}
    return {(row["kind"], row["external_key"]): row for row in conn.execute(
        "SELECT * FROM core.import_records WHERE business_id=?", (business_id,)).fetchall()}


def _prepare(native, account_id, business_id, clean, spec, kind, text):
    wrapper = Connection.from_native(native)
    base_version = native.execute("SELECT current_version FROM core.businesses WHERE id=%s", (business_id,)).fetchone()["current_version"]
    rows, errors = _read(kind, text)
    groups = _groups(kind, rows, errors) if kind in HEADERS else {}
    invalid_rows = {error["row"] for error in errors}
    existing = _existing(wrapper, business_id)
    units = []
    # Validate every row before any commit mutation. Reference checks are scoped
    # to this physical business database and mapped entity, never guessed names.
    for row in rows:
        if row["row"] in invalid_rows:
            continue
        try:
            if kind == "customers":
                _check(wrapper, business_id, spec, clean["customer"], _values(clean["customer"], row, "customer"))
            else:
                adapter._attributes(native, business_id, clean["customer"]["entity"], row["customer_record_id"])
                adapter._attributes(native, business_id, clean["item"]["entity"], row["item_record_id"])
                _check(wrapper, business_id, spec, clean["order"], _values(clean["order"], row, "order"))
                _check(wrapper, business_id, spec, clean["order_line"], _values(clean["order_line"], row, "order_line"))
        except (ValueError, records.RecordError, ValidationError, adapter.NotFound) as error:
            errors.append(_error(row["row"], "record", getattr(error, "detail", str(error))))
    for key, entries in groups.items():
        semantic = [{name: value for name, value in row.items() if name not in ("row", "action")} for row in entries]
        if kind == "orders":
            semantic.sort(key=lambda row: row["line_key"] or "")
        content_hash = _hash({"bindings": clean, "rows": semantic})
        old = existing.get((kind, key))
        action = "skip" if old else "create"
        if old:
            if old["content_hash"] != content_hash:
                errors.append(_error(entries[0]["row"], "external_key", "This external_key was already imported with different content; updates need explicit review"))
            present = native.execute("SELECT 1 FROM core.entities WHERE business_id=%s AND entity_type=%s AND id=%s",
                                     (business_id, old["entity_key"], old["record_id"])).fetchone()
            if present is None:
                errors.append(_error(entries[0]["row"], "external_key", "The previously imported record is unavailable; reconcile its identity before retrying"))
        for row in entries:
            row["action"] = action
        units.append({"external_key": key, "entries": entries, "content_hash": content_hash, "old": old})
    digest = _hash({"kind": kind, "csv_sha256": hashlib.sha256(text.encode()).hexdigest() if isinstance(text, str) else None,
                    "base_version": base_version, "bindings": clean})
    preview = {"kind": kind, "base_version": base_version, "digest": digest, "rows": rows, "errors": errors,
        "summary": {"valid": not errors, "total_rows": len(rows), "total_orders": len(groups) if kind == "orders" else 0,
                    "create_count": sum(not unit["old"] for unit in units), "skip_count": sum(bool(unit["old"]) for unit in units)}}
    return preview, units


def preview_import(account_id, business_id, bindings, kind, csv_text):
    with adapter._connection(account_id, business_id, bindings) as (native, clean, spec):
        with native.transaction():
            native.execute("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ READ ONLY")
            native.execute("SET LOCAL search_path TO core, logistics, public")
            preview, _ = _prepare(native, account_id, business_id, clean, spec, kind, csv_text)
            return domain_db.json_value(preview)


def _create(conn, business_id, spec, mapping, values):
    cleaned = _check(conn, business_id, spec, mapping, values)
    identifier = d.create_entity(conn, business_id, mapping["entity"])
    records._write(conn, mapping["entity"], identifier, cleaned)
    return identifier


def commit_import(account_id, business_id, bindings, kind, csv_text, base_version, digest, idempotency_key):
    key = adapter._request_key(idempotency_key)
    if type(base_version) is not int or base_version < 1 or not isinstance(digest, str):
        raise ValidationError("Use the schema version and digest returned by preview")
    if not isinstance(csv_text, str) or len(csv_text.encode()) > MAX_BYTES:
        raise ValidationError("Provide CSV text of at most 1 MiB")
    payload = {"kind": kind, "csv_sha256": hashlib.sha256(csv_text.encode()).hexdigest(),
               "base_version": base_version, "digest": digest, "bindings": bindings}
    with adapter._connection(account_id, business_id, bindings, lock=True) as (native, clean, _):
        def work(tx):
            tx.execute("SELECT pg_advisory_xact_lock(%s)", (CORE_WRITE_LOCK,))
            tx.execute("SET LOCAL search_path TO core, logistics, public")
            _, spec = adapter._owned_spec(tx, account_id, business_id, clean)
            preview, units = _prepare(tx, account_id, business_id, clean, spec, kind, csv_text)
            if preview["base_version"] != base_version or preview["digest"] != digest:
                raise Conflict("The file, mappings or business schema changed; preview the batch again")
            if preview["errors"]:
                raise ValidationError("CSV batch was not imported: " + "; ".join(
                    f"row {error['row'] or '-'} {error['field']}: {error['message']}" for error in preview["errors"][:20]))
            tx.execute("""CREATE TABLE IF NOT EXISTS core.import_records(
                business_id BIGINT NOT NULL REFERENCES core.businesses(id),
                kind TEXT NOT NULL, external_key TEXT NOT NULL, content_hash TEXT NOT NULL,
                entity_key TEXT NOT NULL, record_id BIGINT NOT NULL REFERENCES core.entities(id),
                created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
                PRIMARY KEY(business_id,kind,external_key))""")
            conn = Connection.from_native(tx)
            created, skipped = [], []
            for unit in units:
                if unit["old"]:
                    skipped.append({"external_key": unit["external_key"], "record_id": unit["old"]["record_id"]})
                    continue
                first = unit["entries"][0]
                role = "customer" if kind == "customers" else "order"
                mapping = clean[role]
                record_id = _create(conn, business_id, spec, mapping, _values(mapping, first, role))
                result = {"external_key": unit["external_key"], "record_id": record_id}
                if kind == "orders":
                    result["line_record_ids"] = []
                    for row in unit["entries"]:
                        values = _values(clean["order_line"], row, "order_line")
                        values[clean["order_line"]["links"]["order"]] = record_id
                        line_id = _create(conn, business_id, spec, clean["order_line"], values)
                        result["line_record_ids"].append(line_id)
                        tx.execute("""INSERT INTO core.import_records
                            (business_id,kind,external_key,content_hash,entity_key,record_id) VALUES(%s,'order_lines',%s,%s,%s,%s)""",
                            (business_id, json.dumps([unit["external_key"], row["line_key"]]), _hash(row), clean["order_line"]["entity"], line_id))
                tx.execute("""INSERT INTO core.import_records
                    (business_id,kind,external_key,content_hash,entity_key,record_id) VALUES(%s,%s,%s,%s,%s,%s)""",
                    (business_id, kind, unit["external_key"], unit["content_hash"], mapping["entity"], record_id))
                created.append(result)
            return {"kind": kind, "created_count": len(created), "skipped_count": len(skipped),
                    "created": created, "skipped": skipped,
                    "rows_imported": sum(len(unit["entries"]) for unit in units if not unit["old"])}
        return domain_db.run_command(native, "host.csv_import", key, payload, work)
