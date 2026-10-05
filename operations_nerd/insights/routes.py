"""Audit, analytics and review endpoints."""

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import PlainTextResponse
from pydantic import BaseModel

from db import db as d
from approval.service import ApprovalError
from insights import service as s
from insights import intake
from pipeline.service import process_event, PipelineError
from packs.loader import load_pack, PackLoadError
import json as _json

router = APIRouter()


class EditApprove(BaseModel):
    body: str


class RejectBody(BaseModel):
    reason: str = ""


class Bulk(BaseModel):
    ids: list[int]
    decision: str


def _need_business(conn, business_id):
    if not d.get_business(conn, business_id):
        raise HTTPException(status_code=404, detail=f"No business with id {business_id}")


@router.get("/businesses/{business_id}/audit")
def audit(business_id: int, kind: str | None = None, limit: int = 100):
    with d.get_conn() as conn:
        _need_business(conn, business_id)
        return s.list_audit(conn, business_id, kind, limit)


@router.get("/businesses/{business_id}/stats")
def stats(business_id: int):
    with d.get_conn() as conn:
        _need_business(conn, business_id)
        return s.stats(conn, business_id)


@router.get("/businesses/{business_id}/export.csv", response_class=PlainTextResponse)
def export_csv(business_id: int):
    with d.get_conn() as conn:
        _need_business(conn, business_id)
        return PlainTextResponse(s.export_actions_csv(conn, business_id), media_type="text/csv")


@router.get("/businesses/{business_id}/overdue")
def overdue(business_id: int, minutes: int = 60):
    with d.get_conn() as conn:
        _need_business(conn, business_id)
        return s.overdue_pending(conn, business_id, minutes)


@router.post("/drafted-actions/{action_id}/edit-approve")
def edit_approve(action_id: int, payload: EditApprove):
    with d.get_conn() as conn:
        try:
            return s.edit_and_approve(conn, action_id, payload.body)
        except ApprovalError as e:
            raise HTTPException(status_code=409, detail=str(e))


@router.post("/drafted-actions/{action_id}/reject-with-reason")
def reject_reason(action_id: int, payload: RejectBody):
    with d.get_conn() as conn:
        try:
            return s.reject_with_reason(conn, action_id, payload.reason)
        except ApprovalError as e:
            raise HTTPException(status_code=409, detail=str(e))


@router.post("/drafted-actions/bulk")
def bulk(payload: Bulk):
    with d.get_conn() as conn:
        try:
            return s.bulk_decide(conn, payload.ids, payload.decision)
        except ApprovalError as e:
            raise HTTPException(status_code=422, detail=str(e))


@router.post("/businesses/{business_id}/contacts/import")
async def import_contacts(business_id: int, request: Request):
    text = (await request.body()).decode("utf-8", errors="replace")
    with d.get_conn() as conn:
        _need_business(conn, business_id)
        try:
            return intake.import_contacts_csv(conn, business_id, text)
        except ValueError as e:
            raise HTTPException(status_code=422, detail=str(e))


@router.post("/webhooks/{business_id}/{source}")
async def webhook(business_id: int, source: str, request: Request):
    """Signed intake: X-Signature header = HMAC-SHA256(body) with WEBHOOK_SECRET. Body: {"raw_content": "..."}."""
    raw = await request.body()
    if not intake.verify_signature(raw, request.headers.get("X-Signature", "")):
        raise HTTPException(status_code=401, detail="Bad or missing signature (is WEBHOOK_SECRET set?)")
    try:
        content = _json.loads(raw)["raw_content"]
        assert isinstance(content, str) and content.strip()
    except Exception:
        raise HTTPException(status_code=422, detail='Body must be JSON like {"raw_content": "..."}')
    with d.get_conn() as conn:
        business = d.get_business(conn, business_id)
        if not business:
            raise HTTPException(status_code=404, detail=f"No business with id {business_id}")
        try:
            pack = load_pack(business["industry_pack"])
        except PackLoadError as e:
            raise HTTPException(status_code=500, detail=str(e))
        event_id = d.create_event(conn, business_id=business_id, source=source[:40], raw_content=content)
        try:
            result = process_event(conn, pack, event_id)
        except PipelineError as e:
            conn.commit()
            s.log(conn, "pipeline_error", str(e), business_id, event_id)
            conn.commit()
            raise HTTPException(status_code=422, detail=str(e))
    return {"event_id": event_id, "drafted_action": result}


@router.get("/pack-health")
def packs():
    from packs.validate import check_all
    return check_all()
