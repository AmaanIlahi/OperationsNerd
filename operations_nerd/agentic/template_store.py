"""
Industry templates: JSON files in agentic/templates/ in spec format.

Every template is validated when this module is first imported, which
happens at app startup, so a broken template stops the server from starting
instead of failing on a user's first request.
"""

import copy
import json
import os

from agentic.validator import validate_spec

TEMPLATES_DIR = os.path.join(os.path.dirname(__file__), "templates")


class TemplateError(Exception):
    pass


def _load_all(templates_dir: str = TEMPLATES_DIR) -> dict[str, dict]:
    templates: dict[str, dict] = {}
    problems: list[str] = []
    for filename in sorted(os.listdir(templates_dir)):
        if not filename.endswith(".json"):
            continue
        template_id = filename[:-len(".json")]
        try:
            with open(os.path.join(templates_dir, filename), "r", encoding="utf-8") as f:
                spec = json.load(f)
        except json.JSONDecodeError as e:
            problems.append(f"{filename}: invalid JSON: {e}")
            continue
        issues = validate_spec(spec)
        problems.extend(f"{filename}: {i}" for i in issues)
        templates[template_id] = spec
    if problems:
        raise TemplateError("Invalid agentic templates:\n" + "\n".join(f"  - {p}" for p in problems))
    return templates


_TEMPLATES = _load_all()


def list_templates() -> list[str]:
    return sorted(_TEMPLATES)


def get_template(template_id: str) -> dict | None:
    """Returns a deep copy, so callers can never mutate the loaded template."""
    spec = _TEMPLATES.get(template_id)
    return copy.deepcopy(spec) if spec is not None else None
