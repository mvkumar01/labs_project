"""
Reads raw 1-min CSV files from data/live/ into DataFrames.
"""
from datetime import date
from pathlib import Path

import pandas as pd

from config.labs_config import DATA_DIR


def load_spot_1min(underlying: str, trade_date: str) -> pd.DataFrame:
    """
    Returns a DataFrame with columns [timestamp, open, high, low, close, volume].
    timestamp is a timezone-naive IST datetime, used as the index.
    Returns empty DataFrame if the file doesn't exist yet.
    """
    path = DATA_DIR / f"{trade_date}_{underlying}_spot_1min.csv"
    if not path.exists():
        return pd.DataFrame(columns=["timestamp", "open", "high", "low", "close", "volume"])

    df = pd.read_csv(path, parse_dates=["timestamp"])
    df = df.sort_values("timestamp").reset_index(drop=True)
    df.set_index("timestamp", inplace=True)
    return df


def load_options_1min(underlying: str, trade_date: str) -> pd.DataFrame:
    """
    Returns all options rows for the date.  No index set — callers filter by timestamp.
    Returns empty DataFrame if the file doesn't exist.
    """
    path = DATA_DIR / f"{trade_date}_{underlying}_options_1min.csv"
    if not path.exists():
        return pd.DataFrame()

    df = pd.read_csv(path, parse_dates=["timestamp"])
    return df.sort_values("timestamp").reset_index(drop=True)


def latest_ltp(options_df: pd.DataFrame, symbol: str) -> float | None:
    """Return the most recent LTP for a given option symbol, or None."""
    rows = options_df[options_df["symbol"] == symbol]
    if rows.empty:
        return None
    return float(rows.iloc[-1]["ltp"])
