"""
Fetches the just-completed 1-min OHLC candle from Kite historical_data and
appends one row to the spot 1-min CSV.

Bug history (fixed): Earlier versions called kite.quote() and wrote
quote["ohlc"] as the per-minute open/high/low. quote["ohlc"] is the day's
running OHLC — open is frozen at 09:15, high/low are the day's running
extremes. Only last_price varied minute-to-minute, so every row had
frozen open/high/low and the file was useless for intra-bar TP/SL
backtesting. The current implementation pulls the most recent COMPLETED
1-min candle via historical_data, which returns proper per-minute
open/high/low/close/volume.

Behavioural notes
-----------------
* On the 09:15 cycle no completed bar exists yet, so we skip the CSV
  write and return the current LTP (used by options_collector for the
  strike band only).
* The CSV row's timestamp is the BAR'S start time (e.g., the 09:15 bar
  is labelled 09:15:00 and is written at the 09:16 cycle). This matches
  the Zerodha minute-candle convention used elsewhere in the codebase.
"""
from __future__ import annotations

import csv
from datetime import datetime, timedelta
from pathlib import Path
from typing import Optional

import pytz

from config.labs_config import UNDERLYINGS, DATA_DIR

IST = pytz.timezone("Asia/Kolkata")

_HEADER = ["timestamp", "open", "high", "low", "close", "volume"]


def _spot_csv_path(underlying: str, trade_date: str) -> Path:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    return DATA_DIR / f"{trade_date}_{underlying}_spot_1min.csv"


def _ensure_header(path: Path) -> None:
    if not path.exists():
        with path.open("w", newline="") as f:
            csv.writer(f).writerow(_HEADER)


def _to_ist_naive(dt: datetime) -> datetime:
    """Return an IST-local naive datetime regardless of input tz."""
    if dt.tzinfo is None:
        return dt
    return dt.astimezone(IST).replace(tzinfo=None)


def _previous_completed_bar(kite, instrument_token: int, ts: datetime) -> Optional[dict]:
    """
    Return the most recently COMPLETED 1-min candle as of ts.
    If ts is 10:15:00 IST, returns the 10:14 bar (covering 10:14:00-10:14:59).
    Returns None if no completed bar exists in the window (e.g., the 09:15 cycle).
    """
    ts_naive = _to_ist_naive(ts)
    bars = kite.historical_data(
        instrument_token=instrument_token,
        from_date=ts_naive - timedelta(minutes=3),
        to_date=ts_naive,
        interval="minute",
    )
    for bar in reversed(bars):
        bar_ts_naive = _to_ist_naive(bar["date"])
        if bar_ts_naive < ts_naive:
            return bar
    return None


def collect_spot(kite, underlying: str, trade_date: str, ts: datetime) -> float:
    """
    Fetch the just-completed 1-min OHLC candle, append one row to the spot CSV,
    and return the bar's close.

    `ts` should be a minute-aligned IST datetime (the current cycle's minute boundary).
    """
    cfg = UNDERLYINGS[underlying]
    bar = _previous_completed_bar(kite, cfg["instrument_token"], ts)

    if bar is None:
        # No completed bar yet (typically the 09:15 cycle, or a brief Kite-historical lag).
        # Fall back to LTP so options_collector still gets a usable spot.
        index_symbol = cfg["index_symbol"]
        ltp = float(kite.quote([index_symbol])[index_symbol]["last_price"])
        return ltp

    bar_ts_str = _to_ist_naive(bar["date"]).strftime("%Y-%m-%d %H:%M:%S")
    row = [
        bar_ts_str,
        float(bar["open"]),
        float(bar["high"]),
        float(bar["low"]),
        float(bar["close"]),
        int(bar.get("volume", 0)),
    ]

    path = _spot_csv_path(underlying, trade_date)
    _ensure_header(path)
    with path.open("a", newline="") as f:
        csv.writer(f).writerow(row)

    return float(bar["close"])
