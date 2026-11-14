# Local business backup and recovery

The integrated launcher keeps the control database, private business databases
and session secret under ignored `.local/`. Restarting the launcher retains
them. Each business is routed by the authenticated owner's control registry;
the browser cannot choose a database URL.

Before a reviewed capability install, the host writes a consistent custom-format
PostgreSQL backup of that business. A failed backup stops the install before
module migration. Migration histories retain their source checksums; changed or
missing applied source files are rejected. A failed install retains its schema
and can retry the same proposal. Disable retains operational history and only
allows re-enabling the original mappings.

The host checks the connected PostgreSQL server's major version before a backup
or restore. It selects `pg_dump` and `pg_restore` from the same directory and
requires both to match that major version. Matching bundled tools take priority
over `PATH`; an older system installation cannot replace them. If no matching
pair is available, installation stops before module migration and restore stops
before creating its quarantine database. The error identifies the required major.

For a custom PostgreSQL installation, set `OPERATIONS_PG_BIN` to the directory
containing both tools. This explicit selection is authoritative; an invalid
selection fails rather than falling back. Tool version probes receive no model
keys or database credentials. The backup manifest records server/tool majors.
Older manifests without those optional fields remain supported.

Exact-major matching is a conservative policy for restoring to the current local
server. PostgreSQL permits some cross-version dumps, but an older client cannot
dump a newer server, and a newer client's output may not restore into an older
server. See [PostgreSQL's compatibility notes](https://www.postgresql.org/docs/16/app-pgdump.html#APP-PGDUMP-NOTES).
The local launcher and bundled-tool workflow are tested on macOS; custom Unix
installations can use the explicit directory setting.

To create an additional backup while the local server is running:

```sh
.venv/bin/python scripts/backup_business.py --owner 1 --business 4
```

Use the actual account and business identifiers. This operator command checks
their relationship. It reads the launcher's ignored `.local/database.json` when
explicit database environment variables are absent. The result lists the archive,
manifest and SHA-256 checksum. Archive and manifest permissions are `0600`.
Keep both files together; the manifest contains identity and checksum metadata,
not database passwords. `--out-dir` can choose a private backup directory.

Rehearse recovery from the exact saved archive path:

```sh
.venv/bin/python scripts/backup_business.py --owner 1 --business 4 \
  --restore-copy /absolute/path/to/business-4-saved.dump
```

The command creates a fresh `opsnerd_restore_<random>` database, restores the
archive, checks business/owner identity, validated foreign keys and the core
migration checksum, and reports the quarantine database name. It does not change
the live registry or overwrite the live database. Module identity/history and
representative core snapshots are also compared in the automated recovery test.
Retain or remove rehearsal databases through the local PostgreSQL operator tools.

If business initialization or a host metadata upgrade fails, the business remains
listed as failed and other businesses continue to work. After fixing the cause,
the authenticated owner can `POST /api/businesses/<id>/retry-setup`. This retries
the same generated database and business ID, preserves an existing spec, and
serializes competing setup attempts. The endpoint rejects a different owner.

This proves local backup and isolated restore. A production release still needs
an operator-reviewed cutover/rollback procedure, off-device retention, control
database recovery, provider-specific provisioning permissions and an explicit
customer-data migration. No legacy SQLite or Supabase customer database has been
migrated or replaced by this fork.
