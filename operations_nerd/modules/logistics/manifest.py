"""The Logistics module's host contract, independent of company-specific keys."""
ID = "logistics"
NAME = "LogisticsNerd"
VERSION = "0.1.0"
SCHEMA = "logistics"
MIN_CORE_SCHEMA = "001_core"
HOST_CAPABILITIES = ("authenticated_owner", "private_business_connection", "customer_order_submission")
OPTIONAL_ROLES = ("carrier",)
DISTINCT_ENTITIES = (("order", "order_line"),)
ROLES = {
    "supplier": {"fields": {"name": "text"}, "optional": {"address": "text"}, "links": {}},
    "customer": {"fields": {"name": "text"}, "optional": {"address": "text"}, "links": {}},
    "item": {"fields": {"name": "text", "sku": "text", "unit": "text"}, "optional": {}, "links": {}},
    "location": {"fields": {"name": "text"}, "optional": {"address": "text"}, "links": {}},
    "order": {"fields": {"number": "text", "currency": "text"}, "optional": {"date": "date"},
              "links": {"customer": "customer"}},
    "order_line": {"fields": {"quantity": "number", "unit_price_minor": "number", "required_by": "date"},
                   "optional": {"budget_minor": "number"}, "links": {"order": "order", "item": "item"}},
    "carrier": {"fields": {"name": "text"}, "optional": {}, "links": {}},
}
