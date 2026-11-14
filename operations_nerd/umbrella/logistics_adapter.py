"""Trusted host boundary for Logistics over authoritative, mapped CRM records.

BusinessRoutingMiddleware selects the business scope before these functions run.
The adapter rechecks ownership and mappings, never accepts a database URL or an
actor from a command, and never writes a second customer/item master catalog.
"""
from __future__ import annotations

from contextlib import contextmanager
from datetime import date
import hashlib
import json
import re
from typing import Annotated, Literal

from pydantic import AwareDatetime, Field, ValidationError as ModelValidationError, model_validator

from db import db as d
from db.postgres import CORE_WRITE_LOCK
from modules.logistics import core, db, fulfillment, purchasing, stock
from modules.logistics.errors import AuthorizationError, Conflict, NotFound, ValidationError
from modules.logistics.http_models import Command, Currency, Input, NonNegativeInt, PositiveInt, RequestKey
from umbrella.bindings import validate_bindings


Text = Annotated[str, Field(min_length=1, max_length=500)]
REFERENCE_ROLES = ('supplier', 'customer', 'item', 'location', 'carrier')


def _reference(business_id, entity_key, record_id):
    return f'core:{business_id}:{entity_key}:{record_id}'


def _subkey(request_key, *parts):
    # One key per logical child request, identical on retries and short enough
    # for the shared cross-command registry. Labels never form the identity.
    encoded = json.dumps([request_key, *parts], separators=(',', ':'), sort_keys=True)
    return 'host:' + hashlib.sha256(encoded.encode()).hexdigest()


def _request_key(value):
    try:
        return _Key(idempotency_key=value).idempotency_key
    except ModelValidationError:
        raise ValidationError('A nonblank idempotency_key of at most 200 characters is required') from None


class _Key(Command):
    pass


@contextmanager
def _connection(account_id, business_id, bindings, *, enabled=True, lock=False):
    with d.raw_postgres_conn(autocommit=True) as conn:
        db.prepare_connection(conn, f'business:{business_id}')
        if lock:
            # Session lifetime spans the public domain command's own transaction.
            # It serializes spec/record changes and disablement until that command
            # has committed; closing the connection also releases it on failure.
            conn.execute('SELECT pg_advisory_lock(%s)', (CORE_WRITE_LOCK,))
        try:
            clean, spec = _owned_spec(conn, account_id, business_id, bindings, enabled=enabled)
            yield conn, clean, spec
        finally:
            if lock:
                conn.execute('SELECT pg_advisory_unlock(%s)', (CORE_WRITE_LOCK,))


def _owned_spec(conn, account_id, business_id, bindings, *, enabled=True):
    row = conn.execute('''SELECT b.current_version,s.spec_json FROM core.businesses b
        JOIN core.spec_versions s ON s.business_id=b.id AND s.version=b.current_version
        WHERE b.id=%s AND b.owner_key=%s''', (business_id, str(account_id))).fetchone()
    if row is None:
        raise NotFound('Business not found')
    spec = json.loads(row['spec_json'])
    clean = validate_bindings(spec, bindings)
    installed = conn.execute("SELECT status,bindings_json FROM core.nerd_installations WHERE nerd_id='logistics'").fetchone()
    allowed = ('enabled',) if enabled else ('enabled', 'enabling')
    if installed is None or installed['status'] not in allowed:
        raise Conflict('Enable Logistics before using its records and commands')
    if validate_bindings(spec, json.loads(installed['bindings_json'])) != clean:
        raise Conflict('The approved Logistics mappings changed; refresh the workspace')
    return clean, spec


def _attributes(conn, business_id, entity_key, record_id):
    found = conn.execute('SELECT 1 FROM core.entities WHERE business_id=%s AND entity_type=%s AND id=%s',
                         (business_id, entity_key, record_id)).fetchone()
    if found is None:
        raise NotFound('The mapped core record does not exist in this business')
    rows = conn.execute('''SELECT attribute_name,value FROM core.entity_attributes
        WHERE entity_type=%s AND entity_id=%s''', (entity_key, record_id)).fetchall()
    return {row['attribute_name']: json.loads(row['value']) for row in rows}


