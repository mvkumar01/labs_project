"""Capital committed at the same instant, without adding reused capital."""
from datetime import datetime, timedelta, timezone
import math

IST = timezone(timedelta(hours=5, minutes=30))


def recorded_nifty_quantity(trade):
    """Recover missing paper quantity only when both recorded gross and fees fit."""
    from labs.engine.charges import round_trip_charges
    try:
        entry, exit_ = float(trade['entry_price']), float(trade['exit_price'])
        gross, costs = float(trade['gross_rs']), float(trade['charges_rs'])
    except (KeyError, TypeError, ValueError):
        return None
    if not all(math.isfinite(x) for x in (entry, exit_, gross, costs)) or entry <= 0 or exit_ < 0:
        return None
    base = round_trip_charges(entry, exit_, 0)['raw_total']
    # Entry/exit premiums and costs were persisted to paise. Solve the
    # resulting interval instead of requiring rounded prices to reproduce
    # the original unrounded calculation exactly.
    precision = .005000001
    low = round_trip_charges(max(0,entry-precision),max(0,exit_-precision),65)['raw_total']-base
    high = round_trip_charges(entry+precision,exit_+precision,65)['raw_total']-base
    if low <= .02:
        return None
    minimum = max(1,math.ceil((costs-base-precision)/high))
    maximum = math.floor((costs-base+precision)/low)
    if minimum != maximum:
        return None
    qty = minimum*65
    if abs((exit_-entry)*qty-gross) > 2*precision*qty+precision:
        return None
    return qty


def peak_deployed_capital(trades):
    events = []
    for trade in trades:
        capital = trade.get('capital')
        if capital is None or not math.isfinite(capital) or capital < 0:
            return None
        if capital == 0:
            continue
        try:
            entry = datetime.fromisoformat(trade['entry_ts'])
            exit_ = datetime.fromisoformat(trade['exit_ts']) if trade.get('exit_ts') else None
        except (ValueError, TypeError, KeyError):
            return None
        entry = entry.replace(tzinfo=IST) if entry.tzinfo is None else entry
        if exit_ is not None:
            exit_ = exit_.replace(tzinfo=IST) if exit_.tzinfo is None else exit_
            if exit_ < entry:
                return None
            if exit_ == entry:
                # Sequential fills can open and close within one stored second.
                events.append((entry, 1, capital))
                continue
            # Release capital before a new position at the same timestamp.
            events.append((exit_, 0, -capital))
        events.append((entry, 2, capital))
    if not events:
        return None
    current = peak = 0.0
    for _, kind, change in sorted(events):
        if kind == 1:
            peak = max(peak, current + change)
            continue
        current += change
        peak = max(peak, current)
    return round(peak, 2)
