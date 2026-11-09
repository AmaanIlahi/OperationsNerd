-- Accounting module, migration 0003: a document and the journal entry it points at
-- stay one thing.
--
-- A receipt dated before the invoice's own entry (or a payment before the bill's)
-- credits the control account on a day the document is not in the aging, so the
-- aging total stops equalling that account. A void dated before the document's
-- entry does the same in the other direction. Nothing here rewrites a row.
--
-- A payment entry is not reversed. amount_paid never falls and there is no refund
-- in v1, so a reversing entry would put the money back in the journal while the
-- document still said it had been paid. Voiding an invoice or a bill still
-- reverses that document's own entry; this only refuses reversals of payments.
--
-- Two documents can't share one journal entry. Posting the same entry against
-- two invoices of the same total would satisfy the "debit receivables by the
-- total" check twice and the aging would count the money twice.

CREATE TRIGGER acct_invoice_payments_on_or_after_entry BEFORE INSERT ON acct_invoice_payments
WHEN NEW.payment_date < (SELECT e.entry_date FROM acct_invoices d
                          JOIN acct_journal_entries e ON e.id = d.entry_id
                          WHERE d.id = NEW.invoice_id)
BEGIN SELECT RAISE(ABORT, 'a receipt is dated on or after the invoice''s entry'); END;

CREATE TRIGGER acct_bill_payments_on_or_after_entry BEFORE INSERT ON acct_bill_payments
WHEN NEW.payment_date < (SELECT e.entry_date FROM acct_bills d
                         JOIN acct_journal_entries e ON e.id = d.entry_id
                         WHERE d.id = NEW.bill_id)
BEGIN SELECT RAISE(ABORT, 'a payment is dated on or after the bill''s entry'); END;

CREATE TRIGGER acct_invoices_void_on_or_after_entry BEFORE UPDATE OF void_entry_id ON acct_invoices
WHEN NEW.void_entry_id IS NOT NULL
 AND (SELECT entry_date FROM acct_journal_entries WHERE id = NEW.void_entry_id)
     < (SELECT entry_date FROM acct_journal_entries WHERE id = NEW.entry_id)
BEGIN SELECT RAISE(ABORT, 'a void is dated on or after the invoice''s entry'); END;

CREATE TRIGGER acct_bills_void_on_or_after_entry BEFORE UPDATE OF void_entry_id ON acct_bills
WHEN NEW.void_entry_id IS NOT NULL
 AND (SELECT entry_date FROM acct_journal_entries WHERE id = NEW.void_entry_id)
     < (SELECT entry_date FROM acct_journal_entries WHERE id = NEW.entry_id)
BEGIN SELECT RAISE(ABORT, 'a void is dated on or after the bill''s entry'); END;

CREATE TRIGGER acct_journal_entries_payment_not_reversed BEFORE INSERT ON acct_journal_entries
WHEN NEW.reverses_id IS NOT NULL
 AND (SELECT source FROM acct_journal_entries WHERE id = NEW.reverses_id)
     IN ('invoice_payment', 'bill_payment')
BEGIN SELECT RAISE(ABORT, 'a payment entry is not reversed'); END;

-- One posting per document. Several drafts have a NULL entry_id, which this leaves alone.
CREATE UNIQUE INDEX acct_invoices_entry ON acct_invoices (entry_id) WHERE entry_id IS NOT NULL;
CREATE UNIQUE INDEX acct_bills_entry ON acct_bills (entry_id) WHERE entry_id IS NOT NULL;
