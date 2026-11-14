"""HR command bodies: append immutable employment and exact half-day events.

The host adapter supplies one enclosing request transaction and actor. These
helpers never open connections, read environment settings, or commit children.
"""
from datetime import date, datetime, timedelta, timezone

from psycopg.types.json import Jsonb

from .errors import Conflict, NotFound, ValidationError


def today():
    return datetime.now(timezone.utc).date()


def calendar(value):
    try:
        parsed = date.fromisoformat(value) if isinstance(value, str) else value
        if type(parsed) is not date or parsed.year < 1900:
            raise ValueError()
        return parsed
    except (ValueError, TypeError):
        raise ValidationError('Use a real date from 1900 onward in YYYY-MM-DD') from None


def employee(conn, staff_id, *, active=False):
    row = conn.execute('SELECT * FROM hr.staff_state WHERE id=%s', (staff_id,)).fetchone()
    if row is None:
        raise NotFound('Staff record not found')
    if active and row['status'] != 'active':
        raise Conflict('This staff member is terminated; rehire before making changes')
    return dict(row) | dict(row['snapshot']) | {'staff_id': row['id'], 'employee_record_id': row['record_id']}


def _event(conn, staff_id, event, effective_date, snapshot, actor_id, request_key, reason=None):
    previous = conn.execute('SELECT COALESCE(max(version),0) AS version FROM hr.employment_events WHERE staff_id=%s', (staff_id,)).fetchone()['version']
    conn.execute('''INSERT INTO hr.employment_events(staff_id,version,event,effective_date,snapshot,reason,actor_id,request_key)
        VALUES(%s,%s,%s,%s,%s,%s,%s,%s)''',
        (staff_id, previous+1, event, calendar(effective_date), Jsonb(snapshot), reason, actor_id, request_key))
    return employee(conn, staff_id)


def hire_employee(conn, *, actor_id, request_key, employee_record_id, employee_entity_key,
                  employee_number, job_title, start_date, location_record_id=None,
                  department=None, employment_type='full_time'):
    identifier = conn.execute('INSERT INTO hr.staff(entity_key,record_id,employee_number) VALUES(%s,%s,%s) RETURNING id',
        (employee_entity_key, employee_record_id, employee_number)).fetchone()['id']
    snapshot = {'job_title': job_title, 'start_date': calendar(start_date).isoformat(),
        'employment_start_date': calendar(start_date).isoformat(), 'status': 'active', 'end_date': None,
        'location_record_id': location_record_id, 'department': department, 'employment_type': employment_type}
    return _event(conn, identifier, 'hired', start_date, snapshot, actor_id, request_key)


def update_employee(conn, *, actor_id, request_key, staff_id, effective_date, reason=None, **changes):
    person = employee(conn, staff_id, active=True)
    allowed = {'job_title', 'department', 'location_record_id', 'employment_type'}
    if not changes or set(changes)-allowed:
        raise ValidationError('Provide an employment role, department, location or employment type to update')
    snapshot = dict(person['snapshot']) | changes
    return _event(conn, staff_id, 'updated', effective_date, snapshot, actor_id, request_key, reason)


def rehire_employee(conn, *, actor_id, request_key, staff_id, rehire_date, reason=None, **changes):
    person = employee(conn, staff_id)
    if person['status'] != 'terminated':
        raise Conflict('Only a terminated staff member can be rehired')
    snapshot = dict(person['snapshot']) | changes | {'status': 'active', 'end_date': None,
        'employment_start_date': calendar(rehire_date).isoformat()}
    return _event(conn, staff_id, 'rehired', rehire_date, snapshot, actor_id, request_key, reason)


def leave_type(conn, code):
    row = conn.execute('SELECT * FROM hr.leave_type_state WHERE code=%s', (code,)).fetchone()
    if row is None:
        raise NotFound('Leave type not found; define its allowance first')
    return dict(row)


def set_leave_allowance(conn, *, actor_id, request_key, leave_type, name, annual_allowance_half_days,
                        paid=True, tracks_balance=True, reason='Owner-approved allowance policy'):
    old = conn.execute('SELECT * FROM hr.leave_types WHERE code=%s', (leave_type,)).fetchone()
    if old:
        if (old['name'], old['paid'], old['tracks_balance']) != (name, paid, tracks_balance):
            raise Conflict('An existing leave type retains its name and paid/tracked policy; only its allowance changes')
    else:
        conn.execute('INSERT INTO hr.leave_types(code,name,paid,tracks_balance) VALUES(%s,%s,%s,%s)', (leave_type, name, paid, tracks_balance))
    if not tracks_balance and annual_allowance_half_days:
        raise ValidationError('Untracked leave cannot have an allowance')
    conn.execute('''INSERT INTO hr.leave_policy_changes(leave_type,annual_allowance_half_days,reason,actor_id,request_key)
        VALUES(%s,%s,%s,%s,%s)''', (leave_type, annual_allowance_half_days, reason, actor_id, request_key))
    return dict(conn.execute('SELECT * FROM hr.leave_type_state WHERE code=%s', (leave_type,)).fetchone())


