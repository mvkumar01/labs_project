"""
Reads raw 1-min CSV files from data/live/ into DataFrames.
"""
from datetime import date, datetime, timedelta
from pathlib import Path

import pandas as pd

from config.labs_config import DATA_DIR, SHARED_ARCHIVE_DIR, SHARED_LIVE_DIR
from market_data.shared_store import load_options_frame


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


def load_spot_1min_with_warmup(
    underlying: str,
    trade_date: str,
    target_completed_bars: int = 70,
    lookback_days: int = 5,
) -> pd.DataFrame:
    """
    Load today's spot file plus prior trading-day files until we have enough
    1-min history for warmup. Keeps raw files untouched and preserves timestamps.
    """
    frames = []
    live_rows = 0
    historical_rows = 0
    current = datetime.strptime(trade_date, "%Y-%m-%d").date()
    min_rows = max(target_completed_bars * 5, 5)

    for delta in range(0, lookback_days + 1):
        day = current - timedelta(days=delta)
        df = load_spot_1min(underlying, day.isoformat())
        if df.empty:
            continue
        frames.append(df)
        if day == current:
            live_rows = len(df)
        else:
            historical_rows += len(df)
        if live_rows + historical_rows >= min_rows:
            break

    if not frames:
        empty = pd.DataFrame(columns=["open", "high", "low", "close", "volume"])
        empty.attrs["warmup"] = {
            "underlying": underlying,
            "trade_date": trade_date,
            "live_rows": 0,
            "historical_rows": 0,
            "target_completed_bars": target_completed_bars,
        }
        return empty

    combined = pd.concat(reversed(frames), axis=0)
    combined = combined[~combined.index.duplicated(keep="last")]
    combined = combined.sort_index()
    combined.attrs["warmup"] = {
        "underlying": underlying,
        "trade_date": trade_date,
        "live_rows": live_rows,
        "historical_rows": historical_rows,
        "target_completed_bars": target_completed_bars,
    }
    return combined


def load_options_1min(underlying: str, trade_date: str) -> pd.DataFrame:
    """
    Returns all options rows for the date from the shared market store.
    Canonical columns: timestamp, underlying, tradingsymbol, strike, option_type,
                       expiry, ltp, bid, ask, oi, volume, spot
    Returns empty DataFrame if the file doesn't exist.
    """
    try:
        df = load_options_frame(
            underlying,
            trade_date,
            live_root=SHARED_LIVE_DIR,
            archive_root=SHARED_ARCHIVE_DIR,
        )
    except FileNotFoundError:
        return pd.DataFrame()
    df["timestamp"] = pd.to_datetime(df["timestamp"], errors="coerce")
    return df.sort_values("timestamp").reset_index(drop=True)


def latest_ltp(options_df: pd.DataFrame, symbol: str) -> float | None:
    """Return the most recent LTP for a given option tradingsymbol, or None."""
    if options_df.empty:
        return None
    rows = options_df[options_df["tradingsymbol"] == symbol]
    if rows.empty:
        return None
    return float(rows.iloc[-1]["ltp"])
