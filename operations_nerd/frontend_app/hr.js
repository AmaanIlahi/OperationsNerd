'use strict';

BusinessModules.register('hr', {
  roles: {
    employee: {label: 'Employees', names: ['staff'], fields: {name: {label: 'Name', type: 'text'}, email: {label: 'Email', type: 'email', optional: true}}},
    location: {label: 'Locations', names: ['warehouse'], fields: {name: {label: 'Name', type: 'text'}, address: {label: 'Address', type: 'text', optional: true}}},
  },
  setupDescription: 'HR links employment and leave records to the employees and locations already in your CRM. Names and contact details continue to come from those shared records.',
  renderWorkspace(ctx, w, h) {
    const {el, table, form, field, select, day, label} = h;
    const staff = w.staff || [], active = staff.filter(person => person.status === 'active');
    const employeeOptions = (w.core_employees || []).map(person => [person.record_id, person.name]);
    const staffOptions = people => people.map(person => [person.id, person.name + ' · ' + person.employee_number]);
    const locationOptions = (w.locations || []).map(location => [location.record_id, location.name]);
    const leaveTypes = (w.leave_types || []).map(type => [type.code, type.name]);
    const requests = w.leave_requests || [];
    const staffName = id => staff.find(person => person.id === id)?.name || '#' + id;
    const requestOptions = rows => rows.map(request => [request.id, '#' + request.id + ' · ' + staffName(request.staff_id) + ' · ' + request.start_date + '–' + request.end_date]);
    const text = (name, value, optional) => field(name, 'text', {value, optional});
    const date = name => field(name, 'date', {value: day()});
    function halfDays(value) {
      const match = /^(\d+)(?:\.([05]))?$/.exec(value);
      if (!match) throw new Error('Use whole days or half days, for example 20 or 20.5.');
      return Number(match[1]) * 2 + (match[2] === '5' ? 1 : 0);
    }
    const people = () => el('div', null,
      el('div', {class: 'row logistics-actions'}, el('button', {text: 'Open employee records', onclick: () => ctx.openRecords(ctx.nerd.bindings.employee.entity)}),
        el('span', {class: 'muted', text: active.length + ' active employee(s)'})),
      table('Staff', staff, [['Employee', person => person.name], ['Number', person => person.employee_number], ['Job title', person => person.job_title], ['Department', person => person.department], ['Location', person => person.location_name], ['Started', person => person.start_date], ['Status', person => label(person.status)]]),
      el('div', {class: 'logistics-grid'},
        form('Hire employee', {employee_record_id: select('Employee record', employeeOptions.filter(([id]) => !staff.some(person => person.employee_record_id === id))),
          employee_number: text('Employee number'), job_title: text('Job title'), start_date: date('Start date'), location_record_id: select('Work location', locationOptions, true),
          department: text('Department', '', true), employment_type: text('Employment type', 'full_time', true)}, 'hire_employee', null,
          'Create the employee in CRM first, then add their employment details here.'),
        form('Update employment', {staff_id: select('Employee', staffOptions(active)), job_title: text('New job title', '', true), department: text('New department', '', true),
          location_record_id: select('New location', locationOptions, true), employment_type: text('New employment type', '', true), effective_date: date('Effective date'), reason: text('Reason', '', true)}, 'update_employee'),
        form('End employment', {staff_id: select('Employee', staffOptions(active)), end_date: date('End date'), reason: text('Reason')}, 'terminate_employee'),
        form('Rehire employee', {staff_id: select('Former employee', staffOptions(staff.filter(person => person.status !== 'active'))), rehire_date: date('Rehire date'),
          job_title: text('Job title', '', true), location_record_id: select('Work location', locationOptions, true), reason: text('Reason', '', true)}, 'rehire_employee')));
    const leave = () => el('div', null,
      table('Leave balances', w.balances || [], [['Employee', row => staffName(row.staff_id)], ['Leave type', row => row.leave_type], ['Balance (days)', row => row.balance_half_days === null ? 'Not tracked' : row.balance_half_days / 2], ['Pending (days)', row => row.pending_half_days / 2], ['Available (days)', row => row.available_half_days === null ? 'Not tracked' : row.available_half_days / 2]]),
      table('Leave requests', requests, [['Request', row => row.id], ['Employee', row => staffName(row.staff_id)], ['Type', row => row.leave_type], ['From', row => row.start_date], ['To', row => row.end_date], ['Days', row => row.half_days / 2], ['Status', row => label(row.status)]]),
      el('div', {class: 'logistics-grid'},
        form('Request leave', {staff_id: select('Employee', staffOptions(active)), leave_type: select('Leave type', leaveTypes), start_date: date('First day'), end_date: date('Last day'),
          half: select('Half day', [['am', 'Morning'], ['pm', 'Afternoon']], true), note: text('Note', '', true)}, 'request_leave', null, 'Leave the half-day option blank for whole days.'),
        form('Approve leave', {request_id: select('Pending request', requestOptions(requests.filter(request => request.status === 'pending')))}, 'approve_leave'),
        form('Reject leave', {request_id: select('Pending request', requestOptions(requests.filter(request => request.status === 'pending'))), reason: text('Reason')}, 'reject_leave'),
        form('Cancel leave', {request_id: select('Request', requestOptions(requests.filter(request => ['pending', 'approved'].includes(request.status)))), reason: text('Reason', '', true)}, 'cancel_leave')));
    const allowances = () => el('div', null,
      table('Leave types', w.leave_types || [], [['Code', row => row.code], ['Name', row => row.name], ['Annual allowance (days)', row => row.annual_allowance_half_days / 2], ['Paid', row => row.paid ? 'Yes' : 'No'], ['Tracks balance', row => row.tracks_balance ? 'Yes' : 'No']]),
      el('div', {class: 'logistics-grid'},
        form('Set leave allowance', {leave_type: text('Leave code', 'annual'), name: text('Leave name', 'Annual leave'),
          annual_allowance_half_days: field('Annual allowance (days)', 'text', {value: '20', pattern: '\\d+(\\.[05])?', inputMode: 'decimal'}),
          paid: select('Paid leave', [['true', 'Yes'], ['false', 'No']]), tracks_balance: select('Track balance', [['true', 'Yes'], ['false', 'No']]), reason: text('Reason', '', true)},
        'set_leave_allowance', payload => ({...payload, annual_allowance_half_days: halfDays(payload.annual_allowance_half_days), paid: payload.paid === 'true', tracks_balance: payload.tracks_balance === 'true'})),
        form('Accrue annual leave', {staff_id: select('Employee', staffOptions(active)), leave_type: select('Leave type', leaveTypes), year: field('Year', 'integer', {value: new Date().getFullYear()})}, 'accrue_annual_leave')));
    const history = () => el('div', null,
      table('Employment history', w.employment_history || [], [['Employee', row => staffName(row.staff_id)], ['Change', row => label(row.event)], ['Effective', row => row.effective_date], ['Version', row => row.version], ['Reason', row => row.reason || row.snapshot?.reason]]),
      table('Leave history', w.leave_ledger || [], [['Employee', row => staffName(row.staff_id)], ['Leave type', row => row.leave_type], ['Change (days)', row => (row.half_days ?? row.delta_half_days ?? 0) / 2], ['Event', row => label(row.event || row.kind)], ['Date', row => row.effective_date || row.created_at]]));
    const shifts = () => el('div', null,
      table('Work shifts', w.shifts || [], [['Employee', row => staffName(row.staff_id)], ['Location', row => (w.locations || []).find(location => location.record_id === row.location_record_id)?.name || row.location_record_id], ['Date', row => row.work_date], ['Start', row => row.start_time], ['End', row => row.end_time], ['Status', row => label(row.status)]]),
      el('div', {class: 'logistics-grid'},
        form('Schedule shift', {staff_id: select('Employee', staffOptions(active)), location_record_id: select('Work location', locationOptions), work_date: date('Work date'),
          start_time: field('Start time', 'time', {value: '09:00'}), end_time: field('End time', 'time', {value: '17:00'})}, 'schedule_shift'),
        form('Cancel shift', {shift_id: select('Shift', (w.shifts || []).filter(shift => shift.status !== 'cancelled').map(shift => [shift.id, staffName(shift.staff_id) + ' · ' + shift.work_date + ' · ' + shift.start_time])), reason: text('Reason')}, 'cancel_shift')));
    const onboarding = () => el('div', null,
      table('Onboarding checklist', w.onboarding_tasks || [], [['Employee', row => staffName(row.staff_id)], ['Task', row => row.title], ['Due', row => row.due_date], ['Status', row => label(row.status)]]),
      el('div', {class: 'logistics-grid'},
        form('Add onboarding task', {staff_id: select('Employee', staffOptions(active)), title: text('Task'), due_date: field('Due date', 'date', {optional: true})}, 'add_onboarding_task'),
        form('Complete onboarding task', {task_id: select('Task', (w.onboarding_tasks || []).filter(task => task.status !== 'completed').map(task => [task.id, staffName(task.staff_id) + ' · ' + task.title]))}, 'complete_onboarding_task')));
    return h.tabs(['Staff', 'Leave', 'Allowances', 'Shifts', 'Onboarding', 'History'], tab => tab === 'Staff' ? people() : tab === 'Leave' ? leave() : tab === 'Allowances' ? allowances() : tab === 'Shifts' ? shifts() : tab === 'Onboarding' ? onboarding() : history());
  },
});
