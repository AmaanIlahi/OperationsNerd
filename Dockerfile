# Operations Nerd -- single service. Build context is the repository root.
#
#   docker build -t operations-nerd .
#   docker run -p 8000:8000 -v opsnerd-data:/data \
#       -e APP_ENV=production -e SESSION_SECRET=... -e ANTHROPIC_API_KEY=... \
#       -e SIGNUP_INVITE_CODE=... operations-nerd
#
# The SQLite file lives on a volume mounted at /data (DATABASE_PATH), never in
# the image. See documentation/deployment.md.

FROM python:3.11-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    DATABASE_PATH=/data/operations_nerd.db

RUN useradd --create-home --uid 10001 app \
    && mkdir -p /data \
    && chown app:app /data

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY operations_nerd/ ./operations_nerd/
COPY scripts/backup_db.py ./scripts/backup_db.py

WORKDIR /app/operations_nerd
USER app

EXPOSE 8000

# The platform sets $PORT; 8000 is the fallback for plain `docker run`.
# --proxy-headers is off on purpose: the app reads X-Forwarded-* itself, and
# only from TRUSTED_PROXIES.
CMD ["sh", "-c", "exec uvicorn main:app --host 0.0.0.0 --port ${PORT:-8000} --no-proxy-headers"]
