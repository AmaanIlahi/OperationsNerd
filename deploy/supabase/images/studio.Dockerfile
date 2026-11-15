# Supabase self-hosted/v0.8.4, commit 189adda99d1b56da55fc5a6ee3f849354aaea92a
FROM busybox:1.37.0-musl@sha256:f6a95d611e0a8780fabdb2aec43e31275b2720dfefa3e41b16835bfc42454478 AS tools
FROM supabase/studio:2026.11.02-sha-a05e7ec@sha256:727a7f37b15f4c76c44e0d765e6be231d8d71c67f2933b2df8c6cbcc9b04c5d0
COPY --from=tools /bin/busybox /opt/busybox
COPY deploy/supabase/render/runtime.sh /opt/supabase-runtime.sh
ENV SUPABASE_COMPONENT=studio
COPY deploy/supabase/upstream/volumes/functions /opt/functions
EXPOSE 3000
ENTRYPOINT ["/opt/busybox", "sh", "/opt/supabase-runtime.sh"]
CMD ["docker-entrypoint.sh", "node", "apps/studio/server.js"]