def _native_records(conn, business_id, entity_key):
    rows = conn.execute('SELECT id FROM core.entities WHERE business_id=%s AND entity_type=%s ORDER BY id',
                        (business_id, entity_key)).fetchall()
    return [(row['id'], _attributes(conn, business_id, entity_key, row['id'])) for row in rows]


def _all_records(account_id, business_id, entity_key):
    from agentic import records
    result, offset = [], 0
    while True:
        page = records.list_records(account_id, business_id, entity_key, limit=500, offset=offset)
        result.extend(page['records'])
        offset += len(page['records'])
        if offset >= page['total']:
            return result
        if not page['records']:
            raise Conflict('The core record list changed; retry synchronization')


def _whole(value, label, *, minimum=0):
    if type(value) is str and re.fullmatch(r'-?[0-9]+', value.strip()):
        value = int(value.strip())
    if type(value) is not int or value < minimum or value > 9223372036854775807:
        raise ValidationError(f'{label} requires an exact whole number >= {minimum}; commas, decimals and exponent notation are not accepted')
    return value


def _text(value, label):
    if not isinstance(value, str) or not value.strip():
        raise ValidationError(f'{label} requires nonblank text')
    return value.strip()


def _calendar(value, label):
    if not isinstance(value, str):
        raise ValidationError(f'{label} requires YYYY-MM-DD')
    try:
        result = date.fromisoformat(value)
        if result.isoformat() != value:
            raise ValueError()
        return result
    except ValueError:
        raise ValidationError(f'{label} requires a real date in YYYY-MM-DD') from None


def _snapshot(mapping, values, role):
    result = {}
    for meaning, key in mapping['fields'].items():
        raw = values.get(key)
        if meaning in ('name', 'sku', 'unit'):
            result[meaning] = _text(raw, f'{role}.{meaning}')
        elif raw is not None:
            result[meaning] = _text(raw, f'{role}.{meaning}')
    return result


def _actor(account_id):
    with d.control_conn() as conn:
        account = conn.execute('SELECT id,email FROM accounts WHERE id=?', (account_id,)).fetchone()
    if account is None:
        raise AuthorizationError('The authenticated owner account no longer exists')
    return {'account_id': account_id, 'email': account['email'], 'name': account['email'],
            'can_approve_purchases': True}


def sync_records(account_id, business_id, bindings, idempotency_key):
    request_key = _request_key(idempotency_key)
    with _connection(account_id, business_id, bindings, enabled=False, lock=True) as (conn, clean, _):
        projections, sku_owner = [], {}
        for role in REFERENCE_ROLES:
            if role not in clean:
                continue
            mapping = clean[role]
            for record in _all_records(account_id, business_id, mapping['entity']):
                snapshot = _snapshot(mapping, record['values'], role)
                if role == 'item':
                    sku = snapshot['sku']
                    if sku in sku_owner:
                        raise ValidationError(f'Duplicate item SKU {sku!r}; use unique SKU values before synchronization')
                    sku_owner[sku] = record['id']
                external = _reference(business_id, mapping['entity'], record['id'])
                projections.append((role, external, snapshot))
        projections.append(('actor', _reference(business_id, 'account', account_id), _actor(account_id)))
        references = []
        for role, external, snapshot in projections:
            old = conn.execute('''SELECT * FROM logistics.core_bindings
                WHERE kind=%s AND external_key=%s AND valid_to IS NULL''', (role, external)).fetchone()
            if old is not None and role != 'supplier':
                # Stable non-supplier identities retain their initial snapshot;
                # the host display reader supplies current labels. Unit/SKU
                # changes after operational use are blocked by the write hook.
                references.append(dict(old))
                continue
            if old is not None and old['snapshot'] == snapshot:
                references.append(dict(old))
                continue
            child_parts = ['binding', role, external]
            if role == 'supplier':
                # A projection revision is a distinct logical child command.
                # This lets an interrupted enablement recover after the owner
                # corrects source records. Previous ID distinguishes A->B->A;
                # the equal-current fast path makes a committed retry a no-op.
                digest = hashlib.sha256(json.dumps(snapshot, sort_keys=True,
                    separators=(',', ':')).encode()).hexdigest()
                child_parts.extend([old['id'] if old else None, digest])
            references.append(core.bind_reference(conn, role, external, snapshot,
                _subkey(request_key, *child_parts)))
        return db.json_value({'synced': len(references), 'references': references})


