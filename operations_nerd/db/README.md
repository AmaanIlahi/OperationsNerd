# Database runtime

SQLite remains the default and existing explicit SQLite file arguments still
work. `OPERATIONS_DATABASE_URL=postgresql://...` enables the optional PostgreSQL
backend. The host resolves and authorizes a business before entering
`database_scope(business_url)`; `get_conn()` then uses that request-local target.
`control_conn()` always ignores the business scope, so authentication, sessions
and routing metadata cannot accidentally follow an operational-data request.
The scope is routing context, not an authorization check.

PostgreSQL core tables live in `core`. Domain modules own their own schemas.
`raw_postgres_conn(url=None, autocommit=True)` returns a native psycopg connection
for modules that manage their own transaction boundary. It starts with
`search_path=core,public`. The host must not begin an outer transaction around a
module command whose contract requires idle/autocommit connections.

The small compatibility adapter covers the SQL forms used by the existing
core; new code should avoid extending the SQLite dialect. It preserves text
JSON/timestamps, integer identity values, mapping/positional row access and
transaction rollback. Every core mutation obtains a database-local advisory
transaction lock, the same lock used to implement `BEGIN IMMEDIATE`. This
deliberately serializes core writers to preserve the existing EAV one-to-one
invariant under PostgreSQL READ COMMITTED isolation. Each business database is
independent. Higher concurrency requires a separately designed constraint and
lock strategy, not removal of this lock.

`schema_postgres.sql` is the checksummed first migration. Do not edit an applied
migration: add forward migrations. Business-side audit actor IDs are stable
references to control identities and deliberately have no local accounts FK.
Only the control database contains real account/session data.

Initialization adds schema without deleting data. PostgreSQL reset is reserved
for database names beginning `operations_test_`, `operationsnerd_test_` or
`opsnerd_test_`, requires `OPERATIONS_ALLOW_DATABASE_RESET=1`, and is refused in
production. It resets only the `core` schema, never the database or other module
schemas. Provisioning and module lifecycle are host responsibilities.

Offline tests: `python operations_nerd/tests/test_database_foundation.py`.
Real PostgreSQL tests: set `OPERATIONS_TEST_DATABASE_URL` to a local disposable
server/admin fixture and run `test_postgres_foundation.py`. Those tests create
randomly named databases and delete only what they created; they never reset
the supplied database and never call a model.
