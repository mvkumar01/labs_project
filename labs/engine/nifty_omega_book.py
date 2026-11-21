"""Forward Nifty Omega paper ledger. No broker order methods. Rules: live/engine/nifty_omega.py.

BACKFILL: sessions before the forward start (omega.START, 12 Nov 2026) were not traded forward. run_backfill() replays
them after the fact with the current rules on the recorded quotes and stores them with strategy_version
BACKFILL_VERSION; the tab and the trade report label every row dated before omega.START as backfill."""
from datetime import date, datetime, time, timedelta

from labs.engine import nna_tracker as nna, nna_twins_tracker as twins, tick_replay
from labs.engine.charges import BROKERAGE_PER_LEG, GST, round_trip_charges
from live.engine import nifty_omega as omega
from storage.db import get_conn

BOOK = twins.Twin(omega.KEY, omega.LABEL, 2, omega.PREMIUM_TARGETS[0], omega.ADVERSE[1], False, omega.ABI, {})
BACKFILL_START = nna.PAPER_START                        # first month with option quotes in the shared archive
BACKFILL_VERSION = omega.ABI+'+backfill'
BACKFILL_BOOK = twins.Twin(omega.KEY, omega.LABEL, 2, omega.PREMIUM_TARGETS[0], omega.ADVERSE[1], False,
                           BACKFILL_VERSION, {})


def is_backfill(day):
    """True for a session before the forward paper start: its rows are a backfill, not forward paper."""
    return str(day) < omega.START


def _trade(day, data, asof, signal):
    """One entry on `signal` (mark, side, ATM): both lots at the next minute's ask, lot 1 / lot 2 sold at the
    minute after the bid first reaches ask + 12.5 / + 20, or on an alpha or end-of-day exit.
    Returns (trade row or None, the 5-min mark of a loss exit that closed the last lot, or None)."""
    mark, side, atm = signal
    entry = mark+omega.MINUTE
    bar = data['bars'].get(entry)
    if entry > asof or bar is None:
        return None, None
    anchor = float(bar[0])
    strike = omega.pick_strike(data, day, entry, atm, side, anchor)
    quote = omega.contract_quote(data, entry, strike, side)
    if quote is None:
        return None, None
    ask = float(quote.ask)
    lots = list(omega.PREMIUM_TARGETS)
    remaining = list(lots)
    exits = {}
    decision = entry+omega.MINUTE
    end = datetime.combine(entry.date(), time(15,25))
    exit_alpha, stop_mark = None, None
    while remaining and decision <= min(asof, end):
        alpha = data['alpha'].get(decision)
        if decision.minute % 5 == 0 and alpha is None:
            raise omega.core.BnAlphaInputError(f'Omega missing holding alpha {decision:%H:%M}')
        now_q = omega.contract_quote(data, decision, strike, side)
        hit, reason = omega.decision_exit(alpha, side, float(now_q.bid) if now_q is not None else None, ask,
                                          remaining, decision == end)
        if hit:
            fill = decision+omega.MINUTE
            if fill > asof:
                break
            bid = omega.contract_quote(data, fill, strike, side)
            if bid is None:
                raise omega.core.BnAlphaInputError(f'Omega missing exit bid {fill:%H:%M}')
            spot = (data['bars'].get(fill) or data['bars'].get(decision) or (anchor,)*4)[0]
            for target in hit:
                exits[target] = (fill, float(bid.bid), reason, spot)
                remaining.remove(target)
            exit_alpha = alpha
            if not remaining and reason == 'alpha_stop':
                stop_mark = decision
        decision += omega.MINUTE
    a, b = exits.get(lots[0]), exits.get(lots[1])
    closed = a is not None and b is not None
    if closed:
        gross = (a[1]+b[1]-2*ask)*omega.LOT
        if a[0] == b[0]:
            fee = round_trip_charges(ask, b[1], 2*omega.LOT)['raw_total']
        else:
            fee = sum(round_trip_charges(ask, leg[1], omega.LOT)['raw_total'] for leg in (a,b))
            fee -= BROKERAGE_PER_LEG*(1+GST)
    else:
        gross = fee = None
    last = max((x for x in (a, b) if x), key=lambda x: x[0], default=None)
    a_pts = (a[3]-anchor)*side if a else None
    b_pts = (b[3]-anchor)*side if b else None
    return dict(status='closed' if closed else 'open', side='CALL' if side==1 else 'PUT',
                entry_ts=entry, exit_ts=last[0] if closed else None, entry_spot=anchor,
                exit_spot=last[3] if closed else anchor, entry_alpha=data['alpha'][mark], exit_alpha=exit_alpha,
                lotA_ts=a[0] if a else None, lotA_pts=a_pts, lotB_pts=b_pts,
                spot_pts=(a_pts+b_pts)/2 if closed else 0., exit_reason=last[2] if closed else 'holding',
                strike=strike, tradingsymbol=quote.tradingsymbol, entry_ask=ask,
                lotA_bid=a[1] if a else None, exit_bid=b[1] if b else None,
                option_pts=gross/(2*omega.LOT) if closed else None, gross_rs=gross,
                charges_rs=fee, net_rs=round(gross-fee,2) if closed else None,
                qty=2*omega.LOT, lots=2, quote_status='priced' if closed else 'open'), stop_mark