class _HostDisplay:
    def __init__(self, business_id, bindings):
        self.business_id, self.bindings = business_id, bindings

    def decorate_references(self, conn, references):
        result = []
        for reference in references:
            row = dict(reference, snapshot=dict(reference.get('snapshot') or {}))
            role = row['kind']
            mapping = self.bindings.get(role)
            prefix = f'core:{self.business_id}:'
            if mapping and row['external_key'].startswith(prefix):
                try:
                    entity_key, record_id = row['external_key'][len(prefix):].rsplit(':', 1)
                    if entity_key != mapping['entity']:
                        raise ValueError()
                    values = _attributes(conn, self.business_id, entity_key, int(record_id))
                    # An old supplier revision is historical evidence, not a
                    # live address-book row. Never decorate its frozen terms.
                    if role != 'supplier' or row['valid_to'] is None:
                        row['snapshot'].update(_snapshot(mapping, values, role))
                    row['core_unavailable'] = False
                except (NotFound, ValidationError, ValueError):
                    row['core_unavailable'] = True
            result.append(row)
        return result

    def order_labels(self, conn, order_refs):
        mapping = self.bindings['order']
        customer = self.bindings['customer']
        prefix = f"core:{self.business_id}:{mapping['entity']}:"
        result = {}
        for reference in order_refs:
            if not reference.startswith(prefix):
                continue
            try:
                values = _attributes(conn, self.business_id, mapping['entity'], int(reference[len(prefix):]))
                customer_id = _whole(values.get(mapping['links']['customer']), 'order.customer', minimum=1)
                customer_values = _attributes(conn, self.business_id, customer['entity'], customer_id)
                result[reference] = {'order_number': values.get(mapping['fields']['number'], reference),
                    'customer_name': customer_values.get(customer['fields']['name']),
                    'ship_to_address': customer_values.get(customer['fields'].get('address'))}
            except (NotFound, ValueError):
                continue
        return result


def workspace(account_id, business_id, bindings):
    with _connection(account_id, business_id, bindings) as (conn, clean, _):
        display = _HostDisplay(business_id, clean)
        # These reads have no synchronization writes or master-record copies.
        with conn.transaction():
            conn.execute('SET TRANSACTION ISOLATION LEVEL REPEATABLE READ READ ONLY')
            tables = {'references': 'core_bindings', 'demands': 'demand_lines', 'quotes': 'quote_versions',
                'purchase_orders': 'purchase_orders', 'purchase_lines': 'purchase_order_lines',
                'allocations': 'purchase_allocations', 'stock': 'stock_levels'}
            result = {name: [dict(row) for row in conn.execute(f'SELECT * FROM logistics.{table} ORDER BY 1').fetchall()]
                      for name, table in tables.items()}
            result['references'] = display.decorate_references(conn, result['references'])
            for row in result['stock']:
                row['available'] = row['on_hand'] - row['reserved']
            for demand in result['demands']:
                position = purchasing._demand_position(conn, demand['id'])
                demand['reserved_quantity'] = position['reserved']
                demand['inbound_quantity'] = position['inbound']
                demand['remaining_to_buy'] = max(0, demand['quantity'] - position['shipped'] - position['reserved'] - position['inbound'])
            frozen = {row['record_id'] for row in conn.execute('''SELECT record_id FROM core.nerd_frozen_records
                WHERE entity_key=%s''', (clean['order']['entity'],)).fetchall()}
            orders = []
            mapping = clean['order']
            for identifier, values in _native_records(conn, business_id, mapping['entity']):
                reference = _reference(business_id, mapping['entity'], identifier)
                labels = display.order_labels(conn, [reference]).get(reference, {})
                orders.append({'id': identifier, 'entity': mapping['entity'], 'display': labels.get('order_number', reference),
                    'values': values, 'links': {}, 'order_ref': reference,
                    'order_number': labels.get('order_number', reference), 'customer_name': labels.get('customer_name'),
                    'status': 'submitted' if identifier in frozen else 'draft', 'submitted': identifier in frozen})
            result['orders'] = orders
            result['submitted_orders'] = [row for row in orders if row['submitted']]
        # fulfillment owns its own repeatable-read snapshot; never nest it.
        result['fulfillment'] = fulfillment.workspace(conn, display=display)
        result['progress'] = [purchasing.purchase_progress(conn, purchase_order_id=row['id']) for row in result['purchase_orders']]
        return db.json_value(result)


