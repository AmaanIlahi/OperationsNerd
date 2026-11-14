ID = "accounting"
NAME = "AccountingNerd"
VERSION = "0.1.0"
MIN_CORE_SCHEMA = "001_core"
OPTIONAL_ROLES = set()
DISTINCT_ENTITIES = []
ROLES = {
    "customer": {"fields": {"name": "text"}, "optional": {"address": "text"}, "links": {}},
    "supplier": {"fields": {"name": "text"}, "optional": {"address": "text"}, "links": {}},
}
