"""
WSGI entry point, for hosts that only run WSGI apps (PythonAnywhere).

The app is ASGI (FastAPI); a2wsgi's ASGIMiddleware lets a WSGI server call it.
Point the host's WSGI file at this module:

    from wsgi import application

Everywhere else (Docker, local development) keep using `uvicorn main:app`.
See documentation/deploy_pythonanywhere.md.
"""

import os
import sys
import threading

# main.py mounts StaticFiles("frontend") relative to the working directory and
# imports its packages by bare name, so run from this folder whatever the host
# started us in.
APP_DIR = os.path.dirname(os.path.abspath(__file__))
os.chdir(APP_DIR)
if APP_DIR not in sys.path:
    sys.path.insert(0, APP_DIR)

from a2wsgi import ASGIMiddleware

from main import app

# ASGIMiddleware starts a background event-loop thread in its constructor. uWSGI
# imports this file in the master process and then forks the worker, and threads
# do not survive a fork, so a wrapper built at import time would hand every
# request to a loop that no longer runs and hang. Build it on the first request
# instead, in whichever process that is, and again if the process id changes.
_lock = threading.Lock()
_wrapper = None
_wrapper_pid = None


def _after_fork_in_child():
    global _lock
    _lock = threading.Lock()          # the master could have been holding it at the fork


if hasattr(os, "register_at_fork"):
    os.register_at_fork(after_in_child=_after_fork_in_child)


def _asgi_to_wsgi():
    global _wrapper, _wrapper_pid
    pid = os.getpid()
    if _wrapper is None or _wrapper_pid != pid:
        with _lock:
            if _wrapper is None or _wrapper_pid != pid:
                _wrapper = ASGIMiddleware(app)
                _wrapper_pid = pid
    return _wrapper


def application(environ, start_response):
    # WSGI servers (uWSGI, wsgiref) set CONTENT_LENGTH="" on requests with no
    # body. a2wsgi turns that into an empty content-length header, which the
    # body-size middleware rightly treats as malformed and answers 413 -- to every
    # GET. Dropping the empty value restores "no declared length".
    if environ.get("CONTENT_LENGTH", None) == "":
        del environ["CONTENT_LENGTH"]
    return _asgi_to_wsgi()(environ, start_response)
