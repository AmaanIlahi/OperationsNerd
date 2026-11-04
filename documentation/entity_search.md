# Owner-scoped record search in the data layer

`db.search_owned_entities(conn, business_id, owner_key, entity_type, *, query='',
fields=None, equals=None, limit=100, offset=0)` searches an owned business's EAV
records without a model call, schema change or write. It returns None when the
business does not exist or is not owned by that key; matching counts never leak
across businesses. Entity types remain separate even when attribute names match.

Literal Unicode-normalized case-insensitive search checks attribute values, not
field keys or SQL patterns. `%`, `_`, and quotes have no special meaning. Lists
are searched element by element. Nulls and objects are not text-searchable.
`fields` limits the searchable field keys, with an empty list matching no nonempty
query. `equals` ANDs exact, type-sensitive filters: missing differs from null and
true differs from 1. Filtering happens before pagination; total is matching rows.

The caller supplies allowed active field keys and checks the current entity spec.
This is a core read-layer capability, not a change to the agent's operation set.
No UI or authenticated HTTP endpoint is wired by this change. Existing
`list_entities` behavior is unchanged. Limit is 1..500; offset is nonnegative;
query is at most 200 characters. Invalid values raise ValueError only after the
ownership check. Reads follow the connection's current transaction snapshot.
