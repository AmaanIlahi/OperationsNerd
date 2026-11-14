"""Owner-only host interface; core owns people, locations, login and routing."""
from contextlib import contextmanager
import json
import re
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, StrictBool, StrictInt, ValidationError as ModelError

from db import db as host_db
from db.postgres import CORE_WRITE_LOCK
from umbrella.bindings import validate_bindings

from . import db, domain
from .errors import AuthorizationError, Conflict, NotFound, ValidationError

Text = Annotated[str, Field(min_length=1, max_length=200, pattern=r'^[^\x00]*$')]
LongText = Annotated[str, Field(min_length=1, max_length=2000, pattern=r'^[^\x00]*$')]
Date = Annotated[str, Field(pattern=r'^[0-9]{4}-[0-9]{2}-[0-9]{2}$')]
ID = Annotated[StrictInt, Field(gt=0, le=9223372036854775807)]
EmploymentType = Literal['full_time', 'part_time', 'contractor', 'temporary', 'intern']
LeaveType = Annotated[str, Field(pattern=r'^[a-z][a-z0-9_]{0,49}$')]


class Command(BaseModel):
    model_config = ConfigDict(extra='forbid', str_strip_whitespace=True)
    idempotency_key: Annotated[str, Field(min_length=1, max_length=200, pattern=r'^\S+$')]


class Hire(Command):
    employee_record_id: ID
    employee_number: Text
    job_title: Text
    start_date: Date
    location_record_id: ID | None = None
    department: Text | None = None
    employment_type: EmploymentType = 'full_time'


class Update(Command):
    staff_id: ID
    effective_date: Date
    job_title: Text | None = None
    location_record_id: ID | None = None
    department: Text | None = None
    employment_type: EmploymentType | None = None
    reason: LongText | None = None


class Terminate(Command):
    staff_id: ID
    end_date: Date
    reason: LongText


class Rehire(Command):
    staff_id: ID
    rehire_date: Date
    job_title: Text | None = None
    location_record_id: ID | None = None
    department: Text | None = None
    employment_type: EmploymentType | None = None
    reason: LongText | None = None


class Allowance(Command):
    leave_type: LeaveType
    name: Text
    annual_allowance_half_days: Annotated[StrictInt, Field(ge=0, le=200000)]
    tracks_balance: StrictBool = True
    paid: StrictBool = True
    reason: LongText = 'Owner-approved allowance policy'


class Accrual(Command):
    staff_id: ID
    leave_type: LeaveType
    year: Annotated[StrictInt, Field(ge=1900, le=9999)]


class Adjustment(Command):
    staff_id: ID
    leave_type: LeaveType
    half_days: Annotated[StrictInt, Field(ge=-200000, le=200000)]
    reason: LongText


class RequestLeave(Command):
    staff_id: ID
    leave_type: LeaveType
    start_date: Date
    end_date: Date
    half: Literal['am', 'pm'] | None = None
    note: LongText | None = None


class Decision(Command):
    request_id: ID


class Rejection(Decision):
    reason: LongText


class Cancellation(Decision):
    reason: LongText | None = None


Clock = Annotated[str, Field(pattern=r'^(?:[01][0-9]|2[0-3]):[0-5][0-9]$')]


class Shift(Command):
    staff_id: ID
    location_record_id: ID
    work_date: Date
    start_time: Clock
    end_time: Clock


class CancelShift(Command):
    shift_id: ID
    reason: LongText


class OnboardingTask(Command):
    staff_id: ID
    title: Text
    due_date: Date | None = None


class OnboardingStep(Command):
    task_id: ID


