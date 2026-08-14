"""
Template rendering for pack prompts.

Resolves {{namespace.path.to.value}} references against a context dict, e.g.
{{settings.office_hours}} or {{event.parsed_data.listing_ref}}. This is the
only thing standing between a pack's prompt text and the real data at
render time -- it has no idea what "office_hours" or "listing_ref" mean,
it just walks the dict. That's what keeps it usable by every industry.
"""

import re

_REF = re.compile(r"\{\{\s*([\w.]+)\s*\}\}")


class TemplateRenderError(Exception):
    pass


def render_template(template: str, context: dict) -> str:
    def replace(match):
        path = match.group(1)
        value = context
        for part in path.split("."):
            if isinstance(value, dict) and part in value:
                value = value[part]
            else:
                raise TemplateRenderError(
                    f"Template references {{{{{path}}}}}, but '{part}' was not found in the "
                    f"provided context. This usually means a prompt references data the "
                    f"pipeline never supplied, or a pack references a field that doesn't exist."
                )
        return str(value)

    return _REF.sub(replace, template)
