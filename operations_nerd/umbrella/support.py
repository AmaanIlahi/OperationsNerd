"""Read-only workflow context for the schema assistant, without business records."""
import json

from agentic import store
from db import db as d
from umbrella import businesses, registry


GUIDES = {
    "logistics": [
        "Create customers, suppliers, items, locations and draft customer orders in Records; sync shared records in Logistics.",
        "Submit a reviewed customer order to freeze its header and lines and create demand. Reserve available stock before buying a shortage.",
        "Record supplier quotes, compare feasible options, build a draft purchase order, explicitly allocate costs to demand and approve it within budgets and deadlines.",
        "Receive physical deliveries against purchase lines with explicit reservation allocations. Each delivery is atomic and supports partial receipts.",
        "Create draft shipments from reservations, then explicitly dispatch with carrier, label and tracking references. Record transit, exception and delivered events; delivered is terminal.",
        "Import customer, draft order or draft shipment CSV through Import preview and explicit commit. Reports show current stock, outstanding purchases, shipment status and quantity-weighted supplier timing.",
        "Purchases, receipts and shipments do not create or pay Accounting documents. Carrier labels and tracking are manually recorded; there is no live carrier integration.",
    ],
    "hr": [
        "Create Employee and Location master records in Records, then sync them in HR. Hire, update employment, terminate or rehire through explicit HR actions.",
        "Review leave allowances and annual accrual before requesting leave. Leave uses exact half days; requests hold available allowance until approved, rejected or cancelled.",
        "Employment history and leave events remain auditable; changing a master record label does not rewrite past events.",
    ],
    "accounting": [
        "Use shared Customer and Supplier master records. Review the chart of accounts and enter explicit balanced opening journals where appropriate.",
        "In Accounting Bills, use Create draft bill, enter exact dollar amounts and dates, review it, then use Post bill. In Invoices, use Create draft invoice then Post invoice.",
        "After the bank or cash movement has actually completed, use Record supplier payment against an open posted bill, or Record customer payment against an open posted invoice. The Payment amount UI input expects dollars: 20.00 means twenty dollars; only the internal API amount_minor uses 2000 cents.",
        "Recording a payment is one explicit action that immediately records its journal. There is no draft-payment or separate post-payment step. It does not execute a bank transfer. Partial payments are supported; subsequent payments cover the remaining balance.",
        "Review trial balance, balance sheet, income statement, receivables/payables aging and customer payment timing in Accounting.",
        "Restricted grants, releases and spending must balance by fund and respect restricted capacity at every later date. Capital spending consumes capacity; permanent funds cannot be released.",
    ],
}


def context(account_id, business_id):
    if not businesses.configured():
        return None
    with d.get_conn() as conn:
        if store.get_business_with_spec(conn, account_id, business_id) is None:
            raise ValueError("Business not found")
        rows = conn.execute("SELECT nerd_id,status,bindings_json FROM nerd_installations ORDER BY nerd_id").fetchall()
    modules = []
    for row in rows:
        manifest = registry.manifest(row["nerd_id"])
        module = {"id": row["nerd_id"], "name": getattr(manifest, "NAME", row["nerd_id"]),
                  "status": row["status"], "mappings": json.loads(row["bindings_json"])}
        if row["status"] == "enabled":
            module["workflow"] = GUIDES.get(row["nerd_id"], [])
        modules.append(module)
    return {"business_id": business_id, "modules": modules,
            "capabilities": "Explain current schema and enabled workflows; propose schema changes for owner review only. No access to operational record values, no operational commands, no SQL execution."}
