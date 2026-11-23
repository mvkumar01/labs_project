"""Nifty Omega market data and strategy rules shared by paper and live.

Refinements of 2026-11-15 (alphaIMB research/experiments/2026-11-14_alpha_velocity_writers, README 27-38):
  strike   on 0-1 calendar days to expiry the strike whose Black-Scholes delta is nearest 0.70 (ATM .. 300 ITM in
           50-point steps, delta at the entry minute); ATM otherwise. One strike for both lots.
  exits    lot 1 when the bid is >= entry ask + 12.5, lot 2 at + 20 (bid checked every minute, sold the next
           minute); alpha favourable +100 and end of day stay. No spot targets.
  Friday   no new entry (re-entries included) from a 12:00 signal mark.
  retest   skip a signal when -10 < ahead <= 10, ahead = distance from spot to the day's high (CALL) / low (PUT)
           as it stood 45 minutes before the signal; a later fresh cross may still trade.
  loss     PUT: both lots out when alpha reaches +20, then ONE re-entry when alpha crosses from >= 0 to < 0 on a
           5-min mark with the EMA gate bearish (fresh strike, same exits, by 15:15). CALL: alpha -30, no re-entry.

Snap exit (Vipin 2026-11-23, from SNAP_START; research README 69-71, snap_oi_filter.py / snap_reentry.py):
  snap     every attempt, both sides: on minutes s+2 .. s+5 (s = the signal mark, entry at s+1), when no premium
           target and no 5-min alpha exit fired that minute, both lots go at the next minute's bid if the 1-min
           directional alpha is below +15 (|alpha| <= 200) AND >= 60% of the in-range OI flow since s is against
           the trade (P, C = cumulative put / call OI change vs baseline on the alpha range; PUT against =
           max(dP,0) + max(-dC,0), CALL mirrored; share of |dP| + |dC|). Reason 'snap_oi'.
  after    the first snap exit of the day arms no PUT reclaim; it allows ONE fresh entry instead: the next normal
           signal (either side, same filters and strike rule) from the first 5-min mark at or after the exit fill
           minute. That trade runs the full rules. A later snap exit is treated like a loss exit (PUT: the day's one
           reclaim if unused; otherwise flat).
"""
from datetime import date, datetime, time, timedelta
from functools import lru_cache
from math import erf, isfinite, log, sqrt

import pandas as pd

from labs.engine import bn_alpha_tracker as core, nna_tracker as nna
from labs.engine.indicator_engine import compute_ema
from labs.engine.resampler import get_resampled_data
from market_data.expiry import expiry_sort_date

KEY = 'nifty_omega'
LABEL = 'Nifty Omega'
ABI = 'nifty-omega-v2-snapoi15-60-fresh1-delta70-prem12.5-20-fri12-retest45-put20'
PRE_SNAP_ABI = 'nifty-omega-v2-delta70-prem12.5-20-fri12-retest45-put20'   # paper rows before SNAP_START
LOT = nna.LOT_SIZE
START = '2026-11-12'
SNAP_START = '2026-11-23'
MINUTE = timedelta(minutes=1)
PREMIUM_TARGETS = (12.5, 20.0)        # lot 1, lot 2: bid above the entry ask
ADVERSE = {1: -30.0, -1: -20.0}       # alpha*side at or below this exits both lots
REENTRY_SIDES = (-1,)                 # one re-entry after a loss exit: PUT only
LAST_MARK = time(15, 15)
FRIDAY_CUT = time(12, 0)
RETEST_AGE, RETEST_NEAR, RETEST_BREAK = 45, 10.0, 10.0
DELTA_TARGET, DELTA_MAX_DTE, DELTA_DEPTH = 0.70, 1, 300
SNAP_FROM, SNAP_TO = 2, 5              # minutes after the signal mark checked for a snap exit
SNAP_BELOW, SNAP_SHARE = 15.0, 0.60


def decision_exit(alpha, side, bid, ask, targets, end=False):
    """One decision minute for the open lots. targets: premium points of the lots still held; bid: the option bid
    at this minute (None if no valid quote); alpha: the 5-min alpha on a mark, else None.
    Returns (lots to sell, reason); premium targets are tested before the alpha exits, as in the research. When a
    premium target and an alpha exit fall on the same minute the reason is 'premium' (the research takes the reason
    of lot 1 on a tie), so such a minute does not arm the PUT re-entry."""
    hit = [t for t in targets if bid is not None and ask is not None and bid >= ask + t]
    if hit and len(hit) == len(targets):
        return list(targets), 'premium'
    if alpha is not None and abs(alpha) <= 200:
        if alpha*side >= 100:
            return list(targets), 'premium' if hit else 'alpha_fav'
        if alpha*side <= ADVERSE[side]:
            return list(targets), 'premium' if hit else 'alpha_stop'
    if end:
        return list(targets), 'eod'
    return hit, 'premium'