def replay(day, data, asof):
    """The day's trades: the first signal that passes the filters, then (PUT only) one re-entry after a loss exit."""
    signal = omega.first_signal(data, day, asof)
    if signal is None:
        return []
    trade, stop_mark = _trade(day, data, asof, signal)
    if trade is None:
        return []
    trades = [trade]
    if stop_mark is not None:
        again = omega.reentry_signal(data, day, signal[1], stop_mark, asof)
        if again is not None:
            try:
                second, _ = _trade(day, data, asof, again)
            except omega.core.BnAlphaInputError:
                second = None                   # as the research: a re-entry that cannot be priced is not taken
            if second is not None:
                trades.append(second)
    return trades


def _record(day, asof, book):
    data = omega.snapshots(day, asof)
    trades = replay(day, data, asof)
    marks = sorted(data['alpha'])
    inp = dict(expiry=data['expiry'], range_lo=data['range_lo'], range_hi=data['range_hi'],
               marks=marks, alpha=[data['alpha'][mark] for mark in marks],
               spot=[data['bars'].get(mark-omega.MINUTE, (0,0,0,0))[3] for mark in marks],
               session_done=asof.time() >= time(15,26) and not any(t['status']=='open' for t in trades))
    result = dict(data, inp=inp, trades=trades, vix_open=None, vix_source=None,
                  replay=tick_replay.ONE_MINUTE)
    conn = get_conn()
    try:
        twins._ensure_tables(conn, omega.KEY)
        twins._save(conn, day, book, result)
    finally:
        conn.close()
    return dict(trades=len(trades), net_rs=sum(t['net_rs'] or 0 for t in trades))


def run_paper_day(trade_date=None, now=None):
    now = now or datetime.now(omega.core.IST)
    asof = now.astimezone(omega.core.IST).replace(tzinfo=None) if now.tzinfo else now
    day = trade_date or asof.date().isoformat()
    if day < omega.START:
        return None
    return _record(day, asof, BOOK)


def run_backfill(start=BACKFILL_START, end=None, sessions=None):
    """BACKFILL of the sessions before omega.START: each replayed to 15:30 with the current rules and saved with
    BACKFILL_VERSION. Forward sessions (omega.START on) are never touched. A session whose inputs are missing is
    saved as 'unavailable' with the reason. Returns {'done': {day: result}, 'failed': {day: reason}}."""
    last = (date.fromisoformat(omega.START)-timedelta(days=1)).isoformat()
    end = min(end or last, last)
    days = sessions(start, end) if sessions else nna.sessions_with_quotes(start, end)
    done, failed = {}, {}
    for day in days:
        if not is_backfill(day):
            continue
        try:
            done[day] = _record(day, datetime.combine(date.fromisoformat(day), time(15,30)), BACKFILL_BOOK)
        except Exception as exc:                    # one bad session must not stop the backfill
            failed[day] = f'{type(exc).__name__}: {exc}'
            conn = get_conn()
            try:
                twins._ensure_tables(conn, omega.KEY)
                twins._save(conn, day, BACKFILL_BOOK, None, error=failed[day])
            finally:
                conn.close()
    return dict(done=done, failed=failed)


def tab_data(conn, date_clause='', date_params=()):
    twins._ensure_tables(conn, omega.KEY)
    return omega.core.tab_data(conn, date_clause, date_params, prefix=omega.KEY,
                               first_unseen=omega.START, extra={'label':omega.LABEL, 'backfill_before':omega.START})
