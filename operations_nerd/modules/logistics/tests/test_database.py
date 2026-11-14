from concurrent.futures import ThreadPoolExecutor
from datetime import datetime,timedelta,timezone
from pathlib import Path
from threading import Barrier
import tempfile

from .. import core,db
from ..errors import AuthorizationError,ConfigurationError,IdempotencyConflict,ValidationError
from ..migrate import initialize_database,MIGRATIONS
from .common import PostgresCase


class DatabaseTests(PostgresCase):
    def test_replay_and_cross_command_content_conflicts(self):
        original=db.run_command(self.conn,'one','request',{'quantity':1},lambda c:{'value':7})
        self.assertEqual(db.run_command(self.conn,'one','request',{'quantity':1},lambda c:self.fail()),original)
        for operation,payload in [('two',{'quantity':1}),('one',{'quantity':True}),('one',{'quantity':2})]:
            with self.assertRaises(IdempotencyConflict):
                db.run_command(self.conn,operation,'request',payload,lambda c:self.fail())

    def test_failed_command_rolls_back_and_key_is_reusable(self):
        def fail(c):
            c.execute("INSERT INTO core_bindings(kind,external_key) VALUES('item','rollback')")
            raise ValidationError('injected failure')
        with self.assertRaises(ValidationError):
            db.run_command(self.conn,'failure','retry',{},fail)
        self.assertEqual(self.conn.execute("SELECT count(*) AS n FROM core_bindings").fetchone()['n'],0)
        self.assertEqual(self.conn.execute("SELECT count(*) AS n FROM command_requests").fetchone()['n'],0)
        self.assertEqual(db.run_command(self.conn,'failure','retry',{},lambda c:{'ok':True}),{'ok':True})

    def test_replay_survives_new_connection(self):
        result=db.run_command(self.conn,'durable','survives',{},lambda c:{'id':123})
        with self.newconn() as another:
            self.assertEqual(db.run_command(another,'durable','survives',{},lambda c:self.fail()),result)

    def test_wrong_business_rejected(self):
        with self.assertRaises(AuthorizationError):
            db.connect(self.dsn,'another-business')

    def test_simultaneous_same_key_runs_once(self):
        barrier=Barrier(2)
        def attempt(_):
            with self.newconn() as c:
                barrier.wait(timeout=5)
                return db.run_command(c,'race','one-key',{},lambda tx:{'id':tx.execute("INSERT INTO core_bindings(kind,external_key) VALUES('item','winner') RETURNING id").fetchone()['id']})
        with ThreadPoolExecutor(max_workers=2) as pool:
            results=list(pool.map(attempt,range(2)))
        self.assertEqual(results[0],results[1])
        self.assertEqual(self.conn.execute("SELECT count(*) AS n FROM core_bindings").fetchone()['n'],1)

    def test_migration_history_and_identity_are_not_reassigned(self):
        self.assertEqual(initialize_database(self.conn,'test-business'),[])
        with self.assertRaises(AuthorizationError):
            initialize_database(self.conn,'another-business')
        with tempfile.TemporaryDirectory() as directory:
            target=Path(directory)
            for p in MIGRATIONS.glob('*.sql'):
                (target/p.name).write_text(p.read_text()+'\n-- changed source\n')
            with self.assertRaises(ConfigurationError):
                initialize_database(self.conn,'test-business',migrations=target)

    def test_supplier_revision_retains_as_of_history(self):
        first=datetime.now(timezone.utc)-timedelta(days=1)
        old=core.bind_reference(self.conn,'supplier','supplier-1',{'address':'Old'},'supplier-old',effective_at=first)
        second=first+timedelta(hours=1)
        new=core.bind_reference(self.conn,'supplier','supplier-1',{'address':'New'},'supplier-new',effective_at=second)
        self.assertNotEqual(old['id'],new['id'])
        self.assertEqual(core.resolve(self.conn,'supplier','supplier-1',as_of=first+timedelta(minutes=5))['snapshot']['address'],'Old')
        self.assertEqual(core.resolve(self.conn,'supplier','supplier-1')['snapshot']['address'],'New')
