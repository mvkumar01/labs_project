"""
Shared helpers for pulling completed 1-min OHLC candles from Kite historical_data.

Extracted from spot_collector so futures_collector can reuse the same logic
without duplicating the OHLC-vs-quote bug fix documented in spot_collector.py
(kite.quote()['ohlc'] is the day's running OHLC, not a per-minute bar).
"""
from __future__ import annotations

from datetime import datetime, timedelta
from typing import Optional

import pytz

IST = pytz.timezone("Asia/Kolkata")


def to_ist_naive(dt) -> datetime:
    """Return an IST-local naive datetime regardless of input tz."""
    if isinstance(dt, str):
        dt = datetime.fromisoformat(dt)
    if dt.tzinfo is None:
        return dt
    return dt.astimezone(IST).replace(tzinfo=None)


def previous_completed_bar(kite, instrument_token: int, ts: datetime) -> Optional[dict]:
    """
    Return the most recently COMPLETED 1-min candle as of ts.
    If ts is 10:15:00 IST, returns the 10:14 bar (covering 10:14:00-10:14:59).
    Returns None if no completed bar exists in the window (e.g., the 09:15 cycle).
    """
    ts_naive = to_ist_naive(ts)
    bars = kite.historical_data(
        instrument_token=instrument_token,
        from_date=ts_naive - timedelta(minutes=3),
        to_date=ts_naive,
        interval="minute",
    )
    for bar in reversed(bars):
        bar_ts_naive = to_ist_naive(bar["date"])
        if bar_ts_naive < ts_naive:
            return bar
    return None