OPERATIONS = {
    'hire_employee': (Hire, domain.hire_employee), 'update_employee': (Update, domain.update_employee),
    'terminate_employee': (Terminate, domain.terminate_employee), 'rehire_employee': (Rehire, domain.rehire_employee),
    'set_leave_allowance': (Allowance, domain.set_leave_allowance), 'accrue_annual_leave': (Accrual, domain.accrue_annual_leave),
    'adjust_leave': (Adjustment, domain.adjust_leave), 'request_leave': (RequestLeave, domain.request_leave),
    'approve_leave': (Decision, domain.approve_leave), 'reject_leave': (Rejection, domain.reject_leave),
    'cancel_leave': (Cancellation, domain.cancel_leave),
    'schedule_shift': (Shift, domain.schedule_shift), 'cancel_shift': (CancelShift, domain.cancel_shift),
    'add_onboarding_task': (OnboardingTask, domain.add_onboarding_task),
    'complete_onboarding_task': (OnboardingStep, domain.complete_onboarding_task),
    'reopen_onboarding_task': (OnboardingStep, domain.reopen_onboarding_task),
}


def _owned(conn, account_id, business_id, bindings, *, enabled=True):
    row = conn.execute('''SELECT s.spec_json FROM core.businesses b JOIN core.spec_versions s
        ON s.business_id=b.id AND s.version=b.current_version WHERE b.id=%s AND b.owner_key=%s''',
        (business_id, str(account_id))).fetchone()
    if row is None:
        raise NotFound('Business not found')
    spec = json.loads(row['spec_json'])
    clean = validate_bindings(spec, bindings, nerd_id='hr')
    installed = conn.execute("SELECT status,bindings_json FROM core.nerd_installations WHERE nerd_id='hr'").fetchone()
    if installed is None or installed['status'] not in (('enabled',) if enabled else ('enabled', 'enabling')):
        raise Conflict('Enable HR before using its commands')
    if validate_bindings(spec, json.loads(installed['bindings_json']), nerd_id='hr') != clean:
        raise Conflict('The approved HR mappings changed; refresh the workspace')
    return clean


@contextmanager
def _connection(account_id, business_id, bindings, *, enabled=True, lock=False):
    with host_db.raw_postgres_conn(autocommit=True) as conn:
        db.prepare_connection(conn, f'business:{business_id}')
        if lock:
            conn.execute('SELECT pg_advisory_lock(%s)', (CORE_WRITE_LOCK,))
        try:
            clean = _owned(conn, account_id, business_id, bindings, enabled=enabled)
            yield conn, clean
        finally:
            if lock:
                conn.execute('SELECT pg_advisory_unlock(%s)', (CORE_WRITE_LOCK,))


def _record(conn, business_id, mapping, identifier):
    if conn.execute('SELECT 1 FROM core.entities WHERE id=%s AND business_id=%s AND entity_type=%s',
                    (identifier, business_id, mapping['entity'])).fetchone() is None:
        raise NotFound('The selected record is not an authoritative mapped HR record in this business')
    attrs = {row['attribute_name']: json.loads(row['value']) for row in conn.execute(
        'SELECT attribute_name,value FROM core.entity_attributes WHERE entity_id=%s AND entity_type=%s',
        (identifier, mapping['entity'])).fetchall()}
    values = {'record_id': identifier}
    for meaning, key in mapping['fields'].items():
        value = attrs.get(key)
        if meaning=='name' and (not isinstance(value, str) or not value.strip()):
            raise ValidationError('The mapped HR name must contain text')
        if value is not None and not isinstance(value, str):
            raise ValidationError(f'HR {meaning} must contain text')
        values[meaning] = value
    return values


def _records(conn, business_id, mapping):
    return [_record(conn, business_id, mapping, row['id']) for row in conn.execute(
        'SELECT id FROM core.entities WHERE business_id=%s AND entity_type=%s ORDER BY id',
        (business_id, mapping['entity'])).fetchall()]


def sync_records(account_id, business_id, bindings, idempotency_key):
    try:
        Command(idempotency_key=idempotency_key)
    except ModelError:
        raise ValidationError('Use a nonblank request key of at most 200 characters') from None
    with _connection(account_id, business_id, bindings, enabled=False, lock=True) as (conn, clean):
        # Validation only: no copied people/catalog or cross-Nerd command calls.
        return {'validated': {role: len(_records(conn, business_id, mapping)) for role, mapping in clean.items()}}


