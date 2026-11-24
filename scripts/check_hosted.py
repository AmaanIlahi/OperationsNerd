"""Production preflight; never loads .env or mutates the database.

The backup-directory check creates/removes one private probe file. Database
checks read metadata and privileges; they create no users, databases or records.
"""
from pathlib import Path
import json
import os
import stat
import sys
import tempfile

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'operations_nerd'))


class HostedConfigurationError(RuntimeError):
    """Operator-safe message without supplied URLs, passwords or keys."""


class DatabaseUnavailable(HostedConfigurationError):
    """Database may still be starting; safe for a bounded startup retry."""


def _number(name, default, minimum, maximum):
    value = os.environ.get(name, str(default))
    if len(value) > len(str(maximum)) or not value.isascii() or not value.isdigit() or not minimum <= int(value) <= maximum:
        raise HostedConfigurationError(f'{name} must be a whole number from {minimum} to {maximum}')
    return int(value)


def _connection_info(name):
    from psycopg.conninfo import conninfo_to_dict
    value = os.environ.get(name, '')
    if not value.startswith(('postgresql://', 'postgres://')):
        raise HostedConfigurationError(f'{name} must be an explicit PostgreSQL connection URL')
    try:
        params = conninfo_to_dict(value)
    except Exception:
        raise HostedConfigurationError(f'{name} is not a valid PostgreSQL connection URL') from None
    if not all(params.get(key) for key in ('host', 'dbname', 'user', 'password')):
        raise HostedConfigurationError(f'{name} needs an explicit host, database, user and password')
    port = params.get('port', '5432')
    if len(port) > 5 or not port.isascii() or not port.isdigit() or not 1 <= int(port) <= 65535:
        raise HostedConfigurationError(f'{name} needs one valid PostgreSQL TCP port')
    host = params['host'].lower()
    if ',' in host or host.startswith('/') or 'pooler' in host or 'supavisor' in host or int(port) == 6543:
        raise HostedConfigurationError(f'{name} must use the direct PostgreSQL service, not a pooler or development socket')
    return params


def validate_environment():
    """Validate without opening a connection or printing environment values."""
    if os.environ.get('APP_ENV', '').strip().lower() != 'production':
        raise HostedConfigurationError('Hosted startup requires APP_ENV=production')
    if os.environ.get('OPERATIONS_UMBRELLA_ENABLED') != '1':
        raise HostedConfigurationError('Hosted startup requires OPERATIONS_UMBRELLA_ENABLED=1')
    if os.environ.get('ALLOW_OPEN_SIGNUP', '').strip() == '1':
        raise HostedConfigurationError('Hosted startup requires invite-only signup')
    invite = os.environ.get('SIGNUP_INVITE_CODE', '')
    if len(invite) < 12 or invite == 'local-demo':
        raise HostedConfigurationError('Set a private SIGNUP_INVITE_CODE of at least 12 characters')
    if os.environ.get('RATE_LIMIT_ENABLED', '1').strip() == '0':
        raise HostedConfigurationError('Hosted startup requires authentication rate limiting')
    if not os.environ.get('TRUSTED_PROXIES', '').strip():
        raise HostedConfigurationError('Set TRUSTED_PROXIES for the hosting reverse proxy')
    provider = os.environ.get('LLM_PROVIDER', '').strip().lower()
    if provider not in ('gemini', 'anthropic', 'baseten', 'stub'):
        raise HostedConfigurationError('Explicitly select LLM_PROVIDER: gemini, anthropic, baseten or stub')
    _number('CHAT_DAILY_LIMIT', 50, 1, 1000000)
    _number('MAX_BODY_BYTES', 8388608, 1, 67108864)
    port = _number('PORT', 8000, 1, 65535)
    wait_seconds = _number('OPERATIONS_STARTUP_WAIT_SECONDS', 60, 0, 300)
    from agentic import config
    problems = config.check_environment()
    if problems:
        raise HostedConfigurationError('; '.join(problems))
    control = _connection_info('OPERATIONS_DATABASE_URL')
    if os.environ.get('OPERATIONS_ADMIN_DATABASE_URL'):
        admin = _connection_info('OPERATIONS_ADMIN_DATABASE_URL')
        # Current provisioning creates a DB owned by the admin connection role,
        # then initializes it with the control role. Separate roles need a new
        # explicit OWNER/grant contract, rather than accidental PUBLIC grants.
        def origin(info):
            return info['host'].lower(), info.get('hostaddr'), info.get('port', '5432'), info['user']
        if origin(admin) != origin(control):
            raise HostedConfigurationError('Admin and runtime connections must use the same PostgreSQL server and owning role')
    backup = Path(os.environ.get('OPERATIONS_BACKUP_DIR', ''))
    if not os.environ.get('OPERATIONS_BACKUP_DIR') or not backup.is_absolute():
        raise HostedConfigurationError('OPERATIONS_BACKUP_DIR must be an absolute path on durable storage')
    return {'port': port, 'wait_seconds': wait_seconds, 'provider': provider, 'backup_directory': backup}


def check_backup_directory(path):
    try:
        path.mkdir(mode=0o700, parents=True, exist_ok=True)
        if not path.is_dir() or stat.S_IMODE(path.stat().st_mode) & 0o077:
            raise HostedConfigurationError('Backup directory must be private (0700) and writable by container uid 10001')
        with tempfile.NamedTemporaryFile(prefix='.hosted-write-check-', dir=path):
            pass
    except HostedConfigurationError:
        raise
    except OSError:
        raise HostedConfigurationError('Durable backup directory is not writable; provision it for container uid 10001') from None


def check_database():
    import psycopg
    try:
        with psycopg.connect(os.environ['OPERATIONS_DATABASE_URL'], autocommit=True,
                             connect_timeout=5, application_name='operationsnerd-hosted-preflight') as conn:
            major = conn.info.server_version // 10000
            row = conn.execute("""SELECT r.rolcreatedb OR r.rolsuper,
                has_database_privilege(current_user,current_database(),'CREATE'),
                pg_is_in_recovery(), current_setting('transaction_read_only')
                FROM pg_roles r WHERE r.rolname=current_user""").fetchone()
            if row is None or not row[0] or not row[1] or row[2] or row[3] != 'off':
                raise HostedConfigurationError('Direct PostgreSQL role needs schema creation and CREATEDB on the writable primary')
    except HostedConfigurationError:
        raise
    except psycopg.Error:
        raise DatabaseUnavailable('PostgreSQL is unavailable; check the direct service, control database and role credentials') from None
    from umbrella.backup import BackupToolError, _toolchain
    try:
        _toolchain(major)
    except BackupToolError as error:
        raise HostedConfigurationError(str(error)) from None
    return major


def preflight():
    settings = validate_environment()
    check_backup_directory(settings['backup_directory'])
    return {**settings, 'postgres_major': check_database()}


def main():
    os.umask(0o077)
    try:
        result = preflight()
    except HostedConfigurationError as error:
        print(f'Hosted preflight failed: {error}', file=sys.stderr)
        return 1
    print(json.dumps({'status': 'ready', 'postgres_major': result['postgres_major'],
                      'provider': result['provider'], 'checks': 'configuration, direct PostgreSQL privileges, backup tools and private writable backup directory'}))
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
