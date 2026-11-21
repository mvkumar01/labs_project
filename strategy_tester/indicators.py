"""Technical indicators (pure pandas, no look-ahead)."""
from __future__ import annotations

import numpy as np
import pandas as pd


def sma(s: pd.Series, n: int) -> pd.Series:
    return s.rolling(int(n), min_periods=int(n)).mean()


def ema(s: pd.Series, n: int) -> pd.Series:
    return s.ewm(span=int(n), adjust=False, min_periods=int(n)).mean()


def rsi(close: pd.Series, n: int = 14) -> pd.Series:
    delta = close.diff()
    gain = delta.clip(lower=0).ewm(alpha=1 / n, adjust=False, min_periods=n).mean()
    loss = (-delta.clip(upper=0)).ewm(alpha=1 / n, adjust=False, min_periods=n).mean()
    rs = gain / loss.replace(0, np.nan)
    out = 100 - 100 / (1 + rs)
    return out.fillna(100).where(loss.notna())


def macd(close: pd.Series, fast: int = 12, slow: int = 26, signal: int = 9):
    line = ema(close, fast) - ema(close, slow)
    sig = line.ewm(span=int(signal), adjust=False, min_periods=int(signal)).mean()
    return line, sig, line - sig


def bollinger(close: pd.Series, n: int = 20, k: float = 2.0):
    mid = sma(close, n)
    sd = close.rolling(int(n), min_periods=int(n)).std(ddof=0)
    return mid, mid + k * sd, mid - k * sd


def true_range(df: pd.DataFrame) -> pd.Series:
    prev = df["Close"].shift()
    return pd.concat([df["High"] - df["Low"], (df["High"] - prev).abs(), (df["Low"] - prev).abs()],
                     axis=1).max(axis=1)


def atr(df: pd.DataFrame, n: int = 14) -> pd.Series:
    return true_range(df).ewm(alpha=1 / n, adjust=False, min_periods=n).mean()


def donchian(df: pd.DataFrame, n: int):
    """Channel of the *previous* n bars (so a breakout is measurable on the current bar)."""
    upper = df["High"].rolling(int(n), min_periods=int(n)).max().shift()
    lower = df["Low"].rolling(int(n), min_periods=int(n)).min().shift()
    return upper, lower


def stochastic(df: pd.DataFrame, k: int = 14, d: int = 3):
    ll = df["Low"].rolling(int(k), min_periods=int(k)).min()
    hh = df["High"].rolling(int(k), min_periods=int(k)).max()
    pk = 100 * (df["Close"] - ll) / (hh - ll).replace(0, np.nan)
    return pk, pk.rolling(int(d), min_periods=int(d)).mean()


def supertrend(df: pd.DataFrame, n: int = 10, mult: float = 3.0) -> pd.Series:
    """Returns +1 (uptrend) / -1 (downtrend) series."""
    a = atr(df, n).to_numpy()
    hl2 = ((df["High"] + df["Low"]) / 2).to_numpy()
    close = df["Close"].to_numpy()
    upper = hl2 + mult * a
    lower = hl2 - mult * a
    fu, fl = upper.copy(), lower.copy()
    trend = np.zeros(len(df))
    for i in range(len(df)):
        if np.isnan(a[i]):
            trend[i] = np.nan
            continue
        if i == 0 or np.isnan(trend[i - 1]):
            trend[i] = 1
            continue
        fu[i] = upper[i] if (upper[i] < fu[i - 1] or close[i - 1] > fu[i - 1]) else fu[i - 1]
        fl[i] = lower[i] if (lower[i] > fl[i - 1] or close[i - 1] < fl[i - 1]) else fl[i - 1]
        if trend[i - 1] == 1:
            trend[i] = -1 if close[i] < fl[i] else 1
        else:
            trend[i] = 1 if close[i] > fu[i] else -1
    return pd.Series(trend, index=df.index)
