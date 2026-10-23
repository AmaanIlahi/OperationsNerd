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

# main.py mounts StaticFiles("frontend") relative to the working directory and
# imports its packages by bare name, so run from this folder whatever the host
# started us in.
APP_DIR = os.path.dirname(os.path.abspath(__file__))
os.chdir(APP_DIR)
if APP_DIR not in sys.path:
    sys.path.insert(0, APP_DIR)

from a2wsgi import ASGIMiddleware

from main import app

_asgi_to_wsgi = ASGIMiddleware(app)


def application(environ, start_response):
    # WSGI servers (uWSGI, wsgiref) set CONTENT_LENGTH="" on requests with no
    # body. a2wsgi turns that into an empty content-length header, which the
    # body-size middleware rightly treats as malformed and answers 413 -- to every
    # GET. Dropping the empty value restores "no declared length".
    if environ.get("CONTENT_LENGTH", None) == "":
        del environ["CONTENT_LENGTH"]
    return _asgi_to_wsgi(environ, start_response)
