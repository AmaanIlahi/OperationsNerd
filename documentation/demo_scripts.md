# Text-first demonstration scripts

Use fictional data in **Playful Planet Toys** and keep **Neighborhood Gym** as
the second business. Each script is intended for a separate video of no more than three
minutes. These are source scripts for narration/translation; a reviewed translated
voice track and published educational videos are separate deliverables.

## Overview — target 90 seconds

“OperationsNerd is an educational prototype with major gaps in security and
business-specific functionality. It combines a configurable CRM with business workspaces. I sign in
once and choose my toy business. Records contains the shared customers, suppliers,
employees, items, locations and orders. Each business owns a separate local
PostgreSQL database. This gym has different records and independent schema keys.

“The business owner reviews which CRM fields each workspace uses before enabling
it. Logistics handles purchasing and physical stock. HR handles employment and
leave. Accounting records explicit bills, invoices, journals and completed
payments. A stock receipt never silently creates or pays a bill.

“The assistant understands this business's schema and enabled workflows. A schema
change remains a proposal until the owner approves it. The selected Baseten model
receives the schema/chat context; business databases remain on this machine.”

Show the business picker, Records and the three enabled tabs. Switch to the gym
and back. Finish on the pending schema proposal review.

## Login — target 60 seconds

“I sign up with the configured local invitation, sign in, and select my business.
The same session opens each enabled workspace. An employee master record is a
business record, separate from a login account. This prototype's owner operates
the workflows; employee self-service and production role administration are not
part of this demo. After signing out, the protected API and workspaces require
sign-in again.”

Show signup/login, the business picker and logout using a fictional account.

## CRM and assistant — target 120 seconds

“A template starts the business with useful record types. The core generates
permanent keys; changing a label preserves identity. I create a customer and an
employee here, once. Other workspaces link to these records.

“I ask the assistant how receiving stock and paying a supplier fit together. It
explains the enabled workflow without executing it. I then ask for an optional
Favorite toy field on Customer. I inspect the proposed change and approve it.
History records the new schema version. An incompatible change to a field used
by an enabled workspace is refused until its dependency is resolved.”

Show schema before/after approval and history. Use fictional messages only.

## Logistics — target 150 seconds

“I enter a ten-unit customer order with a deadline and a 100-dollar purchasing
budget. Dollar inputs are stored as integer cents. I submit the order explicitly;
its operational header and lines are now frozen.

“I compare supplier quotes, allocate a purchase line to this demand and approve
the purchase within its budget. Approval creates no physical stock. I receive
four units, then six. Each delivery records its receipt and reservations together;
a bad later line rolls back the whole delivery.

“I create and dispatch two shipments from the reservations, recording the manual
carrier, label and tracking references. Delivered is the final tracking state.
Tracking events do not alter stock. Reports show outstanding purchases, overdue
orders, shipment states and the dated quantities behind supplier timeliness.
Import previews customer, order and draft-shipment CSV before committing it.”

Show stock before approval/after receipt/after dispatch. Distinguish draft
shipment import from explicit physical dispatch. This demo does not buy a label
or contact a carrier or supplier.

## Inventory — target 120 seconds

“The stock view separates physical on-hand units, reservations and available
units by location. I record an explicit opening count, then reserve some units
for customer demand. The reservation reduces availability without changing
physical on-hand stock. Releasing it restores availability.

“A receipt adds physical stock, and a dispatch consumes the reserved units.
Movements remain in an immutable ledger. Repeating the same request does not
duplicate a receipt or shipment. Partial receipts and split shipments preserve
the relationship to the original customer demand. A tracking update changes
shipment status without adding or removing stock.”

Show stock and movement tables before/after reservation, release and shipment.

## HR — target 140 seconds

“HR uses the employee and location already entered in Records. I hire the
employee and review their employment history. Changing a current master label
does not rewrite past employment events.

“I grant 20.5 days of leave. The ledger stores 41 exact half days. A pending
request holds the required allowance, preventing a second request from spending
the same days. I approve or reject it explicitly. Cancellation and termination
produce auditable adjustments, and rehire starts a new employment period.

“The proposed shifts and onboarding workspace, where enabled, uses the same
employee identity. None of these actions changes Logistics stock or a different
business's records.”

Show allowance, AM/PM request, approval, cancellation and event history. Only
demonstrate optional shift/checklist controls after their acceptance checks pass.

## Accounting for a for-profit company — target 150 seconds

“Accounting uses the same supplier and customer master records. I review the
chart and a balanced opening journal. A 50-dollar-and-50-cent supplier bill is a
draft until I explicitly post it. I record a completed 20-dollar payment, then
the remaining 30.50. These actions record accounting evidence; they do not send
money through a bank.

“I post a customer invoice and record its payment with its actual date. Customer
timing reports use each paid portion's amount and due date. Unpaid and unknown
dates are shown separately.

Show separate create/post/payment actions, trial balance, balance sheet, income
statement and aging. Keep the explanation within the prototype's USD scope.

## Fund accounting for a nonprofit — target 150 seconds

“Restricted funds balance by fund. A temporary grant adds capacity once. A
release or capital bill consumes it once; paying the bill does not consume it
again. A permanent fund cannot be released. Backdated entries are refused when
they would overdraw a later date. Trial balance, statements and aging are
read-only views of the posted ledger.”

Show the grant's balance sheet, fund capacity, release and capital spending, then
one refused permanent release and backdated overspend. Net assets equal assets
minus liabilities; they do not count the grant twice. These are sample
accounting behaviors, not a claim of complete nonprofit reporting or compliance.
