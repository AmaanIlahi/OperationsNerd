# Accounting PostgreSQL adaptation

Reference: Daksh Agarwal's accounting PR 28, commit
`3bd40ccf56b0a827a5f53e84a0a3f248e07037f6` (November 9, 2026).
That source is a standalone SQLite ledger, document/payment system and fund
accounting implementation. Its migrations and HTTP API were not copied into
the PostgreSQL runtime. No upstream PR was merged.

Reused concepts: immutable balanced journal, reversals, draft/posted/void
documents, exact minor-unit money, derived AR/AP balances, fund-level balance,
explicit payments and idempotent commands. Parties remain authoritative core
records; document snapshots preserve financial history without another party
master. Owner IDs come from the authenticated host, never request bodies.

Corrections required by the supplied project context: net assets are assets
minus liabilities, once; designated capital-asset net debits consume restricted
capacity; releases reduce capacity and permanent releases are blocked; all
later posted dates are checked when an entry is backdated. These are enforced
by PostgreSQL deferred constraint triggers, not only Python checks.

Payment timeliness uses each recorded invoice-payment portion's actual date
against the document due date. Reports expose amount/count denominators, date
coverage and overdue unpaid balances separately; future-dated payments are
excluded from today's paid denominator. These are recorded transactions, not
verified bank receipts. Trial-balance rows expose their net debit/credit sides
in addition to gross ledger movement totals.

The initial chart is a visible prototype chart, not a jurisdiction-specific
accounting policy. One explicit currency (USD) is supported. No tax engine,
foreign exchange, payroll calculation, bank transfer, bank reconciliation,
refund/credit-note workflow or automatic Logistics posting is claimed. Bills
and payments are explicit owner actions. The module does not call other Nerds.