def against_share(then, now, side):
    """Share of the in-range OI flow between two (put, call) cumulative changes that is against `side`."""
    dp, dc = now[0]-then[0], now[1]-then[1]
    flow = abs(dp) + abs(dc)
    if not flow:
        return 0.0
    return ((max(dp, 0) + max(-dc, 0)) if side == -1 else (max(dc, 0) + max(-dp, 0))) / flow


def snap_exit(data, side, signal_mark, minute):
    """The snap condition at `minute` for a trade signalled on `signal_mark` (see the module notes). Missing
    one-minute OI at either end means no snap."""
    if not SNAP_FROM*MINUTE <= minute-signal_mark <= SNAP_TO*MINUTE:
        return False
    flow = data.get('pc') or {}
    then, now = flow.get(signal_mark), flow.get(minute)
    if then is None or now is None:
        return False
    den = now[0] + now[1]
    alpha = 100*(now[0]-now[1])/den if den else 0.0
    if abs(alpha) > 200 or alpha*side >= SNAP_BELOW:
        return False
    return against_share(then, now, side) >= SNAP_SHARE


def decide(data, signal_mark, minute, side, bid, ask, targets, end=False, snap=True):
    """decision_exit for `minute`, then the snap check when no exit fired for every held lot. A premium lot sold
    on the snap minute names the exit 'premium' (as a premium + alpha tie, and as the research's lot-1 reason)."""
    hit, reason = decision_exit(data['alpha'].get(minute), side, bid, ask, targets, end)
    if len(hit) < len(targets) and snap and snap_exit(data, side, signal_mark, minute):
        return list(targets), 'premium' if hit else 'snap_oi'
    return hit, reason


def _ceil5(minute):
    return minute + timedelta(minutes=-minute.minute % 5)


def after_exit(cause, side, decision, used):
    """What the exit that flattened a trade (decided at minute `decision`) allows next, given the kinds already
    taken today (`used`): ('fresh', first mark to scan), ('reclaim', loss mark) or None."""
    if cause == 'snap_oi' and 'fresh' not in used:
        return 'fresh', _ceil5(decision+MINUTE)
    if cause in ('alpha_stop', 'snap_oi') and side in REENTRY_SIDES and 'reclaim' not in used:
        return 'reclaim', decision - timedelta(minutes=decision.minute % 5)
    return None


@lru_cache(maxsize=3)
def prior_inputs(day, expiry):
    close, close_date, source = nna.prev_close(day)
    base, baseline_date = nna.baseline(day, expiry)
    return close, close_date, source, base, baseline_date


def completed_closes(bars, asof):
    if not bars:
        return pd.Series(dtype=float)
    frame = pd.DataFrame.from_dict(bars, orient='index', columns=['open','high','low','close']).sort_index()
    frame['volume'] = 0
    finished = get_resampled_data(frame, '5m', now=asof)
    counts = frame.close.resample('5min', origin='start_day', offset='15min').count()
    closes = finished.close.reindex(counts.index).where(counts.eq(5))
    closes.index += timedelta(minutes=5)
    return closes.loc[closes.index <= asof]


@lru_cache(maxsize=3)
def warmup(day):
    history = []
    for prev in reversed(core._previous_weekdays(day, limit=10)):
        bars = nna.spot_bars(prev)
        history.append(completed_closes(bars, datetime.fromisoformat(prev+'T15:30')))
    return pd.concat(history) if history else pd.Series(dtype=float)


