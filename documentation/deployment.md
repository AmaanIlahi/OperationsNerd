# Deployment (Railway or Render)

One container, one service, one persistent volume. Nothing here has been deployed yet: this
document was written and checked offline, and **every platform-specific detail is marked
`[UNVERIFIED]`** where it could not be confirmed without a real account. Check those against the
platform's current docs before relying on them.

What *was* verified locally: the app starts under `uvicorn` with the production settings below,
creates its database directory and schema on first start, refuses to start on a bad configuration,
serves `/healthz`, and sets a `Secure` session cookie. The Docker image itself was **not built**
(the Docker daemon was not running on the development machine).

## 1. What the service needs

| Thing | Value |
|---|---|
| Build | the `Dockerfile` at the repository root (build context = repository root) |
| Start command | already in the Dockerfile: `uvicorn main:app --host 0.0.0.0 --port ${PORT:-8000}` |
| Health check path | `/healthz` (200, no auth, no data) |
| Persistent volume | mounted at `/data`; the SQLite file is `/data/operations_nerd.db` |
| Instances | **exactly one** (SQLite file + in-memory rate limiter; see "Limits of this setup") |

## 2. Environment variables

Set these in the platform's dashboard (never commit them; `.env` is git-ignored and docker-ignored).

| Variable | Required | Meaning |
|---|---|---|
| `APP_ENV` | yes | `production`. Turns on the strict startup checks and always-`Secure` cookies. |
| `SESSION_SECRET` | yes | Random string, at least 32 characters. Keys the hash of session tokens. Changing it logs everyone out. |
| `ANTHROPIC_API_KEY` | yes | API key for the assistant. |
| `ANTHROPIC_MODEL` | no | Model id. Defaults to `claude-sonnet-5-5`. |
| `SIGNUP_INVITE_CODE` | yes, in practice | Signup is **closed** unless this is set (the visitor must type it) or `ALLOW_OPEN_SIGNUP=1`. |
| `DATABASE_PATH` | set by the Dockerfile | `/data/operations_nerd.db`. Only override it if the volume is mounted elsewhere. |
| `PORT` | set by the platform | The container listens on it. `[UNVERIFIED]` Railway and Render both inject `PORT`; the Dockerfile falls back to 8000. |
| `TRUSTED_PROXIES` | yes behind a platform proxy | Who may set `X-Forwarded-Proto` / `X-Forwarded-For`. Use `*` **only** because the container is reachable solely through the platform's proxy. Without it the app ignores those headers (cookie still `Secure` in production, but every visitor looks like the proxy's address to the rate limiter). `[UNVERIFIED]` that both platforms send `X-Forwarded-For` with the real client address as the last entry. |
| `CHAT_DAILY_LIMIT` | no | Assistant messages per account per UTC day. Default 50. |
| `MAX_BODY_BYTES` | no | Largest request body. Default 1048576 (1 MiB). |
| `RATE_LIMIT_ENABLED` | no | Leave unset. `0` turns the login/signup limiter off (tests only). |
| `CONFIG_AGENT_ENABLED` | **must stay unset** | The app refuses to start in production if this is set at all. |

The app **refuses to start** (and the deploy shows the reason in the logs) when `APP_ENV=production` and
`SESSION_SECRET` or `ANTHROPIC_API_KEY` is missing, `SESSION_SECRET` is shorter than 32 characters, or
`CONFIG_AGENT_ENABLED` is set.

### Generate the secrets

```
python -c "import secrets; print(secrets.token_urlsafe(48))"     # SESSION_SECRET
python -c "import secrets; print(secrets.token_urlsafe(12))"     # SIGNUP_INVITE_CODE
```

### The invite code

The invite code is just the value of `SIGNUP_INVITE_CODE`. Share it with the people you want to let in
(it is typed into the Sign up form). To rotate it, change the variable and redeploy; existing accounts are
unaffected. Signup attempts are rate limited per address and per email, so the code cannot be guessed quickly.

## 3. Set a spend limit on the API key (do this before sharing the link)

1. Open the Anthropic Console (console.anthropic.com), sign in, and go to the workspace that owns the key.
2. Create a **dedicated API key** for this deployment, so it can be revoked on its own.
3. Set a **monthly spend limit** for the workspace/organization in the Console's limits/billing settings.
   `[UNVERIFIED]` The exact menu names change; look for "Limits" or "Spend limits" and set a hard monthly cap
   at an amount you are comfortable losing.
4. Optionally set a lower per-day figure with `CHAT_DAILY_LIMIT` (each account can use at most that many
   assistant messages per UTC day, so the worst case is roughly `accounts × limit × cost per message`).

