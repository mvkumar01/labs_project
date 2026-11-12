"""Nifty Omega market data and strategy rules shared by paper and live."""
from datetime import datetime, time, timedelta
from functools import lru_cache
from math import isfinite

import pandas as pd

from labs.engine import bn_alpha_tracker as core, nna_tracker as nna
from labs.engine.indicator_engine import compute_ema
from labs.engine.resampler import get_resampled_data

KEY = 'nifty_omega'
LABEL = 'Nifty Omega'
ABI = 'nifty-omega-ema9-21-split25-40-v1'
LOT = nna.LOT_SIZE
START = '2026-11-12'
MINUTE = timedelta(minutes=1)


def decision_exit(alpha, side, bar, anchor, targets, end=False):
    """Order of decisions from the research replay. Returns hit targets and reason."""
    favourable = (bar[1] - anchor) if side == 1 else (anchor - bar[2])
    hit = [target for target in targets if favourable >= target]
    if alpha is not None and abs(alpha) <= 200 and (alpha*side >= 100 or alpha*side <= -30):
        return list(targets), 'alpha'
    if end:
        return list(targets), 'eod'
    return hit, 'target'


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
    return dict(bars=bars, alpha=alpha, ema=ema, frame=frame, expiry=expiry,
                prev_close=close, prev_close_date=close_date, prev_close_source=source,
                baseline_date=baseline_date, range_lo=lo, range_hi=hi)


def first_signal(data, day, asof):
    origin = datetime.fromisoformat(day+'T09:15')
    mark = origin + 5*MINUTE
    while mark <= min(asof, datetime.fromisoformat(day+'T15:15')):
        current = data['alpha'].get(mark)
        previous = data['alpha'].get(mark-5*MINUTE)
        if current is None or previous is None:
            raise core.BnAlphaInputError(f'Omega missing alpha mark {mark:%H:%M}')
        side = 1 if previous <= 30 < current else -1 if previous >= -30 > current else 0
        if side and abs(current) <= 200:
            ema = data['ema'].get(mark, float('nan'))
            bar = data['bars'].get(mark-MINUTE)
            if not isfinite(ema) or bar is None:
                raise core.BnAlphaInputError(f'Omega missing entry candle or EMA at {mark:%H:%M}')
            if ema*side > 0:
                return mark, side, int(round(bar[3]/50)*50)
        mark += 5*MINUTE
    return None


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
