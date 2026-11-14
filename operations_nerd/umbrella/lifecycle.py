"""Manifest-driven, owner-reviewed installation of business capabilities."""
import json
from uuid import uuid4

from fastapi import HTTPException
from agentic import store
from db import db as d
from umbrella import businesses, registry
from umbrella.bindings import validate_bindings


def ensure_schema(conn):
    conn.execute("""CREATE TABLE IF NOT EXISTS nerd_installations(
        nerd_id TEXT PRIMARY KEY,version TEXT NOT NULL,status TEXT NOT NULL,
        bindings_json TEXT NOT NULL,proposal_id TEXT NOT NULL)""")
    conn.execute("""CREATE TABLE IF NOT EXISTS nerd_proposals(
        id TEXT PRIMARY KEY,business_id BIGINT NOT NULL,base_version BIGINT NOT NULL,
        bindings_json TEXT NOT NULL,status TEXT NOT NULL,approved_by BIGINT,error_code TEXT,
        nerd_id TEXT NOT NULL DEFAULT 'logistics')""")
    conn.execute("ALTER TABLE nerd_proposals ADD COLUMN IF NOT EXISTS nerd_id TEXT NOT NULL DEFAULT 'logistics'")
    conn.execute("""CREATE TABLE IF NOT EXISTS nerd_frozen_records(
        entity_key TEXT NOT NULL,record_id BIGINT NOT NULL,order_ref TEXT NOT NULL,
        PRIMARY KEY(entity_key,record_id))""")


def _business(conn, account_id, business_id):
    found=store.get_business_with_spec(conn,account_id,business_id)
    if found is None:
        raise HTTPException(404,"Business not found")
    return found


def describe(account_id,business_id):
    with d.get_conn() as conn:
        _business(conn,account_id,business_id)
        rows=conn.execute("SELECT * FROM nerd_installations").fetchall() if businesses.configured() else []
    installed={row['nerd_id']:row for row in rows}
    result=[]
    for nerd_id in registry.installed_modules():
        manifest=registry.manifest(nerd_id)
        row=installed.get(nerd_id)
        available=businesses.configured()
        if available:
            try:
                adapter=registry.adapter(nerd_id)
                available=all(callable(getattr(adapter,name,None)) for name in ('sync_records','workspace','invoke'))
            except ImportError:
                available=False
        result.append({'id':nerd_id,'name':getattr(manifest,'NAME',nerd_id.title()),
            'display_name':getattr(manifest,'DISPLAY_NAME',getattr(manifest,'NAME',nerd_id.title())),
            'version':manifest.VERSION,'available':available,'status':row['status'] if row else 'disabled',
            'bindings':json.loads(row['bindings_json']) if row else None,'requirements':manifest.ROLES,
            'frontend_script':'/app/'+nerd_id+'.js'})
    return {'nerds':result}


def propose(account_id,business_id,base_version,bindings,nerd_id='logistics'):
    if not businesses.configured():
        raise HTTPException(409,'Capabilities require the PostgreSQL umbrella runtime')
    registry.manifest(nerd_id)
    with d.get_conn() as conn:
        business=_business(conn,account_id,business_id)
        if base_version!=business['current_version']:
            raise HTTPException(409,'The business schema changed. Refresh and review the mappings again.')
        try:
            clean=validate_bindings(business['spec'],bindings,nerd_id=nerd_id)
        except ValueError as error:
            raise HTTPException(422,str(error)) from None
        installed=conn.execute('SELECT status,bindings_json FROM nerd_installations WHERE nerd_id=?',(nerd_id,)).fetchone()
        if installed and installed['status'] in ('enabled','enabling'):
            raise HTTPException(409,'This capability is already enabled or being enabled')
        if installed and json.loads(installed['bindings_json'])!=clean:
            raise HTTPException(409,'Existing installation data requires the original role mappings. Retargeting needs an explicit migration.')
        identifier=uuid4().hex
        conn.execute("""INSERT INTO nerd_proposals(id,business_id,base_version,bindings_json,status,nerd_id)
            VALUES(?,?,?,?,'pending',?)""",(identifier,business_id,base_version,json.dumps(clean),nerd_id))
    return {'id':identifier,'status':'pending','base_version':base_version,'bindings':clean,'nerd_id':nerd_id}


def approve(account_id,business_id,proposal_id,nerd_id='logistics'):
    registry.manifest(nerd_id)
    with d.raw_postgres_conn() as lock_conn:
        if not lock_conn.execute('SELECT pg_try_advisory_lock(684728314503)').fetchone()[0]:
            raise HTTPException(409,'A capability is being enabled; retry shortly')
        try:
            return _approve(account_id,business_id,proposal_id,nerd_id)
        finally:
            lock_conn.execute('SELECT pg_advisory_unlock(684728314503)')


