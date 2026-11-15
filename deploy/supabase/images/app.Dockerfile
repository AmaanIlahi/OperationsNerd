# Integrated production app; PostgreSQL belongs to the separate Supabase stack.
FROM python:3.12-slim-bookworm

# Match the pinned self-hosted Supabase database. Upgrade server and clients
# together; startup also checks the actual connected server major.
ARG PG_MAJOR=17

RUN set -eu; \
    case "$PG_MAJOR" in 1[0-9]) ;; *) exit 1 ;; esac; \
    apt-get update; \
    apt-get install --no-install-recommends -y ca-certificates curl; \
    install -d /usr/share/postgresql-common/pgdg; \
    curl --fail --show-error --silent --location --proto '=https' --tlsv1.2 \
      https://www.postgresql.org/media/keys/ACCC4CF8.asc \
      -o /usr/share/postgresql-common/pgdg/apt.postgresql.org.asc; \
    printf '%s\n' 'deb [signed-by=/usr/share/postgresql-common/pgdg/apt.postgresql.org.asc] https://apt.postgresql.org/pub/repos/apt bookworm-pgdg main' \
      > /etc/apt/sources.list.d/pgdg.list; \
    apt-get update; \
    apt-get install --no-install-recommends -y "postgresql-client-${PG_MAJOR}"; \
    apt-get purge --auto-remove -y curl; \
    rm -rf /var/lib/apt/lists/*; \
    groupadd --gid 10001 app; \
    useradd --create-home --uid 10001 --gid 10001 app; \
    install -d -m 0700 -o app -g app /data /data/backups

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PYTHONPATH=/app/operations_nerd \
    APP_ENV=production \
    OPERATIONS_UMBRELLA_ENABLED=1 \
    OPERATIONS_PG_BIN=/usr/lib/postgresql/${PG_MAJOR}/bin \
    OPERATIONS_BACKUP_DIR=/data/backups \
    MAX_BODY_BYTES=8388608

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Narrow copies keep local data, Supabase configuration and secrets out.
COPY operations_nerd/ ./operations_nerd/
COPY scripts/backup_business.py scripts/backup_db.py scripts/check_hosted.py scripts/start_hosted.py scripts/bootstrap_hosted.py ./scripts/

WORKDIR /app/operations_nerd

EXPOSE 8000

# Root only prepares the two mounted directories, then drops uid/gid/groups
# before executing any command. This handles fresh root-owned hosted disks.
ENTRYPOINT ["python", "/app/scripts/bootstrap_hosted.py"]

# Validate production config, PG privileges/tools and private backup storage,
# then exec one uvicorn worker on 0.0.0.0:$PORT (default 8000).
CMD ["python", "/app/scripts/start_hosted.py"]
COPY deploy/supabase/render/app-entrypoint.py /app/deploy-entrypoint.py
CMD ["python", "/app/deploy-entrypoint.py", "python", "/app/scripts/start_hosted.py"]
