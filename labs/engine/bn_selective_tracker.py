"""Bank Nifty Selective: independent paper ledger, never broker orders.

PC +/-400; fresh +/-30 cross already at |alpha|>=60 (<=200); one trade/day.
Original BN exits: trail100/50, alpha target100, adverse alpha-30, EOD15:25.
One losing trade/day is implied by the one-trade cap. Historical rows are
replays; genuine forward comparison starts 11 November 2026.
"""
from __future__ import annotations
from datetime import datetime,time,timedelta
import sqlite3
from labs.engine import bn_alpha_tracker as core
from storage.db import get_conn

KEY='bn_selective'
LABEL='Bank Nifty Selective'
STRATEGY_VERSION='bn_selective_pc400_cross30_land60_max1_sl30_trail100_50_v1'
FIRST_FORWARD='2026-11-11'
PAPER_START=core.PAPER_START
LANDING=60.0
MAX_TRADES=1
MAX_LOSSES=1


def simulate(inp):
    a,s,h,l,marks=inp['alpha'],inp['spot'],inp['win_high'],inp['win_low'],inp['marks']
    trades=[];pos=0;entry=best=0.;ei=0;entries=0
    def record(i,px,why):
        return dict(side='CALL' if pos==1 else 'PUT',entry_ts=marks[ei],entry_spot=float(entry),
            entry_alpha=float(a[ei]),exit_ts=marks[i],exit_spot=float(px),exit_alpha=float(a[i]),
            exit_reason=why,spot_pts=round(float((px-entry)*pos),2),entry_i=ei,exit_i=i)
    for i in range(1,len(a)):
        if pos:
            if (best-entry)*pos>=core.TRAIL_ARM:
                stop=best-core.TRAIL_DIST*pos
                if (pos==1 and l[i]<=stop) or (pos==-1 and h[i]>=stop):
                    trades.append(record(i,min(stop,s[i-1]) if pos==1 else max(stop,s[i-1]),'TRAIL'))
                    pos=0;continue
            best=max(best,h[i]) if pos==1 else min(best,l[i])
            if abs(a[i])<=core.ALPHA_SKIP:
                if a[i]*pos>=core.ALPHA_TGT:
                    trades.append(record(i,s[i],'ALPHA_TGT'));pos=0
                elif a[i]*pos<=core.ALPHA_SL:
                    trades.append(record(i,s[i],'ALPHA_SL'));pos=0
            continue
        if entries>=MAX_TRADES:continue
        pa,ca=a[i-1],a[i]
        if abs(ca)>core.ALPHA_SKIP:continue
        side=1 if pa<=core.ENTRY_TH<ca and ca>=LANDING else (
            -1 if pa>=-core.ENTRY_TH>ca and ca<=-LANDING else 0)
        if side:pos,entry,best,ei=side,s[i],s[i],i;entries+=1
    if pos and len(a):
        if inp['session_done']:
            trades.append(record(len(a)-1,s[-1],'EOD'))
        else:return trades,dict(record(len(a)-1,s[-1],'open'),status='open')
    return trades,None


def replay_day(day,*,now=None,kite=None,known_close=None):
    close,close_date,close_source=known_close or core.prev_close(day,kite)
    frame=core.options_frame(day)
    expiry=core.nearest_expiry(frame['expiry'].astype(str).str.upper().unique(),day)
    if expiry is None:raise core.BnAlphaInputError(f'No BANKNIFTY expiry on {day}')
    base,base_date=core.baseline(day,expiry)
    bars=core.spot_bars(day)
    if not bars:raise core.BnAlphaInputError(f'No BANKNIFTY spot bars for {day}')
    inp=core.session_inputs(day,close=close,frame=frame,bars=bars,base=base,now=now)
    closed,opened=simulate(inp)
    trades=[dict(core.price_trade(t,inp),status='closed') for t in closed]
    if opened:trades.append(dict(core.price_trade(opened,inp),status='open'))
    return dict(inp=inp,trades=trades,prev_close=close,prev_close_date=close_date,
                prev_close_source=close_source,baseline_date=base_date)


