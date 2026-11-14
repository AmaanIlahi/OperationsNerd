"""Recoverable provisioning and consistent, isolated business restore proofs."""
from pathlib import Path
import json
import os
import tempfile
import unittest
from unittest.mock import patch

from tests_integration import test_umbrella_workflow as flow


class RecoveryTests(flow.UmbrellaHTTPTests):
    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        from db import db as d
        from umbrella.businesses import initialize_registry
        d.init_db()
        initialize_registry()

    def test_failed_initialization_retries_the_same_private_business(self):
        from umbrella import businesses
        from db import db as d
        original = d.init_db
        calls=[]
        def fail_after_schema(*args,**kwargs):
            original(*args,**kwargs)
            calls.append(True)
            raise RuntimeError('Fictional interrupted setup')
        with patch.object(d,'init_db',side_effect=fail_after_schema):
            self.response(self.owner.post('/api/businesses',json={'name':'Interrupted toy company','template':'toy_store'}),503)
        self.assertTrue(calls)
        rows=self.response(self.owner.get('/api/businesses'))
        failed=next(row for row in rows if row['name']=='Interrupted toy company')
        self.assertEqual(failed['status'],'failed')
        identifier=failed['id']
        old_url=self.fixture.business_url(identifier)
        self.response(self.stranger.post(f'/api/businesses/{identifier}/retry-setup'),404)
        recovered=self.response(self.owner.post(f'/api/businesses/{identifier}/retry-setup'))
        self.assertEqual(recovered['id'],identifier)
        self.assertEqual(self.fixture.business_url(identifier),old_url)
        again=self.response(self.owner.post(f'/api/businesses/{identifier}/retry-setup'))
        self.assertEqual(again['spec'],recovered['spec'])
        self.assertEqual(again['current_version'],1)
        self.assertEqual(len([row for row in self.response(self.owner.get('/api/businesses'))
                             if row['id']==identifier]),1)

    def test_one_failed_metadata_upgrade_does_not_block_other_businesses(self):
        from umbrella import businesses,lifecycle
        from db.runtime import current_database_url
        original=lifecycle.ensure_schema
        toy_url=self.fixture.business_url(self.bid)
        before=self.fixture.core_snapshot(self.gym['id'])
        def fail_one(conn):
            if current_database_url()==toy_url:
                raise RuntimeError('Fictional per-business upgrade failure')
            return original(conn)
        with patch.object(lifecycle,'ensure_schema',side_effect=fail_one):
            businesses.initialize_registry()
        self.response(self.owner.get(self.base),409)
        self.response(self.owner.get(f"/api/businesses/{self.gym['id']}"))
        self.assertEqual(self.fixture.core_snapshot(self.gym['id']),before)
        self.response(self.owner.post(self.base+'/retry-setup'))
        self.response(self.owner.get(self.base))

    def test_backup_restores_verified_copy_without_replacing_live_database(self):
        import psycopg
        from psycopg import sql
        from umbrella.backup import backup_business,restore_copy
        self.enable()
        aid=self.response(self.owner.get('/api/auth/me'))['id']
        live_url=self.fixture.business_url(self.bid)
        before=self.fixture.core_snapshot(self.bid)
        with tempfile.TemporaryDirectory(prefix='business-recovery-proof-') as directory:
            saved=backup_business(aid,self.bid,directory)
            archive=Path(saved['archive'])
            self.assertEqual(archive.stat().st_mode&0o777,0o600)
            self.assertEqual(Path(saved['manifest']).stat().st_mode&0o777,0o600)
            recovered=restore_copy(aid,self.bid,archive)
            try:
                self.assertTrue(recovered['verified'])
                self.assertFalse(recovered['live_database_changed'])
                self.assertEqual(self.fixture.business_url(self.bid),live_url)
                from umbrella.businesses import connection_url
                with psycopg.connect(connection_url(recovered['database_name']),autocommit=True) as conn:
                    snapshot={}
                    tables=[row[0] for row in conn.execute("SELECT tablename FROM pg_tables WHERE schemaname='core' ORDER BY tablename")]
                    for table in tables:
                        query=sql.SQL('SELECT row_to_json(t)::text FROM core.{} t ORDER BY row_to_json(t)::text').format(sql.Identifier(table))
                        snapshot[table]=[row[0] for row in conn.execute(query)]
                    self.assertEqual(snapshot,before)
                    self.assertEqual(conn.execute('SELECT business_key FROM logistics.database_identity').fetchone()[0],f'business:{self.bid}')
                    old_count=conn.execute('SELECT count(*) FROM logistics.schema_migrations').fetchone()[0]
                    self.assertEqual(old_count,3)
                self.assertEqual(self.fixture.core_snapshot(self.bid),before)
            finally:
                with psycopg.connect(self.fixture.admin_url,autocommit=True) as admin:
                    admin.execute(sql.SQL('DROP DATABASE {} WITH (FORCE)').format(sql.Identifier(recovered['database_name'])))
            tampered=Path(directory)/'tampered.dump'
            tampered.write_bytes(archive.read_bytes()+b'not-the-saved-archive')
            tampered.with_suffix('.json').write_text(Path(saved['manifest']).read_text())
            with self.assertRaisesRegex(ValueError,'checksum'):
                restore_copy(aid,self.bid,tampered)
            self.assertEqual(self.fixture.business_url(self.bid),live_url)


def load_tests(loader,tests,pattern):
    return unittest.TestSuite(RecoveryTests(name) for name,method in RecoveryTests.__dict__.items()
        if name.startswith('test_') and callable(method))


if __name__=='__main__':
    unittest.main()
