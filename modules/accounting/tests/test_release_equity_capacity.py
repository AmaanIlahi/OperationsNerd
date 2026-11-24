"""Offline SQLite checks for the confirmed liability-only release journal model."""
from pathlib import Path
import sqlite3
import sys

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import accounting as a

DATE = '2026-11-17'

@pytest.fixture
def books():
    conn = a.connect(':memory:')
    a.init_db(conn)
    ids = {}
    for code, kind in [('1000', 'asset'), ('3000', 'equity'), ('3100', 'equity'),
                       ('3200', 'liability'), ('4000', 'income'), ('5000', 'expense'),
                       ('1500', 'asset'), ('3300', 'equity')]:
        ids[code] = a.add_account(conn, 'account:'+code, code, 'Test '+code, kind)['account_id']
    fid = a.add_fund(conn, 'fund', 'TEMP', 'Temporary restriction', 'temporarily_restricted')['fund_id']
    a.record_grant_receipt(conn, 'grant', fid, DATE, 100_000, ids['1000'], ids['4000'], 'Synthetic grant')
    yield conn, ids, fid
    conn.close()

def release(books, amount=40_000, key='release', clearing='3200'):
    conn, ids, fid = books
    return a.release_from_restriction(conn, key, fid, amount, DATE, 'Purpose met',
                                      ids['3100'], ids['3000'], ids[clearing])

def available(books):
    conn, _, fid = books
    return a.fund(conn, fid, as_of=DATE)['available_cents']

def snapshot(conn):
    names = [r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table' ORDER BY name")]
    return {name: list(map(tuple, conn.execute('SELECT * FROM ' + name))) for name in names}

@pytest.mark.parametrize('amount', [1, 39_999, 40_000, 99_999, 100_000])
def test_release_reduces_restricted_capacity_exactly(books, amount):
    release(books, amount)
    assert available(books) == 100_000 - amount

def test_release_uses_confirmed_four_journal_legs(books):
    conn, ids, fid = books
    entry = release(books)
    actual = {(line['account_id'], line['fund_id']): (line['debit_cents'], line['credit_cents'])
              for line in entry['lines']}
    gen = a.default_fund_id(conn)
    assert actual == {(ids['3100'], fid): (40_000, 0), (ids['3200'], fid): (0, 40_000),
                      (ids['3200'], gen): (40_000, 0), (ids['3000'], gen): (0, 40_000)}

def test_equity_clearing_is_rejected_atomically(books):
    conn, _, _ = books
    before = snapshot(conn)
    with pytest.raises(a.InvalidState, match='liability'):
        release(books, clearing='3300')
    assert snapshot(conn) == before

@pytest.mark.parametrize('clearing', ['1000', '4000', '5000'])
def test_other_nonliability_clearing_is_rejected_atomically(books, clearing):
    conn, _, _ = books
    before = snapshot(conn)
    with pytest.raises(a.InvalidState, match='liability'):
        release(books, clearing=clearing)
    assert snapshot(conn) == before

def test_second_release_cannot_reuse_released_capacity(books):
    conn, _, _ = books
    release(books, 60_000)
    before = snapshot(conn)
    with pytest.raises(a.InsufficientFundBalance):
        release(books, 40_001, key='too-much')
    assert snapshot(conn) == before
    assert available(books) == 40_000

def test_two_releases_can_exhaust_capacity_once(books):
    release(books, 60_000)
    release(books, 40_000, key='remaining')
    assert available(books) == 0
    with pytest.raises(a.InsufficientFundBalance):
        release(books, 1, key='extra')

@pytest.mark.parametrize('debit_code', ['5000', '1500'])
def test_postrelease_expense_or_capital_overspend_is_blocked(books, debit_code):
    conn, ids, fid = books
    release(books, 60_000)
    before = snapshot(conn)
    with pytest.raises(a.InsufficientFundBalance):
        a.post_journal_entry(conn, 'overspend', DATE, 'Synthetic spending', [
            {'account_id': ids[debit_code], 'debit_cents': 40_001, 'fund_id': fid},
            {'account_id': ids['1000'], 'credit_cents': 40_001, 'fund_id': fid}])
    assert snapshot(conn) == before

def test_idempotent_release_does_not_reduce_capacity_twice(books):
    conn, _, _ = books
    first = release(books)
    before = snapshot(conn)
    assert release(books) == first
    assert snapshot(conn) == before
    assert available(books) == 60_000

def test_failed_clearing_attempt_does_not_consume_key(books):
    with pytest.raises(a.InvalidState):
        release(books, clearing='3300')
    release(books)
    assert available(books) == 60_000

def test_balanced_per_fund_and_no_cash_movement(books):
    conn, ids, _ = books
    entry = release(books)
    totals = {}
    for line in entry['lines']:
        totals[line['fund_id']] = totals.get(line['fund_id'], 0) + line['debit_cents'] - line['credit_cents']
    assert all(value == 0 for value in totals.values())
    assert ids['1000'] not in [line['account_id'] for line in entry['lines']]

def test_generic_reversal_remains_disallowed_atomically(books):
    conn, _, _ = books
    entry = release(books)
    before = snapshot(conn)
    with pytest.raises(a.InvalidState, match='only a manual or payroll'):
        a.reverse_journal_entry(conn, entry['entry_id'], 'undo-release', entry_date=DATE)
    assert snapshot(conn) == before
    assert available(books) == 60_000


def test_inactive_liability_clearing_is_rejected_atomically(books):
    conn, ids, _ = books
    a.update_account(conn, ids['3200'], {'is_active': False}, 'disable-clearing')
    before = snapshot(conn)
    with pytest.raises(a.InvalidState):
        release(books)
    assert snapshot(conn) == before
