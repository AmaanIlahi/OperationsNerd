"""
Agentic CRM endpoints, Phases B and C: versions, records, chat, proposals,
revert. Included into the Phase A router, so main.py needs no changes.

Every handler resolves the logged-in account first and passes it down;
anything the account does not own comes back as 404.
"""

from typing import Literal

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel

from agentic import auth, chat, records, store
from db import db as d

router = APIRouter()


class Message(BaseModel):
    role: Literal["user", "assistant"]
    content: str


class ChatRequest(BaseModel):
    messages: list[Message]


class RevertRequest(BaseModel):
    to_version: int


class RecordValues(BaseModel):
    values: dict


def _run(fn, *args):
    try:
        return fn(*args)
    except chat.ChangeError as e:
        detail = {"detail": e.detail, **e.extra} if e.extra else e.detail
        raise HTTPException(status_code=e.status, detail=detail)
    except records.RecordError as e:
        raise HTTPException(status_code=e.status, detail=e.detail)


# ---------- versions ----------

@router.get("/businesses/{business_id}/versions/{version}")
def get_version(business_id: int, version: int, request: Request):
    account = auth.current_account(request)
    with d.get_conn() as conn:
        found = store.get_version(conn, account["id"], business_id, version)
    if found is None:
        raise HTTPException(status_code=404, detail="Version not found")
    return found


@router.post("/businesses/{business_id}/revert")
def revert(business_id: int, payload: RevertRequest, request: Request):
    account = auth.current_account(request)
    return _run(chat.revert, account["id"], business_id, payload.to_version)


# ---------- agent turn ----------

@router.post("/businesses/{business_id}/chat")
def chat_turn(business_id: int, payload: ChatRequest, request: Request):
    account = auth.current_account(request)
    return _run(chat.chat, account["id"], business_id, [m.model_dump() for m in payload.messages])


@router.post("/proposals/{proposal_id}/approve")
def approve(proposal_id: int, request: Request):
    account = auth.current_account(request)
    return _run(chat.approve, account["id"], proposal_id)


@router.post("/proposals/{proposal_id}/reject")
def reject(proposal_id: int, request: Request):
    account = auth.current_account(request)
    return _run(chat.reject, account["id"], proposal_id)


# ---------- records ----------

@router.post("/businesses/{business_id}/records/{entity_key}", status_code=201)
def create_record(business_id: int, entity_key: str, payload: RecordValues, request: Request):
    account = auth.current_account(request)
    return _run(records.create_record, account["id"], business_id, entity_key, payload.values)


@router.get("/businesses/{business_id}/records/{entity_key}")
def list_records(business_id: int, entity_key: str, request: Request,
                 limit: int = records.DEFAULT_LIMIT, offset: int = 0):
    account = auth.current_account(request)
    return _run(records.list_records, account["id"], business_id, entity_key, limit, offset)


@router.get("/businesses/{business_id}/records/{entity_key}/{record_id}")
def get_record(business_id: int, entity_key: str, record_id: int, request: Request):
    account = auth.current_account(request)
    return _run(records.get_record, account["id"], business_id, entity_key, record_id)


@router.patch("/businesses/{business_id}/records/{entity_key}/{record_id}")
def update_record(business_id: int, entity_key: str, record_id: int, payload: RecordValues,
                  request: Request):
    account = auth.current_account(request)
    return _run(records.update_record, account["id"], business_id, entity_key, record_id, payload.values)
