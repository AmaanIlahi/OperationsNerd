"""Authenticated, same-business core-party adapter and typed owner commands."""
from contextlib import contextmanager
from datetime import date
import json
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, StrictBool, StrictInt, ValidationError, field_validator
from psycopg.rows import dict_row

from db import db as core_db
from db.postgres import CORE_WRITE_LOCK
from umbrella.bindings import validate_bindings

from . import db, ledger
from .errors import AccountingError, Conflict, Forbidden, NotFound

Positive = Annotated[StrictInt, Field(gt=0, le=9223372036854775807)]
Money = Annotated[StrictInt, Field(ge=0, le=ledger.MAX_MINOR)]
Text = Annotated[str, Field(min_length=1, max_length=500)]
Key = Annotated[str, Field(min_length=1, max_length=200, pattern=r"^\S+$")]


class Input(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)
    idempotency_key: Key

    @field_validator("entry_date", "issue_date", "due_date", "payment_date", mode="before", check_fields=False)
    @classmethod
    def calendar_dates(cls, value):
        # Pydantic's default date conversion also accepts epoch timestamps.
        # Financial dates are explicit calendar dates, never inferred epochs.
        return ledger.day(value)


class Account(Input):
    code: Text
    name: Text
    account_type: Literal["asset", "liability", "equity", "income", "expense"]
    role: Text | None = None
    is_capital: StrictBool | None = None


class AccountUpdate(Input):
    account_id: Positive
    name: Text | None = None
    is_active: StrictBool | None = None


class Fund(Input):
    code: Text
    name: Text
    restriction_type: Literal["temporarily_restricted", "permanently_restricted"]
    purpose: Annotated[str, Field(max_length=2000)] | None = None


class FundUpdate(Input):
    fund_id: Positive
    name: Text | None = None
    purpose: Annotated[str, Field(max_length=2000)] | None = None
    is_active: StrictBool | None = None


class JournalLine(BaseModel):
    model_config = ConfigDict(extra="forbid")
    account_id: Positive
    debit_minor: Money = 0
    credit_minor: Money = 0
    fund_id: Positive | None = None


class Journal(Input):
    entry_date: date
    memo: Text
    currency: Literal["USD"] = "USD"
    lines: Annotated[list[JournalLine], Field(min_length=2, max_length=500)]


class Reversal(Input):
    entry_id: Positive
    entry_date: date
    reason: Text


class DocumentLine(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)
    description: Text
    quantity: Annotated[StrictInt, Field(gt=0, le=1000000)]
    unit_price_minor: Money
    account_id: Positive


class Document(Input):
    number: Text
    party_record_id: Positive
    issue_date: date
    due_date: date
    currency: Literal["USD"] = "USD"
    fund_id: Positive | None = None
    lines: Annotated[list[DocumentLine], Field(min_length=1, max_length=500)]


class DocumentPost(Input):
    document_id: Positive


class Payment(DocumentPost):
    amount_minor: Annotated[StrictInt, Field(gt=0, le=ledger.MAX_MINOR)]
    cash_account_id: Positive
    payment_date: date
    reference: Text


class Void(DocumentPost):
    entry_date: date
    reason: Text


class Grant(Input):
    fund_id: Positive
    amount_minor: Annotated[StrictInt, Field(gt=0, le=ledger.MAX_MINOR)]
    cash_account_id: Positive
    revenue_account_id: Positive
    entry_date: date
    memo: Text


class Release(Input):
    fund_id: Positive
    amount_minor: Annotated[StrictInt, Field(gt=0, le=ledger.MAX_MINOR)]
    entry_date: date
    memo: Text


@contextmanager
def _connection(account_id, business_id, bindings, *, lock=False, enabling=False):
    with core_db.control_conn() as control:
        if control.execute("SELECT 1 FROM accounts WHERE id=?", (account_id,)).fetchone() is None:
            raise Forbidden("Authenticated owner account is unavailable")
    with core_db.raw_postgres_conn(autocommit=True) as conn:
        conn.row_factory = dict_row
        conn.execute("SET search_path TO accounting, core, public")
        conn.execute("SET statement_timeout TO '30s'")
        conn.execute("SET lock_timeout TO '10s'")
        if lock:
            conn.execute("SELECT pg_advisory_lock(%s)", (CORE_WRITE_LOCK,))
        try:
            business = conn.execute("""SELECT s.spec_json FROM core.businesses b JOIN core.spec_versions s
                ON s.business_id=b.id AND s.version=b.current_version WHERE b.id=%s AND b.owner_key=%s""",
                (business_id, str(account_id))).fetchone()
            if business is None:
                raise NotFound("Business not found")
            clean = validate_bindings(json.loads(business["spec_json"]), bindings, nerd_id="accounting")
            installation = conn.execute("SELECT status,bindings_json FROM core.nerd_installations WHERE nerd_id='accounting'").fetchone()
            if installation is None or installation["status"] not in (("enabled", "enabling") if enabling else ("enabled",)):
                raise Conflict("Enable Accounting before using its records")
            approved = validate_bindings(json.loads(business["spec_json"]), json.loads(installation["bindings_json"]), nerd_id="accounting")
            if approved != clean:
                raise Conflict("The approved Accounting mappings changed; refresh them")
            identity = conn.execute("SELECT business_key,currency FROM accounting.settings WHERE singleton").fetchone()
            if not identity or identity["business_key"] != f"business:{business_id}" or identity["currency"] != "USD":
                raise Forbidden("Accounting database identity or currency does not match this business")
            yield conn, clean
        finally:
            if lock:
                conn.execute("SELECT pg_advisory_unlock(%s)", (CORE_WRITE_LOCK,))