def submit_order(account_id, business_id, bindings, record_id, idempotency_key):
    record_id = _whole(record_id, 'Order record id', minimum=1)
    request_key = _request_key(idempotency_key)
    with _connection(account_id, business_id, bindings, lock=True) as (conn, clean, _):
        def work(tx):
            tx.execute('SELECT pg_advisory_xact_lock(%s)', (CORE_WRITE_LOCK,))
            current, _ = _owned_spec(tx, account_id, business_id, clean)
            header, line_map, item_map = current['order'], current['order_line'], current['item']
            order_ref = _reference(business_id, header['entity'], record_id)
            if tx.execute('SELECT 1 FROM core.nerd_frozen_records WHERE entity_key=%s AND record_id=%s',
                          (header['entity'], record_id)).fetchone():
                raise Conflict('This order was already submitted; use its original request key to replay')
            values = _attributes(tx, business_id, header['entity'], record_id)
            _text(values.get(header['fields']['number']), 'order.number')
            currency = _text(values.get(header['fields']['currency']), 'order.currency')
            if not re.fullmatch('[A-Z]{3}', currency):
                raise ValidationError('Order currency requires an uppercase three-letter code')
            customer_id = _whole(values.get(header['links']['customer']), 'order.customer', minimum=1)
            _attributes(tx, business_id, current['customer']['entity'], customer_id)
            lines = [(identifier, attrs) for identifier, attrs in _native_records(tx, business_id, line_map['entity'])
                     if attrs.get(line_map['links']['order']) == record_id]
            if not lines:
                raise ValidationError('Add at least one line to the order before submitting it')
            demands = []
            for identifier, attrs in lines:
                quantity = _whole(attrs.get(line_map['fields']['quantity']), 'order_line.quantity', minimum=1)
                _whole(attrs.get(line_map['fields']['unit_price_minor']), 'order_line.unit_price_minor')
                required = _calendar(attrs.get(line_map['fields']['required_by']), 'order_line.required_by')
                budget_key = line_map['fields'].get('budget_minor')
                budget = _whole(attrs[budget_key], 'order_line.budget_minor') if budget_key and attrs.get(budget_key) is not None else None
                item_record_id = _whole(attrs.get(line_map['links']['item']), 'order_line.item', minimum=1)
                item_values = _attributes(tx, business_id, item_map['entity'], item_record_id)
                _snapshot(item_map, item_values, 'item')
                item = core.resolve(tx, 'item', _reference(business_id, item_map['entity'], item_record_id))
                external = _reference(business_id, line_map['entity'], identifier)
                demands.append(dict(tx.execute('''INSERT INTO logistics.demand_lines
                    (external_key,order_ref,item_id,quantity,currency,budget_minor,required_by)
                    VALUES(%s,%s,%s,%s,%s,%s,%s) RETURNING *''',
                    (external, order_ref, item['id'], quantity, currency, budget, required)).fetchone()))
                tx.execute('INSERT INTO core.nerd_frozen_records(entity_key,record_id,order_ref) VALUES(%s,%s,%s)',
                           (line_map['entity'], identifier, order_ref))
            tx.execute('INSERT INTO core.nerd_frozen_records(entity_key,record_id,order_ref) VALUES(%s,%s,%s)',
                       (header['entity'], record_id, order_ref))
            return {'record_id': record_id, 'order_ref': order_ref, 'status': 'submitted', 'demands': demands}
        return db.run_command(conn, 'host.submit_order', request_key,
            {'business_id': business_id, 'record_id': record_id, 'bindings': clean}, work)


