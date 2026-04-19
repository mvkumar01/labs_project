"""
Resamples 1-min OHLCV data to 5-min completed bars.

Session: 09:15–15:30 IST.
Bar labels are bar-close times: 09:20, 09:25, ..., 15:30.
The currently-open bar is excluded — only completed bars are returned.
"""
from datetime import datetime

import pandas as pd
import pytz

IST = pytz.timezone("Asia/Kolkata")


def to_5min(df_1min: pd.DataFrame, now: datetime | None = None) -> pd.DataFrame:
    """
    df_1min: DataFrame with DatetimeIndex (IST, tz-naive) and columns
             [open, high, low, close, volume].

    Returns a 5-min OHLCV DataFrame with the same columns, indexed by bar-close time.
    Only completed bars (bar-close < current time) are returned.
    """
    if df_1min.empty:
        return pd.DataFrame(columns=["open", "high", "low", "close", "volume"])

    # Resample: label=right gives bar-close label
    resampled = df_1min.resample("5min", label="right", closed="right").agg({
        "open":   "first",
        "high":   "max",
        "low":    "min",
        "close":  "last",
        "volume": "sum",
    }).dropna(subset=["close"])

    if now is None:
        now = datetime.now()

    # Strip tz from now if present so comparison works with tz-naive index
    if hasattr(now, "tzinfo") and now.tzinfo is not None:
        now = now.astimezone(IST).replace(tzinfo=None)

    # Keep only bars whose close time is strictly before now
    completed = resampled[resampled.index < now]
    return completed
