"""Liveness probe for the hosting platform: no auth, touches no data."""

from fastapi import APIRouter

router = APIRouter()


@router.get("/healthz")
def healthz():
    return {"status": "ok"}
