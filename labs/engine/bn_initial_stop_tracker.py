"""Two independent, forward-only paper books; never broker execution.

Original five-minute signals remain shadow-tracked after protective exits.
Initial stop50, permanently removed on a completed +25 favourable minute close.
Protective signals fill at the next valid minute quote. No reclaim re-entry.
"""
from __future__ import annotations
from datetime import datetime,time,timedelta
import math
import pandas as pd
from labs.engine import bn_alpha_tracker as core
from labs.engine import bn_selective_tracker as selective
from storage.db import get_conn

FIRST_FORWARD='2026-11-11'
BUFFER=50.0
RELEASE=25.0
BOOKS={
    'bn_alpha_guard':dict(label='Bank Nifty Alpha Guard',source='bn_alpha',selective=False),
    'bn_selective_guard':dict(label='Bank Nifty Selective Guard',source='bn_selective',selective=True),
}
VERSION='bn_initial_stop50_release25_1min_nextquote_shadow_noreentry_v1'

def naive(now):
    return now.astimezone(core.IST).replace(tzinfo=None) if now.tzinfo else now

def valid_quote(q):
    return q is not None and all(v is not None and math.isfinite(float(v)) for v in q[:2]) and 0<float(q[0])<=float(q[1])

def minute_quotes(frame,now=None):
    f=frame.copy();ts=pd.to_datetime(f.timestamp)
    if ts.dt.tz is not None:ts=ts.dt.tz_convert(core.IST).dt.tz_localize(None)
    f['minute']=ts.dt.floor('min');f['type']=f.option_type.astype(str).str.lower()
    if now is not None:f=f[f.minute<=naive(now)]
    f=f.sort_values('timestamp').groupby(['minute','strike','type'],as_index=False).last()
    return {(pd.Timestamp(r.minute).to_pydatetime(),float(r.strike),r.type):(r.bid,r.ask,r.tradingsymbol) for r in f.itertuples(index=False)}

def protect(shadow,*,closed,bars,quotes,marks,alpha,now=None):
    """Pure overlay for one causal original signal window, closed or still open."""
    t=dict(shadow);entry=t['entry_ts'];anchor=t['entry_spot'];sgn=1 if t['side']=='CALL' else -1
    strike=float(round(anchor/core.STRIKE_STEP)*core.STRIKE_STEP);typ='ce' if sgn==1 else 'pe'
    terminal=t['exit_ts'] if closed else None
    cutoff=naive(now) if now is not None else None
    available=[m for m in sorted(bars) if m>=entry and (cutoff is None or m+timedelta(minutes=1)<=cutoff)]
    current=[m for m in available if terminal is None or m<=terminal]
    released=False;signal=None
    for m in current:
        if m<=entry or (terminal is not None and m>=terminal):continue
        favorable=(float(bars[m][3])-anchor)*sgn
        if favorable>=RELEASE:released=True
        if not released and favorable<=-BUFFER:signal=m;break
    state='released' if released else ('stop_pending_quote' if signal else 'initial_stop_active')
    if signal is not None:
        fills=[(m,q) for (m,k,opt),q in quotes.items() if k==strike and opt==typ
               and m>=signal+timedelta(minutes=1) and (terminal is None or m<terminal)
               and (cutoff is None or m<=cutoff) and valid_quote(q)]
        if fills:
            fill,q=min(fills,key=lambda x:x[0])
            px=float(bars[fill][3]) if fill in bars and (cutoff is None or fill+timedelta(minutes=1)<=cutoff) else float(bars[signal][3])
            # Spot is a completed-bar annotation; actual option bid determines P&L.
            t.update(exit_ts=fill,exit_spot=px,exit_reason='INITIAL_SPOT_STOP',spot_pts=round((px-anchor)*sgn,2))
            prior=[i for i,m in enumerate(marks) if m<=signal]
            t['exit_alpha']=float(alpha[prior[-1]]) if prior else t['entry_alpha']
            return t,True,'stopped'
    if not closed and current:
        last=current[-1];t.update(exit_ts=last,exit_spot=float(bars[last][3]),exit_reason='holding',
                                  spot_pts=round((float(bars[last][3])-anchor)*sgn,2))
        prior=[i for i,m in enumerate(marks) if m<=last]
        if prior:t['exit_alpha']=float(alpha[prior[-1]])
    return t,closed,state

