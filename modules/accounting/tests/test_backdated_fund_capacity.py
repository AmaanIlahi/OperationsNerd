import importlib.util
import os
from pathlib import Path
import socket
import unittest
from unittest.mock import patch

folder=Path(__file__).resolve().parents[1]
spec=importlib.util.spec_from_file_location('acct_backdate_test', folder/os.environ.get('ACCOUNTING_TEST_MODULE','accounting.py'))
a=importlib.util.module_from_spec(spec);spec.loader.exec_module(a)

class FutureFundCapacityTests(unittest.TestCase):
    def setUp(self):
        self.net=patch.object(socket.socket,'connect',side_effect=AssertionError('network forbidden'))
        self.net.start();self.addCleanup(self.net.stop)
        self.c=a.connect(':memory:');self.addCleanup(self.c.close);a.init_db(self.c)
        self.bank=a.add_account(self.c,'bank','1000','Cash','asset')['account_id']
        self.rev=a.add_account(self.c,'rev','4000','Grants','income')['account_id']
        self.exp=a.add_account(self.c,'exp','5000','Costs','expense')['account_id']
        self.f=a.add_fund(self.c,'fund','F','Restricted','temporarily_restricted')['fund_id']
        self.grant(100,'2026-01-01','grant')
    def grant(self,n,d,k,f=None):
        return a.record_grant_receipt(self.c,k,f or self.f,d,n,self.bank,self.rev,k)
    def spend(self,n,d,k,f=None):
        f=f or self.f
        return a.post_journal_entry(self.c,k,d,k,[{'account_id':self.exp,'debit_cents':n,'fund_id':f},{'account_id':self.bank,'credit_cents':n,'fund_id':f}])
    def counts(self):
        return tuple(self.c.execute('SELECT count(*) FROM '+t).fetchone()[0] for t in ['acct_journal_entries','acct_journal_lines','acct_commands'])
    def seed_later(self):self.spend(80,'2026-01-10','later')
    def test_backdated_spend_rejected(self):
        self.seed_later()
        with self.assertRaises(a.InsufficientFundBalance):self.spend(30,'2026-01-05','bad')
    def test_failed_spend_rolls_back_all_tables(self):
        self.seed_later();before=self.counts()
        try:self.spend(30,'2026-01-05','bad')
        except a.InsufficientFundBalance:pass
        self.assertEqual(self.counts(),before)
    def test_failed_key_remains_available(self):
        self.seed_later()
        try:self.spend(30,'2026-01-05','retry')
        except a.InsufficientFundBalance:pass
        result=self.spend(20,'2026-01-05','retry')
        self.assertEqual(result['total_cents'],20)
    def test_exact_future_capacity_allowed(self):
        self.seed_later();self.spend(20,'2026-01-05','exact')
        self.assertEqual(a.fund(self.c,self.f,'2026-01-10')['available_cents'],0)
    def test_later_grant_does_not_mask_intermediate_deficit(self):
        self.seed_later();self.grant(100,'2026-01-20','topup')
        with self.assertRaises(a.InsufficientFundBalance):self.spend(30,'2026-01-05','masked')
    def test_multiple_future_spend_dates(self):
        self.spend(30,'2026-01-10','first');self.spend(50,'2026-01-15','second')
        with self.assertRaises(a.InsufficientFundBalance):self.spend(21,'2026-01-05','bad')
    def test_one_cent_future_deficit(self):
        self.seed_later()
        with self.assertRaises(a.InsufficientFundBalance):self.spend(21,'2026-01-05','onecent')
    def test_future_date_report_unchanged_after_failure(self):
        self.seed_later();before=a.fund(self.c,self.f,'2026-01-10')
        try:self.spend(30,'2026-01-05','bad')
        except a.InsufficientFundBalance:pass
        self.assertEqual(a.fund(self.c,self.f,'2026-01-10'),before)
    def test_replay_allowed_entry_no_duplicate(self):
        self.seed_later();first=self.spend(20,'2026-01-05','ok');before=self.counts()
        self.assertEqual(self.spend(20,'2026-01-05','ok'),first);self.assertEqual(self.counts(),before)
    def test_unrestricted_spend_unchanged(self):
        self.spend(999,'2026-01-05','general',a.default_fund_id(self.c))
    def test_other_fund_capacity_is_not_shared(self):
        other=a.add_fund(self.c,'other','O','Other','temporarily_restricted')['fund_id']
        self.grant(1000,'2026-01-01','big',other);self.seed_later()
        with self.assertRaises(a.InsufficientFundBalance):self.spend(30,'2026-01-05','bad')
    def test_other_fund_valid_spend_unaffected(self):
        other=a.add_fund(self.c,'other','O','Other','temporarily_restricted')['fund_id']
        self.grant(1000,'2026-01-01','big',other);self.seed_later();self.spend(900,'2026-01-05','ok',other)
        self.assertEqual(a.fund(self.c,self.f,'2026-01-10')['available_cents'],20)
    def test_same_day_exact_capacity_allowed(self):
        self.seed_later();self.spend(20,'2026-01-10','same')
        self.assertEqual(a.fund(self.c,self.f,'2026-01-10')['available_cents'],0)
    def test_forward_overspend_still_rejected(self):
        self.seed_later()
        with self.assertRaises(a.InsufficientFundBalance):self.spend(21,'2026-01-11','forward')
    def test_safe_backdate_before_later_grant(self):
        self.seed_later();self.grant(10,'2026-01-20','topup');self.spend(20,'2026-01-05','ok')
        self.assertEqual(a.fund(self.c,self.f,'2026-01-20')['available_cents'],10)
    def test_no_future_entries_valid(self):
        self.spend(40,'2026-01-02','normal')
        self.assertEqual(a.fund(self.c,self.f,'2026-01-02')['available_cents'],60)

    def test_backdated_capital_purchase_rejected(self):
        asset=a.add_account(self.c,'asset','1100','Equipment','asset')['account_id']
        self.seed_later()
        with self.assertRaises(a.InsufficientFundBalance):
            a.post_journal_entry(self.c,'capital','2026-01-05','Equipment',[
                {'account_id':asset,'debit_cents':30,'fund_id':self.f},
                {'account_id':self.bank,'credit_cents':30,'fund_id':self.f}])
    def test_multiple_funds_entry_rolls_back_atomically(self):
        other=a.add_fund(self.c,'other','O','Other','temporarily_restricted')['fund_id']
        self.grant(1000,'2026-01-01','big',other);self.seed_later();before=self.counts()
        lines=[]
        for fid in [other,self.f]:
            lines.extend([{'account_id':self.exp,'debit_cents':30,'fund_id':fid},
                          {'account_id':self.bank,'credit_cents':30,'fund_id':fid}])
        try:a.post_journal_entry(self.c,'both','2026-01-05','Both',lines)
        except a.InsufficientFundBalance:pass
        self.assertEqual(self.counts(),before)
    def test_backdated_income_reversal_cannot_invalidate_future_spend(self):
        entry=a.post_journal_entry(self.c,'income','2026-01-03','Extra income',[
            {'account_id':self.bank,'debit_cents':50,'fund_id':self.f},
            {'account_id':self.rev,'credit_cents':50,'fund_id':self.f}])
        self.spend(130,'2026-01-10','spend')
        with self.assertRaises(a.InsufficientFundBalance):
            a.reverse_journal_entry(self.c,entry['entry_id'],'reverse','2026-01-05')
    def test_error_identifies_future_deficit_date(self):
        self.seed_later()
        with self.assertRaisesRegex(a.InsufficientFundBalance,'2026-01-10'):
            self.spend(30,'2026-01-05','bad')

if __name__=='__main__':unittest.main()
