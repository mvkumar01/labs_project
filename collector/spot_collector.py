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
from datetime import datetime
from pathlib import Path

from config.labs_config import UNDERLYINGS, DATA_DIR
from collector.kite_bars import previous_completed_bar as _previous_completed_bar
from collector.kite_bars import to_ist_naive as _to_ist_naive

_HEADER = ["timestamp", "open", "high", "low", "close", "volume"]


class MarketSessionClosed(RuntimeError):
    """Raised when Kite is serving a stale quote instead of a live session."""


def _spot_csv_path(underlying: str, trade_date: str) -> Path:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    return DATA_DIR / f"{trade_date}_{underlying}_spot_1min.csv"


def _ensure_header(path: Path) -> None:
    if not path.exists():
        with path.open("w", newline="") as f:
            csv.writer(f).writerow(_HEADER)


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
        quote = kite.quote([index_symbol])[index_symbol]
        quote_ts = quote.get("timestamp") or quote.get("last_trade_time")
        if quote_ts is None or _to_ist_naive(quote_ts).date() != _to_ist_naive(ts).date():
            raise MarketSessionClosed(
                f"{underlying} quote is stale or undated for {trade_date}"
            )
        ltp = float(quote["last_price"])
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
