"""Durable intake API, protected by the app's existing API-key gate."""
from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field
from db import db as d
from inbox import service as s

router = APIRouter()


class Intake(BaseModel):
    source: str = Field(min_length=1, max_length=40)
    idempotency_key: str = Field(min_length=1, max_length=200)
    raw_content: str = Field(min_length=1, max_length=100000)


@router.post("/businesses/{business_id}/inbox", status_code=202)
def enqueue(business_id: int, payload: Intake):
    with d.get_conn() as conn:
        try:
            return s.enqueue(conn, business_id, payload.source, payload.idempotency_key, payload.raw_content)
        except s.InboxConflict as exc:
            raise HTTPException(409, str(exc))
        except LookupError as exc:
            raise HTTPException(404, str(exc))
        except ValueError as exc:
            raise HTTPException(422, str(exc))


@router.get("/businesses/{business_id}/inbox")
def list_inbox(business_id: int, status: str | None = None, limit: int = 100):
    with d.get_conn() as conn:
        if not d.get_business(conn, business_id):
            raise HTTPException(404, "Business not found")
        try:
            return s.list_receipts(conn, business_id, status, limit)
        except ValueError as exc:
            raise HTTPException(422, str(exc))


@router.post("/businesses/{business_id}/inbox/{receipt_id}/process")
def process(business_id: int, receipt_id: int):
    with d.get_conn() as conn:
        try:
            return s.process_receipt(conn, business_id, receipt_id)
        except LookupError as exc:
            raise HTTPException(404, str(exc))