def apply_overlay(inp,bars,quotes,*,selective_book=False,now=None):
    closed,opened=(selective.simulate(inp) if selective_book else core.simulate(inp))
    trades=[];states=[]
    for shadow,is_closed in [(t,True) for t in closed]+([(opened,False)] if opened else []):
        t,done,state=protect(shadow,closed=is_closed,bars=bars,quotes=quotes,marks=inp['marks'],alpha=inp['alpha'],now=now)
        pricing=dict(inp,quotes=quotes)
        # Entries and ordinary closed exits retain the original five-minute fills.
        if t['exit_reason']!='INITIAL_SPOT_STOP':pricing['quotes']={**quotes,**inp['quotes']}
        priced=core.price_trade(t,pricing)
        trades.append(dict(priced,status='closed' if done else 'open'));states.append(state)
    return trades,states

def locked_close(conn,key,day,kite=None):
    for prefix in [key,BOOKS[key]['source'],'bn_alpha']:
        try:
            row=conn.execute(f"SELECT prev_close,prev_close_date,prev_close_source FROM {prefix}_daily WHERE trade_date=? AND prev_close_source='kite_daily'",(day,)).fetchone()
        except Exception as exc:
            if 'no such table' not in str(exc):raise
            row=None
        if row:return tuple(row)
    return core.prev_close(day,kite)

def run_day(day=None,*,now=None,connection=None,persist=True,kite=None):
    """Refresh both books; only their own daily/trade tables may be written."""
    now=now or datetime.now(core.IST);day=day or now.astimezone(core.IST).date().isoformat()
    if day<FIRST_FORWARD:return dict(trade_date=day,status='before_forward_start')
    own=connection is None;conn=connection or get_conn()
    try:
        for key in BOOKS:core._ensure_tables(conn,key)
        cutoff=now if day==now.astimezone(core.IST).date().isoformat() else None
        frame=core.options_frame(day);bars=core.spot_bars(day)
        if not bars:raise core.BnAlphaInputError(f'No BANKNIFTY minute spot bars for {day}')
        expiry=core.nearest_expiry(frame.expiry.astype(str).str.upper().unique(),day)
        if not expiry:raise core.BnAlphaInputError(f'No BANKNIFTY expiry on {day}')
        base,base_date=core.baseline(day,expiry)
        quotes=minute_quotes(frame[frame.expiry.astype(str).str.upper()==expiry],now=cutoff)
        results={}
        for key,meta in BOOKS.items():
            close,close_date,source=locked_close(conn,key,day,kite)
            inp=core.session_inputs(day,close=close,frame=frame,bars=bars,base=base,now=cutoff)
            trades,states=apply_overlay(inp,bars,quotes,selective_book=meta['selective'],now=cutoff)
            r=dict(inp=inp,trades=trades,prev_close=close,prev_close_date=close_date,prev_close_source=source,baseline_date=base_date)
            note='Forward paper only from '+FIRST_FORWARD+'; 1-minute initial stop50 / release25; shadow original signals; '+','.join(states)
            if persist:core._save(conn,day,r,prefix=key,version=VERSION,note=note)
            done=[t for t in trades if t['status']=='closed']
            results[key]=dict(trades=len(trades),open=sum(t['status']=='open' for t in trades),net_rs=round(sum(t['net_rs'] or 0 for t in done),2),risk_states=states)
        return dict(trade_date=day,status='ok',books=results)
    except (core.BnAlphaInputError,FileNotFoundError) as exc:
        # A transient capture gap must never erase already recorded paper trades.
        return dict(trade_date=day,status='waiting_for_data',error=str(exc))
    finally:
        if own:conn.close()

def run_live(now=None):
    now=(now or datetime.now(core.IST)).astimezone(core.IST)
    if now.weekday()>=5 or not time(9,16)<=now.time()<=time(15,45):return None
    return run_day(now=now)

def tab_data(conn,key,date_clause='',date_params=()):
    if key not in BOOKS:raise ValueError('Unknown guarded book')
    return core.tab_data(conn,date_clause,date_params,prefix=key,first_unseen=FIRST_FORWARD)