def _party(conn, business_id, mapping, record_id):
    if conn.execute("SELECT 1 FROM core.entities WHERE business_id=%s AND entity_type=%s AND id=%s",
                    (business_id, mapping["entity"], record_id)).fetchone() is None:
        raise NotFound("Choose a mapped party record in this business")
    rows = conn.execute("SELECT attribute_name,value FROM core.entity_attributes WHERE entity_type=%s AND entity_id=%s",
                        (mapping["entity"], record_id)).fetchall()
    attributes = {row["attribute_name"]: json.loads(row["value"]) for row in rows}
    name = attributes.get(mapping["fields"]["name"])
    if not isinstance(name, str) or not name.strip():
        raise AccountingError("The core party needs a nonblank name")
    result = {"record_id": record_id, "entity": mapping["entity"], "name": name}
    address_key = mapping["fields"].get("address")
    if address_key:
        result["address"] = attributes.get(address_key)
    return result


def _references(conn, business_id, bindings):
    result = {}
    for role, plural in (("customer", "customers"), ("supplier", "suppliers")):
        mapping = bindings[role]
        identifiers = [row["id"] for row in conn.execute("SELECT id FROM core.entities WHERE business_id=%s AND entity_type=%s ORDER BY id",
                                                        (business_id, mapping["entity"])).fetchall()]
        result[plural] = [_party(conn, business_id, mapping, identifier) for identifier in identifiers]
    return result


def sync_records(account_id, business_id, bindings, idempotency_key):
    Input(idempotency_key=idempotency_key)
    with _connection(account_id, business_id, bindings, enabling=True) as (conn, clean):
        with conn.transaction():
            conn.execute("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ READ ONLY")
            references = _references(conn, business_id, clean)
            return {"synced": sum(len(rows) for rows in references.values()), "references": references}


def workspace(account_id, business_id, bindings):
    with _connection(account_id, business_id, bindings) as (conn, clean):
        with conn.transaction():
            conn.execute("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ READ ONLY")
            return {"currency": "USD", **ledger.financial_reports(conn), "references": _references(conn, business_id, clean),
                    "payment_mode": "recorded_only", "limitations": [
                        "Bills, journal postings and payments are explicit owner actions; no bank transfer is executed.",
                        "Restricted capacity is income and net equity credits less expenses and designated capital net debits; permanent releases are blocked.",
                        "Tax, foreign exchange, payroll calculation, refunds and automatic inventory costing are not implemented."]}


MODELS = {"create_account": Account, "update_account": AccountUpdate, "create_fund": Fund, "update_fund": FundUpdate,
    "post_journal_entry": Journal, "reverse_entry": Reversal, "create_invoice": Document, "create_bill": Document,
    "post_invoice": DocumentPost, "post_bill": DocumentPost, "record_invoice_payment": Payment, "record_bill_payment": Payment,
    "void_invoice": Void, "void_bill": Void, "grant_receipt": Grant, "release_fund": Release}


