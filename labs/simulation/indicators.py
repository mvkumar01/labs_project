"""Replay-visible chart transforms and technical indicators."""
from __future__ import annotations

import numpy as np
import pandas as pd

from labs.engine.indicator_engine import compute_adx, compute_ema, compute_rsi, compute_sma
from labs.engine.resampler import get_resampled_data


def visible_bars(frame_1m: pd.DataFrame, replay_index: int,
                 timeframe: str = "1m") -> pd.DataFrame:
    """Return only candles visible through replay_index, with completed HTF bars."""
    if replay_index < 0:
        return frame_1m.iloc[0:0].copy()
    visible = frame_1m.iloc[: replay_index + 1].copy()
    now = visible.index[-1] + pd.Timedelta(minutes=1)
    return get_resampled_data(visible, timeframe, now=now)


def heikin_ashi(frame: pd.DataFrame) -> pd.DataFrame:
    if frame.empty:
        return frame.copy()
    out = frame.copy()
    ha_close = frame[["open", "high", "low", "close"]].mean(axis=1)
    ha_open = pd.Series(index=frame.index, dtype=float)
    ha_open.iloc[0] = (frame["open"].iloc[0] + frame["close"].iloc[0]) / 2
    for idx in range(1, len(frame)):
        ha_open.iloc[idx] = (ha_open.iloc[idx - 1] + ha_close.iloc[idx - 1]) / 2
    out["open"] = ha_open
    out["close"] = ha_close
    out["high"] = pd.concat([frame["high"], ha_open, ha_close], axis=1).max(axis=1)
    out["low"] = pd.concat([frame["low"], ha_open, ha_close], axis=1).min(axis=1)
    return out


def renko(frame: pd.DataFrame, brick_size: float | None = None) -> pd.DataFrame:
    if frame.empty:
        return frame.copy()
    if not brick_size or brick_size <= 0:
        atr = compute_atr(frame, 14).dropna()
        brick_size = float(atr.iloc[-1]) if not atr.empty else max(0.05, float(frame["close"].iloc[-1]) * 0.002)
    bricks = []
    last = float(frame["close"].iloc[0])
    for ts, close in frame["close"].items():
        delta = float(close) - last
        while abs(delta) >= brick_size:
            direction = 1 if delta > 0 else -1
            new_close = last + direction * brick_size
            bricks.append({
                "timestamp": ts, "open": last, "high": max(last, new_close),
                "low": min(last, new_close), "close": new_close, "volume": 0,
            })
            last = new_close
            delta = float(close) - last
    if not bricks:
        return frame.iloc[0:0].copy()
    return pd.DataFrame(bricks).set_index("timestamp")


def compute_atr(frame: pd.DataFrame, period: int = 14) -> pd.Series:
    prev_close = frame["close"].shift(1)
    tr = pd.concat([
        frame["high"] - frame["low"],
        (frame["high"] - prev_close).abs(),
        (frame["low"] - prev_close).abs(),
    ], axis=1).max(axis=1)
    return tr.ewm(alpha=1 / period, adjust=False, min_periods=period).mean()


def compute_supertrend(frame: pd.DataFrame, period: int = 10,
                       multiplier: float = 3.0) -> pd.Series:
    atr = compute_atr(frame, period)
    midpoint = (frame["high"] + frame["low"]) / 2
    upper = midpoint + multiplier * atr
    lower = midpoint - multiplier * atr
    trend = pd.Series(index=frame.index, dtype=float)
    direction = 1
    for i in range(len(frame)):
        if i == 0 or pd.isna(atr.iloc[i]):
            trend.iloc[i] = np.nan
            continue
        if frame["close"].iloc[i] > upper.iloc[i - 1]:
            direction = 1
        elif frame["close"].iloc[i] < lower.iloc[i - 1]:
            direction = -1
        trend.iloc[i] = lower.iloc[i] if direction > 0 else upper.iloc[i]
    return trend


def indicator_series(frame: pd.DataFrame, specs: list[dict]) -> dict[str, list[dict]]:
    output = {}
    for index, spec in enumerate(specs or []):
        name = str(spec.get("name") or "").upper()
        params = spec.get("params") or {}
        period = max(1, int(params.get("period") or 14))
        series_map = {}
        if name == "SMA":
            series_map[f"SMA {period}"] = compute_sma(frame["close"], period)
        elif name == "EMA":
            series_map[f"EMA {period}"] = compute_ema(frame["close"], period)
        elif name == "VWAP":
            typical = (frame["high"] + frame["low"] + frame["close"]) / 3
            volume = frame["volume"].replace(0, np.nan)
            series_map["VWAP"] = (typical * volume).cumsum() / volume.cumsum()
        elif name == "BOLLINGER":
            middle = compute_sma(frame["close"], period)
            std = frame["close"].rolling(period).std()
            mult = float(params.get("stddev") or 2)
            series_map = {"BB Upper": middle + mult * std, "BB Mid": middle, "BB Lower": middle - mult * std}
        elif name == "RSI":
            series_map[f"RSI {period}"] = compute_rsi(frame["close"], period)
        elif name == "MACD":
            fast = int(params.get("fast") or 12); slow = int(params.get("slow") or 26); signal = int(params.get("signal") or 9)
            macd = compute_ema(frame["close"], fast) - compute_ema(frame["close"], slow)
            signal_line = compute_ema(macd, signal)
            series_map = {"MACD": macd, "MACD Signal": signal_line, "MACD Hist": macd - signal_line}
        elif name == "ADX":
            adx = compute_adx(frame, period)
            series_map = {f"ADX {period}": adx["adx"], "DI+": adx["di_plus"], "DI-": adx["di_minus"]}
        elif name == "ATR":
            series_map[f"ATR {period}"] = compute_atr(frame, period)
        elif name == "SUPERTREND":
            series_map["Supertrend"] = compute_supertrend(frame, period, float(params.get("multiplier") or 3))
        for label, series in series_map.items():
            key = f"{index}:{label}"
            output[key] = [
                {"time": _epoch(ts), "value": round(float(value), 4)}
                for ts, value in series.items() if pd.notna(value)
            ]
    return output


def chart_payload(frame: pd.DataFrame, chart_type: str = "candlestick",
                  brick_size: float | None = None) -> list[dict]:
    transformed = (
        heikin_ashi(frame) if chart_type == "heikin_ashi"
        else renko(frame, brick_size) if chart_type == "renko"
        else frame
    )
    return [
        {
            "time": _epoch(ts), "open": round(float(row.open), 4),
            "high": round(float(row.high), 4), "low": round(float(row.low), 4),
            "close": round(float(row.close), 4), "volume": int(row.volume or 0),
        }
        for ts, row in transformed.iterrows()
    ]


def _epoch(ts) -> int:
    stamp = pd.Timestamp(ts)
    if stamp.tzinfo is None:
        stamp = stamp.tz_localize("Asia/Kolkata")
    return int(stamp.timestamp())
