"""
Fetches the current index quote and appends one row to the spot 1-min CSV.
"""
import csv
from datetime import datetime
from pathlib import Path

import pytz

from config.labs_config import UNDERLYINGS, DATA_DIR

IST = pytz.timezone("Asia/Kolkata")


def _spot_csv_path(underlying: str, trade_date: str) -> Path:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    return DATA_DIR / f"{trade_date}_{underlying}_spot_1min.csv"


def _ensure_header(path: Path) -> None:
    if not path.exists():
        with path.open("w", newline="") as f:
            csv.writer(f).writerow(["timestamp", "open", "high", "low", "close", "volume"])


def collect_spot(kite, underlying: str, trade_date: str, ts: datetime) -> float:
    """
    Fetches index LTP, appends one row to the spot CSV, and returns the LTP.
    ts should be a minute-aligned IST datetime.
    """
    symbol = UNDERLYINGS[underlying]["index_symbol"]
    quote  = kite.quote([symbol])[symbol]
    ohlc   = quote.get("ohlc", {})

    row = [
        ts.strftime("%Y-%m-%d %H:%M:%S"),
        ohlc.get("open", quote["last_price"]),
        ohlc.get("high", quote["last_price"]),
        ohlc.get("low",  quote["last_price"]),
        quote["last_price"],
        0,
    ]

    path = _spot_csv_path(underlying, trade_date)
    _ensure_header(path)
    with path.open("a", newline="") as f:
        csv.writer(f).writerow(row)

    return float(quote["last_price"])
