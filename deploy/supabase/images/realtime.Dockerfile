# Supabase self-hosted/v0.8.4, commit 189adda99d1b56da55fc5a6ee3f849354aaea92a
FROM busybox:1.37.0-musl@sha256:f6a95d611e0a8780fabdb2aec43e31275b2720dfefa3e41b16835bfc42454478 AS tools
FROM supabase/realtime:v2.148.3@sha256:25cf375b9b7cb71d90cdf6e04a1a30996e39a8df866ebd0525bba1d959edc8d6
COPY --from=tools /bin/busybox /opt/busybox
COPY deploy/supabase/render/runtime.sh /opt/supabase-runtime.sh
ENV SUPABASE_COMPONENT=realtime
EXPOSE 4000
ENTRYPOINT ["/opt/busybox", "sh", "/opt/supabase-runtime.sh"]
CMD ["/usr/bin/tini", "-s", "-g", "--", "/app/run.sh", "/app/bin/server"]
