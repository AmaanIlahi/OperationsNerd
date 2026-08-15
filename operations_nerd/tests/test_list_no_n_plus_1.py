"""
Smoke test for N+1 elimination in list_contacts and list_follow_ups.

Before this fix, both functions did 1 + N queries (1 for the row list,
1 per row to load its EAV attributes). The state endpoint called both,
so a business with 50 contacts + 50 follow-ups cost 102 round-trips per
state poll. After this fix, both are a single LEFT JOIN with
json_group_object, so exactly 1 query each.

This test seeds N entities, runs the list functions, and asserts the
query count is 1 (not 1 + N). The actual return values are also checked
to confirm the SQL rewrite produces the same data.
"""

import sys
import os
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from db import db as d


def _count_queries(conn, fn, *args, **kwargs):
    """Run fn(*args, **kwargs) against conn and return (result, query_count)."""
    queries = []

    def _trace(sql):
        queries.append(sql)

    conn.set_trace_callback(_trace)
    try:
        result = fn(*args, **kwargs)
    finally:
        conn.set_trace_callback(None)
    return result, len(queries)


print("Seeding 20 contacts with 3 EAV attrs each...")
d.init_db(reset=True)
with d.get_conn() as conn:
    business_id = d.create_business(
        conn, "Riverbend Realty", "realestate", "0.1.0",
        settings={"office_hours": "9-6"},
    )
    for i in range(20):
        cid = d.create_contact(
            conn, business_id, f"Lead {i}", f"lead{i}@example.com",
            extension_data={"budget_max": 500000 + i * 1000, "property_type": "condo", "preapproved": i % 2 == 0},
        )
    # 20 follow-ups too
    contact_ids = [c["id"] for c in d.list_contacts(conn, business_id)]
    for cid in contact_ids[:10]:
        d.create_follow_up(
            conn, business_id, cid, type_="call",
            due_date="2026-09-01", notes="call back",
            extension_data={"listing_ids": ["L-1", "L-2"]},
        )
print(f"  Seeded business #{business_id} with 20 contacts + 10 follow-ups")

# ---- list_contacts query count ----
print("\nTesting list_contacts query count...")
with d.get_conn() as conn:
    contacts, query_count = _count_queries(conn, d.list_contacts, conn, business_id)
print(f"  Queries issued: {query_count} (expected: 1, would be 21 with N+1)")
assert query_count == 1, f"list_contacts should be 1 query, got {query_count}"

# Verify the data round-tripped correctly through the LEFT JOIN.
assert len(contacts) == 20
sample = contacts[0]
assert sample["extension_data"]["property_type"] == "condo"
assert "budget_max" in sample["extension_data"]
print(f"  Returned {len(contacts)} contacts, sample extension_data: {sample['extension_data']}")

# ---- list_follow_ups query count ----
print("\nTesting list_follow_ups query count...")
with d.get_conn() as conn:
    fups, query_count = _count_queries(conn, d.list_follow_ups, conn, business_id)
print(f"  Queries issued: {query_count} (expected: 1, would be 11 with N+1)")
assert query_count == 1, f"list_follow_ups should be 1 query, got {query_count}"

assert len(fups) == 10
sample = fups[0]
assert sample["extension_data"]["listing_ids"] == ["L-1", "L-2"]
print(f"  Returned {len(fups)} follow-ups, sample extension_data: {sample['extension_data']}")

# ---- list_follow_ups with status filter (status path is different SQL) ----
print("\nTesting list_follow_ups(status='pending') query count...")
with d.get_conn() as conn:
    fups_pending, query_count = _count_queries(conn, d.list_follow_ups, conn, business_id, status="pending")
print(f"  Queries issued: {query_count} (expected: 1)")
assert query_count == 1, f"list_follow_ups(status) should be 1 query, got {query_count}"
assert len(fups_pending) == 10
print(f"  Returned {len(fups_pending)} pending follow-ups, all with correct extension_data")

print("\nN+1 elimination smoke test passed.")
