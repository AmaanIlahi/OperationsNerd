import csv, io, sys, threading
from pathlib import Path
from unittest.mock import patch
from concurrent.futures import ThreadPoolExecutor
import pytest
sys.path.insert(0,str(Path(__file__).parents[1]))
from db import db as d
from insights import intake, service as s
from approval.service import approve_and_send, ApprovalError

@pytest.fixture
def store(tmp_path):
    path=str(tmp_path/'large.db'); d.init_db(path)
    with d.get_conn(path) as c:
        bid=d.create_business(c,'R','realestate','0.1.0',{})
        eid=d.create_event(c,bid,'inbound_email','hello')
        aid=d.create_drafted_action(c,eid,bid,'reply',{'body':'Original'})
    return path,bid,aid

def test_edit_race_preserves_only_winning_review(store):
    path,bid,aid=store; original=d.get_drafted_action; gate=threading.Barrier(2); local=threading.local()
    def read(c,i):
        a=original(c,i)
        if not getattr(local,'read',False): local.read=True; gate.wait(timeout=3)
        return a
    def run(edit):
        try:
            with d.get_conn(path) as c: return s.edit_and_approve(c,aid,'Edited') if edit else approve_and_send(c,aid)
        except ApprovalError: return None
    with patch('db.db.get_drafted_action',side_effect=read),patch('insights.service._simulated_send') as edited,patch('approval.service._simulated_send') as normal:
        with ThreadPoolExecutor(2) as pool:
            a=pool.submit(run,True); b=pool.submit(run,False); results=[a.result(),b.result()]
        assert sum(x is not None for x in results)==1
        assert edited.call_count+normal.call_count==1
    with d.get_conn(path) as c:
        assert c.execute('SELECT count(*) FROM audit_log WHERE action_id=?',(aid,)).fetchone()[0]==1

@pytest.mark.parametrize('text',['name,email\nA,a@x.invalid,overflow\n','name,name\nA,B\n',' NAME ,name\nA,B\n','name,email\n"unterminated,a@x.invalid'])
def test_malformed_csv_rejected_before_any_writes(store,text):
    path,bid,_=store
    with d.get_conn(path) as c:
        with pytest.raises(ValueError): intake.import_contacts_csv(c,bid,text)
        assert d.list_contacts(c,bid)==[]

def test_large_import_and_repeat(store):
    path,bid,_=store; out=io.StringIO(); w=csv.writer(out); w.writerow(['name','email','phone'])
    for i in range(1000): w.writerow([f'Contact {i}',f'contact{i}@example.invalid',f'555{i:07d}'])
    for i in range(1000): w.writerow([f'Duplicate {i}',f'CONTACT{i}@example.invalid',''])
    with d.get_conn(path) as c:
        result=intake.import_contacts_csv(c,bid,out.getvalue())
        assert result['added']==1000 and result['duplicates_skipped']==1000
        assert intake.import_contacts_csv(c,bid,out.getvalue())['added']==0

def test_auto_metrics_survive_final_sent_status(store):
    path,bid,aid=store
    with d.get_conn(path) as c:
        d.set_approval_policy(c,bid,'reply',True)
        from approval.service import maybe_auto_approve
        with patch('approval.service._simulated_send'): maybe_auto_approve(c,aid)
        st=s.stats(c,bid)
        assert st['auto_approved']==1 and st['human_decisions']==0

def test_stats_exclude_unfinished_approved_from_sent(store):
    path,bid,aid=store
    with d.get_conn(path) as c:
        d.set_action_status(c,aid,'approved')
        assert s.stats(c,bid)['by_action_type']['reply']['sent']==0
