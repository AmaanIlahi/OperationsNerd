"""Reviewed CSV creation of draft shipments, without dispatch or ledger writes.

A whole file is one shared Logistics command transaction. Stable external keys
deduplicate shipment groups across different request keys; stock's existing SQL
guards and resource locks prevent competing plans from claiming the same units.
"""
from __future__ import annotations

from collections import OrderedDict, defaultdict
import csv
import hashlib
import io
import json
import re

from db.postgres import CORE_WRITE_LOCK
from modules.logistics import db as domain_db, stock
from modules.logistics.errors import Conflict, NotFound, ValidationError
from umbrella import logistics_adapter as adapter


HEADERS = ('external_key', 'line_key', 'shipment_number', 'order_ref', 'reservation_id', 'quantity')
MAX_BYTES = 1024 * 1024
MAX_ROWS = 5000
MAX_INTEGER = 9223372036854775807


def _hash(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(',', ':'), ensure_ascii=False).encode()).hexdigest()


def _error(row, field, message):
    return {'row': row, 'field': field, 'message': str(message)}


def template_csv(kind='shipments'):
    if kind != 'shipments':
        raise ValidationError('Choose the shipments template')
    stream = io.StringIO(newline='')
    csv.writer(stream, lineterminator='\n').writerow(HEADERS)
    return stream.getvalue()


def _text(value, label, limit=200):
    if not value or len(value) > limit or '\x00' in value:
        raise ValueError(f'{label} requires nonblank text of at most {limit} characters')
    return value


def _integer(value, label):
    if not re.fullmatch('[1-9][0-9]*', value) or len(value) > 19 or int(value) > MAX_INTEGER:
        raise ValueError(f'{label} requires a positive whole number without separators or exponents')
    return int(value)


def _read(kind, text):
    if kind != 'shipments':
        return [], [_error(None, 'kind', 'Choose shipments')]
    try:
        valid_size = isinstance(text, str) and 0 < len(text.encode('utf-8')) <= MAX_BYTES
    except UnicodeEncodeError:
        valid_size = False
    if not valid_size:
        return [], [_error(None, 'csv_text', 'Provide valid UTF-8 CSV text of at most 1 MiB')]
    reader = csv.reader(io.StringIO(text.removeprefix('\ufeff'), newline=''), strict=True)
    rows, errors = [], []
    try:
        headers = [name.strip() for name in next(reader)]
        if len(headers) != len(set(headers)) or set(headers) != set(HEADERS):
            return [], [_error(1, 'headers', 'Use each shipment template column exactly once: ' + ','.join(HEADERS))]
        previous_line, data_rows = reader.line_num, 0
        for cells in reader:
            number, previous_line = previous_line + 1, reader.line_num
            if not cells or not any(cell.strip() for cell in cells):
                continue
            data_rows += 1
            if data_rows > MAX_ROWS:
                errors.append(_error(number, 'rows', f'A batch may contain at most {MAX_ROWS} data rows'))
                break
            if len(cells) != len(headers):
                errors.append(_error(number, 'columns', 'The row has a different number of cells than the header'))
                continue
            raw = dict(zip(headers, (cell.strip() for cell in cells)))
            row = {'row': number}
            for field in HEADERS:
                try:
                    row[field] = _integer(raw[field], field) if field in ('reservation_id', 'quantity') \
                        else _text(raw[field], field, 500 if field == 'order_ref' else 200)
                except ValueError as error:
                    row[field] = None
                    errors.append(_error(number, field, error))
            rows.append(row)
    except StopIteration:
        errors.append(_error(1, 'headers', 'The CSV needs a header row'))
    except csv.Error as error:
        errors.append(_error(reader.line_num, 'csv_text', 'Malformed CSV: ' + str(error)))
    if not rows and not errors:
        errors.append(_error(None, 'rows', 'The CSV needs at least one data row'))
    return rows, errors