class QuoteInput(Command):
    supplier_id: PositiveInt
    item_id: PositiveInt
    quantity_priced: PositiveInt
    capacity_quantity: PositiveInt
    price_minor: NonNegativeInt
    pricing_kind: Literal['unit', 'total'] = 'unit'
    currency: Currency
    expires_at: AwareDatetime
    expected_date: date
    quote_ref: Text | None = None


class PurchaseInput(Command):
    supplier_id: PositiveInt
    currency: Currency


class PurchaseLineInput(Command):
    purchase_order_id: PositiveInt
    quote_version_id: PositiveInt
    quantity: PositiveInt
    expected_version: NonNegativeInt | None = None


class AllocationInput(Command):
    po_line_id: PositiveInt
    demand_line_id: PositiveInt
    quantity: PositiveInt
    cost_share_minor: NonNegativeInt | None = None
    expected_version: NonNegativeInt | None = None


class ApprovalInput(Command):
    purchase_order_id: PositiveInt
    expected_version: NonNegativeInt | None = None


class ReceiptAllocation(Input):
    purchase_allocation_id: PositiveInt
    demand_line_id: PositiveInt
    quantity: PositiveInt


class ReceiptLine(Input):
    po_line_id: PositiveInt
    item_id: PositiveInt
    location_id: PositiveInt
    quantity: PositiveInt
    lot_number: Text | None = None
    expiry_date: date | None = None
    allocations: Annotated[list[ReceiptAllocation], Field(max_length=1000)] = []


class DeliveryInput(Command):
    delivery_ref: Text
    received_on: date
    lines: Annotated[list[ReceiptLine], Field(min_length=1, max_length=1000)]
    as_of: date | None = None


class ReserveInput(Command):
    item_id: PositiveInt
    quantity: PositiveInt
    order_ref: Text
    demand_line_id: PositiveInt
    location_id: PositiveInt
    as_of: date | None = None


class OpeningInput(Command):
    item_id: PositiveInt
    quantity: PositiveInt
    source_ref: Text
    location_id: PositiveInt
    lot_number: Text | None = None
    expiry_date: date | None = None


class ShipmentInput(Command):
    shipment_number: Text
    order_ref: Text


class ShipmentLineInput(Command):
    shipment_id: PositiveInt
    reservation_id: PositiveInt
    quantity: PositiveInt


class DispatchInput(Command):
    shipment_id: PositiveInt
    carrier: Text
    as_of: date
    label_ref: Text | None = None
    tracking_ref: Text | None = None

    @model_validator(mode='after')
    def requires_reference(self):
        if not (self.label_ref or self.tracking_ref):
            raise ValueError('A tracking or label reference is required')
        return self


class TrackingInput(Command):
    shipment_id: PositiveInt
    event: Literal['in_transit', 'out_for_delivery', 'delivery_failed', 'exception', 'delivered']
    occurred_at: AwareDatetime
    detail: Annotated[str, Field(max_length=4000)] | None = None


OPERATIONS = {
    'create_quote': (QuoteInput, purchasing.create_quote),
    'create_purchase_order': (PurchaseInput, purchasing.create_purchase_order),
    'add_purchase_line': (PurchaseLineInput, purchasing.add_purchase_line),
    'allocate_purchase_line': (AllocationInput, purchasing.allocate_purchase_line),
    'approve_purchase_order': (ApprovalInput, purchasing.approve_purchase_order),
    'receive_delivery': (DeliveryInput, stock.receive_delivery),
    'reserve': (ReserveInput, stock.reserve),
    'open_stock': (OpeningInput, stock.open_stock),
    'create_shipment': (ShipmentInput, fulfillment.create_shipment),
    'add_shipment_line': (ShipmentLineInput, fulfillment.add_shipment_line),
    'dispatch': (DispatchInput, fulfillment.dispatch),
    'record_tracking_event': (TrackingInput, fulfillment.record_tracking_event),
}


