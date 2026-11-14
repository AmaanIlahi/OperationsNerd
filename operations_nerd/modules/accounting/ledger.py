"""Exact ledger/document rules for a single USD business database."""
from datetime import date, datetime, timezone

from psycopg.types.json import Jsonb

from .db import json_value
from .errors import AccountingError, Conflict, NotFound

MAX_MINOR = 10_000_000_000_000


def one(conn, statement, parameters=(), label="Accounting record"):
    row = conn.execute(statement, parameters).fetchone()
    if row is None:
        raise NotFound(label + " was not found")
    return row


def day(value):
    if type(value) is date:
        return value
    try:
        result = date.fromisoformat(value)
        if result.isoformat() != value:
            raise ValueError()
        return result
    except (ValueError, TypeError):
        raise AccountingError("Use a real date in YYYY-MM-DD format") from None


def amount(value, *, zero=False):
    if type(value) is not int or not (0 if zero else 1) <= value <= MAX_MINOR:
        raise AccountingError("Money must be exact integer minor units within the supported bound")
    return value


def default_fund(conn):
    return one(conn, "SELECT * FROM accounting.funds WHERE is_default", label="General fund")


def account_role(conn, role):
    return one(conn, "SELECT * FROM accounting.accounts WHERE role=%s AND is_active ORDER BY id LIMIT 1", (role,), "Required account role")


def entry(conn, identifier):
    row = dict(one(conn, "SELECT * FROM accounting.journal_entries WHERE id=%s", (identifier,), "Journal entry"))
    row.pop("created_txid", None)
    row["lines"] = [dict(line) for line in conn.execute("""SELECT l.line_no,l.account_id,a.code,a.name,
        l.fund_id,f.code AS fund_code,l.debit_minor,l.credit_minor FROM accounting.journal_lines l
        JOIN accounting.accounts a ON a.id=l.account_id JOIN accounting.funds f ON f.id=l.fund_id
        WHERE l.entry_id=%s ORDER BY l.line_no""", (identifier,)).fetchall()]
    return json_value(row)


def post(conn, *, actor_id, request_key, entry_date, memo, lines, source="manual", source_ref=None,
         reverses_id=None, currency="USD"):
    if currency != "USD":
        raise AccountingError("This accounting version supports explicit USD only")
    if not isinstance(lines, list) or not 2 <= len(lines) <= 500:
        raise AccountingError("A journal entry needs 2 to 500 lines")
    general = default_fund(conn)["id"]
    normalized, debit, credit, funds = [], 0, 0, {}
    for line in lines:
        d, c = amount(line.get("debit_minor", 0), zero=True), amount(line.get("credit_minor", 0), zero=True)
        if (d > 0) == (c > 0):
            raise AccountingError("Each journal line is one debit or one credit")
        fund = line.get("fund_id") or general
        normalized.append({"account_id": line["account_id"], "fund_id": fund, "debit_minor": d, "credit_minor": c})
        debit += d
        credit += c
        funds[fund] = funds.get(fund, 0) + d - c
    if debit != credit or debit == 0 or debit > MAX_MINOR or any(funds.values()):
        raise AccountingError("Debits and credits must balance overall and within every fund")
    identifier = conn.execute("""INSERT INTO accounting.journal_entries
        (entry_date,memo,source,source_ref,currency,total_minor,reverses_id,actor_id,request_key)
        VALUES(%s,%s,%s,%s,%s,%s,%s,%s,%s) RETURNING id""",
        (day(entry_date), memo, source, source_ref, currency, debit, reverses_id, actor_id, request_key)).fetchone()["id"]
    for number, line in enumerate(normalized, 1):
        conn.execute("""INSERT INTO accounting.journal_lines(entry_id,line_no,account_id,fund_id,debit_minor,credit_minor)
            VALUES(%s,%s,%s,%s,%s,%s)""", (identifier, number, line["account_id"], line["fund_id"], line["debit_minor"], line["credit_minor"]))
    return entry(conn, identifier)


