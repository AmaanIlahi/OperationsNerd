"""Resolve private deployment endpoints without placing DSNs in Blueprint files."""
import os
import re
import sys

host = os.environ.get('SUPABASE_DB_HOST', 'db')
password = os.environ.get('APP_DB_PASSWORD', '')
database = os.environ.get('APP_DB_NAME', 'opsnerd_control')
if not re.fullmatch(r'[a-zA-Z0-9.-]+', host) or not re.fullmatch(r'[a-f0-9]{64}', password):
    raise SystemExit('Configure the private host and generated application database password')
if not re.fullmatch(r'[a-z][a-z0-9_]{0,62}', database):
    raise SystemExit('Invalid application database name')
dsn = f'postgresql://app_owner:{password}@{host}:5432/{database}'
kind = os.environ.get('DEPLOY_APP_KIND', 'integrated')
if kind not in ('integrated', 'standalone'):
    raise SystemExit('DEPLOY_APP_KIND must be integrated or standalone')
if kind == 'integrated':
    os.environ['OPERATIONS_DATABASE_URL'] = dsn
    os.environ['OPERATIONS_ADMIN_DATABASE_URL'] = dsn
else:
    os.environ['LOGISTICS_DATABASE_URL'] = dsn
    redis_host = os.environ.get('REDIS_HOST', 'redis')
    if not re.fullmatch(r'[a-zA-Z0-9.-]+', redis_host):
        raise SystemExit('Invalid Redis private host')
    os.environ['REDIS_URL'] = f'redis://{redis_host}:6379/0'
if len(sys.argv) < 2:
    raise SystemExit('A container startup command is required')
os.execvp(sys.argv[1], sys.argv[1:])
