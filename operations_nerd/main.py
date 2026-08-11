"""
Operations Nerd FastAPI app.

This file just assembles routers, it should never contain logic itself.
Run with: uvicorn main:app --reload
Interactive docs (also the v1 demo interface): http://127.0.0.1:8000/docs
"""

from fastapi import FastAPI

from questionnaire.routes import router as questionnaire_router

app = FastAPI(title="Operations Nerd")

app.include_router(questionnaire_router)


@app.get("/")
def root():
    return {"status": "ok", "service": "Operations Nerd"}
