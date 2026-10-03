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


def _duplicate_message(where: str, kind: str, first, second) -> str:
    """Duplicate-label message. When exactly one of the two is archived, say
    so and point at restoring it, since that is almost always what was meant."""
    archived = first if first.archived and not second.archived else (
        second if second.archived and not first.archived else None)
    if archived is not None:
        return (f"{where}: duplicate {kind} label -- an archived {kind} named '{archived.label}' "
                f"already exists; restore it instead of creating a new one")
    return f"{where}: duplicate {kind} label (another {kind} already uses this label)"


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
    seen_entity_labels: dict[str, object] = {}
    for e in spec.entities:
        where = f"entity '{e.label}'"
        if not ENTITY_KEY_RE.match(e.key):
            issues.append(f"{where}: key '{e.key}' must be crm_ followed by lowercase letters, digits or underscores")
        claim(e.key, where)
        entities_by_key.setdefault(e.key, e)
        _check_label(e.label, where, issues)
        norm = _norm(e.label)
        if norm in seen_entity_labels:
            issues.append(_duplicate_message(where, "entity", seen_entity_labels[norm], e))
        seen_entity_labels.setdefault(norm, e)

        # Archived fields still count: restoring one must never create a duplicate.
        seen_field_labels: dict[str, object] = {}
        for f in e.fields:
            fwhere = f"field '{f.label}' on '{e.label}'"
            if not FIELD_KEY_RE.match(f.key):
                issues.append(f"{fwhere}: key '{f.key}' must be f_ followed by lowercase letters, digits or underscores")
            claim(f.key, fwhere)
            _check_label(f.label, fwhere, issues)
            fnorm = _norm(f.label)
            if fnorm in seen_field_labels:
                issues.append(_duplicate_message(fwhere, "field", seen_field_labels[fnorm], f))
            seen_field_labels.setdefault(fnorm, f)

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
        if l.archived_by is not None and not (l.archived and l.archived_by in (l.from_, l.to)):
            issues.append(f"{where}: archived_by must name one of the link's archived entity ends")
        for end_name, end_key in (("from", l.from_), ("to", l.to)):
            target = entities_by_key.get(end_key)
            if target is None:
                issues.append(f"{where}: '{end_name}' refers to entity '{end_key}', which does not exist")
            elif target.archived and not l.archived:
                issues.append(f"{where}: '{end_name}' refers to archived entity '{target.label}'")

    return issues