def _existing(conn):
    if conn.execute("SELECT to_regclass('logistics.host_imports') AS name").fetchone()['name'] is None:
        return {}
    return {row['external_key']: row for row in conn.execute(
        "SELECT * FROM logistics.host_imports WHERE kind='shipments'").fetchall()}


def _reservation_rows(conn, identifiers):
    if not identifiers:
        return {}
    return {row['id']: dict(row) for row in conn.execute('''SELECT r.*,d.external_key AS demand_ref,
        COALESCE(d.order_ref,r.order_ref) AS actual_order_ref
        FROM logistics.stock_reservations r LEFT JOIN logistics.demand_lines d ON d.id=r.demand_line_id
        WHERE r.id=ANY(%s) ORDER BY r.item_id,r.location_id,r.id''', (sorted(identifiers),)).fetchall()}


def _authorized_reservation(conn, business_id, bindings, row, reservation):
    if reservation is None:
        raise ValueError('Reservation does not exist in this business')
    if row['order_ref'] != reservation['actual_order_ref']:
        raise ValueError('The reservation belongs to a different customer order')
    header, line = bindings['order'], bindings['order_line']
    order_prefix = f"core:{business_id}:{header['entity']}:"
    line_prefix = f"core:{business_id}:{line['entity']}:"
    if not row['order_ref'].startswith(order_prefix) or not (reservation['demand_ref'] or '').startswith(line_prefix):
        raise ValueError('Use reservations from submitted, mapped core customer orders')
    header_id = _integer(row['order_ref'][len(order_prefix):], 'order_ref record id')
    line_id = _integer(reservation['demand_ref'][len(line_prefix):], 'demand record id')
    adapter._attributes(conn, business_id, header['entity'], header_id)
    source = adapter._attributes(conn, business_id, line['entity'], line_id)
    if source.get(line['links']['order']) != header_id:
        raise ValueError('The mapped order line no longer belongs to this customer order')
    frozen = conn.execute('''SELECT 1 FROM core.nerd_frozen_records
        WHERE entity_key=%s AND record_id=%s AND order_ref=%s''',
        (line['entity'], line_id, row['order_ref'])).fetchone()
    if frozen is None:
        raise ValueError('The reservation source must be an explicitly submitted order line')
    adapter._check_reference(conn, business_id, bindings, reservation['item_id'], 'item')
    adapter._check_reference(conn, business_id, bindings, reservation['location_id'], 'location')


