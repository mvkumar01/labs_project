"""
Fetches the just-completed 1-min OHLCV candle for the nearest-expiry futures
contract and appends one row to a per-underlying futures CSV.

Kept as separate files from the spot CSVs (data/live/{date}_{underlying}_futures_1min.csv
vs. ..._spot_1min.csv) — futures price/volume diverge materially from the
underlying index and the contract itself rolls to a new expiry monthly.
"""
from __future__ import annotations

import csv
from datetime import datetime
from pathlib import Path

from config.labs_config import DATA_DIR
from collector.instruments import get_current_future
from collector.kite_bars import previous_completed_bar, to_ist_naive

_HEADER = ["timestamp", "tradingsymbol", "open", "high", "low", "close", "volume"]


def _futures_csv_path(underlying: str, trade_date: str) -> Path:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    return DATA_DIR / f"{trade_date}_{underlying}_futures_1min.csv"


def _ensure_header(path: Path) -> None:
    if not path.exists():
        with path.open("w", newline="") as f:
            csv.writer(f).writerow(_HEADER)


def collect_futures(kite, underlying: str, trade_date: str, ts: datetime) -> bool:
    """
    Fetch the just-completed 1-min OHLCV candle for the nearest-expiry future and
    append one row to the futures CSV.

    Returns False (no-op, e.g. the 09:15 cycle where no completed bar exists yet)
    or True if a row was written. `ts` should be a minute-aligned IST datetime.
    """
    future = get_current_future(kite, underlying)
    bar = previous_completed_bar(kite, future["instrument_token"], ts)
    if bar is None:
        return False

    bar_ts_str = to_ist_naive(bar["date"]).strftime("%Y-%m-%d %H:%M:%S")
    row = [
        bar_ts_str,
        future["tradingsymbol"],
        float(bar["open"]),
        float(bar["high"]),
        float(bar["low"]),
        float(bar["close"]),
        int(bar.get("volume", 0)),
    ]

    path = _futures_csv_path(underlying, trade_date)
    _ensure_header(path)
    with path.open("a", newline="") as f:
        csv.writer(f).writerow(row)

    return True
