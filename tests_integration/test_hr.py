"""Native HR module HTTP, raw-SQL and second-capability acceptance on local PG."""
from concurrent.futures import ThreadPoolExecutor
from datetime import date, timedelta
import unittest
from uuid import uuid4

import psycopg
from fastapi.testclient import TestClient

from tests_integration import test_umbrella_workflow as flow


class HRTests(flow.UmbrellaHTTPTests):
    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        from db import db as d
        from umbrella.businesses import initialize_registry
        d.init_db()
        initialize_registry()

    def setUp(self):
        super().setUp()
        self.employee = self.record('Employee', {'Name': 'Maya Reed', 'Email': 'maya@example.test'})
        self.hr_bindings = {
            'employee': {'entity': self.entities['Employee']['key'], 'fields': {
                'name': self.fields['Employee']['Name'], 'email': self.fields['Employee']['Email']}},
            'location': {'entity': self.entities['Location']['key'], 'fields': {
                'name': self.fields['Location']['Name'], 'address': self.fields['Location']['Address']}},
        }
        self.hire_date = self.today.replace(month=1, day=1).isoformat()
        self.monday = self.today + timedelta(days=(7-self.today.weekday()) % 7 or 7)

    def enable_hr(self):
        proposed = self.response(self.owner.post(self.base+'/nerds/hr/proposals', json={
            'base_version': self.toy['current_version'], 'bindings': self.hr_bindings}), 201)
        result = self.response(self.owner.post(self.base+f"/nerds/hr/proposals/{proposed['id']}/approve", json={}))
        self.assertEqual(result['status'], 'enabled')
        return result

    def hr(self, operation, payload, key=None, expected=200):
        return self.response(self.owner.post(self.base+'/hr/commands/'+operation,
            json=dict(payload, idempotency_key=key or uuid4().hex)), expected)

    def hire(self, number='E001', record=None):
        return self.hr('hire_employee', {'employee_record_id': (record or self.employee)['id'], 'employee_number': number,
            'job_title': 'Warehouse associate', 'start_date': self.hire_date, 'location_record_id': self.location['id']}, 'hire-'+number)

    def people(self):
        return self.response(self.owner.get(self.base+'/hr/workspace'))

    def policy(self, staff, half_days=40, code='pto'):
        self.hr('set_leave_allowance', {'leave_type': code, 'name': 'Paid time off' if code=='pto' else code,
            'annual_allowance_half_days': half_days})
        return self.hr('accrue_annual_leave', {'staff_id': staff['id'], 'leave_type': code, 'year': self.today.year}, 'accrue-'+code)

    def request(self, staff, start=None, end=None, half=None, key=None, expected=200):
        body = {'staff_id': staff['id'], 'leave_type': 'pto', 'start_date': (start or self.monday).isoformat(),
            'end_date': (end or start or self.monday).isoformat()}
        if half is not None:
            body['half'] = half
        return self.hr('request_leave', body, key=key, expected=expected)

    def test_hiring_uses_core_identity_and_employment_history_replays(self):
        self.enable_hr()
        first = self.hire()
        self.assertEqual(first, self.hire())
        self.assertEqual(first['employee_record_id'], self.employee['id'])
        self.hr('hire_employee', {'employee_record_id': self.customer['id'], 'employee_number': 'WRONG',
            'job_title': 'Wrong type', 'start_date': self.hire_date}, expected=404)
        self.hr('hire_employee', {'employee_record_id': self.employee['id'], 'employee_number': 'DUP',
            'job_title': 'Duplicate', 'start_date': self.hire_date}, expected=409)
        updated = self.hr('update_employee', {'staff_id': first['id'], 'job_title': 'Warehouse lead', 'effective_date': self.hire_date})
        self.assertEqual(updated['version'], 2)
        terminated = self.hr('terminate_employee', {'staff_id': first['id'], 'end_date': self.hire_date, 'reason': 'Season ended'}, 'terminate')
        self.assertEqual(terminated['status'], 'terminated')
        self.hr('update_employee', {'staff_id': first['id'], 'job_title': 'Not active', 'effective_date': self.hire_date}, expected=409)
        rehire_date = self.today.replace(month=1, day=2).isoformat()
        active = self.hr('rehire_employee', {'staff_id': first['id'], 'rehire_date': rehire_date, 'job_title': 'Returning lead'})
        self.assertEqual((active['status'], active['start_date'], active['employment_start_date']), ('active', self.hire_date, rehire_date))
        workspace = self.people()
        self.assertEqual([event['event'] for event in workspace['employment_history']], ['hired', 'updated', 'terminated', 'rehired'])
        self.assertEqual([event['version'] for event in workspace['employment_history']], [1, 2, 3, 4])
        self.response(self.owner.patch(self.base+f"/records/{self.entities['Employee']['key']}/{self.employee['id']}",
            json={'values': {self.fields['Employee']['Name']: 'Maya Updated'}}))
        self.assertEqual(self.people()['staff'][0]['name'], 'Maya Updated')
        self.assertIsNone(self.fixture.scalar(self.bid, "SELECT to_regnamespace('logistics')"))
        self.response(self.stranger.get(self.base+'/hr/workspace'), 404)
        self.hr('hire_employee', {'employee_record_id': self.employee['id'], 'employee_number': 'INJECT',
            'job_title': 'Injected actor', 'start_date': self.hire_date, 'actor_id': 999}, expected=422)

    def test_exact_halfday_holds_approve_reject_cancel_and_annual_uniqueness(self):
        self.enable_hr()
        staff = self.hire()
        self.assertEqual(self.policy(staff)['granted_half_days'], 40)
        self.hr('accrue_annual_leave', {'staff_id': staff['id'], 'leave_type': 'pto', 'year': self.today.year}, expected=409)
        week = self.request(staff, self.monday, self.monday+timedelta(days=4), key='week')
        self.assertEqual((week['status'], week['half_days']), ('pending', 10))
        balance = self.people()['balances'][0]
        self.assertEqual((balance['balance_half_days'], balance['pending_half_days'], balance['available_half_days']), (40, 10, 30))
        approved = self.hr('approve_leave', {'request_id': week['id']}, 'approve-week')
        self.assertEqual(approved['status'], 'approved')
        self.assertEqual(self.hr('approve_leave', {'request_id': week['id']}, 'approve-week'), approved)
        self.hr('cancel_leave', {'request_id': week['id'], 'reason': 'Plans changed'})
        day = self.monday+timedelta(days=7)
        am = self.request(staff, day, half='am')
        pm = self.request(staff, day, half='pm')
        self.assertEqual((am['half_days'], pm['half_days']), (1, 1))
        self.request(staff, day, expected=409)
        self.hr('reject_leave', {'request_id': am['id'], 'reason': 'Coverage needed'})
        self.hr('cancel_leave', {'request_id': pm['id']})
        balance = self.people()['balances'][0]
        self.assertEqual((balance['balance_half_days'], balance['pending_half_days'], balance['available_half_days']), (40, 0, 40))
        self.hr('request_leave', {'staff_id': staff['id'], 'leave_type': 'pto', 'start_date': '2026-02-30', 'end_date': '2026-02-30'}, expected=422)
        self.hr('adjust_leave', {'staff_id': staff['id'], 'leave_type': 'pto', 'half_days': 0.5, 'reason': 'Invalid units'}, expected=422)

    def test_pending_holds_prevent_concurrent_overspending(self):
        self.enable_hr()
        staff = self.hire()
        self.policy(staff, 8)
        cookies = dict(self.owner.cookies)
        payloads = [{'staff_id': staff['id'], 'leave_type': 'pto', 'start_date': (self.monday+timedelta(days=offset)).isoformat(),
            'end_date': (self.monday+timedelta(days=offset+2)).isoformat(), 'idempotency_key': 'competing-'+str(offset)} for offset in (0, 7)]
        def request(payload):
            with TestClient(self.app, base_url='https://testserver') as client:
                client.cookies.update(cookies)
                return client.post(self.base+'/hr/commands/request_leave', json=payload).status_code
        with ThreadPoolExecutor(max_workers=2) as pool:
            statuses = list(pool.map(request, payloads))
        self.assertEqual(sorted(statuses), [200, 409])
        balance = self.people()['balances'][0]
        self.assertEqual((balance['balance_half_days'], balance['pending_half_days'], balance['available_half_days']), (8, 6, 2))
        self.hr('adjust_leave', {'staff_id': staff['id'], 'leave_type': 'pto', 'half_days': -3, 'reason': 'Cannot consume held leave'}, expected=409)

    def test_termination_settles_future_and_spanning_leave_and_rehire_keeps_balance(self):
        self.enable_hr()
        staff = self.hire()
        self.policy(staff)
        span = self.request(staff, self.monday, self.monday+timedelta(days=4))
        self.hr('approve_leave', {'request_id': span['id']})
        pending = self.request(staff, self.monday+timedelta(days=7), half='am')
        end = self.monday+timedelta(days=1)
        terminated = self.hr('terminate_employee', {'staff_id': staff['id'], 'end_date': end.isoformat(), 'reason': 'Season ended'}, 'settled-termination')
        self.assertEqual(terminated['status'], 'terminated')
        self.assertEqual(set(terminated['cancelled_leave_requests']), {span['id'], pending['id']})
        self.assertEqual(terminated['shortened_leave'][0]['kept_half_days'], 4)
        self.assertEqual(terminated['shortened_leave'][0]['credited_half_days'], 6)
        self.assertEqual(self.people()['balances'][0]['balance_half_days'], 36)
        self.hr('approve_leave', {'request_id': pending['id']}, expected=409)
        rehired = self.hr('rehire_employee', {'staff_id': staff['id'], 'rehire_date': (end+timedelta(days=1)).isoformat()})
        self.assertEqual(rehired['status'], 'active')
        self.hr('accrue_annual_leave', {'staff_id': staff['id'], 'leave_type': 'pto', 'year': self.today.year}, expected=409)
        self.assertEqual(self.people()['balances'][0]['balance_half_days'], 36)
        self.assertEqual(self.hr('terminate_employee', {'staff_id': staff['id'], 'end_date': end.isoformat(), 'reason': 'Season ended'}, 'settled-termination'), terminated)
        self.assertEqual(self.people()['staff'][0]['status'], 'active')

    def test_raw_sql_cannot_rewrite_history_or_forge_employee_leave_and_amounts(self):
        self.enable_hr()
        staff = self.hire()
        self.policy(staff)
        request = self.request(staff, self.monday, half='am')
        url = self.fixture.business_url(self.bid)
        for statement in ('UPDATE hr.employment_events SET version=100', 'DELETE FROM hr.leave_ledger',
                          'TRUNCATE hr.employment_events', 'TRUNCATE hr.leave_ledger',
                          "UPDATE hr.staff SET employee_number='REWRITTEN'"):
            with self.subTest(statement=statement), psycopg.connect(url, autocommit=True) as conn:
                with self.assertRaises(psycopg.errors.CheckViolation):
                    conn.execute(statement)
        with psycopg.connect(url, autocommit=True) as conn:
            with self.assertRaises(psycopg.errors.CheckViolation):
                conn.execute('''INSERT INTO hr.staff(entity_key,record_id,employee_number) VALUES(%s,%s,'WRONG-TYPE')''',
                    (self.entities['Customer']['key'], self.customer['id']))
            with self.assertRaises(psycopg.errors.CheckViolation):
                conn.execute('''INSERT INTO hr.leave_requests(staff_id,leave_type,start_date,end_date,half,half_days,actor_id,request_key)
                    VALUES(%s,'pto',%s,%s,'pm',99,1,'bad-request')''', (staff['id'], self.monday, self.monday))
            with self.assertRaises(psycopg.errors.CheckViolation):
                conn.execute('''INSERT INTO hr.leave_ledger(staff_id,leave_type,event,half_days,request_id,as_of,actor_id,request_key)
                    VALUES(%s,'pto','approve',-99,%s,%s,1,'forged-debit')''', (staff['id'], request['id'], self.today))
            from psycopg.types.json import Jsonb
            snapshot = self.people()['staff'][0]['snapshot']
            for change in ({'status': None}, {'employment_type': None}, {'job_title': 42}, {'department': []}):
                with self.subTest(snapshot_change=change), self.assertRaises(psycopg.errors.CheckViolation):
                    conn.execute('''INSERT INTO hr.employment_events(staff_id,version,event,effective_date,snapshot,actor_id,request_key)
                        VALUES(%s,2,'updated',%s,%s,1,'bad-snapshot')''', (staff['id'], self.hire_date, Jsonb(snapshot | change)))
        self.assertEqual(self.people()['balances'][0]['balance_half_days'], 40)
        self.assertEqual(self.people()['leave_requests'][0]['status'], 'pending')

    def test_second_nerd_enablement_and_hr_work_leave_logistics_data_untouched(self):
        self.enable()
        workspace = self.workspace()
        item = self.reference(workspace, 'item', 'Toy Robot')['id']
        location = self.reference(workspace, 'location', 'Toy Warehouse')['id']
        self.command('open_stock', {'item_id': item, 'location_id': location, 'quantity': 4, 'source_ref': 'Second-Nerd isolation fixture'})
        def snapshot():
            with psycopg.connect(self.fixture.business_url(self.bid), autocommit=True) as conn:
                tables = [row[0] for row in conn.execute("SELECT tablename FROM pg_tables WHERE schemaname='logistics' ORDER BY tablename")]
                return {table: [row[0] for row in conn.execute(psycopg.sql.SQL('SELECT row_to_json(t)::text FROM logistics.{} t ORDER BY row_to_json(t)::text').format(psycopg.sql.Identifier(table)))] for table in tables}
        before = snapshot()
        self.enable_hr()
        staff = self.hire()
        self.policy(staff)
        requested = self.request(staff, half='am')
        self.hr('approve_leave', {'request_id': requested['id']})
        self.assertEqual(snapshot(), before)
        capabilities = self.response(self.owner.get(self.base+'/nerds'))['nerds']
        self.assertEqual({row['id']: row['status'] for row in capabilities if row['id'] in ('hr','logistics')}, {'hr':'enabled','logistics':'enabled'})
        self.response(self.owner.post(self.base+'/nerds/hr/disable', json={'confirm': True}))
        self.assertEqual(snapshot(), before)
        self.response(self.owner.get(self.base+'/hr/workspace'), 409)
        self.assertEqual(self.response(self.owner.get(self.base+'/logistics/workspace'))['stock'][0]['on_hand'], 4)

    def test_midyear_allowance_is_prorated_with_integer_half_days(self):
        self.enable_hr()
        start = date(self.today.year, 7, 1)
        staff = self.hr('hire_employee', {'employee_record_id': self.employee['id'], 'employee_number': 'MIDYEAR',
            'job_title': 'Seasonal associate', 'start_date': start.isoformat()})
        granted = self.policy(staff)
        first, last = date(start.year, 1, 1), date(start.year, 12, 31)
        expected = 40*((last-start).days+1)//((last-first).days+1)
        self.assertEqual(granted['granted_half_days'], expected)
        self.assertEqual(self.people()['balances'][0]['available_half_days'], expected)

    def test_shifts_use_core_locations_and_conflict_with_work_and_approved_leave(self):
        self.enable_hr()
        staff = self.hire()
        self.policy(staff)
        payload = {'staff_id': staff['id'], 'location_record_id': self.location['id'],
            'work_date': self.monday.isoformat(), 'start_time': '09:00', 'end_time': '17:00'}
        shift = self.hr('schedule_shift', payload, 'shift-once')
        self.assertEqual(shift['status'], 'scheduled')
        self.assertEqual(self.hr('schedule_shift', payload, 'shift-once'), shift)
        self.hr('schedule_shift', payload | {'start_time': '13:00', 'end_time': '18:00'}, expected=409)
        self.hr('schedule_shift', payload | {'location_record_id': self.employee['id']}, expected=404)
        morning = self.request(staff, self.monday, half='am')
        self.hr('approve_leave', {'request_id': morning['id']}, expected=409)
        self.hr('cancel_shift', {'shift_id': shift['id'], 'reason': 'Approve morning leave'})
        self.hr('approve_leave', {'request_id': morning['id']})
        afternoon = self.hr('schedule_shift', payload | {'start_time': '13:00'})
        self.assertEqual(afternoon['status'], 'scheduled')
        self.hr('schedule_shift', payload | {'end_time': '11:00'}, expected=409)
        future = self.hr('schedule_shift', payload | {'work_date': (self.monday+timedelta(days=7)).isoformat()})
        self.hr('terminate_employee', {'staff_id': staff['id'], 'end_date': (self.monday+timedelta(days=1)).isoformat(), 'reason': 'Season ended'})
        states = {row['id']: row['status'] for row in self.people()['shifts']}
        self.assertEqual(states[future['id']], 'cancelled')
        self.assertEqual(states[afternoon['id']], 'scheduled')  # Work before the end date is preserved.

    def test_onboarding_checklist_has_replay_safe_completion_and_immutable_history(self):
        self.enable_hr()
        staff = self.hire()
        task = self.hr('add_onboarding_task', {'staff_id': staff['id'], 'title': 'Safety orientation', 'due_date': self.monday.isoformat()}, 'onboard-add')
        self.assertEqual(task['status'], 'pending')
        completed = self.hr('complete_onboarding_task', {'task_id': task['id']}, 'onboard-complete')
        self.assertEqual(completed['status'], 'completed')
        self.assertEqual(self.hr('complete_onboarding_task', {'task_id': task['id']}, 'onboard-complete'), completed)
        self.hr('complete_onboarding_task', {'task_id': task['id']}, expected=409)
        self.assertEqual(self.hr('reopen_onboarding_task', {'task_id': task['id']})['status'], 'pending')
        self.hr('complete_onboarding_task', {'task_id': task['id']})
        self.assertEqual(self.people()['onboarding_tasks'][0]['status'], 'completed')
        with psycopg.connect(self.fixture.business_url(self.bid), autocommit=True) as conn:
            for statement in ("UPDATE hr.onboarding_tasks SET title='Rewrite'", 'DELETE FROM hr.onboarding_events'):
                with self.assertRaises(psycopg.errors.CheckViolation):
                    conn.execute(statement)
            self.assertEqual(conn.execute('SELECT count(*) FROM hr.schema_migrations').fetchone()[0], 3)

    def test_raw_sql_concurrent_requests_respect_pending_holds(self):
        self.enable_hr()
        staff = self.hire()
        self.policy(staff, 8)
        actor = self.people()['employment_history'][0]['actor_id']
        url = self.fixture.business_url(self.bid)

        def raw_request(offset):
            key = 'raw-leave-'+str(offset)
            try:
                with psycopg.connect(url, autocommit=True) as conn:
                    with conn.transaction():
                        request = conn.execute('''INSERT INTO hr.leave_requests(staff_id,leave_type,start_date,end_date,half_days,actor_id,request_key)
                            VALUES(%s,'pto',%s,%s,6,%s,%s) RETURNING id''',
                            (staff['id'], self.monday+timedelta(days=offset), self.monday+timedelta(days=offset+2), actor, key)).fetchone()[0]
                        conn.execute('''INSERT INTO hr.leave_ledger(staff_id,leave_type,event,half_days,request_id,as_of,actor_id,request_key)
                            VALUES(%s,'pto','request',0,%s,%s,%s,%s)''', (staff['id'], request, self.today, actor, key))
                        conn.execute("INSERT INTO hr.command_requests(idempotency_key,operation,payload,result) VALUES(%s,'raw-test','{}','{}')", (key,))
                return True
            except psycopg.errors.CheckViolation:
                return False

        with ThreadPoolExecutor(max_workers=2) as pool:
            self.assertEqual(sorted(pool.map(raw_request, (0, 7))), [False, True])
        self.assertEqual(self.people()['balances'][0]['pending_half_days'], 6)


def load_tests(loader, tests, pattern):
    return unittest.TestSuite(HRTests(name) for name, method in HRTests.__dict__.items()
        if name.startswith('test_') and callable(method))


if __name__ == '__main__':
    unittest.main()
