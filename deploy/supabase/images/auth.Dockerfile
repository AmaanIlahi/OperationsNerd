# Supabase self-hosted/v0.8.4, commit 189adda99d1b56da55fc5a6ee3f849354aaea92a
FROM busybox:1.37.0-musl@sha256:f6a95d611e0a8780fabdb2aec43e31275b2720dfefa3e41b16835bfc42454478 AS tools
FROM supabase/gotrue:v2.197.0@sha256:1736a63078f5922b198c4cbe50f80ab9a2d3b54fe8b7b6cfb2e9dc5dbbc12c6b
COPY --from=tools /bin/busybox /opt/busybox
COPY deploy/supabase/render/runtime.sh /opt/supabase-runtime.sh
ENV SUPABASE_COMPONENT=auth
EXPOSE 9999
ENTRYPOINT ["/opt/busybox", "sh", "/opt/supabase-runtime.sh"]
CMD ["auth"]
