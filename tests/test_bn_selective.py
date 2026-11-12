import sqlite3
from datetime import datetime,timedelta
import numpy as np
from labs.engine import bn_selective_tracker as book
from labs.engine import bn_alpha_tracker as core

def inp(a,s=None,done=True):
    s=s or [55600.]*len(a)
    return dict(alpha=np.array(a,float),spot=np.array(s,float),win_high=np.array(s,float),win_low=np.array(s,float),
        marks=[datetime(2026,11,9,9,15)+timedelta(minutes=5*i) for i in range(len(a))],session_done=done,
        quotes={},expiry='26NOV',range_lo=55200,range_hi=56000)

def test_fresh_cross_must_land_at_60_and_later_continuation_is_rejected():
    assert book.simulate(inp([0,35,65,80]))[0]==[]
    t,_=book.simulate(inp([0,60,80]))
    assert len(t)==1 and t[0]['entry_alpha']==60
    t,_=book.simulate(inp([0,-60,-80]))
    assert len(t)==1 and t[0]['side']=='PUT'
    assert book.simulate(inp([0,201,70]))[0]==[]

def test_cap_stops_after_first_trade_whether_win_or_loss():
    for prices in [[55600,55600,55630,55600,55600],[55600,55600,55570,55600,55600]]:
        t,_=book.simulate(inp([0,65,-35,0,65],prices))
        assert len(t)==1 and t[0]['exit_reason']=='ALPHA_SL'

def test_original_trail_and_open_position_are_preserved():
    t,_=book.simulate(inp([0,65,70,70],[55600,55600,55720,55650]))
    assert t[0]['exit_reason']=='TRAIL' and t[0]['spot_pts']==70
    t,opened=book.simulate(inp([0,65,70],done=False))
    assert not t and opened['status']=='open'

def test_new_ledger_is_isolated_and_forward_start_is_honest():
    conn=sqlite3.connect(':memory:')
    core._ensure_tables(conn);core._ensure_tables(conn,book.KEY)
    conn.execute("INSERT INTO bn_alpha_daily(trade_date,status,strategy_version,updated_at) VALUES ('2026-11-09','no_trade','original','saved')")
    i=inp([0,65,70]);closed,_=book.simulate(i)
    t=core.price_trade(closed[0],i);t.update(status='closed',net_rs=-100.,gross_rs=-50.,charges_rs=50.)
    core._save(conn,'2026-11-09',dict(inp=i,trades=[t],prev_close=55600.,prev_close_date='2026-11-06',
        prev_close_source='kite_daily',baseline_date='2026-11-06'),prefix=book.KEY,version=book.STRATEGY_VERSION)
    rows,trades,stats=book.tab_data(conn)
    assert len(rows)==len(trades)==1 and stats['first_unseen']=='2026-11-11'
    assert stats['unseen_trades']==0 and stats['max_dd_rs']==100.
    assert conn.execute('SELECT strategy_version FROM bn_alpha_daily').fetchone()[0]=='original'

def test_registered_in_reports_overview_and_loop():
    from labs.ui.routes import LIVE_TABS
    from labs.services.book_overview_more import MORE_BOOKS
    from labs.services.strategy_report import SOURCES
    from pathlib import Path
    root=Path(__file__).resolve().parents[1]
    assert LIVE_TABS[book.KEY]==book.LABEL==MORE_BOOKS[book.KEY]['label']
    assert SOURCES[book.KEY].table=='bn_selective_trades'
    assert '("bn_selective", run_bn_selective_day)' in (root/'pa_paper_tracker_loop.py').read_text(encoding='utf-8')
    import jinja2
    env=jinja2.Environment(loader=jinja2.FileSystemLoader(root/'templates'))
    html=env.get_template('_bn_selective.html').render(bn_selective_stats={},bn_selective_rows=[])
    assert 'Bank Nifty Selective' in html and '11 November 2026' in html