def balance(conn, staff_id, kind):
    policy = leave_type(conn, kind)
    values = conn.execute('SELECT hr.balance(%s,%s) AS balance_half_days,hr.pending(%s,%s) AS pending_half_days',
                          (staff_id, kind, staff_id, kind)).fetchone()
    return {'staff_id': staff_id, 'leave_type': kind,
        'balance_half_days': values['balance_half_days'] if policy['tracks_balance'] else None,
        'pending_half_days': values['pending_half_days'],
        'available_half_days': values['balance_half_days']-values['pending_half_days'] if policy['tracks_balance'] else None}


def _ledger(conn, staff_id, kind, event, amount, actor_id, key, *, request_id=None, year=None,
            reason=None, termination_date=None):
    conn.execute('''INSERT INTO hr.leave_ledger(staff_id,leave_type,event,half_days,request_id,accrual_year,as_of,reason,termination_date,actor_id,request_key)
        VALUES(%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)''',
        (staff_id, kind, event, amount, request_id, year, today(), reason, termination_date, actor_id, key))


def accrue_annual_leave(conn, *, actor_id, request_key, staff_id, leave_type, year):
    person = employee(conn, staff_id, active=True)
    policy = globals()['leave_type'](conn, leave_type)
    if not policy['tracks_balance']:
        raise Conflict('Untracked leave has no accrual balance')
    first, last = date(year, 1, 1), date(year, 12, 31)
    start = calendar(person['employment_start_date'])
    if start > last:
        raise ValidationError('Cannot accrue annual leave before the current employment begins')
    amount = policy['annual_allowance_half_days']
    if start > first:
        amount = amount * ((last-start).days+1) // ((last-first).days+1)
    _ledger(conn, staff_id, leave_type, 'grant', amount, actor_id, request_key, year=year)
    return balance(conn, staff_id, leave_type) | {'granted_half_days': amount, 'year': year}


def adjust_leave(conn, *, actor_id, request_key, staff_id, leave_type, half_days, reason):
    employee(conn, staff_id, active=True)
    if not half_days:
        raise ValidationError('A leave adjustment needs a nonzero amount')
    _ledger(conn, staff_id, leave_type, 'adjustment', half_days, actor_id, request_key, reason=reason)
    return balance(conn, staff_id, leave_type)


def request(conn, request_id):
    row = conn.execute('SELECT * FROM hr.leave_request_state WHERE id=%s', (request_id,)).fetchone()
    if row is None:
        raise NotFound('Leave request not found')
    return dict(row)


def _working(first, last, half=None):
    if last < first or (last-first).days > 3660:
        raise ValidationError('Leave dates must be ordered and span at most ten years')
    if half is not None:
        if first != last or first.weekday() > 4:
            raise ValidationError('A half-day request must be one weekday')
        return 1
    return sum(2 for offset in range((last-first).days+1) if (first+timedelta(days=offset)).weekday() < 5)


def _request(conn, actor_id, key, staff_id, kind, start_date, end_date, half=None, note=None, replaces=None):
    employee(conn, staff_id, active=True)
    leave_type(conn, kind)
    first, last = calendar(start_date), calendar(end_date)
    amount = _working(first, last, half)
    if not amount:
        raise ValidationError('The request must contain at least one working half-day')
    identifier = conn.execute('''INSERT INTO hr.leave_requests(staff_id,leave_type,start_date,end_date,half,half_days,note,replaces_request_id,actor_id,request_key)
        VALUES(%s,%s,%s,%s,%s,%s,%s,%s,%s,%s) RETURNING id''',
        (staff_id, kind, first, last, half, amount, note, replaces, actor_id, key)).fetchone()['id']
    _ledger(conn, staff_id, kind, 'request', 0, actor_id, key, request_id=identifier)
    return request(conn, identifier)


def request_leave(conn, *, actor_id, request_key, staff_id, leave_type, start_date, end_date, half=None, note=None):
    return _request(conn, actor_id, request_key, staff_id, leave_type, start_date, end_date, half, note)


def approve_leave(conn, *, actor_id, request_key, request_id):
    row = request(conn, request_id)
    _ledger(conn, row['staff_id'], row['leave_type'], 'approve', -row['half_days'], actor_id, request_key, request_id=request_id)
    return request(conn, request_id)


def reject_leave(conn, *, actor_id, request_key, request_id, reason):
    row = request(conn, request_id)
    _ledger(conn, row['staff_id'], row['leave_type'], 'reject', 0, actor_id, request_key, request_id=request_id, reason=reason)
    return request(conn, request_id)


def cancel_leave(conn, *, actor_id, request_key, request_id, reason=None):
    row = request(conn, request_id)
    _ledger(conn, row['staff_id'], row['leave_type'], 'cancel', row['half_days'] if row['status']=='approved' else 0,
        actor_id, request_key, request_id=request_id, reason=reason)
    return request(conn, request_id)


