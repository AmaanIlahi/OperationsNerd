"""
Smoke test for the core data layer. Not a unit test suite yet -- just proves
the schema and helper functions work end to end with realistic real estate
shaped data, using extension_data for anything real-estate-specific.
"""

import sys
import os
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from db import db as d

d.init_db(reset=True)

with d.get_conn() as conn:
    # Setup questionnaire result for a fictional real estate business
    business_id = d.create_business(
        conn,
        name="Riverbend Realty",
        industry_pack="realestate",
        pack_version="0.1.0",
        settings={"office_hours": "9-6", "default_follow_up_days": 2},
    )
    print(f"Created business #{business_id}")

    # A lead comes in -- real-estate-specific fields go in extension_data
    contact_id = d.create_contact(
        conn,
        business_id=business_id,
        name="Jordan Lee",
        email="jordan@example.com",
        phone="555-0101",
        extension_data={"budget_max": 650000, "property_type": "condo", "preapproved": True},
    )
    print(f"Created contact #{contact_id}")

    follow_up_id = d.create_follow_up(
        conn,
        business_id=business_id,
        contact_id=contact_id,
        type_="call",
        due_date="2026-08-10",
        notes="Follow up on 3 listings sent Tuesday",
        extension_data={"listing_ids": ["L-1042", "L-1090"]},
    )
    print(f"Created follow-up #{follow_up_id}")

    event_id = d.create_event(
        conn,
        business_id=business_id,
        source="email",
        raw_content="Hi, just wanted to check if the condo at 5th and Main is still available?",
        contact_id=contact_id,
    )
    d.mark_event_processed(conn, event_id, parsed_data={"intent": "availability_check", "listing_ref": "5th and Main"})
    print(f"Created + processed event #{event_id}")

    action_id = d.create_drafted_action(
        conn,
        event_id=event_id,
        business_id=business_id,
        contact_id=contact_id,
        action_type="send_availability_reply",
        payload={"subject": "Re: 5th and Main", "body": "Yes, it's still available! Want to schedule a showing?"},
    )
    print(f"Created drafted action #{action_id}")

    d.set_approval_policy(conn, business_id, "send_availability_reply", auto_approve=False)
    print("Auto-approve for send_availability_reply:", d.is_auto_approved(conn, business_id, "send_availability_reply"))

    pending = d.list_pending_actions(conn, business_id)
    print(f"Pending actions: {len(pending)}")
    print(pending[0])

    contacts = d.list_contacts(conn, business_id)
    print(f"Contacts for business: {len(contacts)}")
    print(contacts[0])

    # The concrete payoff of the EAV switch: filter contacts by a custom field
    # without touching schema or code, something a JSON blob couldn't do cleanly.
    condo_leads = d.find_entity_ids_by_attribute(conn, "contact", "property_type", "condo")
    print(f"Contact IDs with property_type=condo: {condo_leads}")

print("\nSmoke test passed.")
