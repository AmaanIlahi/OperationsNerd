# Deploying on PythonAnywhere (free account, WSGI)

This runs the app as a normal PythonAnywhere (PA) **WSGI web app**, with no Docker. The app is
FastAPI (ASGI); `operations_nerd/wsgi.py` wraps it with `a2wsgi` so PA's WSGI server can call it.
Docker, Railway and Render are covered in `deployment.md`; this file is only for PA.

**What was verified, and what was not.** On a development machine the app was served through
`wsgi.py` with two WSGI servers (`waitress` and Python's `wsgiref`) in production mode, from a
different working directory and with the database at a custom path: startup and DB creation, the
expired-session sweep, signup/login/logout and the `Secure` `HttpOnly` cookie, `/app/`, `/healthz`,
a chat turn plus approve (with a scripted model), and `404` on the legacy routes are covered by
`operations_nerd/tests/test_wsgi_entry.py` (wsgiref). The same startup, signup, `/app/`, chat and
legacy-404 checks were also done by hand against a real `waitress` process. **Nothing here has run on PA itself.** Steps that depend on
PA's own behaviour are marked `[UNVERIFIED]`; check them against PA's current help pages.

`<username>` below means your PythonAnywhere username. Replace it everywhere, including inside the
file paths.

## 0. Before you start

- You need the Gemini API key (or an Anthropic one) and a spend limit on it. See section 3 of
  `deployment.md` and do that first.
- Pick the invite code and generate the secrets now (any computer with Python):
  ```
  python -c "import secrets; print(secrets.token_urlsafe(48))"     # SESSION_SECRET
  python -c "import secrets; print(secrets.token_urlsafe(12))"     # SIGNUP_INVITE_CODE
  ```
- The repo must be cloneable from the PA console. A public repo clones with a plain `https://` URL.
  For a private repo use a GitHub personal access token as the password
  `[UNVERIFIED: free accounts have no SSH access to your account, so use HTTPS]`.

## 1. Clone the code

1. Log in at pythonanywhere.com. Open the **Consoles** tab, then **Bash** under "Start a new console".
2. Run:
   ```
   git clone https://github.com/AmaanIlahi/OperationsNerd.git
   cd OperationsNerd
   git checkout main
   ```
   The code is now in `/home/<username>/OperationsNerd`.

## 2. Create the virtualenv (Python 3.11) and install

In the same Bash console:

```
ls /usr/bin/python3.11                                   # should print the path; if not, see below
mkvirtualenv opsnerd --python=/usr/bin/python3.11
pip install -r requirements.txt
python -c "import a2wsgi, fastapi, bcrypt; print('ok')"
du -sh ~/.virtualenvs/opsnerd ~/OperationsNerd           # free accounts have 512 MB in total
```

- The prompt shows `(opsnerd)` while the virtualenv is active. In a new console, run `workon opsnerd`.
- If `/usr/bin/python3.11` does not exist, run `ls /usr/bin/python3*`, pick the nearest 3.11+ and use
  that path instead, and set the same version in the Web tab in step 5.
  `[UNVERIFIED: which Python versions the current PA system image offers]`
- Installing `google-genai` pulls in a fair number of packages. If `du` shows the virtualenv is huge,
  tell the maintainer before going on.

## 3. Create the data folder and the `.env` file

The database lives **outside** the git clone, so `git pull` can never touch it.

```
mkdir -p ~/opsnerd_data ~/opsnerd_backups
nano ~/OperationsNerd/.env
```

Paste this, replacing every `<...>`:

```
APP_ENV=production
SESSION_SECRET=<the 48-character secret you generated>
SIGNUP_INVITE_CODE=<the invite code you generated>
LLM_PROVIDER=gemini
GEMINI_API_KEY=<your Gemini API key>
DATABASE_PATH=/home/<username>/opsnerd_data/operations_nerd.db
TRUSTED_PROXIES=*
```

Save in nano with Ctrl+O, Enter, then Ctrl+X. Then lock the file down:

```
chmod 600 ~/OperationsNerd/.env
```

Notes:

- `.env` is git-ignored, so it is never committed and `git pull` leaves it alone. The app reads it at
  startup from the repo root (the WSGI entry moves into `operations_nerd/` first, and the loader
  searches upward from there).
- Do **not** set `CONFIG_AGENT_ENABLED`. The app refuses to start in production if it is set at all.
- To use Anthropic instead: `LLM_PROVIDER=anthropic` and `ANTHROPIC_API_KEY=<key>` (and no Gemini line).
- `TRUSTED_PROXIES=*` tells the app to believe `X-Forwarded-Proto` and `X-Forwarded-For`. That is only
  safe because PA's front end is the only way to reach the app. Without it, every visitor looks like the
  same address to the login rate limiter. `[UNVERIFIED: that PA sends X-Forwarded-For with the real client
  address last]`
- If you change `.env` later, you must **Reload** the web app (step 5) for it to take effect.
- Changing `SESSION_SECRET` logs everyone out.

Optional sanity check, still in the console, that the settings are accepted (it creates the database
and prints `ok`; it makes no model call):

```
cd ~/OperationsNerd/operations_nerd
python -c "import wsgi; print('ok')"
ls -l ~/opsnerd_data
```

If it prints `Refusing to start:` followed by a list, fix each line it names and run it again.

## 4. Check the Gemini API is reachable from PA (free accounts)

Free accounts can only reach sites on PA's allowlist. At the time of writing the allowlist page
(`https://www.pythonanywhere.com/whitelist/`) lists `.googleapis.com` and `api.anthropic.com`.
Check it still does, and test from the console:

```
curl -s -o /dev/null -w "%{http_code}\n" https://generativelanguage.googleapis.com/
```

Any HTTP status (even 404) means the host is reachable. `000` or a proxy error means it is blocked.
The real test is the first chat message in step 7. `[UNVERIFIED: that the google-genai SDK works through
PA's free-account proxy]`

## 5. Create the web app (Web tab)

1. Open the **Web** tab, click **Add a new web app**, then **Next**.
   (A free account gets one app, at `<username>.pythonanywhere.com`.)
2. Choose **Manual configuration** (not "Flask" or "FastAPI"), then pick **Python 3.11**, then **Next**.
3. On the web app's page, set:

   | Setting | Value |
   |---|---|
   | **Source code** | `/home/<username>/OperationsNerd/operations_nerd` |
   | **Working directory** | leave as is (`wsgi.py` changes into the right folder itself) |
   | **Virtualenv** | `/home/<username>/.virtualenvs/opsnerd` |
   | **Force HTTPS** | switch **on** |
   | **Static files** | add nothing; the app serves its own pages |

   Check that the **Python version** at the top says 3.11 and matches the virtualenv.

4. Under **Code**, click the link next to **WSGI configuration file**
   (`/var/www/<username>_pythonanywhere_com_wsgi.py`). **Delete everything in it** and paste exactly
   this, replacing `<username>`:

   ```python
   import sys

   path = "/home/<username>/OperationsNerd/operations_nerd"
   if path not in sys.path:
       sys.path.insert(0, path)

   from wsgi import application
   ```

   Click **Save**. Do not put API keys here; they are in `.env`.

5. Go back to the Web tab and click the green **Reload `<username>.pythonanywhere.com`** button.

## 6. Check it started

- Open `https://<username>.pythonanywhere.com/healthz`. It should show `{"status":"ok"}`.
- If you get an error page, open the **Error log** link on the Web tab and read the bottom of it.
  Common causes:
  - `Refusing to start: ...`: a missing or short `SESSION_SECRET`, a missing `GEMINI_API_KEY`, or
    `CONFIG_AGENT_ENABLED` set. Fix `.env`, then Reload.
  - `ModuleNotFoundError`: the Web tab's **Virtualenv** path is wrong or empty, or
    `pip install -r requirements.txt` was run in the wrong virtualenv.
  - `unable to open database file`: the folder in `DATABASE_PATH` does not exist or the username is wrong.
  - A blank page with no error. `[UNVERIFIED]` A forum report describes an a2wsgi app that ran under
    waitress but returned nothing on PA, with no resolution; if this happens, copy the error and server
    logs before changing anything.

## 7. Smoke test (about 5 minutes)

1. `https://<username>.pythonanywhere.com/app/` loads the login screen.
2. Sign up with your email, a password and the invite code.
3. In the browser dev tools (Application, then Cookies), the `session` cookie has `HttpOnly`, `Secure`
   and `SameSite=Lax`.
4. Create a business from the health club template and send one chat message. This is the first real
   Gemini call. Approve the proposed change.
5. Reload the page and confirm the business and the change are still there.
6. `https://<username>.pythonanywhere.com/ui/` and `/packs` must return "Not Found" (the legacy demo
   routes are off in production).
7. Follow `documentation/phase_d_manual_test.md` for the fuller pass.

## 8. Back up the database

Never copy the `.db` file by hand while the app runs (it uses WAL mode). Use the script, which takes a
consistent snapshot and checks it:

```
workon opsnerd
python ~/OperationsNerd/scripts/backup_db.py --db ~/opsnerd_data/operations_nerd.db --out-dir ~/opsnerd_backups
```

It prints `Backup written: /home/<username>/opsnerd_backups/operations_nerd-<timestamp>.db`.

- **Before every update (step 9), run it.**
- To get a copy off PA: **Files** tab, open `opsnerd_backups`, click the backup's download icon. Do this
  regularly: a backup on PA does not protect you if the account is lost or deleted.
- Backups are never pruned automatically; delete old ones from the Files tab (free accounts have 512 MB).
- Free accounts have no scheduled tasks `[UNVERIFIED: check the Tasks tab]`, so backups are manual.

**Restore:** in the Web tab click **Disable** (or just do it between visits), then in a Bash console:

```
cd ~/opsnerd_data
rm -f operations_nerd.db-wal operations_nerd.db-shm
cp ~/opsnerd_backups/operations_nerd-<timestamp>.db operations_nerd.db
```

Then Reload.

## 9. Update after a `git pull`

```
workon opsnerd
cd ~/OperationsNerd
python scripts/backup_db.py --db ~/opsnerd_data/operations_nerd.db --out-dir ~/opsnerd_backups   # 1. back up
git pull origin main                                                                              # 2. new code
pip install -r requirements.txt                                                                   # 3. new packages, if any
```

Then open the **Web** tab and click **Reload**, and open `/healthz` to confirm.

The app only ever adds columns, tables and indexes, and does so at startup, so the new code upgrades the
database on the first request after Reload. New indexes on a small database take a moment at most.

**Rolling back the code:**

```
cd ~/OperationsNerd
git log --oneline -5            # find the previous good commit
git checkout <commit>           # then Reload; run `git checkout main` later to go back to following main
```

An older version of the code can read a database written by a newer one. Only restore a backup
(section 8) if the data itself was damaged.

## 10. What will not work, or may not work, on a free account

| Issue | Detail |
|---|---|
| **Outbound internet is allow-listed** | Only hosts on PA's list can be called. `.googleapis.com` and `api.anthropic.com` were listed when this was written; any other provider will not work. If Gemini is blocked, the CRM loads but chat fails. |
| **CPU budget** `[UNVERIFIED figure]` | Free accounts are reported to get about **100 CPU-seconds per day**. Password hashing (bcrypt) and request handling count against it. A busy day can exhaust it, after which the site is throttled until the next day. Keep the invite list small. Check your usage on the **Account** page. |
| **One web app, fixed domain** | `<username>.pythonanywhere.com` only; no custom domain. HTTPS is provided. |
| **512 MB of disk** | Holds the virtualenv, the clone, the database and backups together. |
| **Expiry** `[UNVERIFIED]` | Free web apps have had to be re-confirmed periodically with a button on the Web tab or they are disabled. Check the Web tab for an "extend" banner and its date, and PA's help page. |
| **No scheduled tasks, no always-on processes** `[UNVERIFIED]` | Backups are manual (section 8). |
| **Single, small worker** | Fine for this app's design (SQLite, in-memory rate limiter reset on each reload), but long requests tie up the worker. A chat turn can take up to 90 seconds (the model call's own timeout). `[UNVERIFIED: PA's request timeout]` Expect the app to feel slow under more than a couple of simultaneous users. |
| **SQLite on PA's disk** `[UNVERIFIED]` | The database uses WAL mode, which needs shared-memory files (`-wal`, `-shm`) and can misbehave on network file systems. If you see `database is locked`, `disk I/O error` or lost writes in the error log, stop and report it; the fix needs a code change (a non-WAL mode), not a setting. Always back up first. |
| **a2wsgi on PA** | This setup is a standard WSGI entry, not PA's separate (beta) ASGI hosting, which has no static files and is experimental. The one forum report of silence is in step 6. |
| **`TRUSTED_PROXIES=*`** | Safe only because the app is reachable solely through PA's front end. See step 3. |
| **No password reset or email verification** | Same as every other deployment of this app (see `deployment.md` section 9). |
| **Reload after every change** | Edits to `.env`, the WSGI file or pulled code only take effect after **Reload** on the Web tab. |

## Where things live

| What | Path |
|---|---|
| Code | `/home/<username>/OperationsNerd` |
| WSGI entry | `/home/<username>/OperationsNerd/operations_nerd/wsgi.py` |
| Secrets | `/home/<username>/OperationsNerd/.env` (mode 600, git-ignored) |
| Database | `/home/<username>/opsnerd_data/operations_nerd.db` |
| Backups | `/home/<username>/opsnerd_backups/` |
| Virtualenv | `/home/<username>/.virtualenvs/opsnerd` |
| PA's WSGI config | `/var/www/<username>_pythonanywhere_com_wsgi.py` |