def invoke(account_id, business_id, bindings, operation, payload):
    if operation not in MODELS:
        raise AccountingError("Unknown Accounting operation")
    try:
        values = MODELS[operation].model_validate(payload).model_dump(mode="json")
    except ValidationError as error:
        raise AccountingError("; ".join(".".join(map(str, item["loc"])) + ": " + item["msg"]
                                        for item in error.errors(include_input=False, include_url=False)[:5])) from None
    key = values.pop("idempotency_key")
    with _connection(account_id, business_id, bindings, lock=True) as (conn, clean):
        def work(tx):
            if operation == "create_account":
                capital = values["is_capital"]
                if values["account_type"] == "asset" and capital is None:
                    raise AccountingError("Asset accounts require an explicit is_capital true/false designation")
                if capital and values["account_type"] != "asset":
                    raise AccountingError("Only an asset account can be designated capital")
                return dict(tx.execute("""INSERT INTO accounting.accounts(code,name,account_type,role,is_capital)
                    VALUES(%s,%s,%s,%s,%s) RETURNING *""", (values["code"], values["name"], values["account_type"], values["role"], bool(capital))).fetchone())
            if operation in ("update_account", "update_fund"):
                is_account = operation == "update_account"
                table = "accounts" if is_account else "funds"
                identifier = values["account_id" if is_account else "fund_id"]
                updates = {name: value for name, value in values.items() if name not in ("account_id", "fund_id") and value is not None}
                if not updates:
                    raise AccountingError("Choose at least one field to update")
                ledger.one(tx, f"SELECT id FROM accounting.{table} WHERE id=%s", (identifier,))
                assignments = ",".join(name + "=%s" for name in updates)
                return dict(tx.execute(f"UPDATE accounting.{table} SET {assignments} WHERE id=%s RETURNING *", [*updates.values(), identifier]).fetchone())
            if operation == "create_fund":
                return dict(tx.execute("INSERT INTO accounting.funds(code,name,restriction_type,purpose) VALUES(%s,%s,%s,%s) RETURNING *",
                    (values["code"], values["name"], values["restriction_type"], values["purpose"])).fetchone())
            if operation == "post_journal_entry":
                return ledger.post(tx, actor_id=account_id, request_key=key, **values)
            if operation == "reverse_entry":
                return ledger.reverse(tx, actor_id=account_id, request_key=key, **values)
            if operation in ("create_invoice", "create_bill"):
                kind = "invoice" if operation == "create_invoice" else "bill"
                mapping = clean["customer" if kind == "invoice" else "supplier"]
                party = _party(tx, business_id, mapping, values["party_record_id"])
                return ledger.create_document(tx, kind=kind, actor_id=account_id, request_key=key,
                    party_entity_key=mapping["entity"], party_snapshot=party, **values)
            if operation in ("post_invoice", "post_bill"):
                kind = "invoice" if operation == "post_invoice" else "bill"
                doc = ledger.one(tx, "SELECT party_record_id FROM accounting.documents WHERE id=%s AND kind=%s",
                                 (values["document_id"], kind), "Document")
                party = _party(tx, business_id, clean["customer" if kind == "invoice" else "supplier"], doc["party_record_id"])
                return ledger.post_document(tx, kind=kind, actor_id=account_id, request_key=key, party_snapshot=party, **values)
            if operation in ("record_invoice_payment", "record_bill_payment"):
                return ledger.pay_document(tx, kind="invoice" if operation == "record_invoice_payment" else "bill",
                    actor_id=account_id, request_key=key, **values)
            if operation in ("void_invoice", "void_bill"):
                return ledger.void_document(tx, kind="invoice" if operation == "void_invoice" else "bill",
                    actor_id=account_id, request_key=key, **values)
            if operation == "grant_receipt":
                cash = ledger.one(tx, "SELECT * FROM accounting.accounts WHERE id=%s", (values["cash_account_id"],), "Cash account")
                revenue = ledger.one(tx, "SELECT * FROM accounting.accounts WHERE id=%s", (values["revenue_account_id"],), "Revenue account")
                if cash["role"] != "cash" or revenue["account_type"] != "income":
                    raise AccountingError("Grant receipt needs a cash account and an income account")
                return ledger.post(tx, actor_id=account_id, request_key=key, entry_date=values["entry_date"], memo=values["memo"],
                    source="grant_receipt", source_ref=key, lines=[
                        {"account_id": cash["id"], "fund_id": values["fund_id"], "debit_minor": values["amount_minor"]},
                        {"account_id": revenue["id"], "fund_id": values["fund_id"], "credit_minor": values["amount_minor"]}])
            if operation == "release_fund":
                fund = ledger.one(tx, "SELECT * FROM accounting.funds WHERE id=%s", (values["fund_id"],), "Fund")
                if fund["restriction_type"] != "temporarily_restricted" or not fund["is_active"]:
                    raise Conflict("Only an active temporary restriction can be released; permanent funds remain restricted")
                general = ledger.default_fund(tx)["id"]
                restricted = ledger.account_role(tx, "donor_restricted")["id"]
                unrestricted = ledger.account_role(tx, "unrestricted_net_assets")["id"]
                clearing = ledger.account_role(tx, "interfund_clearing")["id"]
                amount = values["amount_minor"]
                return ledger.post(tx, actor_id=account_id, request_key=key, entry_date=values["entry_date"], memo=values["memo"],
                    source="fund_release", source_ref=key, lines=[
                        {"account_id": restricted, "fund_id": fund["id"], "debit_minor": amount},
                        {"account_id": clearing, "fund_id": fund["id"], "credit_minor": amount},
                        {"account_id": clearing, "fund_id": general, "debit_minor": amount},
                        {"account_id": unrestricted, "fund_id": general, "credit_minor": amount}])
            raise AccountingError("Unsupported Accounting operation")
        return db.run_command(conn, operation, key, {"business_id": business_id, **values}, work)
