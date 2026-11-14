"""Explicit trusted host projections, with supplier valid-time history.

This module supplies a binding seam and development fixtures, not a claim that
OperationsNerd has approved these mappings. The public API exposes reads only.
A trusted host adapter is responsible for authenticating and supplying writes.
"""
from datetime import date, datetime, timedelta, timezone
from typing import Protocol

from psycopg.types.json import Jsonb
from .db import json_value, run_command
from .errors import Conflict, NotFound, ValidationError

KINDS = {'item','supplier','customer','location','carrier','actor'}


class CoreAdapter(Protocol):
    """A deployment supplies its authoritative binding implementation."""
    def resolve(self, conn, kind: str, external_key: str) -> dict: ...


def resolve(conn, kind: str, external_key: str, *, as_of: datetime | None = None) -> dict:
    if kind not in KINDS:
        raise ValidationError('Unknown core entity kind')
    if as_of is None:
        now=datetime.now(timezone.utc)
        row = conn.execute('SELECT * FROM core_bindings WHERE kind=%s AND external_key=%s AND valid_from<=%s AND (valid_to IS NULL OR valid_to>%s)',
                           (kind, external_key,now,now)).fetchone()
    else:
        if as_of.utcoffset() is None:
            raise ValidationError('History timestamps require a timezone')
        row = conn.execute('''SELECT * FROM core_bindings WHERE kind=%s AND external_key=%s
            AND valid_from<=%s AND (valid_to IS NULL OR valid_to>%s)''', (kind,external_key,as_of,as_of)).fetchone()
    if row is None:
        raise NotFound('Authoritative core binding is missing')
    return row


def require_binding(conn, binding_id: int, kind: str) -> dict:
    row = conn.execute('SELECT * FROM core_bindings WHERE id=%s AND kind=%s', (binding_id,kind)).fetchone()
    if row is None:
        raise NotFound(f'Unknown {kind} binding')
    return row


def bind_reference(conn, kind: str, external_key: str, snapshot: dict,
                   idempotency_key: str, *, effective_at: datetime | None = None) -> dict:
    if kind not in KINDS or not isinstance(external_key,str) or not external_key.strip():
        raise ValidationError('A valid entity kind and external key are required')
    if type(snapshot) is not dict:
        raise ValidationError('A binding snapshot must be an object')
    if effective_at is not None and effective_at.utcoffset() is None:
        raise ValidationError('Effective time must include a timezone')
    payload = {'kind':kind,'external_key':external_key,'snapshot':snapshot,'effective_at':effective_at}
    def work(c):
        c.execute('SELECT pg_advisory_xact_lock(1768842363,hashtext(%s))', (kind+':'+external_key,))
        old = c.execute('SELECT * FROM core_bindings WHERE kind=%s AND external_key=%s AND valid_to IS NULL FOR UPDATE',
                        (kind,external_key)).fetchone()
        if old and old['snapshot'] == snapshot:
            return json_value(old)
        if old and kind != 'supplier':
            raise Conflict('Only supplier revisions are supported; other core identities must remain stable')
        when = effective_at or datetime.now(timezone.utc)
        if when > datetime.now(timezone.utc):
            raise ValidationError('Future supplier revisions require a scheduling contract')
        if old:
            if when <= old['valid_from']:
                raise ValidationError('A revision must start after the current revision')
            c.execute('UPDATE core_bindings SET valid_to=%s WHERE id=%s', (when,old['id']))
        row=c.execute('''INSERT INTO core_bindings(kind,external_key,snapshot,valid_from)
            VALUES(%s,%s,%s,%s) RETURNING *''', (kind,external_key,Jsonb(snapshot),when)).fetchone()
        return json_value(row)
    return run_command(conn,'bind_reference',idempotency_key,payload,work)


def register_demand(conn, *, external_key: str, order_ref: str, item_id: int,
                    quantity: int, currency: str, required_by: date | None,
                    idempotency_key: str, budget_minor: int | None = None) -> dict:
    if type(quantity) is not int or quantity<=0 or type(item_id) is not int or item_id<=0:
        raise ValidationError('Demand quantity and item reference must be positive integers')
    if not external_key or not order_ref or len(currency)!=3 or not currency.isupper() or not currency.isalpha():
        raise ValidationError('Demand references and an uppercase three-letter currency are required')
    if budget_minor is not None and (type(budget_minor) is not int or budget_minor<0):
        raise ValidationError('Budget must be nonnegative integer minor units')
    if required_by is not None and type(required_by) is not date:
        raise ValidationError('Required date must be a calendar date')
    payload={'external_key':external_key,'order_ref':order_ref,'item_id':item_id,'quantity':quantity,
             'currency':currency,'required_by':required_by,'budget_minor':budget_minor}
    def work(c):
        require_binding(c,item_id,'item')
        old=c.execute('SELECT * FROM demand_lines WHERE external_key=%s', (external_key,)).fetchone()
        if old:
            raise Conflict('Customer demand already exists; amendment requires a host contract')
        return dict(c.execute('''INSERT INTO demand_lines(external_key,order_ref,item_id,quantity,currency,budget_minor,required_by)
            VALUES(%s,%s,%s,%s,%s,%s,%s) RETURNING *''',
            (external_key,order_ref,item_id,quantity,currency,budget_minor,required_by)).fetchone())
    return run_command(conn,'register_demand',idempotency_key,payload,work)


def reference_catalog(conn) -> dict:
    return {'bindings': json_value(conn.execute('SELECT * FROM core_bindings WHERE valid_to IS NULL ORDER BY kind,id').fetchall()),
            'demands': json_value(conn.execute('SELECT * FROM demand_lines ORDER BY id').fetchall()),
            'mapping_status':'explicit_host_projection; OperationsNerd mapping pending'}