def snapshots(day, asof):
    frame = nna.options_frame(day)
    frame['minute'] = pd.to_datetime(frame.timestamp, utc=False).dt.floor('min')
    if frame.minute.dt.tz is not None:
        frame['minute'] = frame.minute.dt.tz_convert(core.IST).dt.tz_localize(None)
    frame['expiry'] = frame.expiry.astype(str).str.upper()
    frame = frame[frame.minute <= asof].copy()
    expiry = core.nearest_expiry(frame.expiry.unique(), day)
    if not expiry:
        raise core.BnAlphaInputError('Omega weekly expiry unavailable')
    frame = frame[frame.expiry == expiry].copy()
    frame['type'] = frame.option_type.str.lower()
    frame = frame.sort_values('timestamp').drop_duplicates(['minute','strike','type'], keep='last')
    close, close_date, source, base, baseline_date = prior_inputs(day, expiry)
    lo, hi = core.range_for(close, 350, 50)
    expected = pd.MultiIndex.from_product([range(lo, hi+1, 50), ['ce','pe']], names=['strike','type'])
    b = base.set_index(['strike','type']).oi.reindex(expected, fill_value=0)
    marks = frame[(frame.minute.dt.minute % 5 == 0) & frame.strike.between(lo, hi)]
    rows = frame[frame.strike.between(lo, hi)].set_index(['minute','strike','type']).oi.unstack(['strike','type'])
    rows = rows.reindex(columns=expected).dropna()
    delta = rows - b.to_numpy()
    pc = {m.to_pydatetime(): (float(p), float(c)) for m, p, c in
          zip(delta.index, delta.xs('pe', axis=1, level='type').sum(axis=1),
              delta.xs('ce', axis=1, level='type').sum(axis=1))}
    alpha = {}
    for minute, group in marks.groupby('minute'):
        oi = group.set_index(['strike','type']).oi.reindex(expected)
        if oi.isna().any():
            continue
        delta = (oi-b).groupby(level='type').sum()
        den = delta.pe + delta.ce
        alpha[minute.to_pydatetime()] = 100*(delta.pe-delta.ce)/den if den else 0.0
    bars = nna.spot_bars(day)
    closes = pd.concat([warmup(day), completed_closes(bars, asof)])
    ema = compute_ema(closes, 9)-compute_ema(closes, 21)
    ema = ema.where(closes.notna() & (closes.notna().cumsum() >= 21))
    return dict(bars=bars, alpha=alpha, pc=pc, ema=ema, frame=frame, expiry=expiry, expiry_date=expiry_sort_date(expiry),
                prev_close=close, prev_close_date=close_date, prev_close_source=source,
                baseline_date=baseline_date, range_lo=lo, range_hi=hi)


def _ema_agrees(data, mark, side):
    ema = data['ema'].get(mark, float('nan'))
    if not isfinite(ema):
        raise core.BnAlphaInputError(f'Omega missing EMA at {mark:%H:%M}')
    return ema*side > 0


def _atm(data, mark):
    bar = data['bars'].get(mark-MINUTE)
    if bar is None:
        raise core.BnAlphaInputError(f'Omega missing entry candle at {mark:%H:%M}')
    return int(round(bar[3]/50)*50)


def retest_blocked(data, day, mark, side):
    """The signal tests the day's high (CALL) / low (PUT) as it stood RETEST_AGE minutes before the mark:
    -RETEST_BREAK < ahead <= RETEST_NEAR, ahead = level - spot (CALL) / spot - level (PUT), spot = the mark
    minute's open (the previous close if that bar is not in yet)."""
    origin = datetime.fromisoformat(day+'T09:15')
    cut = mark - timedelta(minutes=RETEST_AGE)
    if (cut-origin) < 5*MINUTE:
        return False
    old = [bar for minute, bar in data['bars'].items() if origin <= minute < cut]
    if not old:
        return False
    level = max(bar[1] for bar in old) if side == 1 else min(bar[2] for bar in old)
    bar = data['bars'].get(mark)
    if bar is not None:
        spot = bar[0]
    else:
        prev = data['bars'].get(mark-MINUTE)
        if prev is None:
            raise core.BnAlphaInputError(f'Omega missing spot at {mark:%H:%M}')
        spot = prev[3]
    ahead = (level-spot) if side == 1 else (spot-level)
    return -RETEST_BREAK < ahead <= RETEST_NEAR


def _friday_cut(day, mark):
    return date.fromisoformat(day).weekday() == 4 and mark.time() >= FRIDAY_CUT


def signals(data, day, asof, start=None):
    """Fresh +-30 crosses on 5-min marks with EMA9/21 agreeing that pass the Friday and retest filters,
    in time order: (mark, side, ATM strike). Nothing from a Friday 12:00 signal on. With `start` (the fresh entry
    after a snap exit) the scan begins on that mark and a mark without alpha is skipped, as in the research."""
    origin = datetime.fromisoformat(day+'T09:15')
    mark = max(start, origin + 5*MINUTE) if start else origin + 5*MINUTE
    while mark <= min(asof, datetime.combine(origin.date(), LAST_MARK)):
        current = data['alpha'].get(mark)
        previous = data['alpha'].get(mark-5*MINUTE)
        if current is None or previous is None:
            if start:
                mark += 5*MINUTE
                continue
            raise core.BnAlphaInputError(f'Omega missing alpha mark {mark:%H:%M}')
        side = 1 if previous <= 30 < current else -1 if previous >= -30 > current else 0
        if side and abs(current) <= 200 and _ema_agrees(data, mark, side):
            if _friday_cut(day, mark):
                return
            if not retest_blocked(data, day, mark, side):
                yield mark, side, _atm(data, mark)
        mark += 5*MINUTE


def first_signal(data, day, asof):
    return next(signals(data, day, asof), None)


def fresh_signal(data, day, start, asof):
    """The one fresh entry after a snap exit: the first normal signal from the mark `start`, or None."""
    return next(signals(data, day, asof, start), None)