## 4. First deploy on Railway `[UNVERIFIED — follow the platform's current docs]`

1. New project → deploy from the GitHub repo (branch `feature/agentic-crm`, or `main` once merged).
   Railway should detect the root `Dockerfile`.
2. Add a **Volume** to the service and mount it at `/data`.
3. Add the environment variables from section 2 (plus `TRUSTED_PROXIES=*`).
4. Settings → Networking → generate a public domain. Settings → Deploy → health check path `/healthz`.
5. Run **one** replica (leave scaling at 1).
6. Known gotcha `[UNVERIFIED]`: a freshly mounted volume can be owned by root while the container runs as the
   non-root user `app` (uid 10001), which makes the first start fail with "unable to open database file".
   If you see that, Railway documents a variable (`RAILWAY_RUN_UID=0`) that runs the container as root so it
   can write to the volume; confirm against their volume docs before using it.

## 5. First deploy on Render `[UNVERIFIED — follow the platform's current docs]`

1. New → Web Service → connect the repo → Runtime: **Docker**.
2. A persistent disk is a paid-plan feature `[UNVERIFIED]`. Add a **Disk** with mount path `/data`.
3. Add the environment variables from section 2 (plus `TRUSTED_PROXIES=*`).
4. Health check path: `/healthz`.
5. Instance count 1. Render disks only attach to a single instance `[UNVERIFIED]`, which matches the
   requirement here.
6. Same volume-ownership caveat as Railway: if the container cannot write to `/data`, check the platform's
   disk docs for the supported way to set ownership/user.

## 6. After the first deploy

- [ ] `https://<your-domain>/healthz` returns `{"status":"ok"}`.
- [ ] `https://<your-domain>/app` loads; sign up with the invite code; browser dev tools show the `session`
      cookie with `HttpOnly`, `Secure`, `SameSite=Lax`.
- [ ] Create a business from a template and send one chat message (this is the first real API call).
- [ ] Redeploy once and confirm your account and business are still there (proves the volume is persistent).
- [ ] Follow `documentation/phase_d_manual_test.md` against the live URL.

## 7. Backups

SQLite in WAL mode must not be backed up by copying the file while the app runs. Use the script, which uses
SQLite's online backup API and verifies the copy:

```
python scripts/backup_db.py --db /data/operations_nerd.db --out-dir /data/backups
```

The script is included in the image at `/app/scripts/backup_db.py`.

**Before every deploy:**

1. Open a shell in the running service `[UNVERIFIED — Railway: `railway ssh` / service shell; Render: Shell tab on paid plans]`
   and run `python /app/scripts/backup_db.py --db /data/operations_nerd.db --out-dir /data/backups`.
2. Confirm it prints `Backup written: /data/backups/operations_nerd-<timestamp>.db`.
3. Download a copy off the platform (the backup lives on the same volume, so it does not protect against
   losing the volume). `[UNVERIFIED]` platform-specific; `scp`/`railway` CLI file copy or the platform's
   volume backup feature, if your plan has one.
4. Then deploy.

Backups are never pruned automatically; delete old ones from `/data/backups` when space matters.

**Restoring:** stop the service (or scale to 0), replace `/data/operations_nerd.db` with the chosen backup
(also delete any `operations_nerd.db-wal` / `-shm` next to it), start the service.

## 8. Rollback

The app only ever **adds** columns and tables (`init_db` is additive and runs at every start), so an older
image can read a database written by a newer one.

1. Code rollback: redeploy the previous image/commit from the platform's deploy history. No database change needed.
2. Data rollback (a bad migration or data damage): restore the pre-deploy backup as described above, then
   redeploy the previous code.
3. Spec-level rollback (the owner changed their CRM and wants it back): no operator action; use History →
   Revert in the app. That only changes the CRM definition, never the records.

## 9. Limits of this setup (read before sharing widely)

- **Single instance only.** SQLite is one file on one volume, and the login/signup rate limiter keeps its
  counters in the process's memory. The counters **reset on every restart/deploy** and are **not shared
  between instances**, so running two instances would halve the protection and break the database.
- The daily chat cap *is* stored in the database, so it survives restarts.
- Passwords are bcrypt-hashed; sessions last 7 days; there is no password reset or email verification yet.
- There is no automatic scheduled backup; run the script (or set up a platform cron job `[UNVERIFIED]`).
- Rate limiting by address depends on `TRUSTED_PROXIES` being right; verify with two different networks
  that one address being limited does not lock out the other.
