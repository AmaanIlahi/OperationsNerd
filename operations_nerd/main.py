"""
Operations Nerd FastAPI app.

This file just assembles routers, it should never contain logic itself.
Run with: uvicorn main:app --reload
Interactive API docs: http://127.0.0.1:8000/docs
Frontend (questionnaire + live state view): http://127.0.0.1:8000/ui/
"""

from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles

from questionnaire.routes import router as questionnaire_router
from state.routes import router as state_router

app = FastAPI(title="Operations Nerd")

app.include_router(questionnaire_router)
app.include_router(state_router)

# Mounted at /ui, not /, so it never conflicts with API routes like
# /businesses or /packs/{pack_id}/questionnaire.
app.mount("/ui", StaticFiles(directory="frontend", html=True), name="ui")


@app.get("/")
def root():
    return {"status": "ok", "service": "Operations Nerd"}
