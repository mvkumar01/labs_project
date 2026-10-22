"""Read-only one-minute excursion estimates. No fetching and no broker calls."""
from __future__ import annotations

from datetime import datetime, timedelta
from pathlib import Path

from labs.services.strategy_report import IST, number, stamp

BASE_DIR = Path(__file__).resolve().parents[2]


def contract_marks(conn, trade, _frames=None):
    """(time, low, high, close); futures timestamps are bar CLOSE times.

    Option snapshots are instantaneous LTP observations, not candle extremes.
    No other contract or underlying proxy can substitute for the held contract.
    """
    _frames = {} if _frames is None else _frames
    if trade.get('multi_leg'):
        legs=trade.get('legs') or []
        if not legs:
            return [], 'A complete leg breakdown is unavailable; no single-leg proxy is used.'
        grids=[]
        for leg in legs:
            if leg['entry_price'] is None or leg['qty'] is None:
                return [], 'A leg fill price or quantity is missing.'
            marks,_=contract_marks(conn,leg,_frames)
            if not marks:
                return [], 'Exact-contract history is missing for at least one leg.'
            grids.append({m[0]:m[3] for m in marks})
        common=set.intersection(*(set(g) for g in grids))
        out=[]
        overall_exit=stamp(trade['exit_ts'])
        for at in sorted(common):
            value=0.0
            for leg,grid in zip(legs,grids):
                leg_exit=stamp(leg['exit_ts'])
                if overall_exit and at<=overall_exit and leg_exit and at>=leg_exit and leg['gross'] is not None:
                    value+=leg['gross']
                else:
                    value+=pnl(leg,grid[at])
            out.append((at,value,value,value))
        return out, 'Synchronized exact-contract leg snapshots; combined gross P&L, not separate leg peaks.'
    und, day, symbol = trade['underlying'], trade['date'], trade['contract']
    now = datetime.now(IST)
    if und in ('GOLD', 'GOLDM', 'CRUDEOIL', 'CRUDEOILM'):
        try:
            data = conn.execute('SELECT ts,low,high,close FROM crude_minute_bars '
                                'WHERE tradingsymbol=? AND substr(ts,1,10)=? ORDER BY ts', (symbol,day)).fetchall()
        except Exception:
            return [], 'Exact contract candles are not stored.'
        out=[]
        for ts,low,high,close in data:
            at=stamp(ts)
            if at is not None and at+timedelta(minutes=1)<=now:
                out.append((at+timedelta(minutes=1),float(low),float(high),float(close)))
        return out, 'One-minute contract candle estimates; partial entry/exit bars excluded.'
    if und in ('NIFTY','SENSEX','BANKNIFTY'):
        from config.labs_config import SHARED_LIVE_DIR, SHARED_ARCHIVE_DIR
        from market_data.shared_store import load_options_frame
        try:
            if (und,day) not in _frames:
                _frames[(und,day)]=load_options_frame(und,day,live_root=SHARED_LIVE_DIR,archive_root=SHARED_ARCHIVE_DIR)
            frame=_frames[(und,day)]
        except Exception:
            return [], 'Contract quote history is unavailable.'
        if symbol and 'tradingsymbol' in frame.columns and symbol in set(frame['tradingsymbol']):
            frame=frame[frame['tradingsymbol']==symbol]
        elif trade.get('strike') is not None and trade.get('expiry_code') and trade.get('option_type'):
            frame=frame[(frame['strike']==trade['strike']) &
                        (frame['expiry'].astype(str).str.upper()==str(trade['expiry_code']).upper()) &
                        (frame['option_type'].astype(str).str.upper()==trade['option_type'])]
        else:
            return [], 'The exact contract cannot be resolved from this ledger.'
        out=[]
        for row in frame.to_dict('records'):
            at=stamp(row['timestamp'])
            value=number(row.get('ltp'))
            if at and at<=now and value is not None and value>0:
                out.append((at,value,value,value))
        return sorted(out), 'Recorded one-minute contract LTP snapshots; intraminute peaks are unknown.'
    return [], 'No intraday exact-contract price series is available for this strategy.'


