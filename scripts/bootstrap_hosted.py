"""Prepare only the app's volume directories, then permanently drop root.

Fresh hosted disks can replace the image's uid-10001 directory with an empty
root-owned mount. No recursive ownership changes and no environment-selected
paths are allowed here. O_NOFOLLOW rejects symlinks before fchown/fchmod.
"""
import os
import sys

APP_UID = 10001
APP_GID = 10001


def prepare_volume():
    os.makedirs('/data', mode=0o700, exist_ok=True)
    flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW
    data = os.open('/data', flags)
    try:
        try:
            os.mkdir('backups', mode=0o700, dir_fd=data)
        except FileExistsError:
            pass
        backups = os.open('backups', flags, dir_fd=data)
        try:
            for descriptor in (data, backups):
                os.fchown(descriptor, APP_UID, APP_GID)
                os.fchmod(descriptor, 0o700)
        finally:
            os.close(backups)
    finally:
        os.close(data)


def main():
    os.umask(0o077)
    if os.geteuid() == 0:
        try:
            prepare_volume()
            os.setgroups([])
            os.setgid(APP_GID)
            os.setuid(APP_UID)
        except OSError:
            print('Hosted volume initialization failed; use a dedicated writable /data mount without symlinks.', file=sys.stderr)
            return 1
    if os.getuid() != APP_UID or os.getgid() != APP_GID:
        print('Hosted commands must run as application uid/gid 10001.', file=sys.stderr)
        return 1
    if len(sys.argv) < 2:
        print('Supply the hosted application command.', file=sys.stderr)
        return 1
    os.environ['HOME'] = '/home/app'
    os.execvp(sys.argv[1], sys.argv[1:])


if __name__ == '__main__':
    raise SystemExit(main())
