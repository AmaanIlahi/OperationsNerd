# Hosting the integrated application

This is the production app container for branch `logistics_integration`. It uses
the separate, full self-hosted Supabase stack described in
[self-hosted-supabase.md](self-hosted-supabase.md). It does not start PostgreSQL
inside the app container and does not use a hosted Supabase project.

The app continues to use its own account/session authentication. Supabase Auth
and its API keys do not replace `SESSION_SECRET` or the app's invite code. The
control database stores app accounts, sessions and the business registry. Each
business gets its own native PostgreSQL database on that app's Supabase cluster.
Keep the standalone Logistics app's cluster and credentials separate.

## Image and database connection

Build from the repository root:

```sh
docker build --build-arg PG_MAJOR=17 -t operationsnerd-integrated .
```

The image uses Python 3.12 and the PostgreSQL project's Debian client packages.
`PG_MAJOR=17` matches the pinned Supabase database image
`supabase/postgres:17.6.1.136` from upstream release `self-hosted/v0.8.4`, commit
`189adda99d1b56da55fc5a6ee3f849354aaea92a`. Major upgrades must change the server
and this build argument together. Startup verifies the actual connected server
against both `pg_dump` and `pg_restore`; a mismatch prevents serving the app.
Client installation follows the [PostgreSQL Debian repository instructions](https://www.postgresql.org/download/linux/debian/).

Use the direct private database service on port 5432. Do not connect this app
through Supavisor or another transaction pooler: it relies on connection-level
settings and session advisory locks across transactions. Database credentials
stay in runtime secret settings. Do not put them in image build arguments,
the Dockerfile, committed URLs, browser JavaScript or logs.

The stack must bootstrap the control database before the app becomes ready.
Give a dedicated app role `LOGIN`, `CREATEDB` and ownership of its control
database. That same role owns the business databases it creates. It needs
ordinary schema, table, function and trigger creation privileges; application
queries do not require a PostgreSQL superuser. If an admin URL is supplied, it
must point to the same server and use the same owning role as the runtime URL.
Separate admin/runtime roles need an explicit database-owner/grant implementation
before they can be used safely with the current provisioner.

## Runtime settings

| Setting | Required value or purpose |
| --- | --- |
| `APP_ENV` | `production`; image default. |
| `OPERATIONS_UMBRELLA_ENABLED` | `1`; image default. |
| `OPERATIONS_DATABASE_URL` | Explicit direct PostgreSQL URL with host, port, app role/password and control database. |
| `OPERATIONS_ADMIN_DATABASE_URL` | Optional; same server/role, usually a different initial database. Omit to use the control URL. |
| `SESSION_SECRET` | Private random value of at least 32 characters; retain across redeploys. |
| `SIGNUP_INVITE_CODE` | Private random code of at least 12 characters; the local `local-demo` code is refused. |
| `LLM_PROVIDER` | Explicitly choose `stub`, `baseten`, `gemini` or `anthropic`. Stub makes no model calls. |
| Provider key | Required only for the selected live provider; keep it server-side. |
| `TRUSTED_PROXIES` | Explicit addresses/CIDRs of the proxy reaching the container. Use `*` only when all routes to the app are controlled proxies. |
| `OPERATIONS_BACKUP_DIR` | `/data/backups`; image default, backed by the app's persistent disk. |
| `OPERATIONS_PG_BIN` | `/usr/lib/postgresql/17/bin`; derived from the image build argument. |
| `MAX_BODY_BYTES` | `8388608`; image default allows JSON wrapping of the separately limited 1 MiB CSV input. |
| `PORT` | Hosting-provided HTTP port; defaults to 8000. |
| `OPERATIONS_STARTUP_WAIT_SECONDS` | Bounded database-startup wait, default 60, maximum 300 seconds. |

Keep `ALLOW_OPEN_SIGNUP` unset, rate limiting enabled and
`CONFIG_AGENT_ENABLED` unset. Hosted startup rejects development mode, SQLite,
missing authentication/provider settings, pooler addresses and invalid ports.
It never prints supplied connection URLs or credentials.

The app listens on `0.0.0.0:$PORT` with one worker. The hosting proxy terminates
HTTPS; production session cookies remain `Secure`, `HttpOnly` and `SameSite=Lax`.
The current login/signup rate limiter is process-local, so keep one app instance.
For Render, verify the actual forwarded-client-address behavior before opening
signup to invited users. An unrestricted wildcard must not be used if callers
can bypass the trusted proxy and connect directly to the app.

## Persistent volume and privilege drop

Attach a dedicated persistent disk at `/data`. A fresh hosted disk may initially
be root-owned, replacing the ownership created in the image. The image therefore
has a narrow root entrypoint: it opens only `/data` and its `backups` child with
symlink protection, sets those two directories to mode 0700 and uid/gid 10001,
clears supplementary groups, then permanently drops uid/gid before executing
the requested command. It does not recursively change archive ownership or
modify an environment-selected path. The web server runs as uid/gid 10001.

Do not override this entrypoint or mount unrelated data at `/data`. Existing
archives with a different owner need a separately reviewed ownership migration;
the bootstrap deliberately does not rewrite their permissions. A missing disk
can still look writable, so the preflight does not claim to prove durability:
verify retention by recreating the container against the same disk.

Render persistent disks attach to one service instance and introduce deploy
downtime. Files outside their mount are ephemeral. See
[Render's disk constraints](https://render.com/docs/disks).

## Startup checks and backup commands

The default command validates configuration, private writable backup storage,
the actual server major, matching native backup tools, `CREATEDB`, schema
creation permission and a writable primary before executing Uvicorn. Database
checks read metadata only; a private probe file is created and removed in the
backup directory. The normal application startup then initializes its core and
registry. No local development database or demo account is created by the image.

To inspect the same checks in a running container:

```sh
docker compose exec --user 10001:10001 app python /app/scripts/check_hosted.py
```

Substitute the app service name defined in the stack. `docker exec` bypasses
the image entrypoint, so explicitly select uid/gid 10001 for operator commands.
To create a business backup or rehearse recovery:

```sh
docker compose exec --user 10001:10001 app \
  python /app/scripts/backup_business.py --owner 1 --business 1
docker compose exec --user 10001:10001 app \
  python /app/scripts/backup_business.py --owner 1 --business 1 \
  --restore-copy /data/backups/<saved-business-archive>.dump
```

Use real owner/business IDs and the exact returned archive filename. The app
also backs up a business before module migrations. Restore creates a separate
quarantine database and verifies identity; it does not overwrite the live
registry. Archive and manifest files are private. See
[integrated_recovery.md](integrated_recovery.md) for the recovery contract.

Back up the control database and every business database, retain copies outside
the service's disk, and test recovery of the complete Supabase stack separately.
The app's business archives alone do not recover accounts, Supabase services or
files stored by Supabase Storage. Do not treat raw disk snapshots of a running
database as a substitute for tested database backups.

## Verification before sharing the URL

Offline hosted-startup regression checks:

```sh
.venv/bin/python scripts/test_hosted_startup.py -v
```

Container verification must use disposable PostgreSQL, never the customer's
existing database. Check a fresh root-owned `/data` mount with Docker's
`volume-nocopy` option, effective uid/gid 10001, matching client tools, startup
refusal on missing secrets or mismatched clients, and absence of `.env`, `.local`
and database files in the image. Then check real app startup against the pinned
Supabase PostgreSQL image, invited signup, two isolated businesses, reviewed
module enabling, native backups and a separate verified restore. Recreate the
app container and confirm retained accounts, data and archives.

`/healthz` is a liveness response; it does not test database access. A public
HTTPS check must additionally verify session cookies, authenticated workspace
access, denied cross-owner access, CSV preview/commit and unavailable legacy
routes. Live provider behavior needs its own explicitly authorized model call.
No image build or local smoke test is proof that the full hosted Supabase stack
or remote Render deployment has been verified.

The existing `scripts/run_integrated.py` remains the local development launcher.
Its loopback binding, optional local PostgreSQL and local invitation defaults
are not used as the hosted startup path.
