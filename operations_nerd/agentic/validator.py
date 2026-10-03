"""
Spec validator. Returns every problem found, not just the first, as plain
strings -- same convention as packs/loader.py's validate_pack. An empty list
means the spec is valid.

Two layers, like the pack loader: Pydantic checks shape (types, unknown
attributes, supported field types), then hand-written checks cover what
needs a view across the whole spec (references, duplicates, key rules).
"""

from pydantic import ValidationError

from agentic.spec import (
    Spec, ENTITY_KEY_RE, FIELD_KEY_RE, LINK_KEY_RE, MAX_LABEL_LENGTH,
)

OPTION_TYPES = ("select", "multiselect")


def _norm(label: str) -> str:
    return " ".join(label.split()).casefold()


def _check_label(label: str, where: str, issues: list[str]):
    if not label.strip():
        issues.append(f"{where}: label is empty")
    elif len(label) > MAX_LABEL_LENGTH:
        issues.append(f"{where}: label is longer than {MAX_LABEL_LENGTH} characters")


def validate_spec(data) -> list[str]:
    if not isinstance(data, dict):
        return ["spec must be a JSON object"]
    try:
        spec = Spec.model_validate(data)
    except ValidationError as e:
        return [f"{'.'.join(str(p) for p in err['loc'])}: {err['msg']}" for err in e.errors()]

    issues: list[str] = []
    _check_label(spec.business.name, "business name", issues)

    seen_keys: dict[str, str] = {}

    def claim(key: str, where: str):
        if key in seen_keys:
            issues.append(f"{where}: key '{key}' is already used by {seen_keys[key]}")
        else:
            seen_keys[key] = where

    entities_by_key = {}
    seen_entity_labels: dict[str, str] = {}
    for e in spec.entities:
        where = f"entity '{e.label}'"
        if not ENTITY_KEY_RE.match(e.key):
            issues.append(f"{where}: key '{e.key}' must be crm_ followed by lowercase letters, digits or underscores")
        claim(e.key, where)
        entities_by_key.setdefault(e.key, e)
        _check_label(e.label, where, issues)
        norm = _norm(e.label)
        if norm in seen_entity_labels:
            issues.append(f"{where}: duplicate entity label (also used by '{seen_entity_labels[norm]}')")
        seen_entity_labels.setdefault(norm, e.label)

        # Archived fields still count: restoring one must never create a duplicate.
        seen_field_labels: dict[str, str] = {}
        for f in e.fields:
            fwhere = f"field '{f.label}' on '{e.label}'"
            if not FIELD_KEY_RE.match(f.key):
                issues.append(f"{fwhere}: key '{f.key}' must be f_ followed by lowercase letters, digits or underscores")
            claim(f.key, fwhere)
            _check_label(f.label, fwhere, issues)
            fnorm = _norm(f.label)
            if fnorm in seen_field_labels:
                issues.append(f"{fwhere}: duplicate field label within this entity type")
            seen_field_labels.setdefault(fnorm, f.label)

            if f.type in OPTION_TYPES:
                if not f.options:
                    issues.append(f"{fwhere}: {f.type} needs a non-empty options list")
                else:
                    cleaned = [o.strip() for o in f.options]
                    if any(not o for o in cleaned):
                        issues.append(f"{fwhere}: options must not be blank")
                    if len({o.casefold() for o in cleaned}) != len(cleaned):
                        issues.append(f"{fwhere}: options must be unique")
            elif f.options:
                issues.append(f"{fwhere}: options are only allowed on select and multiselect fields")

    for l in spec.links:
        where = f"link '{l.label}'"
        if not LINK_KEY_RE.match(l.key):
            issues.append(f"{where}: key '{l.key}' must be l_ followed by lowercase letters, digits or underscores")
        claim(l.key, where)
        _check_label(l.label, where, issues)
        for end_name, end_key in (("from", l.from_), ("to", l.to)):
            target = entities_by_key.get(end_key)
            if target is None:
                issues.append(f"{where}: '{end_name}' refers to entity '{end_key}', which does not exist")
            elif target.archived and not l.archived:
                issues.append(f"{where}: '{end_name}' refers to archived entity '{target.label}'")

    return issues
