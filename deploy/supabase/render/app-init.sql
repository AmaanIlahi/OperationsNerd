-- Run only by the official Supabase image on a NEW PostgreSQL data volume.
-- psql quoting keeps passwords out of SQL interpolation and logs.
\getenv app_password APP_DB_PASSWORD
\getenv app_database APP_DB_NAME
\getenv allow_create APP_ALLOW_DATABASE_CREATE
\if :allow_create
SELECT format('CREATE ROLE app_owner LOGIN CREATEDB PASSWORD %L', :'app_password')
 WHERE NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname='app_owner') \gexec
\else
SELECT format('CREATE ROLE app_owner LOGIN PASSWORD %L', :'app_password')
 WHERE NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname='app_owner') \gexec
\endif
SELECT format('CREATE DATABASE %I OWNER app_owner', :'app_database')
 WHERE NOT EXISTS (SELECT 1 FROM pg_database WHERE datname=:'app_database') \gexec