def reverse(conn, *, entry_id, entry_date, reason, actor_id, request_key, from_document=False):
    original = entry(conn, entry_id)
    if original["source"] in ("invoice", "bill", "invoice_payment", "bill_payment") and not from_document:
        raise Conflict("Document and payment entries need the corresponding void or refund workflow")
    if conn.execute("SELECT 1 FROM accounting.journal_entries WHERE reverses_id=%s", (entry_id,)).fetchone():
        raise Conflict("This entry already has a reversal")
    if day(entry_date) < day(original["entry_date"]):
        raise AccountingError("A reversal cannot predate its original entry")
    lines = [{"account_id": line["account_id"], "fund_id": line["fund_id"],
              "debit_minor": line["credit_minor"], "credit_minor": line["debit_minor"]} for line in original["lines"]]
    return post(conn, actor_id=actor_id, request_key=request_key, entry_date=entry_date, memo=reason,
                lines=lines, source="reversal", source_ref=str(entry_id), reverses_id=entry_id)


def document(conn, identifier, *, as_of=None):
    row = dict(one(conn, "SELECT * FROM accounting.documents WHERE id=%s", (identifier,), "Invoice or bill"))
    row.pop("created_txid", None)
    cutoff = day(as_of) if as_of else datetime.now(timezone.utc).date()
    row["lines"] = [dict(line) for line in conn.execute("""SELECT l.*,a.code AS account_code,a.name AS account_name
        FROM accounting.document_lines l JOIN accounting.accounts a ON a.id=l.account_id
        WHERE l.document_id=%s ORDER BY l.line_no""", (identifier,)).fetchall()]
    row["payments"] = [dict(payment) for payment in conn.execute("SELECT * FROM accounting.payments WHERE document_id=%s ORDER BY payment_date,id", (identifier,)).fetchall()]
    row["paid_minor"] = sum(payment["amount_minor"] for payment in row["payments"] if payment["payment_date"] <= cutoff)
    # A later void must not erase the document from an earlier dated report.
    if row["status"] == "void" and row["posted_entry_id"] is not None:
        void_date = one(conn, "SELECT entry_date FROM accounting.journal_entries WHERE id=%s", (row["void_entry_id"],))["entry_date"]
        if cutoff < void_date:
            row["status"] = "posted"
    row["remaining_minor"] = row["total_minor"] - row["paid_minor"] if row["status"] == "posted" else 0
    if row["status"] == "posted" and row["remaining_minor"] == 0:
        row["status"] = "paid"
    row["party_name"] = row["party_snapshot"].get("name")
    return json_value(row)


def create_document(conn, *, kind, actor_id, request_key, party_entity_key, party_snapshot,
                    number, party_record_id, issue_date, due_date, currency, lines, fund_id=None):
    if currency != "USD":
        raise AccountingError("This accounting version supports explicit USD only")
    issue, due = day(issue_date), day(due_date)
    if due < issue:
        raise AccountingError("A document due date cannot precede its issue date")
    if not isinstance(lines, list) or not 1 <= len(lines) <= 500:
        raise AccountingError("A document needs 1 to 500 lines")
    total = 0
    for line in lines:
        quantity = line["quantity"]
        if type(quantity) is not int or not 1 <= quantity <= 1_000_000:
            raise AccountingError("Document quantities must be positive whole units")
        total += quantity * amount(line["unit_price_minor"], zero=True)
    amount(total)
    fund = fund_id or default_fund(conn)["id"]
    if not one(conn, "SELECT is_active FROM accounting.funds WHERE id=%s", (fund,), "Fund")["is_active"]:
        raise AccountingError("Use an active fund")
    identifier = conn.execute("""INSERT INTO accounting.documents(kind,number,party_entity_key,party_record_id,
        party_snapshot,issue_date,due_date,currency,fund_id,total_minor,actor_id,request_key)
        VALUES(%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s) RETURNING id""",
        (kind, number, party_entity_key, party_record_id, Jsonb(party_snapshot), issue, due, currency, fund, total, actor_id, request_key)).fetchone()["id"]
    for number, line in enumerate(lines, 1):
        conn.execute("""INSERT INTO accounting.document_lines(document_id,line_no,description,quantity,unit_price_minor,amount_minor,account_id)
            VALUES(%s,%s,%s,%s,%s,%s,%s)""", (identifier, number, line["description"], line["quantity"], line["unit_price_minor"],
            line["quantity"] * line["unit_price_minor"], line["account_id"]))
    return document(conn, identifier)


