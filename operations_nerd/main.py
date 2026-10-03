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

app = FastAPI(title="Operations Nerd")

app.include_router(questionnaire_router)
app.include_router(state_router)
app.include_router(pipeline_router)
app.include_router(approval_router)
app.include_router(agentic_router)

# Mounted at /ui, not /, so it never conflicts with API routes like
# /businesses or /packs/{pack_id}/questionnaire.
app.mount("/ui", StaticFiles(directory="frontend", html=True), name="ui")

# Config-change agent demo: writes files into the repo's pack folders, so it
# is only mounted when explicitly enabled -- off by default, never on a
# deployed server.
if os.environ.get("CONFIG_AGENT_ENABLED") == "1":
    from config_agent.routes import router as config_agent_router
    app.include_router(config_agent_router)
    app.mount("/agent", StaticFiles(directory="frontend_agent", html=True), name="agent")


@app.get("/")
def root():
    return {"status": "ok", "service": "Operations Nerd"}
