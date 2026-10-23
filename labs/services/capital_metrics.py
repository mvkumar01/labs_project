"""Capital committed at the same instant, without adding reused capital."""
from datetime import datetime, timedelta, timezone
import math

IST = timezone(timedelta(hours=5, minutes=30))


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
            if exit_ <= entry:
                return None
            # Release capital before a new position at the same timestamp.
            events.append((exit_, 0, -capital))
        events.append((entry, 1, capital))
    if not events:
        return None
    current = peak = 0.0
    for _, _, change in sorted(events):
        current += change
        peak = max(peak, current)
    return round(peak, 2)