def _check_reference(conn, business_id, bindings, identifier, role, *, supplier_terms=False):
    reference = conn.execute('SELECT * FROM logistics.core_bindings WHERE id=%s AND kind=%s',
                             (identifier, role)).fetchone()
    if reference is None:
        raise NotFound(f'The {role} binding does not exist in this business')
    mapping = bindings[role]
    prefix = f"core:{business_id}:{mapping['entity']}:"
    if not reference['external_key'].startswith(prefix):
        raise ValidationError(f'The {role} must use an authoritative mapped core record')
    try:
        record_id = int(reference['external_key'][len(prefix):])
    except ValueError:
        raise ValidationError(f'The {role} binding has an invalid core key') from None
    values = _attributes(conn, business_id, mapping['entity'], record_id)
    current = _snapshot(mapping, values, role)
    if supplier_terms and (reference['valid_to'] is not None or reference['snapshot'] != current):
        raise Conflict('Synchronize changed supplier records and use their current revision before creating or approving purchases')


def _preflight(conn, business_id, bindings, operation, values):
    # Completed requests must reach the domain registry: a lost-response retry
    # still returns its saved historical result after a supplier/record change.
    if conn.execute('SELECT 1 FROM logistics.command_requests WHERE idempotency_key=%s',
                    (values['idempotency_key'],)).fetchone():
        return
    for role in ('supplier', 'item', 'location'):
        key = role + '_id'
        if key in values:
            _check_reference(conn, business_id, bindings, values[key], role,
                supplier_terms=role == 'supplier')
    if operation == 'approve_purchase_order':
        po = conn.execute('SELECT supplier_id FROM logistics.purchase_orders WHERE id=%s',
                          (values['purchase_order_id'],)).fetchone()
        if po is None:
            raise NotFound('Purchase order does not exist')
        _check_reference(conn, business_id, bindings, po['supplier_id'], 'supplier', supplier_terms=True)
    if operation == 'receive_delivery':
        for line in values['lines']:
            for role in ('item', 'location'):
                _check_reference(conn, business_id, bindings, line[role+'_id'], role)
    demand_id = values.get('demand_line_id')
    if demand_id is not None:
        demand = conn.execute('SELECT external_key FROM logistics.demand_lines WHERE id=%s', (demand_id,)).fetchone()
        prefix = f"core:{business_id}:{bindings['order_line']['entity']}:"
        if demand is None or not demand['external_key'].startswith(prefix):
            raise ValidationError('Customer demand must come from an approved mapped core order line')


def invoke(account_id, business_id, bindings, operation, payload):
    if operation not in OPERATIONS or not isinstance(payload, dict):
        raise ValidationError('Unknown Logistics operation or invalid command object')
    model, command = OPERATIONS[operation]
    try:
        values = model.model_validate(payload).model_dump(mode='json')
    except ModelValidationError as error:
        details = error.errors(include_url=False, include_input=False)
        raise ValidationError('; '.join('.'.join(map(str, item['loc'])) + ': ' + item['msg'] for item in details[:5])) from None
    with _connection(account_id, business_id, bindings, lock=True) as (conn, clean, _):
        _preflight(conn, business_id, clean, operation, values)
        if operation == 'approve_purchase_order':
            actor = core.resolve(conn, 'actor', _reference(business_id, 'account', account_id))
            values['actor_id'] = actor['id']
        return db.json_value(command(conn, **values))


