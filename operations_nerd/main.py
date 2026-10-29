"""
Operations Nerd FastAPI app.

This file just assembles routers, it should never contain logic itself.
Run with: uvicorn main:app --reload
Interactive API docs: http://127.0.0.1:8000/docs
Frontend (questionnaire + live state view): http://127.0.0.1:8000/ui/
"""

import os

from dotenv import load_dotenv
load_dotenv()

from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles

from questionnaire.routes import router as questionnaire_router
from state.routes import router as state_router
from pipeline.routes import router as pipeline_router
from approval.routes import router as approval_router
from agentic.routes import router as agentic_router
from agentic.health import router as health_router
from agentic.middleware import BodyLimitMiddleware
from agentic import config as agentic_config

agentic_config.startup()      # refuses to start on a bad production config; creates the DB if missing

app = FastAPI(title="Operations Nerd")

app.include_router(questionnaire_router)
app.include_router(state_router)
app.include_router(pipeline_router)
app.include_router(approval_router)
app.include_router(agentic_router)
app.include_router(health_router)
app.add_middleware(BodyLimitMiddleware)

# Mounted at /ui, not /, so it never conflicts with API routes like
# /businesses or /packs/{pack_id}/questionnaire.
app.mount("/ui", StaticFiles(directory="frontend", html=True), name="ui")

# Agentic CRM single-page app (login, spec-driven CRM, chat, history).
app.mount("/app", StaticFiles(directory=os.path.join(os.path.dirname(__file__), "frontend_app"), html=True), name="app")

# Config-change agent demo: writes files into the repo's pack folders, so it
# is only mounted when explicitly enabled -- off by default, never on a
# deployed server.
if os.environ.get("CONFIG_AGENT_ENABLED") == "1":
    from config_agent.routes import router as config_agent_router
    app.include_router(config_agent_router)
    app.mount("/agent", StaticFiles(directory="frontend_agent", html=True), name="agent")

# HR module API (modules/hr/): employee records and leave, on the SQLite file in
# HR_DATABASE_PATH. It has no login or per-business database routing yet, so it
# is a local development surface only: mounted when HR_MODULE_ENABLED=1, never
# when APP_ENV=production.
if os.environ.get("HR_MODULE_ENABLED") == "1" and not agentic_config.is_production():
    import importlib.util
    _hr_api_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "modules", "hr", "api.py")
    _hr_spec = importlib.util.spec_from_file_location("hr_module_api", _hr_api_path)
    _hr_api = importlib.util.module_from_spec(_hr_spec)
    _hr_spec.loader.exec_module(_hr_api)
    app.include_router(_hr_api.router)


# Production serves only /app, /api/* and /healthz. The legacy demo routes
# (questionnaire, state, pipeline, approval, /ui, /agent) have no auth and are
# not scoped to an account, so they are dropped from the route table when
# APP_ENV=production. Local development keeps them. Done here, after
# everything is mounted, so the legacy route files stay untouched.
if agentic_config.is_production():
    _legacy_routers = (questionnaire_router, state_router, pipeline_router, approval_router)
    _legacy_paths = {r.path for router in _legacy_routers for r in router.routes} | {"/ui", "/agent"}

    def _is_legacy(route) -> bool:
        # Newer FastAPI wraps each include_router() in one entry that points back
        # at the router it was made from; older versions copy the routes in flat.
        return getattr(route, "original_router", None) in _legacy_routers or getattr(route, "path", None) in _legacy_paths

    app.router.routes[:] = [r for r in app.router.routes if not _is_legacy(r)]


@app.get("/")
def root():
    return {"status": "ok", "service": "Operations Nerd"}