def post_document(conn, *, kind, document_id, actor_id, request_key, party_snapshot):
    row = one(conn, "SELECT * FROM accounting.documents WHERE id=%s AND kind=%s FOR UPDATE", (document_id, kind), "Document")
    if row["status"] != "draft":
        raise Conflict("Only a draft document can be posted")
    control = account_role(conn, "accounts_receivable" if kind == "invoice" else "accounts_payable")
    lines = [dict(line) for line in conn.execute("SELECT * FROM accounting.document_lines WHERE document_id=%s ORDER BY line_no", (document_id,)).fetchall()]
    debit_control = kind == "invoice"
    journal = [{"account_id": control["id"], "fund_id": row["fund_id"],
                "debit_minor": row["total_minor"] if debit_control else 0,
                "credit_minor": 0 if debit_control else row["total_minor"]}]
    journal += [{"account_id": line["account_id"], "fund_id": row["fund_id"],
                 "debit_minor": 0 if debit_control else line["amount_minor"],
                 "credit_minor": line["amount_minor"] if debit_control else 0} for line in lines if line["amount_minor"]]
    posted = post(conn, actor_id=actor_id, request_key=request_key, entry_date=row["issue_date"],
                  memo=f"{kind.title()} {row['number']}", lines=journal, source=kind, source_ref=str(document_id))
    conn.execute("UPDATE accounting.documents SET status='posted',posted_entry_id=%s,party_snapshot=%s WHERE id=%s",
                 (posted["id"], Jsonb(party_snapshot), document_id))
    return document(conn, document_id)


def pay_document(conn, *, kind, document_id, amount_minor, cash_account_id, payment_date, reference, actor_id, request_key):
    row = one(conn, "SELECT * FROM accounting.documents WHERE id=%s AND kind=%s FOR UPDATE", (document_id, kind), "Document")
    paid = conn.execute("SELECT COALESCE(sum(amount_minor),0) AS paid FROM accounting.payments WHERE document_id=%s", (document_id,)).fetchone()["paid"]
    amount(amount_minor)
    if row["status"] != "posted" or amount_minor > row["total_minor"] - paid:
        raise Conflict("Payment requires a posted document and cannot exceed its remaining amount")
    if day(payment_date) < row["issue_date"]:
        raise AccountingError("Payment cannot predate the document")
    cash = one(conn, "SELECT * FROM accounting.accounts WHERE id=%s", (cash_account_id,), "Cash account")
    if cash["role"] != "cash" or not cash["is_active"]:
        raise AccountingError("Choose an active cash or bank account")
    control = account_role(conn, "accounts_receivable" if kind == "invoice" else "accounts_payable")
    payment_id = conn.execute("SELECT nextval(pg_get_serial_sequence('accounting.payments','id')) AS id").fetchone()["id"]
    receive = kind == "invoice"
    lines = [{"account_id": cash_account_id, "fund_id": row["fund_id"],
              "debit_minor": amount_minor if receive else 0, "credit_minor": 0 if receive else amount_minor},
             {"account_id": control["id"], "fund_id": row["fund_id"],
              "debit_minor": 0 if receive else amount_minor, "credit_minor": amount_minor if receive else 0}]
    posted = post(conn, actor_id=actor_id, request_key=request_key, entry_date=payment_date,
                  memo=f"Payment for {kind} {row['number']}: {reference}", lines=lines,
                  source=kind + "_payment", source_ref=str(payment_id))
    conn.execute("""INSERT INTO accounting.payments(id,document_id,amount_minor,cash_account_id,payment_date,reference,entry_id,actor_id,request_key)
        VALUES(%s,%s,%s,%s,%s,%s,%s,%s,%s)""", (payment_id, document_id, amount_minor, cash_account_id,
        day(payment_date), reference, posted["id"], actor_id, request_key))
    return document(conn, document_id)


def void_document(conn, *, kind, document_id, entry_date, reason, actor_id, request_key):
    row = one(conn, "SELECT * FROM accounting.documents WHERE id=%s AND kind=%s FOR UPDATE", (document_id, kind), "Document")
    if row["status"] == "void":
        raise Conflict("Document is already void")
    if conn.execute("SELECT 1 FROM accounting.payments WHERE document_id=%s", (document_id,)).fetchone():
        raise Conflict("A paid document needs a refund or credit workflow; voiding is unavailable")
    reversal = None
    if row["posted_entry_id"] is not None:
        reversal = reverse(conn, entry_id=row["posted_entry_id"], entry_date=entry_date, reason=reason,
                           actor_id=actor_id, request_key=request_key, from_document=True)["id"]
    conn.execute("UPDATE accounting.documents SET status='void',void_entry_id=%s WHERE id=%s", (reversal, document_id))
    return document(conn, document_id)