def _prepare(conn, account_id, business_id, bindings, kind, text, *, lock=False):
    rows, errors = _read(kind, text)
    groups = OrderedDict()
    for row in rows:
        if row['external_key'] is not None:
            groups.setdefault(row['external_key'], []).append(row)
    existing = _existing(conn)
    invalid_rows = {error['row'] for error in errors}
    units, requested, numbers = [], defaultdict(int), {}
    for external, entries in groups.items():
        first, seen_keys, seen_reservations = entries[0], set(), set()
        for row in entries:
            for field in ('shipment_number', 'order_ref'):
                if row[field] != first[field]:
                    errors.append(_error(row['row'], field, 'All rows for one external_key must have the same shipment header'))
            if row['line_key'] is not None and row['line_key'] in seen_keys:
                errors.append(_error(row['row'], 'line_key', 'A line_key may appear only once within a shipment'))
            seen_keys.add(row['line_key'])
            if row['reservation_id'] is not None and row['reservation_id'] in seen_reservations:
                errors.append(_error(row['row'], 'reservation_id', 'Use one row per reservation within a shipment'))
            seen_reservations.add(row['reservation_id'])
        if first['shipment_number'] is not None:
            if first['shipment_number'] in numbers:
                errors.append(_error(first['row'], 'shipment_number', 'Different external keys must use different shipment numbers'))
            numbers[first['shipment_number']] = external
        semantic = [{field: row[field] for field in HEADERS} for row in entries]
        semantic.sort(key=lambda row: row['line_key'] or '')
        content_hash = _hash({'bindings': bindings, 'rows': semantic})
        old = existing.get(external)
        for row in entries:
            row['action'] = 'skip' if old else 'create'
        if old:
            if old['content_hash'] != content_hash:
                errors.append(_error(first['row'], 'external_key', 'This shipment external_key was already imported with different content; changes need explicit review'))
            present = conn.execute('SELECT shipment_number,order_ref FROM logistics.shipments WHERE id=%s',
                                   (old['shipment_id'],)).fetchone()
            if present is None or present['shipment_number'] != first['shipment_number'] or present['order_ref'] != first['order_ref']:
                errors.append(_error(first['row'], 'external_key', 'The previously imported shipment is unavailable or inconsistent; reconcile its identity'))
        else:
            if first['shipment_number'] is not None and conn.execute('SELECT 1 FROM logistics.shipments WHERE shipment_number=%s',
                    (first['shipment_number'],)).fetchone():
                errors.append(_error(first['row'], 'shipment_number', 'This shipment number is already used; choose a new number'))
            for row in entries:
                if row['row'] not in invalid_rows:
                    requested[row['reservation_id']] += row['quantity']
        units.append({'external_key': external, 'entries': entries, 'content_hash': content_hash, 'old': old})
    reservations = _reservation_rows(conn, set(requested))
    if lock:
        # Recheck quantities after taking the same sorted resource locks as the
        # stock engine. Direct-domain writers therefore cannot over-plan a race.
        stock._lock_resources(conn, [(row['item_id'], row['location_id']) for row in reservations.values()])
        reservations = _reservation_rows(conn, set(requested))
    capacities = {}
    for identifier, reservation in reservations.items():
        planned = conn.execute('''SELECT COALESCE(sum(l.quantity),0)::bigint AS quantity
            FROM logistics.shipment_lines l WHERE l.reservation_id=%s
            AND NOT EXISTS(SELECT 1 FROM logistics.shipment_dispatches d WHERE d.shipment_id=l.shipment_id)''',
            (identifier,)).fetchone()['quantity']
        capacities[identifier] = reservation['quantity'] - reservation['quantity_shipped'] - planned
    for unit in units:
        if unit['old']:
            continue  # Stable replay remains safe even after the shipment dispatches.
        for row in unit['entries']:
            if row['row'] in invalid_rows:
                continue
            try:
                reservation = reservations.get(row['reservation_id'])
                _authorized_reservation(conn, business_id, bindings, row, reservation)
                if reservation['status'] != 'active':
                    raise ValueError('Only active reservations can supply a new draft shipment')
                if requested[row['reservation_id']] > capacities[row['reservation_id']]:
                    raise ValueError('The CSV exceeds this reservation\'s remaining quantity not already planned on drafts')
            except (ValueError, NotFound, ValidationError) as error:
                errors.append(_error(row['row'], 'reservation_id', error))
    version = conn.execute('SELECT current_version FROM core.businesses WHERE id=%s', (business_id,)).fetchone()['current_version']
    try:
        csv_digest = hashlib.sha256(text.encode()).hexdigest() if isinstance(text, str) else None
    except UnicodeEncodeError:
        csv_digest = None
    digest = _hash({'kind': kind, 'base_version': version, 'bindings': bindings,
        'csv_sha256': csv_digest})
    preview = {'kind': kind, 'base_version': version, 'digest': digest, 'rows': rows, 'errors': errors,
        'summary': {'valid': not errors, 'total_rows': len(rows), 'total_shipments': len(groups),
            'create_count': sum(unit['old'] is None for unit in units), 'skip_count': sum(unit['old'] is not None for unit in units)}}
    return preview, units


def preview_import(account_id, business_id, bindings, kind, csv_text):
    with adapter._connection(account_id, business_id, bindings) as (conn, clean, _):
        with conn.transaction():
            conn.execute('SET TRANSACTION ISOLATION LEVEL REPEATABLE READ READ ONLY')
            preview, _ = _prepare(conn, account_id, business_id, clean, kind, csv_text)
            return domain_db.json_value(preview)