def reentry_signal(data, day, side, stop_mark, asof):
    """After a loss exit decided on the 5-min mark stop_mark: the first later mark where alpha (in the trade's
    direction) crosses from <= 0 to > 0 with the EMA gate agreeing, by 15:15 and not on a Friday from 12:00.
    Marks with no alpha or |alpha| > 200 reset the cross. (mark, side, ATM strike) or None."""
    if side not in REENTRY_SIDES:
        return None
    prev = data['alpha'].get(stop_mark)
    prev = prev*side if prev is not None and abs(prev) <= 200 else None
    mark = stop_mark + 5*MINUTE
    while mark <= min(asof, datetime.combine(stop_mark.date(), LAST_MARK)):
        if _friday_cut(day, mark):
            return None
        current = data['alpha'].get(mark)
        if current is None or abs(current) > 200:
            prev = None
        else:
            current *= side
            if prev is not None and prev <= 0 < current and _ema_agrees(data, mark, side):
                return mark, side, _atm(data, mark)
            prev = current
        mark += 5*MINUTE
    return None


def _cdf(x):
    return 0.5*(1+erf(x/sqrt(2)))


def _bs(fwd, strike, T, iv, call):
    sd = iv*sqrt(T)
    d1 = (log(fwd/strike)+0.5*sd*sd)/sd
    c = fwd*_cdf(d1) - strike*_cdf(d1-sd)
    return c if call else c - fwd + strike


def _implied(price, fwd, strike, T, call):
    intrinsic = max(fwd-strike, 0) if call else max(strike-fwd, 0)
    if not isfinite(price) or price <= intrinsic + 0.05 or T <= 0:
        return None
    lo, hi = 0.01, 3.0
    for _ in range(40):
        mid = (lo+hi)/2
        if _bs(fwd, strike, T, mid, call) > price:
            hi = mid
        else:
            lo = mid
    return (lo+hi)/2


def strike_deltas(data, minute, atm, side, spot):
    """{strike: Black-Scholes delta} for ATM .. DELTA_DEPTH points ITM at `minute`: forward from put-call parity
    (median of the three strikes nearest spot), one IV per strike from its out-of-the-money mid, time to the
    expiry's 15:30 in years of 365 days, no rate. A strike without a usable quote is left out."""
    frame = data['frame']
    rows = frame[frame.minute == minute]
    ok = (rows.bid > 0) & (rows.ask >= rows.bid)
    mids = {(int(s), t): (b+a)/2 for s, t, b, a, good in zip(rows.strike, rows.type, rows.bid, rows.ask, ok) if good}
    strikes = sorted({s for s, _ in mids} | {int(s) for s in rows.strike})
    near = sorted(strikes, key=lambda s: abs(s-spot))[:3]
    parity = sorted(s + mids[(s, 'ce')] - mids[(s, 'pe')] for s in near if (s, 'ce') in mids and (s, 'pe') in mids)
    if not parity or data.get('expiry_date') is None:
        return {}
    n = len(parity)
    fwd = parity[n//2] if n % 2 else (parity[n//2-1]+parity[n//2])/2
    T = (datetime.combine(data['expiry_date'], time(15, 30)) - minute).total_seconds()/(365*86400)
    out = {}
    for shift in range(0, DELTA_DEPTH+1, 50):
        strike = atm - side*shift
        otm_call = strike >= fwd
        price = mids.get((strike, 'ce' if otm_call else 'pe'))
        iv = _implied(price, fwd, strike, T, otm_call) if price is not None else None
        if iv is None:
            continue
        sd = iv*sqrt(T)
        d1 = (log(fwd/strike)+0.5*sd*sd)/sd
        out[strike] = _cdf(d1) if side == 1 else 1-_cdf(d1)
    return out


def pick_strike(data, day, minute, atm, side, spot):
    """ATM, or on 0-1 calendar days to expiry the strike whose delta at `minute` is nearest DELTA_TARGET
    (ties: the lower strike); ATM when no delta can be computed."""
    expiry = data.get('expiry_date')
    if expiry is None or (expiry - date.fromisoformat(day)).days > DELTA_MAX_DTE:
        return atm
    deltas = strike_deltas(data, minute, atm, side, spot)
    if not deltas:
        return atm
    return min((abs(d-DELTA_TARGET), k) for k, d in deltas.items())[1]


def contract_quote(data, minute, strike, side):
    frame = data['frame']
    rows = frame[(frame.minute == minute) & (frame.strike == strike)
                 & (frame.type == ('ce' if side == 1 else 'pe'))]
    if rows.empty:
        return None
    row = rows.iloc[-1]
    bid, ask = float(row.bid), float(row.ask)
    if not isfinite(bid+ask) or bid <= 0 or ask < bid:
        return None
    return row
