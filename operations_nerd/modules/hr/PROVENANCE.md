# Native PostgreSQL HR capability

Reference: OperationsNerd PR #24, fetched head
`702a8e64d202a18fd3455e4d2cfe093d6dcfff4b`, `modules/hr/README.md`,
`hr.py`, and migrations 0001-0003. That work credits Harsh's PR #21 for
the original HR field design. This integration is a new native PostgreSQL
implementation of the documented employment/leave behavior, not a claim of
byte-for-byte porting or passing the upstream SQLite suite.

Preserved ideas: append-only employment snapshots and monotonic versions,
hire/update/terminate/rehire, exact integer half-day leave ledger, pending holds,
weekday/AM/PM overlap checks, annual-policy proration, once-per-year accrual,
approval/rejection/cancellation, terminal employment and termination settlement,
global request-key uniqueness within HR, atomic command results and raw-SQL
integrity guards. The PostgreSQL schema uses its own checksum migration history.

The November umbrella's proposed shift planning and onboarding are implemented
in forward migration 0002: shifts use mapped locations, reject overlapping work
and approved leave, and preserve cancellation events; onboarding tasks retain
add/complete/reopen history. Approving conflicting leave requires cancelling the
shift first. Termination cancels future scheduled shifts in the same transaction.

Core owns authoritative people names/emails and locations, plus accounts,
sessions, owner authorization, approvals and database routing. `hr.staff` keeps
only the permanent core record reference and stable employment number; job
information belongs to employment events. Current display reads core directly.
There is no copied editable employee directory, SQLite router, standalone token,
or import/call to another Nerd. Actor IDs always come from the authenticated
owner. The current permission model is owner-only.

Leave uses Monday-Friday working days, integer half-day units and a UTC server
business date. It does not infer local holidays. Public approved-leave cancellation
is allowed before leave starts; termination can settle pending/future/spanning
leave atomically, retaining only the portion through the employment end date.
Historical balances carry across rehire; a year's grant is never issued twice.
Names/emails remain ordinary core records and do not grant login privileges.

Upstream manager/report reassignment and its additional personnel/contact fields
are not claimed as implemented here. Payroll, tax, bank/national identifiers and
medical details are excluded. No live models, external payroll or calendar APIs
are needed for this capability.
