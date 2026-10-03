"""
Agentic CRM endpoints, Phase A: accounts, businesses, spec versions.

Everything except signup and login requires a logged-in account, and every
business lookup is scoped to that account (404 otherwise).
"""

from fastapi import APIRouter, HTTPException, Request, Response
from pydantic import BaseModel, Field

from agentic import auth, store, template_store
from agentic.spec import empty_spec, rekey_spec
from agentic.validator import validate_spec
from db import db as d

router = APIRouter(prefix="/api")


class SignupRequest(BaseModel):
    email: str
    password: str
    invite_code: str | None = None


class LoginRequest(BaseModel):
    email: str
    password: str


class CreateBusinessRequest(BaseModel):
    name: str = Field(min_length=1, max_length=100)
    template: str | None = None


# ---------- auth ----------

@router.post("/auth/signup", status_code=201)
def signup(payload: SignupRequest, request: Request, response: Response):
    if not auth.invite_code_ok(payload.invite_code):
        raise HTTPException(status_code=403, detail="Invalid invite code")
    email = auth.normalize_email(payload.email)
    if not auth.check_email(email):
        raise HTTPException(status_code=422, detail="Enter a valid email address")
    problem = auth.check_password(payload.password)
    if problem:
        raise HTTPException(status_code=422, detail=problem)

    password_hash = auth.hash_password(payload.password)
    with d.get_conn() as conn:
        try:
            account_id = store.create_account(conn, email, password_hash)
        except store.EmailTaken:
            raise HTTPException(status_code=409, detail="An account with this email already exists")
        token = auth.create_session(conn, account_id)
    auth.set_session_cookie(request, response, token)
    return {"id": account_id, "email": email}


@router.post("/auth/login")
def login(payload: LoginRequest, request: Request, response: Response):
    email = auth.normalize_email(payload.email)
    with d.get_conn() as conn:
        account = store.get_account_by_email(conn, email)
        ok = auth.verify_password(payload.password, account["password_hash"] if account else None)
        if not ok:
            raise HTTPException(status_code=401, detail="Wrong email or password")
        token = auth.create_session(conn, account["id"])
    auth.set_session_cookie(request, response, token)
    return {"id": account["id"], "email": account["email"]}


@router.post("/auth/logout")
def logout(request: Request, response: Response):
    token = request.cookies.get(auth.SESSION_COOKIE)
    if token:
        with d.get_conn() as conn:
            auth.delete_session(conn, token)
    auth.clear_session_cookie(request, response)
    return {"ok": True}


@router.get("/auth/me")
def me(request: Request):
    return auth.current_account(request)


# ---------- businesses ----------

@router.post("/businesses", status_code=201)
def create_business(payload: CreateBusinessRequest, request: Request):
    account = auth.current_account(request)
    name = payload.name.strip()
    if not name:
        raise HTTPException(status_code=422, detail="Business name is required")

    if payload.template is not None:
        template = template_store.get_template(payload.template)
        if template is None:
            raise HTTPException(
                status_code=422,
                detail=f"Unknown template '{payload.template}'. Available: {template_store.list_templates()}",
            )
        spec = rekey_spec(template)
        spec["business"]["name"] = name
    else:
        spec = empty_spec(name)

    issues = validate_spec(spec)
    if issues:
        # Templates are validated at startup, so this is a server bug, not a client error.
        raise HTTPException(status_code=500, detail="Generated spec failed validation: " + "; ".join(issues))

    with d.get_conn() as conn:
        business_id = store.create_business_with_version_1(
            conn, account["id"], name, payload.template, spec
        )
        business = store.get_business_with_spec(conn, account["id"], business_id)
    return business


@router.get("/businesses")
def list_businesses(request: Request):
    account = auth.current_account(request)
    with d.get_conn() as conn:
        return store.list_businesses(conn, account["id"])


@router.get("/businesses/{business_id}")
def get_business(business_id: int, request: Request):
    account = auth.current_account(request)
    with d.get_conn() as conn:
        business = store.get_business_with_spec(conn, account["id"], business_id)
    if business is None:
        raise HTTPException(status_code=404, detail="Business not found")
    return business


@router.get("/businesses/{business_id}/versions")
def get_versions(business_id: int, request: Request):
    account = auth.current_account(request)
    with d.get_conn() as conn:
        versions = store.list_versions(conn, account["id"], business_id)
    if versions is None:
        raise HTTPException(status_code=404, detail="Business not found")
    return versions