def workspace(account_id, business_id, bindings):
    with _connection(account_id, business_id, bindings) as (conn, clean):
        with conn.transaction():
            conn.execute('SET TRANSACTION ISOLATION LEVEL REPEATABLE READ READ ONLY')
            people = _records(conn, business_id, clean['employee'])
            locations = _records(conn, business_id, clean['location'])
            names, places = {row['record_id']: row for row in people}, {row['record_id']: row for row in locations}
            staff = []
            for row in conn.execute('SELECT id FROM hr.staff_state ORDER BY id').fetchall():
                person = domain.employee(conn, row['id'])
                source = names.get(person['record_id'], {})
                person.update(name=source.get('name', f"#{person['record_id']}"), email=source.get('email'),
                    location_name=places.get(person.get('location_record_id'), {}).get('name'))
                staff.append(person)
            types = [dict(row) for row in conn.execute('SELECT * FROM hr.leave_type_state ORDER BY code').fetchall()]
            balances = [domain.balance(conn, person['id'], kind['code']) for person in staff for kind in types]
            return db.json_value({'core_employees': people, 'locations': locations, 'staff': staff,
                'employment_history': [dict(row) for row in conn.execute('SELECT * FROM hr.employment_events ORDER BY id').fetchall()],
                'leave_types': types, 'balances': balances,
                'leave_requests': [dict(row) for row in conn.execute('SELECT * FROM hr.leave_request_state ORDER BY id').fetchall()],
                'leave_ledger': [dict(row) for row in conn.execute('SELECT * FROM hr.leave_ledger ORDER BY id').fetchall()],
                'shifts': [dict(row) for row in conn.execute('SELECT * FROM hr.shift_state ORDER BY id').fetchall()],
                'onboarding_tasks': [dict(row) for row in conn.execute('SELECT * FROM hr.onboarding_state ORDER BY id').fetchall()]})


def invoke(account_id, business_id, bindings, operation, payload):
    if operation not in OPERATIONS or not isinstance(payload, dict):
        raise ValidationError('Unknown HR operation or invalid command object')
    model, function = OPERATIONS[operation]
    try:
        values = model.model_validate(payload).model_dump(exclude_unset=True)
    except ModelError as error:
        raise ValidationError('; '.join('.'.join(map(str, e['loc']))+': '+e['msg'] for e in error.errors(include_input=False)[:5])) from None
    key = values.pop('idempotency_key')
    with _connection(account_id, business_id, bindings, lock=True) as (conn, clean):
        with host_db.control_conn() as control:
            if control.execute('SELECT 1 FROM accounts WHERE id=?', (account_id,)).fetchone() is None:
                raise AuthorizationError('Authenticated owner account no longer exists')
        def work(tx):
            _owned(tx, account_id, business_id, clean)
            if 'employee_record_id' in values:
                _record(tx, business_id, clean['employee'], values['employee_record_id'])
            if values.get('location_record_id') is not None:
                _record(tx, business_id, clean['location'], values['location_record_id'])
            if operation=='hire_employee':
                values['employee_entity_key'] = clean['employee']['entity']
            if operation=='schedule_shift':
                values['location_entity_key'] = clean['location']['entity']
            return function(tx, actor_id=account_id, request_key=key, **values)
        return db.run_command(conn, operation, key, {'actor_id': account_id, 'values': values, 'bindings': clean}, work)


def assert_record_writable(conn, business_id, entity_key, values, record_id=None):
    from agentic.records import RecordError
    if getattr(conn, 'dialect', None) != 'postgresql':
        return
    if conn.execute("SELECT to_regclass('core.nerd_installations') AS name").fetchone()['name'] is None:
        return
    row = conn.execute("SELECT status,bindings_json FROM core.nerd_installations WHERE nerd_id='hr'").fetchone()
    if not row or row['status'] not in ('enabled', 'enabling'):
        return
    for mapping in json.loads(row['bindings_json']).values():
        if mapping['entity'] != entity_key:
            continue
        old = host_db.get_entity(conn, record_id)['attributes'] if record_id is not None else {}
        merged = old | values
        name = merged.get(mapping['fields']['name'])
        if not isinstance(name, str) or not name.strip():
            raise RecordError(422, 'The mapped HR name must remain nonblank text')
