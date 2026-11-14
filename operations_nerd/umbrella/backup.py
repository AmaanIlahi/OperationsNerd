"""Consistent PostgreSQL backups and verified, isolated restore rehearsals."""
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess
from uuid import uuid4

from db import db as d
from umbrella import businesses


def _tool(name):
    found = shutil.which(name)
    if found:
        return found
    try:
        import pgserver
        bundled = Path(pgserver.__file__).parent / 'pginstall' / 'bin' / name
        if bundled.is_file():
            return str(bundled)
    except ImportError:
        pass
    raise RuntimeError('PostgreSQL backup tools are missing; install matching pg_dump and pg_restore')


def _environment(url):
    from psycopg.conninfo import conninfo_to_dict
    params = conninfo_to_dict(url)
    environment = {key: value for key,value in os.environ.items() if not key.startswith('PG')
                   and not any(part in key for part in ('API_KEY','TOKEN','SECRET','PASSWORD'))}
    for option,key in [('host','PGHOST'),('hostaddr','PGHOSTADDR'),('port','PGPORT'),
                       ('dbname','PGDATABASE'),('user','PGUSER'),('password','PGPASSWORD'),('sslmode','PGSSLMODE')]:
        if option in params:
            environment[key]=params[option]
    environment['PGPASSFILE']=os.devnull
    environment['PGCONNECT_TIMEOUT']='10'
    return environment


def _digest(path):
    digest=hashlib.sha256()
    with path.open('rb') as source:
        for chunk in iter(lambda:source.read(1024*1024),b''):
            digest.update(chunk)
    return digest.hexdigest()


def backup_business(account_id,business_id,directory=None):
    registration=businesses._registry(account_id,business_id)
    url=businesses.connection_url(registration['database_name'])
    return backup_scoped_business(account_id,business_id,directory,url=url)


def backup_scoped_business(account_id,business_id,directory=None,*,url=None):
    from db.runtime import current_database_url
    from psycopg.conninfo import conninfo_to_dict
    url=url or current_database_url()
    with d.raw_postgres_conn(url) as conn:
        row=conn.execute('SELECT owner_key FROM core.businesses WHERE id=%s',(business_id,)).fetchone()
        if row is None or row[0]!=str(account_id):
            raise ValueError('The backup context does not belong to this business owner')
    database_name=conninfo_to_dict(url)['dbname']
    folder=Path(directory or os.getenv('OPERATIONS_BACKUP_DIR') or
                Path(__file__).resolve().parents[2]/'.local'/'backups')
    folder.mkdir(mode=0o700,parents=True,exist_ok=True)
    stamp=datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%S')
    target=folder/f'business-{business_id}-{stamp}-{uuid4().hex[:10]}.dump'
    partial=target.with_suffix('.partial')
    descriptor=os.open(partial,os.O_WRONLY|os.O_CREAT|os.O_EXCL,0o600)
    os.close(descriptor)
    try:
        result=subprocess.run([_tool('pg_dump'),'--format=custom','--no-owner','--no-acl','--no-password',
            '--file',str(partial)],env=_environment(url),capture_output=True,timeout=180)
        if result.returncode:
            raise RuntimeError('Consistent business backup failed; no schema upgrade was attempted')
        checksum=_digest(partial)
        partial.replace(target)
        metadata={'business_id':business_id,'owner_key':str(account_id),'sha256':checksum,
                  'created_at':datetime.now(timezone.utc).isoformat(),'database_name':database_name}
        manifest=target.with_suffix('.json')
        descriptor=os.open(manifest,os.O_WRONLY|os.O_CREAT|os.O_EXCL,0o600)
        with os.fdopen(descriptor,'w') as output:
            json.dump(metadata,output,indent=2)
        return {'archive':str(target),'manifest':str(manifest),'sha256':checksum,'business_id':business_id}
    except Exception:
        partial.unlink(missing_ok=True)
        raise


def restore_copy(account_id,business_id,archive):
    """Restore into a fresh quarantine database; never overwrite/repoint live data.

    A release operator can inspect this verified copy before a maintenance
    cutover. This rehearsal proves recovery while retaining the live database.
    """
    import psycopg
    from psycopg import sql
    registration=businesses._registry(account_id,business_id)
    source=Path(archive)
    metadata=json.loads(source.with_suffix('.json').read_text())
    if metadata.get('business_id')!=business_id or metadata.get('owner_key')!=str(account_id):
        raise ValueError('The backup belongs to another business or owner')
    if not source.is_file() or _digest(source)!=metadata.get('sha256'):
        raise ValueError('The backup checksum does not match')
    name='opsnerd_restore_'+uuid4().hex
    admin_url=os.getenv('OPERATIONS_ADMIN_DATABASE_URL') or os.environ['OPERATIONS_DATABASE_URL']
    created=False
    try:
        with psycopg.connect(admin_url,autocommit=True) as admin:
            admin.execute(sql.SQL('CREATE DATABASE {} TEMPLATE template0').format(sql.Identifier(name)))
            created=True
        url=businesses.connection_url(name)
        result=subprocess.run([_tool('pg_restore'),'--exit-on-error','--no-owner','--no-acl','--no-password',
            '--dbname',name,str(source)],env=_environment(url),capture_output=True,timeout=180)
        if result.returncode:
            raise RuntimeError('Backup restore failed; the live database was not changed')
        with d.raw_postgres_conn(url) as conn:
            rows=conn.execute('SELECT id,owner_key FROM core.businesses').fetchall()
            if len(rows)!=1 or rows[0][0]!=business_id or rows[0][1]!=str(account_id):
                raise ValueError('Restored database identity does not match')
            foreign_keys=conn.execute("SELECT count(*) FROM pg_constraint WHERE contype='f' AND NOT convalidated").fetchone()[0]
            if foreign_keys:
                raise ValueError('Restored data contains unvalidated foreign keys')
        d.init_db(url)  # Verifies the immutable core migration checksum.
        return {'database_name':name,'business_id':business_id,'verified':True,'live_database_changed':False}
    except Exception:
        if created:
            with psycopg.connect(admin_url,autocommit=True) as admin:
                admin.execute(sql.SQL('DROP DATABASE {} WITH (FORCE)').format(sql.Identifier(name)))
        raise
