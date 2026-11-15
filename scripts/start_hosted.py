"""Start the integrated production app after a bounded, read-only preflight."""
import os
import sys
import time

from check_hosted import (
    ROOT, DatabaseUnavailable, HostedConfigurationError, check_backup_directory,
    check_database, validate_environment,
)


def main():
    os.umask(0o077)
    try:
        settings = validate_environment()
        check_backup_directory(settings['backup_directory'])
        deadline = time.monotonic() + settings['wait_seconds']
        while True:
            try:
                major = check_database()
                break
            except DatabaseUnavailable:
                if time.monotonic() >= deadline:
                    raise
                print('Waiting for the application PostgreSQL database...', flush=True)
                time.sleep(min(2, max(0, deadline - time.monotonic())))
    except HostedConfigurationError as error:
        print(f'Hosted startup refused: {error}', file=sys.stderr)
        return 1
    print(f'Hosted checks passed for PostgreSQL {major}; starting the integrated application.', flush=True)
    os.chdir(ROOT / 'operations_nerd')
    os.execv(sys.executable, [sys.executable, '-m', 'uvicorn', 'main:app', '--host', '0.0.0.0',
                            '--port', str(settings['port']), '--workers', '1', '--no-proxy-headers'])


if __name__ == '__main__':
    raise SystemExit(main())