def assert_record_writable(conn, business_id, entity_key, values, record_id=None):
    """Called under core's write lock, before its lenient generic coercion.

    Exact integer strings are canonicalized in ``values`` before generic numeric
    conversion can pass through floating point. No unrelated fields are changed.
    Frozen operational orders/lines stay immutable even after module disablement.
    """
    from agentic.records import RecordError
    if getattr(conn, 'dialect', None) != 'postgresql':
        return
    if conn.execute("SELECT to_regclass('core.nerd_frozen_records') AS name").fetchone()['name'] is None:
        return
    if record_id is not None and conn.execute('SELECT 1 FROM core.nerd_frozen_records WHERE entity_key=? AND record_id=?',
                                              (entity_key, record_id)).fetchone():
        raise RecordError(409, 'Submitted logistics orders and lines are frozen; amendments need an explicit fulfillment contract')
    installed = conn.execute("SELECT status,bindings_json FROM core.nerd_installations WHERE nerd_id='logistics'").fetchone()
    if installed is None:
        return
    mappings = json.loads(installed['bindings_json'])
    line_map, order_map = mappings['order_line'], mappings['order']
    old = d.get_entity(conn, record_id) if record_id is not None else None
    attributes = dict(old['attributes']) if old else {}
    attributes.update(values)
    if entity_key == line_map['entity']:
        order_id = attributes.get(line_map['links']['order'])
        try:
            order_id = _whole(order_id, 'order_line.order', minimum=1)
        except ValidationError:
            order_id = None  # The ordinary core link validator reports invalid links.
        if order_id is not None and conn.execute('SELECT 1 FROM core.nerd_frozen_records WHERE entity_key=? AND record_id=?',
                                                 (order_map['entity'], order_id)).fetchone():
            raise RecordError(409, 'New or changed lines cannot target a submitted logistics order')
    try:
        # Disablement does not erase stock/history or reinterpret an existing
        # binding. Its SKU/unit remain stable before first use as well: initial
        # binding snapshots have no non-supplier revision contract today.
        item_map = mappings['item']
        if old and entity_key == item_map['entity']:
            immutable = (item_map['fields']['sku'], item_map['fields']['unit'])
            changed = any(key in values and values[key] != old['attributes'].get(key) for key in immutable)
            has_schema = conn.execute("SELECT to_regclass('logistics.core_bindings') AS name").fetchone()['name'] is not None
            if changed and has_schema and conn.execute('''SELECT 1 FROM logistics.core_bindings
                WHERE kind='item' AND external_key=?''', (_reference(business_id, entity_key, record_id),)).fetchone():
                raise ValidationError('A bound item SKU and unit cannot change; disabling Logistics retains its historical identity')
        if installed['status'] not in ('enabled', 'enabling'):
            return
        for role, mapping in mappings.items():
            if mapping['entity'] != entity_key:
                continue
            if role in REFERENCE_ROLES:
                _snapshot(mapping, attributes, role)
            if role == 'item':
                sku_key, unit_key = mapping['fields']['sku'], mapping['fields']['unit']
                sku = _text(attributes.get(sku_key), 'item.sku')
                for other in d.list_entities(conn, business_id, entity_key):
                    other_sku = other['attributes'].get(sku_key)
                    if other['id'] != record_id and isinstance(other_sku, str) and other_sku.strip() == sku:
                        raise ValidationError('Item SKU must be unique within this business')
            if role == 'order':
                _text(attributes.get(mapping['fields']['number']), 'order.number')
                if not re.fullmatch('[A-Z]{3}', _text(attributes.get(mapping['fields']['currency']), 'order.currency')):
                    raise ValidationError('Order currency requires an uppercase three-letter code')
            if role == 'order_line':
                for semantic, minimum in (('quantity', 1), ('unit_price_minor', 0), ('budget_minor', 0)):
                    key = mapping['fields'].get(semantic)
                    if key is None or (semantic == 'budget_minor' and attributes.get(key) in (None, '')):
                        continue
                    exact = _whole(attributes.get(key), f'order_line.{semantic}', minimum=minimum)
                    if key in values:
                        values[key] = exact
                _calendar(attributes.get(mapping['fields']['required_by']), 'order_line.required_by')
    except ValidationError as error:
        raise RecordError(422, str(error)) from None
