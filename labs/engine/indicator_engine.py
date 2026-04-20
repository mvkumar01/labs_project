"""
Technical indicators for the Labs strategy engine.
All functions operate on a completed-bar OHLCV DataFrame with at minimum a 'close' column.
"""
import pandas as pd
import numpy as np


# ── RSI ──────────────────────────────────────────────────────────────────────

def compute_rsi(close: pd.Series, period: int = 3) -> pd.Series:
    delta  = close.diff()
    gain   = delta.clip(lower=0)
    loss   = (-delta).clip(lower=0)
    avg_g  = gain.ewm(com=period - 1, min_periods=period).mean()
    avg_l  = loss.ewm(com=period - 1, min_periods=period).mean()
    rs     = avg_g / avg_l.replace(0, np.nan)
    return 100 - (100 / (1 + rs))


# ── EMA / SMA ────────────────────────────────────────────────────────────────

def compute_ema(close: pd.Series, period: int) -> pd.Series:
    return close.ewm(span=period, adjust=False).mean()


def compute_sma(close: pd.Series, period: int) -> pd.Series:
    return close.rolling(window=period).mean()


# ── ADX / DI+ / DI- ─────────────────────────────────────────────────────────

def compute_adx(df: pd.DataFrame, period: int = 14) -> pd.DataFrame:
    """
    Returns a DataFrame with columns: adx, di_plus, di_minus.
    Requires df to have columns: high, low, close.
    Uses Wilder's smoothing (equivalent to EMA with alpha=1/period).
    """
    high  = df["high"]
    low   = df["low"]
    close = df["close"]

    prev_close = close.shift(1)
    prev_high  = high.shift(1)
    prev_low   = low.shift(1)

    tr = pd.concat([
        high - low,
        (high - prev_close).abs(),
        (low  - prev_close).abs(),
    ], axis=1).max(axis=1)

    plus_dm  = high - prev_high
    minus_dm = prev_low - low

    plus_dm  = plus_dm.where((plus_dm > minus_dm) & (plus_dm > 0), 0.0)
    minus_dm = minus_dm.where((minus_dm > plus_dm) & (minus_dm > 0), 0.0)

    alpha = 1.0 / period
    atr       = tr.ewm(alpha=alpha, adjust=False).mean()
    smooth_pdm = plus_dm.ewm(alpha=alpha, adjust=False).mean()
    smooth_mdm = minus_dm.ewm(alpha=alpha, adjust=False).mean()

    di_plus  = 100 * smooth_pdm / atr.replace(0, np.nan)
    di_minus = 100 * smooth_mdm / atr.replace(0, np.nan)

    dx  = 100 * (di_plus - di_minus).abs() / (di_plus + di_minus).replace(0, np.nan)
    adx = dx.ewm(alpha=alpha, adjust=False).mean()

    return pd.DataFrame({"adx": adx, "di_plus": di_plus, "di_minus": di_minus}, index=df.index)


# ── Combined ─────────────────────────────────────────────────────────────────

def compute_all(
    df: pd.DataFrame,
    rsi_period: int = 3,
    ema_fast: int = 9,
    ema_slow: int = 13,
    sma_period: int = 50,
    adx_period: int = 14,
) -> pd.DataFrame:
    """
    Adds indicator columns to a copy of df.

    Added columns:
        rsi, ema_fast, ema_slow, sma,
        ema_cross_up, ema_cross_down,
        adx, di_plus, di_minus   (only when high/low columns present)
    """
    out = df.copy()
    out["rsi"]      = compute_rsi(out["close"], rsi_period)
    out["ema_fast"] = compute_ema(out["close"], ema_fast)
    out["ema_slow"] = compute_ema(out["close"], ema_slow)
    out["sma"]      = compute_sma(out["close"], sma_period)

    fast_above           = out["ema_fast"] > out["ema_slow"]
    out["ema_cross_up"]   = fast_above & (~fast_above.shift(1).fillna(False))
    out["ema_cross_down"] = (~fast_above) & fast_above.shift(1).fillna(False)

    if {"high", "low"}.issubset(out.columns):
        adx_df = compute_adx(out, adx_period)
        out["adx"]      = adx_df["adx"]
        out["di_plus"]  = adx_df["di_plus"]
        out["di_minus"] = adx_df["di_minus"]

    return out


# ── Single-value helpers used by condition_evaluator ─────────────────────────

def rsi_value(df: pd.DataFrame, period: int) -> float:
    s = compute_rsi(df["close"], period)
    return float(s.iloc[-1]) if not s.empty else float("nan")


def ema_value(df: pd.DataFrame, period: int) -> float:
    s = compute_ema(df["close"], period)
    return float(s.iloc[-1]) if not s.empty else float("nan")


def sma_value(df: pd.DataFrame, period: int) -> float:
    s = compute_sma(df["close"], period)
    return float(s.iloc[-1]) if not s.empty else float("nan")


def adx_values(df: pd.DataFrame, period: int) -> tuple[float, float, float]:
    """Returns (adx, di_plus, di_minus) for the last bar."""
    if not {"high", "low"}.issubset(df.columns):
        return float("nan"), float("nan"), float("nan")
    adx_df = compute_adx(df, period)
    row = adx_df.iloc[-1]
    return float(row["adx"]), float(row["di_plus"]), float(row["di_minus"])
