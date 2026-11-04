"""Owner-scoped EAV filtering: real SQLite, no model calls or external services."""
import os
import sys
import tempfile
sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))
from db import db as d


def run():
    with tempfile.TemporaryDirectory() as temp:
        path = os.path.join(temp, 'db.sqlite')
        d.init_db(path)
        with d.get_conn(path) as c:
            a = d.create_business(c, 'a', 'generic', '1', {})
            b = d.create_business(c, 'b', 'generic', '1', {})
            c.execute('UPDATE businesses SET owner_key=? WHERE id=?', ('owner', a))
            c.execute('UPDATE businesses SET owner_key=? WHERE id=?', ('other', b))
            ids = [d.create_entity(c, a, 'crm_person', v) for v in [
                {'name': 'Straße', 'status': 'open', 'number': 1},
                {'name': '１００%_Ready', 'status': 'closed', 'number': True},
                {'name': 'Ravi', 'tags': ['Gold', 'Blue'], 'none': None, 'nested': [True]},
                {'name': 'Another', 'status': 'open', 'hidden': 'Ravi'}]]
            d.create_entity(c, a, 'crm_supplier', {'name': 'Ravi'})
            d.create_entity(c, b, 'crm_person', {'name': 'Ravi', 'status': 'secret'})
            def search(**kw):
                return d.search_owned_entities(c, a, 'owner', 'crm_person', **kw)
            assert search()['total'] == 4
            assert search(query=' STRASSE ')['records'][0]['id'] == ids[0]
            assert search(query='100%_')['records'][0]['id'] == ids[1]
            assert search(query='%')['total'] == 1
            assert search(query="' OR 1=1 --")['total'] == 0
            assert search(query='gold')['records'][0]['id'] == ids[2]
            assert search(query='ravi', fields=['name'])['total'] == 1
            assert search(query='ravi', fields=[])['total'] == 0
            assert search(equals={'status': 'open'}, limit=1, offset=1)['records'][0]['id'] == ids[3]
            assert search(equals={'status': 'open'}, limit=1)['total'] == 2
            assert search(equals={'number': 1})['total'] == 1
            assert search(equals={'number': True})['records'][0]['id'] == ids[1]
            assert search(equals={'none': None})['total'] == 1
            assert search(equals={'nested': [1]})['total'] == 0
            assert search(equals={'nested': [True]})['total'] == 1
            assert search(offset=99)['records'] == []
            assert d.search_owned_entities(c, b, 'owner', 'crm_person') is None
            assert d.search_owned_entities(c, 99999, 'owner', 'crm_person') is None
            before = c.total_changes
            search(query='ravi')
            assert c.total_changes == before
            for bad in ({'limit': 0}, {'limit': True}, {'offset': -1}, {'query': 'a'*201},
                        {'fields': 'name'}, {'equals': []}):
                try:
                    search(**bad)
                    raise AssertionError(f'Accepted {bad}')
                except ValueError:
                    pass
    print('entity search: 25 assertions/invalid-input cases passed')

if __name__ == '__main__':
    run()
