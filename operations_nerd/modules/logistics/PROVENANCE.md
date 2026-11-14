# Reused Logistics domain

Source: `somadisingh/logisticsnerd`, exact commit
`b16b6224821f0e13d649316227edf08e7de2e9b2` on
`feature/purchasing-stock-integration` (31 October 2026).

Copied domain sources: `logistics_persistence/{core,db,errors,purchasing,stock,fulfillment,http_models}.py`
and `logistics_rules/selection.py`. Purchasing's rules import is relative to
this package. Stock, purchasing rules, money/quantity representations, locking,
ledger guards, allocation validation and command replay semantics are retained.
Public commands require an idle **autocommit** connection, now checked explicitly.

The source stock implementation is a PostgreSQL port of Jasmeet's stock engine
from `AmaanIlahi/OperationsNerd`, branch `jasmeet/logistics-stock`, commit
`68554a10c43959779ebce0089f93a4eeec7a7635`. Jasmeet owns the stock design. The
PostgreSQL port's whole-delivery API and explicit purchase-allocation linkage
are later LogisticsNerd additions, not an assertion about upstream completion.

## Host adaptation

- `db.prepare_connection(conn, business_key=None)` configures an already selected
  psycopg connection, with dict rows, module search path and timeouts. Business
  routing and authenticated actor/permissions remain host responsibilities.
- `migrate.initialize_database(conn, business_key)` (also `apply_migrations`)
  accepts a host connection rather than opening a URL from environment settings.
  It checks the pinned SQL digests, database history, and private-business identity.
  It neither seeds records nor closes its caller's connection.
- `fulfillment.workspace(conn, display=adapter)` obtains current reference and
  order display data from an injected `DisplayAdapter`. The default returns
  immutable binding snapshots and raw order references. No standalone record
  table is queried. Host decoration never changes persisted domain identities.
- The original standalone FastAPI transports, token authentication, record CRUD,
  onboarding questionnaire, seed data and deployment configuration are excluded.
- `core_bindings` and `demand_lines` remain typed host projections, not an
  independent editable catalog. The host must supply stable authoritative keys,
  current record validation and replay-safe customer-order submission. Snapshots
  preserve historical purchase terms.

Both import layouts are supported by relative imports:
`operations_nerd.modules.logistics` and, with `PYTHONPATH=operations_nerd`,
`modules.logistics`.

## Immutable migration history and transitional tables

The three original SQL files are byte-for-byte copies. Their SHA-256 digests are
recorded in `migrations/source-checksums.json` and checked before execution. The
module retains `logistics.schema_migrations`, the single purchasing/stock
`command_requests` registry and the database-identity assertion. Original keys
are globally unique across Logistics commands, not unique per operation.

`202610280001_business_records.sql` mixes typed foreign-key safeguards with
standalone record metadata and guards. Preserve this history. Its now-unused
`business_setup`, `record_tables`, `record_fields`, `record_binding_links` and
`record_events` tables are transitional historical schema; no public writer or
setup route is exposed. Fresh installation does not populate business records or
create the former dynamic `record_customers`, `record_items`, etc. tables.
An upgrade of an existing business must archive/preserve historical data and
retire dependencies through a reviewed **forward** migration, never by rewriting
or omitting these three source migrations. Do not drop metadata still used by an
installed original trigger.

Migration and public domain commands each own their transaction. A host may not
wrap them inside an already active approval transaction. Define the host approval
state transition around command execution explicitly; do not publish module-ready
state until migrations and authoritative binding validation have completed.

## Verification

The reused database/purchasing/stock acceptance tests are copied from the same
LogisticsNerd commit with package imports and migration-call signatures adapted.
New tests cover host display reads without standalone tables, source checksums,
business identity, module connection settings and nested-transaction rejection.

Run from the repository root using an environment with `psycopg[binary]` and
`pgserver==0.1.4` installed:

```sh
python -m operations_nerd.modules.logistics.tests.run_postgres
```

The runner creates a temporary local server/database and stops it afterward.
Alternatively explicitly set `LOGISTICS_MODULE_TEST_DATABASE_URL` to a disposable
local database named `logistics_module_test*` or `operations_test_*`. Tests reject
other names/hosts **before** resetting tables. No app environment file, customer
database, model, supplier or carrier is used. The domain test suite passed
52 tests on 13 November 2026.