def pnl(trade,price):
    if trade.get('multi_leg') and trade.get('legs'):
        return price  # synchronized combined marks already contain gross rupees
    return (price-trade['entry_price'])*trade['qty']*(-1 if trade['direction'] in ('SHORT','SELL') else 1)


def estimate(trade,marks):
    empty=dict(peak=trade.get('peak'),post_peak=trade.get('post_peak'),post_trough=trade.get('post_trough'),
               day_peak=None,through=None)
    entry,exit_=stamp(trade['entry_ts']),stamp(trade['exit_ts'])
    combined = trade.get('multi_leg') and trade.get('legs')
    if not entry or (not combined and (trade['entry_price'] is None or trade['qty'] is None)):
        return dict(empty,note='Entry timestamp, fill price or quantity is missing.')
    if not marks:
        return dict(empty,note='No exact-contract marks for this session.')
    if entry.date().isoformat()!=trade['date'] or (exit_ and exit_.date()!=entry.date()):
        return dict(empty,note='Intraday excursion calculation is unavailable for multi-session trades.')
    # A futures candle spans the preceding minute. Its extrema cannot be assigned
    # to a trade that entered halfway through it, or to the post-exit window.
    candle=trade['underlying'] in ('GOLD','GOLDM','CRUDEOIL','CRUDEOILM')
    lag=timedelta(minutes=1) if candle else timedelta(0)
    held=[m for m in marks if m[0]-lag>=entry and (not exit_ or m[0]<=exit_)]
    post=[m for m in marks if exit_ and m[0]-lag>exit_]
    known=[0.0]
    if trade['closed'] and trade['gross'] is not None:
        known.append(trade['gross'])
    values=known+[pnl(trade,p) for m in held for p in m[1:3]]
    empty['peak']=max(values) if held else empty['peak']
    if post:
        after=[pnl(trade,p) for m in post for p in m[1:3]]
        empty['post_peak'],empty['post_trough']=max(after),min(after)
    empty['through']=marks[-1][0].isoformat()
    return dict(empty,note='Estimated gross P&L against the original entry; costs excluded. '
                'Post-exit values are hypothetical, not realized. Through '+marks[-1][0].strftime('%H:%M IST')+'.')


def day_peak(trades,series):
    """Portfolio peak using synchronized close/snapshot marks plus actual exits.

    Returns unknown if any trade is not independently priceable. Never sum
    individual MFE values: those maxima can occur at different times.
    """
    eligible=[t for t in trades if t.get('entry_ts')]
    if not eligible or any((not (t.get('multi_leg') and t.get('legs')) and
                           (t['qty'] is None or t['entry_price'] is None)) or
                           not series.get(t['id']) for t in eligible):
        return None
    if any(stamp(t['entry_ts']).date().isoformat()!=t['date'] or
           t['exit_ts'] and stamp(t['exit_ts']).date()!=stamp(t['entry_ts']).date() for t in eligible):
        return None
    for t in eligible:
        entry, exit_ = stamp(t['entry_ts']), stamp(t['exit_ts'])
        if (t['closed'] and t['gross'] is None) or not any(m[0]>=entry and (not exit_ or m[0]<=exit_) for m in series[t['id']]):
            return None
    times=sorted({m[0] for marks in series.values() for m in marks} |
                 {stamp(t['exit_ts']) for t in eligible if t['exit_ts']})
    peak=0.0
    cursors={t['id']:0 for t in eligible}
    for at in times:
        total=0.0
        valid=True
        for t in eligible:
            entry,exit_=stamp(t['entry_ts']),stamp(t['exit_ts'])
            if at<entry:
                continue
            if exit_ and at>=exit_:
                if t['gross'] is None:
                    valid=False
                    break
                total+=t['gross']
                continue
            marks=series[t['id']]
            idx=cursors[t['id']]
            while idx+1<len(marks) and marks[idx+1][0]<=at:
                idx+=1
            cursors[t['id']]=idx
            mark=marks[idx]
            if mark[0]<entry or mark[0]>at or at-mark[0]>timedelta(minutes=2):
                valid=False
                break
            total+=pnl(t,mark[3])
        if valid:
            peak=max(peak,total)
    return peak
