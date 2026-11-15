# Managed free database assessment

Checked 2026-11-15. This is a deployment assessment, not a remote deployment or compatibility sign-off. No paid resources were created.

## Candidate: two separate Neon Free projects

Use PostgreSQL 17 to match the production images, direct TLS connections (not the -pooler hostname), and independent credentials for the integrated and standalone apps. Integration keeps its control database and creates one SQL database per business. Standalone keeps one dedicated business database. Supabase remains available as the separate local Docker development option; Neon is not Supabase.

Official documentation supports SQL database creation, CREATEDB for administrator roles, and multiple databases per branch. Actual project permissions and CREATE DATABASE TEMPLATE template0 still require a live probe, module installation and archive restore. Do not claim those tests have passed merely from provider documentation. Do not replace business databases with shared tables.

Neon Free currently includes 100 CU-hours/project/month, 1 GB database storage/project and 5 GB network transfer/project/month. Inactive compute sleeps after five minutes. Check the selected account and plan before provisioning. Keep the account on Free; do not enable paid upgrades or external AI without the authorized cost decision.

Sources: https://neon.com/docs/introduction/plans ; https://neon.com/docs/manage/databases ; https://neon.com/docs/manage/roles ; https://neon.com/docs/connect/connection-pooling

## Other options

Render Free PostgreSQL: one active instance per workspace, 1 GB, expires after 30 days. It is unsuitable as the durable database for these two demos. https://render.com/docs/free

Supabase Free: two active projects, 500 MB database/project; inactive projects pause after one week. Additional SQL databases are possible but outside Studio/Auth/REST integration. Direct/session connectivity, arbitrary database selection and matching client tools must be verified. https://supabase.com/pricing ; https://supabase.com/kb/troubleshooting/manually-created-databases-are-not-visible-in-the-supabase-dashboard-4415aa

Vercel connects external database providers through its Marketplace; it does not supply the former Vercel Postgres product. The current Vercel account has no installed database integrations in the checked account/team scope. https://vercel.com/docs/postgres

## App hosting is a separate cost

Render free web services sleep after 15 minutes and share 750 running hours/workspace/month. They cannot attach persistent disks. SQL business data can persist in the external managed database, but local pre-install backup archives disappear on app restart/redeploy. Do not call this a complete recovery implementation. Free database pricing alone does not solve the archive-retention requirement.

Two paid starter app services plus 2 GB archive disks each would have a base estimate of $15/month combined ($7 compute + $0.50 disk per app), if the standalone app passes a 512 MB memory-limit workflow test. Its current conservative 2 GB compute selection instead yields $33/month combined. Both exclude tax, metered usage and provider calls, and require usable credits or explicit spending approval. No paid services are approved by this document. https://render.com/pricing

A fully free app demo remains a candidate only after a durable archive destination is implemented/tested or a clearly limited temporary-demo checkpoint is accepted. Do not silently drop automatic backups, isolation, idempotency or accounting confirmation requirements to reach $0.

## Remaining live checks

1. Connect Neon and verify the account is on Free with available quota.
2. Create two fresh PostgreSQL17 projects without migrating real business data.
3. Verify direct TLS, role ownership/CREATEDB, fresh business databases and session advisory locks.
4. Enable logistics and verify a matching-major backup and restore into a new isolated database.
5. Verify standalone login, purchasing, partial receipt, shipment, restart persistence and denied anonymous requests.
6. Select durable backup storage and review the final app-hosting cost before deployment.

## Live Neon checks completed

The Somaditya organization was verified as Free. Two new PostgreSQL17 projects were created without changing the plan or touching the existing project: `holy-violet-30688438` (integrated) and `dry-dawn-24205292` (standalone). Direct certificate-verified TLS connections succeeded. Integration passed account creation, SQL database-per-business creation, mapping approval, logistics installation with matching-major backup, workspace access and verified restore into a new isolated database without changing the live business. Standalone passed schema setup, owner readiness and six-page routing. These are local containers talking to real managed Neon databases, not deployed Render websites. Fictional verification records and an isolated restore database remain in the new projects.

Current standalone choice: public shared fictional demo without login (`HOST_PUBLIC_DEMO=true`), with server-side tokens, CSRF browser sessions and external paid-provider features disabled. Combined app authentication is unchanged: an account may own multiple businesses, each with its own database. A business database does not create a distinct login.

Neon TLS probes found and fixed a container HOME issue after privilege dropping. Backup subprocesses now preserve the explicitly configured CA bundle as well as sslmode. The CA path is platform-specific: /etc/ssl/cert.pem for this Mac, /etc/ssl/certs/ca-certificates.crt for the Linux images.

## Free app hosting with durable archives

Private Neon Object Storage was verified available on both Free projects. Each app now uploads backups to its own private bucket; failed uploads fail backup completion before schema migration. The integrated archive and manifest can be retrieved and checksum-verified after container replacement. Scoped storage credentials are held only in ignored, mode-0600 local settings and the hosting environment. The object store is part of the same Neon project, so this protects app restarts but is not an independent-provider disaster recovery copy.

Current target: two Render Free web services and the two Neon Free projects. No Supabase cloud services or paid Render disks are needed for this target. Free runtime hours, transfer, compute and storage quotas still apply; no upgrade is authorized. Local Supabase Docker configurations remain an optional development fallback, separate from the actual Neon deployment.

Live deployment completed: see [hosted-demo-status.md](hosted-demo-status.md). Current app URL: https://operationsnerd-logistics-demo.onrender.com/app/
