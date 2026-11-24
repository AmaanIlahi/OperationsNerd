# Supabase self-hosted/v0.8.4, commit 189adda99d1b56da55fc5a6ee3f849354aaea92a
FROM busybox:1.37.0-musl@sha256:f6a95d611e0a8780fabdb2aec43e31275b2720dfefa3e41b16835bfc42454478 AS tools
FROM supabase/postgres:17.6.1.136@sha256:f371b5f3f2ac0a05703f33d6e6134515fb2498cab708fb948a0aeb7481467c00
COPY --from=tools /bin/busybox /opt/busybox
COPY deploy/supabase/render/runtime.sh /opt/supabase-runtime.sh
ENV SUPABASE_COMPONENT=db
COPY deploy/supabase/upstream/volumes/db/realtime.sql /docker-entrypoint-initdb.d/migrations/99-realtime.sql
COPY deploy/supabase/upstream/volumes/db/webhooks.sql /docker-entrypoint-initdb.d/init-scripts/98-webhooks.sql
COPY deploy/supabase/upstream/volumes/db/roles.sql /docker-entrypoint-initdb.d/init-scripts/99-roles.sql
COPY deploy/supabase/upstream/volumes/db/jwt.sql /docker-entrypoint-initdb.d/init-scripts/99-jwt.sql
COPY deploy/supabase/upstream/volumes/db/_supabase.sql /docker-entrypoint-initdb.d/migrations/97-_supabase.sql
COPY deploy/supabase/upstream/volumes/db/logs.sql /docker-entrypoint-initdb.d/migrations/99-logs.sql
COPY deploy/supabase/upstream/volumes/db/pooler.sql /docker-entrypoint-initdb.d/migrations/99-pooler.sql
COPY deploy/supabase/render/app-init.sql /docker-entrypoint-initdb.d/migrations/zz-app.sql
RUN cp -a /etc/postgresql-custom /opt/postgresql-custom-template && rm -rf /etc/postgresql-custom && ln -s /var/lib/postgresql/custom /etc/postgresql-custom
EXPOSE 5432
ENTRYPOINT ["/opt/busybox", "sh", "/opt/supabase-runtime.sh"]
CMD ["docker-entrypoint.sh", "postgres", "-c", "config_file=/etc/postgresql/postgresql.conf", "-c", "log_min_messages=warning"]
