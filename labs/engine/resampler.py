"""
Resamples 1-min OHLCV data to completed bars at any supported timeframe.

Supported timeframes: 1m, 5m, 10m, 15m.
Bar labels are bar-close times. Only completed bars are returned.
"""
from datetime import datetime

import pandas as pd
import pytz

IST = pytz.timezone("Asia/Kolkata")

_TF_MINUTES = {"1m": 1, "5m": 5, "10m": 10, "15m": 15}


def get_resampled_data(df_1min: pd.DataFrame, timeframe: str, now: datetime | None = None) -> pd.DataFrame:
    """
    Resample df_1min (DatetimeIndex, tz-naive IST) to any supported timeframe.
    Returns only completed bars (bar-close < now).

    timeframe: '1m' | '5m' | '10m' | '15m'
    """
    if df_1min.empty:
        return pd.DataFrame(columns=["open", "high", "low", "close", "volume"])

    if now is None:
        now = datetime.now()
    if hasattr(now, "tzinfo") and now.tzinfo is not None:
        now = now.astimezone(IST).replace(tzinfo=None)

    minutes = _TF_MINUTES.get(timeframe)
    if minutes is None:
        raise ValueError(f"Unsupported timeframe: {timeframe!r}. Use one of {list(_TF_MINUTES)}")

    if minutes == 1:
        # Return all 1-min bars whose timestamp < now
        completed = df_1min[df_1min.index < now].copy()
        return completed

    rule = f"{minutes}min"
    resampled = df_1min.resample(rule, label="left", closed="left").agg({
        "open":   "first",
        "high":   "max",
        "low":    "min",
        "close":  "last",
        "volume": "sum",
    }).dropna(subset=["close"])

    # With left-labeled bars, a bar is completed only after its right edge.
    return resampled[(resampled.index + pd.Timedelta(minutes=minutes)) <= now]


# Keep backward-compatible alias used by strategy_runner
def to_5min(df_1min: pd.DataFrame, now: datetime | None = None) -> pd.DataFrame:
    return get_resampled_data(df_1min, "5m", now=now)
