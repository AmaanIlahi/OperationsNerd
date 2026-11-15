# Separate self-hosted Supabase deployments

Status: preparation and local validation; no paid resources have been applied.
Each app has its own Render Blueprint at `deploy/supabase/render.yaml`, PostgreSQL cluster, credentials and persistent disks. The pinned upstream release, file hashes and image digests are in `upstream.lock.json`.

Included: PostgreSQL 17, Auth, REST, Realtime, Storage, metadata API, Studio, Edge Functions and authenticated gateway. Optional image transformations, analytics/vector and connection pooler are omitted. Applications use direct SQL and their existing authentication. Each OperationsNerd business remains a separate database; these databases do not become separate Supabase projects or automatically appear in Studio.

Generate fresh local test credentials without loading existing environment files:

```sh
python3 deploy/supabase/prepare.py --output .local/hosting/stack.env
docker compose --env-file .local/hosting/stack.env -f deploy/supabase/compose.yaml --profile app up --build -d
```

Local app and Supabase gateway ports bind only 127.0.0.1. Integration uses 58080/58000; standalone uses 58180/58100. Credentials are private in the generated file. Do not commit or share that file. Production uses Render HTTPS origins instead. Default AI/carrier calls are disabled. Supabase Auth signup is disabled; application login remains separate.

Render: validate the Blueprint, commit/push the requested branch, create a plan with autoSync disabled and create_new resource handling, review its service list and cost, then apply only after deployment approval and verified credits or an approved spending amount. Supply the generated corresponding secret values privately; never use defaults from upstream examples. Do not apply to existing unrelated services.

Database and file disks persist across app restarts. They do not supply offsite recovery. Before real business use, configure exports for every application/control database plus Supabase Auth/Storage metadata, roles, encryption keys and Storage files, store them off the service and rehearse recovery into an empty stack. Never restore a raw disk snapshot over a running PostgreSQL server. Never run synthetic fixtures against real business data.

Base monthly estimates are recorded in `cost-estimate.json`. They exclude taxes, bandwidth, extra build usage, workspace upgrades, scaling and offsite backups. Credits are unverified. Local Docker tests do not constitute a Render deployment or security sign-off.

Selected cloud deployment uses the managed-free profile instead of this paid self-hosted profile. Local fallback images build; database/Auth/Studio startup was exercised. The default Edge hello function and anonymous REST API route have not received a complete end-to-end sign-off. Do not treat optional self-hosted services as a production acceptance result.