def terminate_employee(conn, *, actor_id, request_key, staff_id, end_date, reason):
    person = employee(conn, staff_id, active=True)
    end = calendar(end_date)
    if end < calendar(person['effective_date']):
        raise ValidationError('Termination cannot predate the latest employment event')
    # Cancel future scheduled work before the employment event. This is in the
    # same request transaction as leave settlement and the final state change.
    future_shifts = conn.execute("SELECT id FROM hr.shift_state WHERE staff_id=%s AND status='scheduled' AND work_date>%s ORDER BY id", (staff_id, end)).fetchall()
    for shift in future_shifts:
        cancel_shift(conn, actor_id=actor_id, request_key=request_key, shift_id=shift['id'], reason='Employment termination: '+reason)
    cancelled, shortened = [], []
    rows = conn.execute("SELECT * FROM hr.leave_request_state WHERE staff_id=%s AND status IN('pending','approved') ORDER BY id", (staff_id,)).fetchall()
    for row in rows:
        if row['status']=='approved' and row['end_date']<=end:
            continue
        amount = row['half_days'] if row['status']=='approved' else 0
        _ledger(conn, staff_id, row['leave_type'], 'cancel', amount, actor_id, request_key,
            request_id=row['id'], reason='Employment termination: '+reason, termination_date=end)
        cancelled.append(row['id'])
        if row['status']=='approved' and row['start_date']<=end and _working(row['start_date'],end,row['half']):
            replacement = _request(conn, actor_id, request_key, staff_id, row['leave_type'], row['start_date'], end,
                row['half'], 'Shortened on termination', replaces=row['id'])
            _ledger(conn, staff_id, row['leave_type'], 'approve', -replacement['half_days'], actor_id, request_key, request_id=replacement['id'])
            shortened.append({'request_id': row['id'], 'replaced_by': replacement['id'],
                'kept_half_days': replacement['half_days'], 'credited_half_days': amount-replacement['half_days']})
    snapshot = dict(person['snapshot']) | {'status': 'terminated', 'end_date': end.isoformat()}
    result = _event(conn, staff_id, 'terminated', end, snapshot, actor_id, request_key, reason)
    return result | {'cancelled_leave_requests': cancelled, 'shortened_leave': shortened}


def schedule_shift(conn, *, actor_id, request_key, staff_id, location_record_id, location_entity_key, work_date, start_time, end_time):
    employee(conn, staff_id, active=True)
    identifier = conn.execute('''INSERT INTO hr.shifts(staff_id,location_entity_key,location_record_id,work_date,start_time,end_time,actor_id,request_key)
        VALUES(%s,%s,%s,%s,%s,%s,%s,%s) RETURNING id''',
        (staff_id, location_entity_key, location_record_id, calendar(work_date), start_time, end_time, actor_id, request_key)).fetchone()['id']
    conn.execute("INSERT INTO hr.shift_events(shift_id,event,actor_id,request_key) VALUES(%s,'scheduled',%s,%s)", (identifier, actor_id, request_key))
    return dict(conn.execute('SELECT * FROM hr.shift_state WHERE id=%s', (identifier,)).fetchone())


def cancel_shift(conn, *, actor_id, request_key, shift_id, reason):
    if conn.execute('SELECT 1 FROM hr.shift_state WHERE id=%s', (shift_id,)).fetchone() is None:
        raise NotFound('Shift not found')
    conn.execute("INSERT INTO hr.shift_events(shift_id,event,reason,actor_id,request_key) VALUES(%s,'cancelled',%s,%s,%s)", (shift_id, reason, actor_id, request_key))
    return dict(conn.execute('SELECT * FROM hr.shift_state WHERE id=%s', (shift_id,)).fetchone())


def add_onboarding_task(conn, *, actor_id, request_key, staff_id, title, due_date=None):
    employee(conn, staff_id, active=True)
    identifier = conn.execute('''INSERT INTO hr.onboarding_tasks(staff_id,title,due_date,actor_id,request_key)
        VALUES(%s,%s,%s,%s,%s) RETURNING id''', (staff_id, title, calendar(due_date) if due_date else None, actor_id, request_key)).fetchone()['id']
    conn.execute("INSERT INTO hr.onboarding_events(task_id,event,actor_id,request_key) VALUES(%s,'added',%s,%s)", (identifier, actor_id, request_key))
    return dict(conn.execute('SELECT * FROM hr.onboarding_state WHERE id=%s', (identifier,)).fetchone())


def complete_onboarding_task(conn, *, actor_id, request_key, task_id):
    return _onboarding_step(conn, actor_id, request_key, task_id, 'completed')


def reopen_onboarding_task(conn, *, actor_id, request_key, task_id):
    return _onboarding_step(conn, actor_id, request_key, task_id, 'reopened')


def _onboarding_step(conn, actor_id, key, task_id, event):
    if conn.execute('SELECT 1 FROM hr.onboarding_tasks WHERE id=%s', (task_id,)).fetchone() is None:
        raise NotFound('Onboarding task not found')
    conn.execute('INSERT INTO hr.onboarding_events(task_id,event,actor_id,request_key) VALUES(%s,%s,%s,%s)', (task_id, event, actor_id, key))
    return dict(conn.execute('SELECT * FROM hr.onboarding_state WHERE id=%s', (task_id,)).fetchone())
