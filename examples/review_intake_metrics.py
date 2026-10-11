"""Large offline example: import, policy analytics, and decision replay."""
import csv, io, sys, tempfile
from pathlib import Path
from unittest.mock import patch
sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'operations_nerd'))
from db import db as d
from insights import intake, service as s
from approval.service import maybe_auto_approve, ApprovalError
with tempfile.TemporaryDirectory() as tmp:
    path=str(Path(tmp)/'demo.db'); d.init_db(path)
    with d.get_conn(path) as c:
        bid=d.create_business(c,'Synthetic CRM','realestate','0.1.0',{})
        out=io.StringIO(); w=csv.writer(out);w.writerow(['name','email','phone'])
        for i in range(1000): w.writerow([f'Contact{i}',f'person{i}@example.invalid',f'555{i:07d}'])
        for i in range(1000): w.writerow([f'Duplicate{i}',f'PERSON{i}@example.invalid',''])
        print('2000-row CRM import:',intake.import_contacts_csv(c,bid,out.getvalue()))
        print('Replay:',intake.import_contacts_csv(c,bid,out.getvalue()))
        d.set_approval_policy(c,bid,'reply',True)
        with patch('approval.service._simulated_send') as sends:
            for i in range(500):
                eid=d.create_event(c,bid,'inbound_email',f'Synthetic inquiry{i}')
                aid=d.create_drafted_action(c,eid,bid,'reply',{'body':'Synthetic reply'})
                maybe_auto_approve(c,aid)
            print('500 policy decisions:',{k:s.stats(c,bid)[k] for k in ['drafted_actions','auto_approved','human_decisions']})
            print('Mocked policy sends:',sends.call_count)
        try: s.edit_and_approve(c,aid,'Cannot edit a completed decision')
        except ApprovalError: print('Completed decision edit: blocked')
        try: intake.import_contacts_csv(c,bid,'name,email\nBroken,x@example.invalid,extra\n')
        except ValueError: print('Malformed import: blocked; contacts remain',len(d.list_contacts(c,bid)))
