# Supabase self-hosted/v0.8.4, commit 189adda99d1b56da55fc5a6ee3f849354aaea92a
FROM busybox:1.37.0-musl@sha256:f6a95d611e0a8780fabdb2aec43e31275b2720dfefa3e41b16835bfc42454478 AS tools
FROM supabase/postgres-meta:v0.100.0@sha256:145325b7e39f8ef4931d6e8cfd3f6f4d655de09d72a2ccc96d9513542c2202bd
COPY --from=tools /bin/busybox /opt/busybox
COPY deploy/supabase/render/runtime.sh /opt/supabase-runtime.sh
ENV SUPABASE_COMPONENT=meta
EXPOSE 8080
ENTRYPOINT ["/opt/busybox", "sh", "/opt/supabase-runtime.sh"]
CMD ["docker-entrypoint.sh", "node", "dist/server/server.js"]
