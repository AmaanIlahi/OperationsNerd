"""Session-authenticated module lifecycle and typed Logistics commands."""
from fastapi import APIRouter, HTTPException, Request, Response
from pydantic import BaseModel, ConfigDict, Field, StrictInt
from typing import Literal

from agentic import auth
from umbrella import lifecycle

router = APIRouter(prefix="/api/businesses/{business_id}")


@router.post("/retry-setup")
def retry_setup(business_id: int, request: Request):
    from umbrella import businesses
    account = auth.current_account(request)
    return businesses.retry_provision(account["id"], business_id)


class SetupProposal(BaseModel):
    model_config = ConfigDict(extra="forbid")
    base_version: StrictInt = Field(ge=1)
    bindings: dict


class SubmitOrder(BaseModel):
    model_config = ConfigDict(extra="forbid")
    idempotency_key: str = Field(min_length=1, max_length=256)
    record_id: StrictInt | None = None


class DisableCapability(BaseModel):
    model_config = ConfigDict(extra="forbid")
    confirm: Literal[True]


class ImportPreview(BaseModel):
    model_config = ConfigDict(extra="forbid")
    csv_text: str = Field(min_length=1, max_length=1048576)


class ImportCommit(ImportPreview):
    base_version: StrictInt = Field(ge=1)
    digest: str = Field(pattern=r"^[a-f0-9]{64}$")
    idempotency_key: str = Field(min_length=1, max_length=200)


def _call(fn, *args, **kwargs):
    from modules.logistics import errors
    from agentic.records import RecordError
    try:
        return fn(*args, **kwargs)
    except HTTPException:
        raise
    except RecordError as error:
        raise HTTPException(error.status, error.detail) from None
    except errors.LogisticsError as error:
        status = 403 if isinstance(error, errors.AuthorizationError) else 404 if isinstance(error, errors.NotFound) \
            else 409 if isinstance(error, errors.Conflict) else 422
        raise HTTPException(status, str(error)) from None
    except ValueError as error:
        status = getattr(error, "status", 422)
        raise HTTPException(status if status in (400,403,404,409,422) else 422, str(error)) from None
    except Exception:
        raise HTTPException(503, "The operation could not complete. Check backend availability and retry with the same request key.") from None


@router.get("/nerds")
def list_nerds(business_id: int, request: Request):
    account = auth.current_account(request)
    return lifecycle.describe(account["id"], business_id)


@router.post("/nerds/{nerd_id}/proposals", status_code=201)
def propose(business_id: int, nerd_id: str, payload: SetupProposal, request: Request):
    account = auth.current_account(request)
    return _call(lifecycle.propose, account["id"], business_id, payload.base_version, payload.bindings, nerd_id=nerd_id)


@router.post("/nerds/{nerd_id}/proposals/{proposal_id}/approve")
def approve(business_id: int, nerd_id: str, proposal_id: str, request: Request):
    account = auth.current_account(request)
    return _call(lifecycle.approve, account["id"], business_id, proposal_id, nerd_id=nerd_id)


@router.post("/nerds/{nerd_id}/disable")
def disable(business_id: int, nerd_id: str, payload: DisableCapability, request: Request):
    account = auth.current_account(request)
    return _call(lifecycle.disable, account["id"], business_id, nerd_id=nerd_id)


@router.get("/logistics/workspace")
def workspace(business_id: int, request: Request):
    from umbrella import logistics_adapter
    account = auth.current_account(request)
    bindings = lifecycle.installed_bindings(account["id"], business_id)
    return _call(logistics_adapter.workspace, account["id"], business_id, bindings)


@router.post("/logistics/orders/{record_id}/submit")
def submit_order(business_id: int, record_id: int, payload: SubmitOrder, request: Request):
    from umbrella import logistics_adapter
    if payload.record_id is not None and payload.record_id != record_id:
        raise HTTPException(422, "Order identifiers do not match")
    account = auth.current_account(request)
    bindings = lifecycle.installed_bindings(account["id"], business_id)
    return _call(logistics_adapter.submit_order, account["id"], business_id, bindings, record_id, payload.idempotency_key)


@router.post("/logistics/commands/{operation}")
def command(business_id: int, operation: str, payload: dict, request: Request):
    from umbrella import logistics_adapter
    account = auth.current_account(request)
    bindings = lifecycle.installed_bindings(account["id"], business_id)
    if operation == "sync_records":
        key = payload.get("idempotency_key")
        if set(payload) != {"idempotency_key"} or not isinstance(key, str) or not key.strip() or len(key) > 256:
            raise HTTPException(422, "Record synchronization needs one non-empty request key")
        return _call(logistics_adapter.sync_records, account["id"], business_id, bindings, key)
    return _call(logistics_adapter.invoke, account["id"], business_id, bindings, operation, payload)


@router.get("/logistics/imports/{kind}/template")
def import_template(business_id: int, kind: str, request: Request):
    from umbrella import imports, shipment_imports
    account = auth.current_account(request)
    lifecycle.installed_bindings(account["id"], business_id)
    importer = shipment_imports if kind == "shipments" else imports
    contents = _call(importer.template_csv, kind)
    return Response(contents, media_type="text/csv", headers={"Content-Disposition": f'attachment; filename="{kind}-template.csv"'})


@router.post("/logistics/imports/{kind}/preview")
def preview_import(business_id: int, kind: str, payload: ImportPreview, request: Request):
    from umbrella import imports, shipment_imports
    account = auth.current_account(request)
    bindings = lifecycle.installed_bindings(account["id"], business_id)
    importer = shipment_imports if kind == "shipments" else imports
    return _call(importer.preview_import, account["id"], business_id, bindings, kind, payload.csv_text)


@router.post("/logistics/imports/{kind}/commit")
def commit_import(business_id: int, kind: str, payload: ImportCommit, request: Request):
    from umbrella import imports, shipment_imports
    account = auth.current_account(request)
    bindings = lifecycle.installed_bindings(account["id"], business_id)
    importer = shipment_imports if kind == "shipments" else imports
    return _call(importer.commit_import, account["id"], business_id, bindings, kind, **payload.model_dump())


@router.get("/logistics/reports")
def reports(business_id: int, request: Request, as_of: str | None = None):
    from umbrella import reports as operational_reports
    account = auth.current_account(request)
    bindings = lifecycle.installed_bindings(account["id"], business_id)
    return _call(operational_reports.build_reports, account["id"], business_id, bindings, as_of)


@router.get("/{nerd_id}/workspace")
def module_workspace(business_id: int, nerd_id: str, request: Request):
    from umbrella import registry
    account = auth.current_account(request)
    bindings = _call(lifecycle.installed_bindings, account["id"], business_id, nerd_id=nerd_id)
    adapter = _call(registry.adapter, nerd_id)
    return _call(adapter.workspace, account["id"], business_id, bindings)


@router.post("/{nerd_id}/commands/{operation}")
def module_command(business_id: int, nerd_id: str, operation: str, payload: dict, request: Request):
    from umbrella import registry
    account = auth.current_account(request)
    bindings = _call(lifecycle.installed_bindings, account["id"], business_id, nerd_id=nerd_id)
    adapter = _call(registry.adapter, nerd_id)
    if operation == "sync_records":
        key = payload.get("idempotency_key")
        if set(payload) != {"idempotency_key"} or not isinstance(key, str) or not key.strip() or len(key) > 200:
            raise HTTPException(422, "Synchronization needs a non-empty request key")
        return _call(adapter.sync_records, account["id"], business_id, bindings, key)
    return _call(adapter.invoke, account["id"], business_id, bindings, operation, payload)