def accounts(conn, cutoff):
    return [dict(row) for row in conn.execute("""SELECT a.*,COALESCE(t.debits,0)::bigint AS debit_minor,
        COALESCE(t.credits,0)::bigint AS credit_minor,
        (CASE WHEN a.account_type IN('asset','expense') THEN COALESCE(t.debits,0)-COALESCE(t.credits,0)
         ELSE COALESCE(t.credits,0)-COALESCE(t.debits,0) END)::bigint AS balance_minor
        FROM accounting.accounts a LEFT JOIN(SELECT l.account_id,sum(l.debit_minor) AS debits,sum(l.credit_minor) AS credits
        FROM accounting.journal_lines l JOIN accounting.journal_entries e ON e.id=l.entry_id WHERE e.entry_date<=%s GROUP BY l.account_id)t
        ON t.account_id=a.id ORDER BY a.code""", (cutoff,)).fetchall()]


def funds(conn, cutoff):
    result = []
    for fund in conn.execute("SELECT * FROM accounting.funds ORDER BY code").fetchall():
        values = conn.execute("""SELECT COALESCE(sum(CASE WHEN a.account_type='asset' THEN l.debit_minor-l.credit_minor ELSE 0 END),0)::bigint AS assets,
            COALESCE(sum(CASE WHEN a.account_type='liability' THEN l.credit_minor-l.debit_minor ELSE 0 END),0)::bigint AS liabilities,
            COALESCE(sum(CASE WHEN a.account_type IN('income','equity') OR a.account_type='expense' OR a.is_capital
                              THEN l.credit_minor-l.debit_minor ELSE 0 END),0)::bigint AS capacity
            FROM accounting.journal_lines l JOIN accounting.journal_entries e ON e.id=l.entry_id
            JOIN accounting.accounts a ON a.id=l.account_id WHERE l.fund_id=%s AND e.entry_date<=%s""", (fund["id"], cutoff)).fetchone()
        result.append({**dict(fund), "assets_minor": values["assets"], "liabilities_minor": values["liabilities"],
                       "net_assets_minor": values["assets"] - values["liabilities"],
                       "available_minor": None if fund["restriction_type"] == "unrestricted" else values["capacity"]})
    return result


def payment_timeliness(invoices, cutoff):
    """Amount-weighted reliability of recorded invoice payment portions.

    Overdue unpaid balances remain separate from the paid denominator. Required
    dates are also exposed as coverage, rather than silently assuming that
    undated payments arrived on time if a future migration imports legacy data.
    """
    totals = dict(invoice_count=0, invoice_total_minor=0, paid_minor=0, eligible_paid_minor=0,
        on_time_paid_minor=0, late_paid_minor=0, unknown_date_paid_minor=0,
        payment_portions_count=0, dated_payment_portions_count=0, unknown_date_portions_count=0,
        unpaid_minor=0, overdue_unpaid_minor=0)
    result = {"as_of": cutoff, "currency": "USD", "basis": "invoice_payment_portions",
              **totals, "rows": [], "customers": [], "overdue_unpaid": [],
              "quality": {"due_dates_required": True, "payment_dates_required": True,
                  "missing_due_date_invoice_count": 0, "missing_payment_date_portions_count": 0}}
    customers = {}
    for invoice in invoices:
        if invoice["status"] not in ("posted", "paid") or day(invoice["issue_date"]) > cutoff:
            continue
        party = invoice["party_record_id"]
        customer = customers.setdefault(party, {"party_record_id": party,
            "party_name": invoice["party_name"], **totals})
        due = day(invoice["due_date"]) if invoice.get("due_date") else None
        if due is None:
            result["quality"]["missing_due_date_invoice_count"] += 1
        paid = 0
        for payment in invoice["payments"]:
            actual = day(payment["payment_date"]) if payment.get("payment_date") else None
            if actual is not None and actual > cutoff:
                continue
            value = payment["amount_minor"]
            known = actual is not None and due is not None
            on_time = actual <= due if known else None
            days_late = max(0, (actual - due).days) if known else None
            paid += value
            result["rows"].append({"payment_id": payment["id"], "document_id": invoice["id"],
                "number": invoice["number"], "party_record_id": party, "party_name": invoice["party_name"],
                "due_date": due, "payment_date": actual, "amount_minor": value,
                "days_late": days_late, "on_time": on_time})
            if actual is None:
                result["quality"]["missing_payment_date_portions_count"] += 1
            for summary in (result, customer):
                summary["paid_minor"] += value
                summary["payment_portions_count"] += 1
                summary["eligible_paid_minor"] += value if known else 0
                summary["on_time_paid_minor"] += value if on_time is True else 0
                summary["late_paid_minor"] += value if on_time is False else 0
                summary["unknown_date_paid_minor"] += 0 if known else value
                summary["dated_payment_portions_count"] += int(known)
                summary["unknown_date_portions_count"] += int(not known)
        remaining = invoice["total_minor"] - paid
        overdue = remaining > 0 and due is not None and due < cutoff
        for summary in (result, customer):
            summary["invoice_count"] += 1
            summary["invoice_total_minor"] += invoice["total_minor"]
            summary["unpaid_minor"] += remaining
            summary["overdue_unpaid_minor"] += remaining if overdue else 0
        if overdue:
            result["overdue_unpaid"].append({"document_id": invoice["id"], "number": invoice["number"],
                "party_record_id": party, "party_name": invoice["party_name"], "due_date": due,
                "days_overdue": (cutoff - due).days, "remaining_minor": remaining})
    result["customers"] = list(customers.values())
    for summary in [result, *result["customers"]]:
        denominator = summary["eligible_paid_minor"]
        summary["on_time_basis_points"] = summary["on_time_paid_minor"] * 10000 // denominator if denominator else None
    return json_value(result)