def run_day(trade_date=None,*,now=None,kite=None,persist=True,connection=None):
    now=now or datetime.now(core.IST)
    day=trade_date or now.astimezone(core.IST).date().isoformat()
    own=connection is None;conn=connection or get_conn()
    core._ensure_tables(conn,KEY)
    try:
        row=conn.execute(f'SELECT prev_close,prev_close_date,prev_close_source FROM {KEY}_daily WHERE trade_date=? AND prev_close_source=?',
                         (day,'kite_daily')).fetchone()
        if not row:
            try:row=conn.execute("SELECT prev_close,prev_close_date,prev_close_source FROM bn_alpha_daily WHERE trade_date=? AND prev_close_source='kite_daily'",(day,)).fetchone()
            except sqlite3.OperationalError:row=None
        known=tuple(row) if row else None
        try:
            cutoff=now if day==now.astimezone(core.IST).date().isoformat() else None
            r=replay_day(day,now=cutoff,kite=kite,known_close=known)
        except core.BnAlphaInputError as exc:
            if persist:core._save(conn,day,{},error=str(exc),prefix=KEY,version=STRATEGY_VERSION)
            return dict(trade_date=day,status='unavailable',error=str(exc))
        if persist:
            note='Historical research replay; forward paper starts '+FIRST_FORWARD if day<FIRST_FORWARD else None
            core._save(conn,day,r,prefix=KEY,version=STRATEGY_VERSION,note=note)
        closed=[t for t in r['trades'] if t['status']=='closed']
        return dict(trade_date=day,trades=len(r['trades']),open=sum(t['status']=='open' for t in r['trades']),
                    spot_pts=round(sum(t['spot_pts'] for t in closed),2),
                    net_rs=round(sum(t['net_rs'] or 0 for t in closed),2))
    finally:
        if own:conn.close()


def run_paper_day(trade_date=None):
    now=datetime.now(core.IST)
    if now.weekday()>=5 or not time(9,16)<=now.time()<=time(15,45):return None
    day=trade_date or now.date().isoformat()
    conn=get_conn()
    try:
        core._ensure_tables(conn,KEY)
        row=conn.execute(f'SELECT latest_mark,status FROM {KEY}_daily WHERE trade_date=?',(day,)).fetchone()
    finally:conn.close()
    newest=(now-timedelta(seconds=70)).replace(second=0,microsecond=0,tzinfo=None)
    newest-=timedelta(minutes=newest.minute%5)
    newest=min(newest,datetime.combine(now.date(),core.LAST_MARK))
    if row and row[1]!='unavailable' and row[0] and row[0]>=newest.isoformat(timespec='minutes'):return None
    return run_day(day,now=now)


def tab_data(conn,date_clause='',date_params=()):
    rows,trades,stats=core.tab_data(conn,date_clause,date_params,prefix=KEY,first_unseen=FIRST_FORWARD,
        extra=dict(label=LABEL,landing=LANDING,max_trades=MAX_TRADES,max_losses=MAX_LOSSES,
                   paper_start=PAPER_START,first_forward=FIRST_FORWARD))
    # Keep all closed trades in risk statistics, including incomplete source days.
    if stats:
        totals={}
        for t in trades:
            if t['status']=='closed' and t['net_rs'] is not None:
                totals[t['trade_date']]=totals.get(t['trade_date'],0.)+float(t['net_rs'])
        equity=peak=drawdown=0.
        for date in sorted(totals):
            equity+=totals[date];peak=max(peak,equity);drawdown=max(drawdown,peak-equity)
        stats['max_dd_rs']=round(drawdown,2)
        stats['worst_day_rs']=round(min(totals.values(),default=0.),2)
    return rows,trades,stats


def initialize_history():
    """Only this new book's tables are written; original BN rows supply locked closes."""
    conn=get_conn()
    try:
        core._ensure_tables(conn,KEY)
        days=[r[0] for r in conn.execute("SELECT trade_date FROM bn_alpha_daily WHERE status!='unavailable' ORDER BY trade_date")]
    finally:conn.close()
    result=dict(done=[],unavailable={})
    for day in days:
        out=run_day(day)
        if out.get('error'):result['unavailable'][day]=out['error']
        else:result['done'].append(day)
    return result
