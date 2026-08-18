"""Minute-boundary aggregation of the two-second live spot poll.

`v2.12_closed_confirmed` decides on a 60-second CLOSE, not on a completed
collector candle. The distinction matters for execution latency: the labs
one-minute collector publishes its OHLC row for 09:25 well after 09:26:00, so a
runner keyed on `latest_completed_ohlc_minute()` executed a 09:25 decision at
09:26:49 (2026-08-17). The samples needed to make that decision at 09:26:00 were
already being fetched every two seconds — they were only being logged.

This module turns that existing sample stream into the strategy's clock:

    1. every runner cycle feeds one spot sample in,
    2. samples accumulate into the current clock minute,
    3. the FIRST sample of a new minute freezes the minute that just ended,
       whose `close` is that minute's last fresh sample.

The frozen minute is what the close-confirmed replay consumes, so the decision
lands on the first two-second poll after `:00` instead of waiting for the CSV.

Deliberately NOT a stop overlay: nothing here reacts to an intraminute tick, so
a spot that crosses the anchor mid-minute and recovers by the boundary produces
no exit. Only the frozen boundary close is ever compared to the anchor.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta

# A minute is only trustworthy if sampling survived to near its end. A gap wider
# than this (rate-limit stall, auth blip, PA scheduling pause) means the "close"
# would really be a stale mid-minute print, so the minute is dropped and the
# strategy holds rather than acting on a fabricated close.
DEFAULT_MAX_STALENESS_S = 10.0

# The runner is an always-on task spanning many sessions; only today and the
# prior day are ever consulted, so older sessions are dropped.
_RETAINED_SESSIONS = 2


@dataclass(frozen=True)
class FrozenMinute:
    """One completed clock minute, built purely from live spot samples."""

    trade_date: str          # YYYY-MM-DD (IST)
    minute: str              # HH:MM (IST) — the minute that just ENDED
    open: float
    high: float
    low: float
    close: float             # last fresh sample of the minute
    last_ts: datetime        # timestamp of that last sample
    samples: int

    @property
    def key(self) -> str:
        """Replay-clock key, same shape as latest_completed_ohlc_minute()."""
        return f"{self.trade_date}T{self.minute}"

    def as_ohlc(self) -> tuple[float, float, float, float]:
        return (self.open, self.high, self.low, self.close)


class _OpenMinute:
    __slots__ = ("trade_date", "minute", "open", "high", "low",
                 "close", "last_ts", "samples")

    def __init__(self, trade_date: str, minute: str,
                 value: float, ts: datetime):
        self.trade_date = trade_date
        self.minute = minute
        self.open = value
        self.high = value
        self.low = value
        self.close = value
        self.last_ts = ts
        self.samples = 1

    def update(self, value: float, ts: datetime) -> None:
        self.high = max(self.high, value)
        self.low = min(self.low, value)
        self.close = value      # last sample wins — this becomes the 60s close
        self.last_ts = ts
        self.samples += 1


class MinuteTickAggregator:
    """Fold a 2-second spot stream into frozen one-minute boundary closes.

    Single instance per runner process: the spot is fetched once globally per
    cycle, so every connection sees the SAME boundary minute and cannot diverge
    from another connection by sampling the feed at a different instant.
    """

    def __init__(self, max_staleness_s: float = DEFAULT_MAX_STALENESS_S):
        self.max_staleness_s = float(max_staleness_s)
        self._open: _OpenMinute | None = None
        self._frozen: dict[str, dict[str, tuple]] = {}   # date -> HH:MM -> ohlc
        self._last_key: dict[str, str] = {}              # date -> HH:MM
        self._rejected: dict[str, list[str]] = {}        # date -> [HH:MM]

    # ── ingest ──────────────────────────────────────────────────────────
    def add(self, ts: datetime, value) -> FrozenMinute | None:
        """Feed one sample. Returns a FrozenMinute only at a minute rollover.

        A `None`/unusable value never rolls the minute forward on its own: it is
        simply not recorded, so a feed outage leaves the minute short of fresh
        samples and the staleness guard drops it at the boundary.
        """
        spot = _as_float(value)
        trade_date = ts.strftime("%Y-%m-%d")
        minute = ts.strftime("%H:%M")

        current = self._open
        if current is not None and (current.minute != minute
                                    or current.trade_date != trade_date):
            frozen = self._freeze(current)
            self._open = (_OpenMinute(trade_date, minute, spot, ts)
                          if spot is not None else None)
            return frozen

        if spot is None:
            return None
        if current is None:
            self._open = _OpenMinute(trade_date, minute, spot, ts)
            return None
        current.update(spot, ts)
        return None

    def _freeze(self, minute: _OpenMinute) -> FrozenMinute | None:
        """Close out an ended minute, or drop it when sampling went stale."""
        end = _minute_end(minute.trade_date, minute.minute,
                          tzinfo=minute.last_ts.tzinfo)
        if end is None:
            return None
        age = (end - minute.last_ts).total_seconds()
        if age > self.max_staleness_s:
            # Last print landed too early in the minute to be its close.
            self._rejected.setdefault(minute.trade_date, []).append(minute.minute)
            return None
        frozen = FrozenMinute(
            trade_date=minute.trade_date,
            minute=minute.minute,
            open=minute.open,
            high=minute.high,
            low=minute.low,
            close=minute.close,
            last_ts=minute.last_ts,
            samples=minute.samples,
        )
        self._frozen.setdefault(frozen.trade_date, {})[frozen.minute] = \
            frozen.as_ohlc()
        self._last_key[frozen.trade_date] = frozen.minute
        self._prune()
        return frozen

    def _prune(self) -> None:
        """Keep only the current and prior session.

        This lives in an always-on task that is not restarted between trading
        days, so without a bound the frozen map would accrue ~375 minutes per
        session indefinitely.
        """
        while len(self._frozen) > _RETAINED_SESSIONS:
            oldest = min(self._frozen)
            self._frozen.pop(oldest, None)
            self._last_key.pop(oldest, None)
            self._rejected.pop(oldest, None)

    # ── read ────────────────────────────────────────────────────────────
    def minutes_for(self, trade_date: str) -> dict:
        """{'HH:MM': (o,h,l,c)} frozen so far — replay OHLC gap-fill."""
        return dict(self._frozen.get(trade_date, {}))

    def boundary_key(self, trade_date: str) -> str | None:
        """Newest frozen minute as a replay-clock key, or None."""
        minute = self._last_key.get(trade_date)
        return f"{trade_date}T{minute}" if minute else None

    def rejected(self, trade_date: str) -> list[str]:
        return list(self._rejected.get(trade_date, []))

    def reset(self) -> None:
        self._open = None
        self._frozen.clear()
        self._last_key.clear()
        self._rejected.clear()


def _as_float(value):
    try:
        out = float(value)
    except (TypeError, ValueError):
        return None
    # A zero/negative index print is a feed artefact, never a real NIFTY level.
    return out if out > 0 else None


def _minute_end(trade_date: str, minute: str, tzinfo=None) -> datetime | None:
    """End instant of a clock minute, in the sample stream's own timezone.

    The runner samples in IST-aware datetimes; building the boundary naive here
    would make the staleness subtraction raise instead of guarding.
    """
    try:
        start = datetime.strptime(f"{trade_date} {minute}", "%Y-%m-%d %H:%M")
    except ValueError:
        return None
    if tzinfo is not None:
        start = start.replace(tzinfo=tzinfo)
    return start + timedelta(minutes=1)
