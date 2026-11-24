# Supabase self-hosted/v0.8.4, commit 189adda99d1b56da55fc5a6ee3f849354aaea92a
FROM busybox:1.37.0-musl@sha256:f6a95d611e0a8780fabdb2aec43e31275b2720dfefa3e41b16835bfc42454478 AS tools
FROM postgrest/postgrest:v14.18@sha256:c7cd7e265a85b05248abf53f4473fcec7e327c282d0bd0cf60f1551996d09fcf
COPY --from=tools /bin/busybox /opt/busybox
COPY deploy/supabase/render/runtime.sh /opt/supabase-runtime.sh
ENV SUPABASE_COMPONENT=rest
EXPOSE 3000
ENTRYPOINT ["/opt/busybox", "sh", "/opt/supabase-runtime.sh"]
CMD ["postgrest"]
