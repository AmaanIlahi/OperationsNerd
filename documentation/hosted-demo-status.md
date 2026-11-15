# Hosted demo status — 2026-11-15

URL: https://operationsnerd-logistics-demo.onrender.com/app/
Render service: srv-dbt2uj6c6qcc73aj582g
Verified live deployment: dep-dbt2ujec6qcc73aj5900
Runtime source commit: 2f4fb1a

Hosting: Render Free web service. Database and private backup storage: separate Neon Free project for this app. No paid instance, Supabase cloud stack, workspace upgrade or external AI/carrier provider was enabled. Free usage quotas and cold starts apply. This is a demonstration deployment, not a security or production sign-off.

Authentication: the earlier local demo email/password is preserved; each business has a separate SQL database, and an account may own multiple businesses.

Validation: 117 PostgreSQL tests (six groups), 27 offline scripts (production-packaging script rerun after updating its obsolete SQLite assertion), 12 hosted startup checks, 7 backup-tool tests and 3 durable-storage tests.
Both final container images built and ran without root, using a valid application HOME for TLS. Direct Neon connections verify the certificate using the Linux CA bundle. Each app uploaded and retrieved a real PostgreSQL archive from its own private object bucket. Integration additionally verified an isolated restore without changing the live demo.

Public HTTPS verification: a real Chromium browser logged into the combined app with the earlier credentials and opened all six standalone pages without login; no JavaScript exceptions were observed. The hosted integrated toy-shop flow passed mappings, core records, explicit purchase approval, two partial receipts, two shipments, delivery tracking, dispatch replay and denial of another account's access.

External chat/AI and carrier calls remain disabled. The standalone fixture workflow is in memory and separate from its persistent business records. Local Supabase Docker files remain available as a separate development fallback. Category constraints, actual carrier/label services and local-model privacy acceptance retain their previously documented limits.

Auto-deploy is off. A later source push does not automatically replace these live instances. A manual redeploy preserves Neon data and private archives, while local files and in-memory sessions reset. Backup storage is in the same Neon project, not an independent disaster recovery provider.

Render services were created through the connected API and currently use the platform default TCP health check. The standalone /readyz endpoint was separately verified over public HTTPS. The reproducible Blueprint specifies an HTTP health path for future Blueprint-based provisioning.