def _approve(account_id,business_id,proposal_id,nerd_id):
    manifest=registry.manifest(nerd_id)
    with d.get_conn() as conn:
        conn.execute('BEGIN IMMEDIATE')
        business=_business(conn,account_id,business_id)
        proposal=conn.execute('SELECT * FROM nerd_proposals WHERE id=? AND business_id=? AND nerd_id=?',
            (proposal_id,business_id,nerd_id)).fetchone()
        if proposal is None:
            raise HTTPException(404,'Setup proposal not found')
        installed=conn.execute('SELECT * FROM nerd_installations WHERE nerd_id=?',(nerd_id,)).fetchone()
        if proposal['status']=='approved':
            if installed and installed['proposal_id']==proposal_id and installed['status']=='enabled':
                return {'id':proposal_id,'status':'enabled','nerd_id':nerd_id}
            raise HTTPException(409,'This setup is no longer active. Review a new proposal to enable again.')
        if proposal['status'] not in ('pending','failed','enabling'):
            raise HTTPException(409,'This setup cannot be applied')
        if proposal['base_version']!=business['current_version']:
            raise HTTPException(409,'The business schema changed. Propose setup again.')
        bindings=validate_bindings(business['spec'],json.loads(proposal['bindings_json']),nerd_id=nerd_id)
        if installed and installed['proposal_id']!=proposal_id and installed['status'] in ('enabled','enabling'):
            raise HTTPException(409,'Another setup has enabled this capability')
        if installed and json.loads(installed['bindings_json'])!=bindings:
            raise HTTPException(409,'Existing history requires the original role mappings')
        conn.execute("UPDATE nerd_proposals SET status='enabling',approved_by=?,error_code=NULL WHERE id=?",(account_id,proposal_id))
        conn.execute("""INSERT INTO nerd_installations(nerd_id,version,status,bindings_json,proposal_id)
            VALUES(?,?,'enabling',?,?) ON CONFLICT(nerd_id) DO UPDATE SET
            status='enabling',bindings_json=excluded.bindings_json,proposal_id=excluded.proposal_id,version=excluded.version""",
            (nerd_id,manifest.VERSION,json.dumps(bindings),proposal_id))
    try:
        # Take a consistent, private archive before running module DDL. A dump
        # failure stops installation; it never silently proceeds without backup.
        from umbrella.backup import backup_scoped_business
        backup_scoped_business(account_id,business_id)
        with d.raw_postgres_conn() as native:
            registry.migrator(nerd_id).initialize_database(native,f'business:{business_id}')
        registry.adapter(nerd_id).sync_records(account_id,business_id,bindings,'enable:'+proposal_id)
        with d.get_conn() as conn:
            conn.execute('BEGIN IMMEDIATE')
            current=_business(conn,account_id,business_id)
            validate_bindings(current['spec'],bindings,nerd_id=nerd_id)
            conn.execute("UPDATE nerd_installations SET status='enabled' WHERE nerd_id=? AND proposal_id=?",(nerd_id,proposal_id))
            conn.execute("UPDATE nerd_proposals SET status='approved' WHERE id=?",(proposal_id,))
    except Exception as error:
        with d.get_conn() as conn:
            conn.execute("UPDATE nerd_installations SET status='failed' WHERE nerd_id=? AND proposal_id=?",(nerd_id,proposal_id))
            conn.execute("UPDATE nerd_proposals SET status='failed',error_code=? WHERE id=?",(type(error).__name__,proposal_id))
        if isinstance(error,HTTPException):
            raise
        if isinstance(error,ValueError):
            raise HTTPException(getattr(error,'status',422),str(error)) from None
        raise HTTPException(503,'Capability setup failed. Data was retained; retry the reviewed proposal after fixing connectivity.') from None
    return {'id':proposal_id,'status':'enabled','bindings':bindings,'nerd_id':nerd_id}


def installed_bindings(account_id,business_id,nerd_id='logistics'):
    if not businesses.configured():
        raise HTTPException(409,'Capabilities require the PostgreSQL umbrella runtime')
    registry.manifest(nerd_id)
    with d.get_conn() as conn:
        business=_business(conn,account_id,business_id)
        row=conn.execute('SELECT * FROM nerd_installations WHERE nerd_id=?',(nerd_id,)).fetchone()
        if row is None or row['status']!='enabled':
            raise HTTPException(409,'Enable this capability before using its commands')
        try:
            return validate_bindings(business['spec'],json.loads(row['bindings_json']),nerd_id=nerd_id)
        except ValueError as error:
            raise HTTPException(409,'Capability mappings need attention: '+str(error)) from None


def disable(account_id,business_id,nerd_id='logistics'):
    if not businesses.configured():
        raise HTTPException(409,'The umbrella runtime is not configured')
    registry.manifest(nerd_id)
    with d.get_conn() as conn:
        conn.execute('BEGIN IMMEDIATE')
        _business(conn,account_id,business_id)
        row=conn.execute('SELECT status FROM nerd_installations WHERE nerd_id=?',(nerd_id,)).fetchone()
        if row is None:
            raise HTTPException(409,'This capability has not been installed')
        if row['status']=='enabling':
            raise HTTPException(409,'Wait for the current setup before disabling')
        conn.execute("UPDATE nerd_installations SET status='disabled' WHERE nerd_id=?",(nerd_id,))
    return {'status':'disabled','data_retained':True,'nerd_id':nerd_id}
