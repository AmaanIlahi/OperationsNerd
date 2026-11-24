#!/opt/busybox sh
# Render and local Compose use the same component images and private topology.
set -eu
box=/opt/busybox
component=${SUPABASE_COMPONENT:?component is required}
host=${SUPABASE_DB_HOST:-db}
case "$host" in *[!a-zA-Z0-9.-]*|'') echo 'Invalid private database host' >&2; exit 1;; esac
export POSTGRES_HOST="$host" POSTGRES_PORT=5432 POSTGRES_DB=postgres
export JWT_EXPIRY=${JWT_EXPIRY:-3600}
export PGRST_DB_SCHEMAS=public,graphql_public PGRST_DB_MAX_ROWS=1000 PGRST_DB_EXTRA_SEARCH_PATH=public
: "${POSTGRES_PASSWORD:?Configure the generated Supabase password}"
: "${JWT_SECRET:?Configure the generated JWT secret}"
: "${ANON_KEY:?Configure the generated anon JWT}"
: "${SERVICE_ROLE_KEY:?Configure the generated service JWT}"
case "$POSTGRES_PASSWORD" in *[!a-zA-Z0-9]*|'') echo 'Use the generated alphanumeric password' >&2; exit 1;; esac
export SUPABASE_PUBLIC_URL=${SUPABASE_PUBLIC_URL:?Set the actual HTTPS gateway origin}
export SITE_URL=${APP_ORIGIN:?Set the actual application origin}
export API_EXTERNAL_URL="$SUPABASE_PUBLIC_URL/auth/v1"
export SUPABASE_PUBLISHABLE_KEY='' SUPABASE_SECRET_KEY='' ANON_KEY_ASYMMETRIC='' SERVICE_ROLE_KEY_ASYMMETRIC=''
export SUPABASE_URL="http://${SUPABASE_GATEWAY_HOST:-api-gw}:8000"
case "$component" in
 db)
  export POSTGRES_HOST=/var/run/postgresql POSTGRES_USER=supabase_admin PGPORT=5432
  export PGDATA=/var/lib/postgresql/data PGDATABASE=postgres PGPASSWORD="$POSTGRES_PASSWORD" JWT_EXP="$JWT_EXPIRY"
  mkdir -p /var/lib/postgresql/custom
  "$box" cp -an /opt/postgresql-custom-template/* /var/lib/postgresql/custom/
  chown -R postgres:postgres /var/lib/postgresql/custom
  ;;
 auth)
  export GOTRUE_API_HOST=0.0.0.0 GOTRUE_API_PORT=9999 GOTRUE_DB_DRIVER=postgres
  export GOTRUE_DB_DATABASE_URL="postgres://supabase_auth_admin:$POSTGRES_PASSWORD@$host:5432/postgres"
  export GOTRUE_SITE_URL="$SITE_URL" GOTRUE_URI_ALLOW_LIST="$SITE_URL" GOTRUE_DISABLE_SIGNUP=true
  export GOTRUE_JWT_ADMIN_ROLES=service_role GOTRUE_JWT_AUD=authenticated GOTRUE_JWT_DEFAULT_GROUP_NAME=authenticated
  export GOTRUE_JWT_SECRET="$JWT_SECRET" GOTRUE_JWT_EXP="$JWT_EXPIRY" GOTRUE_JWT_ISSUER="$API_EXTERNAL_URL"
  export GOTRUE_EXTERNAL_EMAIL_ENABLED=false GOTRUE_EXTERNAL_ANONYMOUS_USERS_ENABLED=false
  export GOTRUE_EXTERNAL_PHONE_ENABLED=false GOTRUE_SMS_AUTOCONFIRM=false GOTRUE_MAILER_AUTOCONFIRM=false
  ;;
 rest)
  export PGRST_DB_URI="postgres://authenticator:$POSTGRES_PASSWORD@$host:5432/postgres"
  export PGRST_DB_ANON_ROLE=anon PGRST_ADMIN_SERVER_PORT=3001 PGRST_ADMIN_SERVER_HOST=0.0.0.0
  export PGRST_JWT_SECRET="$JWT_SECRET" PGRST_DB_USE_LEGACY_GUCS=false PGRST_APP_SETTINGS_JWT_EXP="$JWT_EXPIRY"
  ;;
 realtime)
  export PORT=4000 DB_HOST="$host" DB_PORT=5432 DB_USER=supabase_admin DB_PASSWORD="$POSTGRES_PASSWORD" DB_NAME=postgres
  export DB_AFTER_CONNECT_QUERY='SET search_path TO _realtime' DB_ENC_KEY="$REALTIME_DB_ENC_KEY"
  export API_JWT_SECRET="$JWT_SECRET" METRICS_JWT_SECRET="$JWT_SECRET" ERL_AFLAGS='-proto_dist inet_tcp'
  export DNS_NODES="''" RLIMIT_NOFILE=10000 APP_NAME=realtime SEED_SELF_HOST=true RUN_JANITOR=true DISABLE_HEALTHCHECK_LOGGING=true
  ;;
 storage)
  export DATABASE_URL="postgres://supabase_storage_admin:$POSTGRES_PASSWORD@$host:5432/postgres"
  export POSTGREST_URL="http://${SUPABASE_REST_HOST:-rest}:3000" AUTH_JWT_SECRET="$JWT_SECRET" SERVICE_KEY="$SERVICE_ROLE_KEY"
  export STORAGE_PUBLIC_URL="$SUPABASE_PUBLIC_URL" REQUEST_ALLOW_X_FORWARDED_PATH=true FILE_SIZE_LIMIT=52428800
  export STORAGE_BACKEND=file FILE_STORAGE_BACKEND_PATH=/var/lib/storage GLOBAL_S3_BUCKET=stub TENANT_ID=stub REGION=local
  export ENABLE_IMAGE_TRANSFORMATION=false S3_PROTOCOL_ACCESS_KEY_ID="$S3_PROTOCOL_ACCESS_KEY_ID" S3_PROTOCOL_ACCESS_KEY_SECRET="$S3_PROTOCOL_ACCESS_KEY_SECRET"
  ;;
 meta)
  export PG_META_PORT=8080 PG_META_DB_HOST="$host" PG_META_DB_PORT=5432 PG_META_DB_NAME=postgres
  export PG_META_DB_USER=postgres PG_META_DB_PASSWORD="$POSTGRES_PASSWORD" CRYPTO_KEY="$PG_META_CRYPTO_KEY"
  ;;
 studio)
  export HOSTNAME=0.0.0.0 PORT=3000 STUDIO_PG_META_URL="http://${SUPABASE_META_HOST:-meta}:8080"
  export POSTGRES_USER_READ_WRITE=postgres DEFAULT_ORGANIZATION_NAME='Private app' DEFAULT_PROJECT_NAME='Self-hosted Supabase'
  export OPENAI_API_KEY='' SUPABASE_ANON_KEY="$ANON_KEY" SUPABASE_SERVICE_KEY="$SERVICE_ROLE_KEY" AUTH_JWT_SECRET="$JWT_SECRET"
  export ENABLED_FEATURES_LOGS_ALL=false SNIPPETS_MANAGEMENT_FOLDER=/var/lib/studio/snippets EDGE_FUNCTIONS_MANAGEMENT_FOLDER=/opt/functions
  mkdir -p /var/lib/studio/snippets
  ;;
 functions)
  export SUPABASE_ANON_KEY="$ANON_KEY" SUPABASE_SERVICE_ROLE_KEY="$SERVICE_ROLE_KEY"
  export SUPABASE_PUBLISHABLE_KEYS='{}' SUPABASE_SECRET_KEYS='{}' VERIFY_JWT=true
  export SUPABASE_DB_URL="postgresql://postgres:$POSTGRES_PASSWORD@$host:5432/postgres"
  ;;
 api-gw)
  # Preserve official gateway auth/routing. Change only service DNS addresses
  # and writable config paths; do not expose the Envoy admin listener.
  for key in AUTH REST REALTIME STORAGE FUNCTIONS META STUDIO; do
   eval "value=\${SUPABASE_${key}_HOST:-}"
   case "$value" in *[!a-zA-Z0-9.-]*) echo 'Invalid upstream host' >&2; exit 1;; esac
  done
  "$box" sed \
   -e "s/address: auth$/address: ${SUPABASE_AUTH_HOST:-auth}/" \
   -e "s/address: rest$/address: ${SUPABASE_REST_HOST:-rest}/" \
   -e "s/address: realtime-dev.supabase-realtime$/address: ${SUPABASE_REALTIME_HOST:-realtime}/" \
   -e "s/address: storage$/address: ${SUPABASE_STORAGE_HOST:-storage}/" \
   -e "s/address: functions$/address: ${SUPABASE_FUNCTIONS_HOST:-functions}/" \
   -e "s/address: meta$/address: ${SUPABASE_META_HOST:-meta}/" \
   -e "s/address: studio$/address: ${SUPABASE_STUDIO_HOST:-studio}/" \
   /opt/envoy/cds.yaml > /tmp/cds.yaml
  "$box" sed -e 's|/etc/envoy/cds.yaml|/tmp/cds.yaml|g' -e 's|/etc/envoy/lds.yaml|/tmp/lds.yaml|g' \
   /opt/envoy/envoy.yaml > /tmp/envoy.yaml
  hash=$(printf '%s' "$DASHBOARD_PASSWORD" | openssl sha1 -binary | openssl base64)
  basic="$DASHBOARD_USERNAME:{SHA}$hash"
  "$box" sed -e "s|\${ANON_KEY}|$ANON_KEY|g" -e "s|\${SERVICE_ROLE_KEY}|$SERVICE_ROLE_KEY|g" \
   -e 's|${ANON_KEY_ASYMMETRIC}||g' -e 's|${SERVICE_ROLE_KEY_ASYMMETRIC}||g' \
   -e 's|${SUPABASE_PUBLISHABLE_KEY}||g' -e 's|${SUPABASE_SECRET_KEY}||g' \
   -e "s|\${SUPABASE_PUBLIC_URL}|$SUPABASE_PUBLIC_URL|g" -e "s|\${DASHBOARD_BASIC_AUTH}|$basic|g" \
   /opt/envoy/lds.template.yaml > /tmp/lds.yaml
  exec envoy -c /tmp/envoy.yaml
  ;;
 *) echo 'Unknown Supabase component' >&2; exit 1;;
esac
exec "$@"