def financial_reports(conn, as_of=None):
    cutoff = day(as_of) if as_of else datetime.now(timezone.utc).date()
    chart = accounts(conn, cutoff)
    totals = {kind: sum(row["balance_minor"] for row in chart if row["account_type"] == kind)
              for kind in ("asset", "liability", "equity", "income", "expense")}
    for row in chart:
        row["trial_debit_minor"] = max(0, row["debit_minor"] - row["credit_minor"])
        row["trial_credit_minor"] = max(0, row["credit_minor"] - row["debit_minor"])
    trial = {"accounts": chart, "total_debits_minor": sum(row["trial_debit_minor"] for row in chart),
             "total_credits_minor": sum(row["trial_credit_minor"] for row in chart)}
    reports = {"as_of": cutoff, "accounts": chart, "funds": funds(conn, cutoff), "trial_balance": trial,
        "balance_sheet": {"assets_minor": totals["asset"], "liabilities_minor": totals["liability"],
                          "net_assets_minor": totals["asset"] - totals["liability"],
                          "equity_and_current_profit_minor": totals["equity"] + totals["income"] - totals["expense"]},
        "income_statement": {"income_minor": totals["income"], "expense_minor": totals["expense"],
                             "profit_minor": totals["income"] - totals["expense"]}}
    documents = {"invoice": [], "bill": []}
    for row in conn.execute("SELECT id,kind FROM accounting.documents ORDER BY id").fetchall():
        documents[row["kind"]].append(document(conn, row["id"], as_of=cutoff))
    reports["invoices"], reports["bills"] = documents["invoice"], documents["bill"]
    reports["payment_timeliness"] = payment_timeliness(documents["invoice"], cutoff)
    for kind, name, control_role in (("invoice", "ar_aging", "accounts_receivable"), ("bill", "ap_aging", "accounts_payable")):
        rows = []
        for doc in documents[kind]:
            if doc["status"] not in ("posted", "paid") or day(doc["issue_date"]) > cutoff or doc["remaining_minor"] <= 0:
                continue
            late = max(0, (cutoff - day(doc["due_date"])).days)
            bucket = "current" if late == 0 else "1_30" if late <= 30 else "31_60" if late <= 60 else "61_90" if late <= 90 else "over_90"
            rows.append({"document_id": doc["id"], "number": doc["number"], "party_record_id": doc["party_record_id"],
                         "party_name": doc["party_name"], "due_date": doc["due_date"], "days_overdue": late,
                         "bucket": bucket, "remaining_minor": doc["remaining_minor"]})
        total = sum(row["remaining_minor"] for row in rows)
        control = sum(row["balance_minor"] for row in chart if row["role"] == control_role)
        reports[name] = {"rows": rows, "total_minor": total, "control_balance_minor": control, "difference_minor": control - total}
    reports["journal"] = [entry(conn, row["id"]) for row in conn.execute("SELECT id FROM accounting.journal_entries ORDER BY entry_date,id").fetchall()]
    return json_value(reports)
