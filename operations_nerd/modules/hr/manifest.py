ID = 'hr'
NAME = 'HRNerd'
VERSION = '0.1.0'
ROLES = {
    'employee': {'fields': {'name': 'text'}, 'optional': {'email': 'email'}, 'links': {}},
    'location': {'fields': {'name': 'text'}, 'optional': {'address': 'text'}, 'links': {}},
}
OPTIONAL_ROLES = set()
DISTINCT_ENTITIES = ()