def _insert_line(conn, shipment_id, row):
    # The immutable source SQL trigger enforces draft state, one customer order,
    # active reservation and resource-locked remaining capacity for this write.
    return conn.execute('''INSERT INTO logistics.shipment_lines(shipment_id,reservation_id,quantity)
        VALUES(%s,%s,%s) RETURNING id''', (shipment_id, row['reservation_id'], row['quantity'])).fetchone()['id']


def commit_import(account_id, business_id, bindings, kind, csv_text, base_version, digest, idempotency_key):
    key = adapter._request_key(idempotency_key)
    if kind != 'shipments' or type(base_version) is not int or base_version < 1 or not isinstance(digest, str):
        raise ValidationError('Use the shipments schema version and digest returned by preview')
    try:
        valid_size = isinstance(csv_text, str) and 0 < len(csv_text.encode()) <= MAX_BYTES
    except UnicodeEncodeError:
        valid_size = False
    if not valid_size:
        raise ValidationError('Provide valid UTF-8 shipment CSV text of at most 1 MiB')
    payload = {'kind': kind, 'base_version': base_version, 'digest': digest, 'bindings': bindings,
        'csv_sha256': hashlib.sha256(csv_text.encode()).hexdigest()}
    with adapter._connection(account_id, business_id, bindings, lock=True) as (conn, clean, _):
        def work(tx):
            tx.execute('SELECT pg_advisory_xact_lock(%s)', (CORE_WRITE_LOCK,))
            adapter._owned_spec(tx, account_id, business_id, clean)
            preview, units = _prepare(tx, account_id, business_id, clean, kind, csv_text, lock=True)
            if preview['base_version'] != base_version or preview['digest'] != digest:
                raise Conflict('The file, mappings or business schema changed; preview the shipment batch again')
            if preview['errors']:
                raise ValidationError('Shipment CSV was not imported: ' + '; '.join(
                    f"row {error['row'] or '-'} {error['field']}: {error['message']}" for error in preview['errors'][:20]))
            tx.execute('''CREATE TABLE IF NOT EXISTS logistics.host_imports(
                kind TEXT NOT NULL CHECK(kind='shipments'), external_key TEXT NOT NULL CHECK(length(external_key)>0),
                content_hash TEXT NOT NULL CHECK(length(content_hash)=64),
                shipment_id BIGINT NOT NULL UNIQUE REFERENCES logistics.shipments(id),
                created_at TIMESTAMPTZ NOT NULL DEFAULT now(), PRIMARY KEY(kind,external_key))''')
            created, skipped = [], []
            for unit in units:
                if unit['old']:
                    skipped.append({'external_key': unit['external_key'], 'shipment_id': unit['old']['shipment_id']})
                    continue
                first = unit['entries'][0]
                identifier = tx.execute('''INSERT INTO logistics.shipments(shipment_number,order_ref,request_key)
                    VALUES(%s,%s,%s) RETURNING id''', (first['shipment_number'], first['order_ref'], key)).fetchone()['id']
                line_ids = [_insert_line(tx, identifier, row) for row in unit['entries']]
                tx.execute('''INSERT INTO logistics.host_imports(kind,external_key,content_hash,shipment_id)
                    VALUES('shipments',%s,%s,%s)''', (unit['external_key'], unit['content_hash'], identifier))
                created.append({'external_key': unit['external_key'], 'shipment_id': identifier, 'line_ids': line_ids, 'status': 'draft'})
            return {'kind': kind, 'created_count': len(created), 'skipped_count': len(skipped),
                'created': created, 'skipped': skipped, 'rows_imported': sum(len(unit['entries']) for unit in units if not unit['old'])}
        return domain_db.run_command(conn, 'host.shipment_csv_import', key, payload, work)
