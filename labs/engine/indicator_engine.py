"""
Technical indicators for the Labs strategy engine.
All functions operate on a completed-bar OHLCV DataFrame with a 'close' column.
Returns pd.Series or adds columns to a copy of the DataFrame.
"""
import pandas as pd
import numpy as np


def compute_rsi(close: pd.Series, period: int = 3) -> pd.Series:
    delta  = close.diff()
    gain   = delta.clip(lower=0)
    loss   = (-delta).clip(lower=0)
    avg_g  = gain.ewm(com=period - 1, min_periods=period).mean()
    avg_l  = loss.ewm(com=period - 1, min_periods=period).mean()
    rs     = avg_g / avg_l.replace(0, np.nan)
    return 100 - (100 / (1 + rs))


def compute_ema(close: pd.Series, period: int) -> pd.Series:
    return close.ewm(span=period, adjust=False).mean()


def compute_sma(close: pd.Series, period: int) -> pd.Series:
    return close.rolling(window=period).mean()


def compute_all(
    df: pd.DataFrame,
    rsi_period: int = 3,
    ema_fast: int = 9,
    ema_slow: int = 13,
    sma_period: int = 50,
) -> pd.DataFrame:
    """
    Adds indicator columns to a copy of df.
    Added columns:
        rsi, ema_fast, ema_slow, sma,
        ema_cross_up   (True on bar where ema_fast crossed above ema_slow),
        ema_cross_down (True on bar where ema_fast crossed below ema_slow)
    """
    out = df.copy()
    out["rsi"]      = compute_rsi(out["close"], rsi_period)
    out["ema_fast"] = compute_ema(out["close"], ema_fast)
    out["ema_slow"] = compute_ema(out["close"], ema_slow)
    out["sma"]      = compute_sma(out["close"], sma_period)

    fast_above      = out["ema_fast"] > out["ema_slow"]
    out["ema_cross_up"]   = fast_above & (~fast_above.shift(1).fillna(False))
    out["ema_cross_down"] = (~fast_above) & fast_above.shift(1).fillna(False)

    return out
