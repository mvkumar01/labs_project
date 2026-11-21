"""Nifty Omega backfill: sessions before the forward start are replayed with the backfill version and labelled."""
from datetime import datetime, time

import jinja2

from labs.engine import nifty_omega_book as book
from labs.services import strategy_report as sr
from live.engine import nifty_omega as omega


def test_backfill_only_touches_sessions_before_the_forward_start(monkeypatch):
    seen, saved = [], []
    monkeypatch.setattr(book, '_record', lambda day, asof, b: seen.append((day, asof, b.version)) or {'trades': 1})

    class Conn:
        def close(self):
            pass
    monkeypatch.setattr(book, 'get_conn', lambda: Conn())
    monkeypatch.setattr(book.twins, '_ensure_tables', lambda conn, key: None)
    monkeypatch.setattr(book.twins, '_save', lambda conn, day, b, r, error=None: saved.append((day, b.version, error)))
    days = ['2026-06-02', '2026-11-11', omega.START, '2026-11-20']
    out = book.run_backfill('2026-06-01', '2026-11-30', sessions=lambda s, e: [d for d in days if s <= d <= e])
    assert [d for d, _, _ in seen] == ['2026-06-02', '2026-11-11']
    assert all(v == book.BACKFILL_VERSION and v.endswith('+backfill') for _, _, v in seen)
    assert seen[0][1] == datetime.combine(datetime(2026, 6, 2).date(), time(15, 30))
    assert set(out['done']) == {'2026-06-02', '2026-11-11'} and not out['failed'] and not saved


def test_backfill_records_a_failed_session_as_unavailable(monkeypatch):
    def boom(day, asof, b):
        raise omega.core.BnAlphaInputError('No NIFTY option quotes')
    saved = []

    class Conn:
        def close(self):
            pass
    monkeypatch.setattr(book, '_record', boom)
    monkeypatch.setattr(book, 'get_conn', lambda: Conn())
    monkeypatch.setattr(book.twins, '_ensure_tables', lambda conn, key: None)
    monkeypatch.setattr(book.twins, '_save', lambda conn, day, b, r, error=None: saved.append((day, b.version, error)))
    out = book.run_backfill('2026-06-01', None, sessions=lambda s, e: ['2026-06-03'])
    assert out['failed']['2026-06-03'].startswith('BnAlphaInputError')
    assert saved == [('2026-06-03', book.BACKFILL_VERSION, out['failed']['2026-06-03'])]


def test_forward_day_is_still_refused_before_the_start():
    assert book.run_paper_day('2026-11-11', now=datetime(2026, 11, 11, 15, 30)) is None
    assert book.is_backfill('2026-11-11') and not book.is_backfill(omega.START)


def test_report_marks_backfill_trades_and_adds_a_note():
    raw = [dict(id=1, trade_date='2026-11-11', status='closed', side='CALL', entry_ts='2026-11-11T09:31',
                exit_ts='2026-11-11T09:50', net_rs=100.0, gross_rs=150.0, charges_rs=50.0, qty=130, entry_ask=100.0,
                exit_bid=101.0, tradingsymbol='NIFTY25300CE'),
           dict(id=2, trade_date='2026-11-12', status='closed', side='PUT', entry_ts='2026-11-12T09:31',
                exit_ts='2026-11-12T09:50', net_rs=-50.0, gross_rs=0.0, charges_rs=50.0, qty=130, entry_ask=100.0,
                exit_bid=100.0, tradingsymbol='NIFTY25300PE')]
    rep = sr.make_report(raw, sr.SOURCES['nifty_omega'], 'nifty_omega', 'Nifty Omega')
    flags = {t['date']: t['backfill'] for t in rep['trades']}
    assert flags == {'2026-11-11': True, '2026-11-12': False}
    assert any(n.startswith('Backfill:') for n in rep['notes'])
    other = sr.make_report(raw, sr.SOURCES['nna_c1'], 'nna_c1', 'NNA')
    assert not any(t['backfill'] for t in other['trades']) and not any(n.startswith('Backfill') for n in other['notes'])


def test_tab_template_labels_backfill_rows():
    env = jinja2.Environment(loader=jinja2.FileSystemLoader('templates'))
    html = env.get_template('_nifty_omega.html').render(
        nna_tw_stats=dict(backfill_before='2026-11-12', trades=3, unseen_trades=1, net_rs=900.0, unseen_net=-100.0),
        nna_tw_rows=[dict(trade_date='2026-11-12', status='traded', n_trades=1, net_rs=-100.0),
                     dict(trade_date='2026-11-11', status='traded', n_trades=2, net_rs=1000.0)],
        nna_tw_trades=[dict(trade_date='2026-11-11', tradingsymbol='X', entry_ts='t', lotA_ts=None, exit_ts=None, net_rs=1.0)])
    assert html.count('<span class="muted">backfill</span>') == 2
    assert 'Backfill before 2026-11-12: 2 trades, &#8377;1,000' in html and 'Forward paper from 2026-11-12: 1 trades' in html
