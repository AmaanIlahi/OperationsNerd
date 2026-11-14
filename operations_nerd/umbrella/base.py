"""Common CRM starter entities, independently instantiated per business."""
from agentic.spec import rekey_spec


def with_common_base(spec):
    seed = {"business": dict(spec["business"]), "links": [], "entities": []}
    for name in ("Employee", "Customer", "Location"):
        seed["entities"].append({"key": "crm_" + name.lower(), "label": name, "archived": False,
            "fields": [{"key": "f_name", "label": "Name", "type": "text", "required": True, "archived": False},
                       {"key": "f_detail", "label": "Address" if name == "Location" else "Email",
                        "type": "text" if name == "Location" else "email", "required": False, "archived": False}]})
    generated = rekey_spec(seed)
    labels = {entity["label"].strip().casefold() for entity in spec["entities"]}
    return {**spec, "entities": [*spec["entities"], *[entity for entity in generated["entities"]
            if entity["label"].casefold() not in labels]]}
