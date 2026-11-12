from datetime import datetime,timedelta
import sqlite3
from unittest.mock import patch
import pytest
from labs.engine import bn_initial_stop_tracker as G

BASE=datetime(2026,11,11,9,20)
def fixture(values,closed=True):
    times=[BASE+timedelta(minutes=i) for i in range(len(values))]
    bars={t:(v,v,v,v) for t,v in zip(times,values)}
    q={(t,60000.,'ce'):(100.+i,101.+i,'TESTCE') for i,t in enumerate(times)}
    t=dict(side='CALL',entry_ts=BASE,entry_spot=60000.,entry_alpha=66.,
           exit_ts=times[-1],exit_spot=float(values[-1]),exit_alpha=0.,exit_reason='EOD' if closed else 'open',
           spot_pts=float(values[-1]-60000),entry_i=0,exit_i=1)
    return t,bars,q,times

def protect(values,closed=True,now=None):
    t,b,q,times=fixture(values,closed)
    return G.protect(t,closed=closed,bars=b,quotes=q,marks=[BASE],alpha=[66.],now=now)

def test_stop_fills_next_minute_and_never_entry_bar():
    t,done,state=protect([60000,59949,59940,59930,59920])
    assert t['exit_ts']==BASE+timedelta(minutes=2) and done and state=='stopped'

def test_release_permanent_after_observed25():
    t,done,state=protect([60000,60025,59949,59940,59920])
    assert t['exit_reason']=='EOD' and state=='released'

def test_exact_thresholds():
    assert protect([60000,59951,59960,59970])[0]['exit_reason']=='EOD'
    assert protect([60000,59950,59960,59970])[0]['exit_reason']=='INITIAL_SPOT_STOP'

def test_incomplete_favourable_bar_cannot_release_stop():
    t,b,q,times=fixture([60000,60025,59940,59930],False)
    out,done,state=G.protect(t,closed=False,bars=b,quotes=q,marks=[BASE],alpha=[66.],now=BASE+timedelta(minutes=1,seconds=30))
    assert not done and state=='initial_stop_active' and out['exit_ts']==BASE

def test_pending_quote_closes_open_shadow_without_new_five_minute_mark():
    t,b,q,times=fixture([60000,59940,59930,59920],False)
    out,done,state=G.protect(t,closed=False,bars=b,quotes=q,marks=[BASE],alpha=[66.],now=BASE+timedelta(minutes=2,seconds=10))
    assert done and out['exit_ts']==BASE+timedelta(minutes=2)

def test_missing_quote_waits_and_terminal_has_priority():
    t,b,q,times=fixture([60000,59940,59930,59920,59910])
    del q[(times[2],60000.,'ce')]
    out,done,state=G.protect(t,closed=True,bars=b,quotes=q,marks=[BASE],alpha=[66.])
    assert out['exit_ts']==times[3]
    del q[(times[3],60000.,'ce')]
    out,done,state=G.protect(t,closed=True,bars=b,quotes=q,marks=[BASE],alpha=[66.])
    assert out['exit_reason']=='EOD'

def test_put_mirrors_thresholds():
    t,b,q,times=fixture([60000,60050,60060,60070]);t['side']='PUT'
    q={(m,k,'pe'):v for (m,k,opt),v in q.items()}
    out,done,state=G.protect(t,closed=True,bars=b,quotes=q,marks=[BASE],alpha=[-66.])
    assert done and state=='stopped' and out['exit_ts']==times[2]

def test_forward_boundary_no_read_or_write():
    with patch.object(G,'get_conn',side_effect=AssertionError('must not open DB')):
        assert G.run_day('2026-11-10')['status']=='before_forward_start'

def test_table_isolation_and_sources():
    from labs.services.strategy_report import SOURCES
    c=sqlite3.connect(':memory:')
    for key in G.BOOKS:G.core._ensure_tables(c,key)
    tables={r[0] for r in c.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    assert tables=={k+s for k in G.BOOKS for s in ['_daily','_trades']}
    assert all(SOURCES[k].table==k+'_trades' for k in G.BOOKS)

def test_templates_and_tab_registration():
    from pathlib import Path
    from jinja2 import Environment,FileSystemLoader
    root=Path(__file__).resolve().parents[1]
    env=Environment(loader=FileSystemLoader(str(root/'templates')))
    env.parse((root/'templates/live_strategy.html').read_text(encoding='utf-8'))
    env.parse((root/'templates/_bn_initial_stop.html').read_text(encoding='utf-8'))
    routes=(root/'labs/ui/routes.py').read_text(encoding='utf-8')
    assert all('"'+k+'"' in routes for k in G.BOOKS)

def test_persist_refresh_both_books_without_changing_controls(monkeypatch):
    import pandas as pd
    import numpy as np
    c=sqlite3.connect(':memory:');c.row_factory=sqlite3.Row
    for key in ['bn_alpha','bn_selective']:
        G.core._ensure_tables(c,key)
        c.execute(f"INSERT INTO {key}_daily (trade_date,status,strategy_version,updated_at) VALUES (?,?,?,?)",('2026-11-10','no_trade','control','2026-11-10'))
    c.commit()
    before={k:list(map(tuple,c.execute(f'SELECT * FROM {k}_daily'))) for k in ['bn_alpha','bn_selective']}
    t,b,q,times=fixture([60000,59940,59930,59920,59910,59900,59900,59900,59900,59900,59900],False)
    marks=[BASE-timedelta(minutes=5),BASE,BASE+timedelta(minutes=5)]
    inp=dict(day='2026-11-11',expiry='26NOV',range_lo=59600,range_hi=60400,marks=marks,
             alpha=np.array([0.,66.,60.]),spot=np.array([60000.,60000.,59900.]),
             win_high=np.array([60000.,60000.,59940.]),win_low=np.array([60000.,60000.,59900.]),
             quotes=q,session_done=False)
    frame=pd.DataFrame([dict(timestamp=m,expiry='26NOV',strike=k,option_type=typ,bid=v[0],ask=v[1],tradingsymbol=v[2]) for (m,k,typ),v in q.items()])
    monkeypatch.setattr(G.core,'options_frame',lambda day:frame)
    monkeypatch.setattr(G.core,'spot_bars',lambda day:b)
    monkeypatch.setattr(G.core,'baseline',lambda *a:(pd.DataFrame(),'2026-11-10'))
    monkeypatch.setattr(G.core,'session_inputs',lambda *a,**kw:inp)
    monkeypatch.setattr(G,'locked_close',lambda *a:(60000.,'2026-11-10','kite_daily'))
    out=G.run_day('2026-11-11',now=datetime(2026,11,11,9,29,tzinfo=G.core.IST),connection=c)
    assert out['status']=='ok'
    for key in G.BOOKS:
        rows=c.execute(f'SELECT * FROM {key}_trades').fetchall()
        assert len(rows)==1 and rows[0]['exit_reason']=='INITIAL_SPOT_STOP' and rows[0]['status']=='closed'
    for key,rows in before.items():assert rows==list(map(tuple,c.execute(f'SELECT * FROM {key}_daily')))

def test_background_worker_is_singleton_and_independent(monkeypatch):
    import threading
    import pa_bn_initial_stop_loop as loop
    class Worker:
        def __init__(self,**kwargs):self.kwargs=kwargs;self.alive=False
        def start(self):self.alive=True
        def is_alive(self):return self.alive
    monkeypatch.setattr(threading,'Thread',Worker);monkeypatch.setattr(loop,'_worker',None)
    a=loop.start_background();b=loop.start_background()
    assert a is b and a.kwargs['daemon'] and a.kwargs['target'] is loop._supervise
