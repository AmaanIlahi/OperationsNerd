"""Permanent, explicitly reviewed semantic roles over the current CRM spec."""
import json


from modules.logistics.manifest import ROLES as REQUIREMENTS
from umbrella import registry


def validate_bindings(spec, bindings, nerd_id="logistics"):
    manifest = registry.manifest(nerd_id)
    requirements = manifest.ROLES
    optional_roles = getattr(manifest, "OPTIONAL_ROLES", ())
    distinct_entities = getattr(manifest, "DISTINCT_ENTITIES", getattr(manifest, "DISTINCT", ()))
    if not isinstance(bindings, dict):
        raise ValueError("Role mappings must be an object")
    missing = set(requirements) - set(optional_roles) - set(bindings)
    unknown = set(bindings) - set(requirements)
    if missing or unknown:
        raise ValueError("Required roles: " + ", ".join(sorted(set(requirements) - set(optional_roles))))
    if any(not isinstance(value, dict) for value in bindings.values()):
        raise ValueError("Each role needs an entity, fields and links mapping")
    entities = {entity["key"]: entity for entity in spec["entities"] if not entity["archived"]}
    links = {link["key"]: link for link in spec["links"] if not link["archived"]}
    cleaned = {}
    for role, mapping in bindings.items():
        if not isinstance(mapping, dict) or set(mapping) - {"entity", "fields", "links"}:
            raise ValueError(f"Invalid mapping for {role}")
        entity_key = mapping.get("entity")
        entity = entities.get(entity_key) if isinstance(entity_key, str) else None
        if entity is None:
            raise ValueError(f"Choose an active record type for {role}")
        fields = mapping.get("fields", {})
        mapped_links = mapping.get("links", {})
        if not isinstance(fields, dict) or not isinstance(mapped_links, dict):
            raise ValueError(f"Invalid field/link mapping for {role}")
        definition = requirements[role]
        expected = {**definition["fields"], **definition["optional"]}
        if set(definition["fields"]) - set(fields) or set(fields) - set(expected):
            raise ValueError(f"Missing or unknown fields for {role}")
        available = {field["key"]: field for field in entity["fields"] if not field["archived"]}
        for semantic, key in fields.items():
            field = available.get(key) if isinstance(key, str) else None
            allowed = {"text", "long_text", "select"} if expected[semantic] == "text" else {expected[semantic]}
            if field is None or field["type"] not in allowed:
                raise ValueError(f"{role}.{semantic} needs an active {expected[semantic]} field")
        if len(set(fields.values())) != len(fields):
            raise ValueError(f"Different meanings in {role} need distinct fields")
        if set(mapped_links) != set(definition["links"]):
            raise ValueError(f"Missing or unknown links for {role}")
        for semantic, target_role in definition["links"].items():
            key = mapped_links.get(semantic)
            link = links.get(key) if isinstance(key, str) else None
            if not link or link["from"] != entity["key"] or link["to"] != bindings[target_role].get("entity") \
                    or link["cardinality"] not in ("many_to_one", "one_to_one"):
                raise ValueError(f"{role}.{semantic} needs a link to the mapped {target_role}")
        cleaned[role] = {"entity": entity["key"], "fields": dict(fields), "links": dict(mapped_links)}
    # Roles may intentionally share a core party type, but an order and its
    # lines must be separate record types for unambiguous submission/freeze.
    for left, right in distinct_entities:
        if cleaned[left]["entity"] == cleaned[right]["entity"]:
            raise ValueError(f"{left} and {right} need separate record types")
    return cleaned


def compatibility_errors(conn, business_id, new_spec):
    old = conn.execute("""SELECT v.spec_json FROM spec_versions v JOIN businesses b
        ON b.id=v.business_id AND b.current_version=v.version WHERE b.id=?""", (business_id,)).fetchone()
    old_spec = json.loads(old["spec_json"]) if old else {"entities": []}
    old_fields = {field["key"]: field for entity in old_spec["entities"] for field in entity["fields"]}
    new_fields = {field["key"]: field for entity in new_spec["entities"] for field in entity["fields"]}
    rows = conn.execute("SELECT nerd_id,bindings_json FROM nerd_installations WHERE status IN ('enabled','enabling')").fetchall()
    problems = []
    for row in rows:
        try:
            bindings = json.loads(row["bindings_json"])
            validate_bindings(new_spec, bindings, nerd_id=row["nerd_id"])
            for mapping in bindings.values():
                for key in mapping["fields"].values():
                    before, after = old_fields.get(key), new_fields.get(key)
                    if before and after and before["type"] == "select" \
                            and set(before.get("options") or []) - set(after.get("options") or []):
                        problems.append("An enabled capability still depends on the existing mapped select options")
        except (ValueError, TypeError, KeyError) as error:
            problems.append(str(error))
    return problems
