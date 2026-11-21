"""Strategy library.

Every strategy maps OHLC data + params to a *target position* series in {-1, 0, +1},
evaluated on the bar's close. The engine executes changes on the NEXT bar's open, so
there is no look-ahead.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Callable

import numpy as np
import pandas as pd

from . import indicators as ind


@dataclass
class Param:
    name: str
    default: float
    lo: float
    hi: float
    integer: bool = True
    label: str = ""


@dataclass
class Strategy:
    key: str
    label: str
    family: str
    description: str
    params: list[Param]
    fn: Callable[[pd.DataFrame, dict, bool], pd.Series]
    overlays: Callable[[pd.DataFrame, dict], dict] = field(default=lambda df, p: {})
    title: Callable[[dict], str] | None = None

    def defaults(self) -> dict:
        return {p.name: p.default for p in self.params}

    def clean(self, params: dict | None) -> dict:
        out = self.defaults()
        for p in self.params:
            if params and params.get(p.name) is not None:
                try:
                    v = float(params[p.name])
                except (TypeError, ValueError):
                    continue
                v = min(max(v, p.lo), p.hi)
                out[p.name] = int(round(v)) if p.integer else round(v, 4)
        return out

    def warmup(self, params: dict) -> int:
        nums = [float(v) for k, v in params.items() if k not in ("lower", "upper", "std", "mult", "exit_level")]
        return int(max(nums) * 3) + 5 if nums else 5

    def name(self, params: dict) -> str:
        if self.title:
            return self.title(params)
        return self.label


def _state_machine(long_entry, long_exit, short_entry, short_exit, allow_short: bool,
                   index: pd.Index) -> pd.Series:
    le, lx = np.asarray(long_entry, bool), np.asarray(long_exit, bool)
    se, sx = np.asarray(short_entry, bool), np.asarray(short_exit, bool)
    pos = np.zeros(len(index))
    cur = 0
    for i in range(len(index)):
        if cur == 1 and lx[i]:
            cur = 0
        elif cur == -1 and sx[i]:
            cur = 0
        if cur == 0:
            if le[i]:
                cur = 1
            elif allow_short and se[i]:
                cur = -1
        elif cur == 1 and allow_short and se[i]:
            cur = -1
        elif cur == -1 and le[i]:
            cur = 1
        pos[i] = cur
    return pd.Series(pos, index=index)


def _regime(cond_long: pd.Series, cond_valid: pd.Series, allow_short: bool) -> pd.Series:
    pos = np.where(cond_long, 1.0, -1.0 if allow_short else 0.0)
    return pd.Series(np.where(cond_valid, pos, 0.0), index=cond_long.index)


# ---------------------------------------------------------------- implementations
def buy_hold(df, p, allow_short):
    return pd.Series(1.0, index=df.index)


def sma_cross(df, p, allow_short):
    f, s = ind.sma(df.Close, p["fast"]), ind.sma(df.Close, p["slow"])
    return _regime(f > s, f.notna() & s.notna(), allow_short)


def ema_cross(df, p, allow_short):
    f, s = ind.ema(df.Close, p["fast"]), ind.ema(df.Close, p["slow"])
    return _regime(f > s, f.notna() & s.notna(), allow_short)


def price_sma(df, p, allow_short):
    m = ind.sma(df.Close, p["period"])
    return _regime(df.Close > m, m.notna(), allow_short)


def rsi_reversion(df, p, allow_short):
    r = ind.rsi(df.Close, p["period"])
    ex = p.get("exit_level", 50)
    return _state_machine(r < p["lower"], r > ex, r > p["upper"], r < ex, allow_short, df.index)


def macd_strategy(df, p, allow_short):
    line, sig, _ = ind.macd(df.Close, p["fast"], p["slow"], p["signal"])
    return _regime(line > sig, line.notna() & sig.notna(), allow_short)


def bollinger_reversion(df, p, allow_short):
    mid, up, lo = ind.bollinger(df.Close, p["period"], p["std"])
    c = df.Close
    return _state_machine(c < lo, c > mid, c > up, c < mid, allow_short, df.index)


def bollinger_breakout(df, p, allow_short):
    mid, up, lo = ind.bollinger(df.Close, p["period"], p["std"])
    c = df.Close
    return _state_machine(c > up, c < mid, c < lo, c > mid, allow_short, df.index)


def donchian_breakout(df, p, allow_short):
    up_e, lo_e = ind.donchian(df, p["entry"])
    up_x, lo_x = ind.donchian(df, p["exit"])
    c = df.Close
    return _state_machine(c > up_e, c < lo_x, c < lo_e, c > up_x, allow_short, df.index)


def momentum(df, p, allow_short):
    r = df.Close / df.Close.shift(int(p["lookback"])) - 1
    return _regime(r > 0, r.notna(), allow_short)


def supertrend_strategy(df, p, allow_short):
    t = ind.supertrend(df, p["period"], p["mult"])
    return _regime(t > 0, t.notna(), allow_short)


def stochastic_strategy(df, p, allow_short):
    k, d = ind.stochastic(df, p["k"], p["d"])
    cross_up = (k > d) & (k.shift() <= d.shift())
    cross_dn = (k < d) & (k.shift() >= d.shift())
    return _state_machine(cross_up & (k < p["lower"] + 10), k > p["upper"],
                          cross_dn & (k > p["upper"] - 10), k < p["lower"], allow_short, df.index)


# ---------------------------------------------------------------- chart overlays
def _ov_ma(kind):
    def f(df, p):
        fn = ind.sma if kind == "sma" else ind.ema
        u = kind.upper()
        return {f"{u} {p['fast']}": fn(df.Close, p["fast"]), f"{u} {p['slow']}": fn(df.Close, p["slow"])}
    return f


def _ov_boll(df, p):
    mid, up, lo = ind.bollinger(df.Close, p["period"], p["std"])
    return {"Upper band": up, "Middle": mid, "Lower band": lo}


def _ov_donch(df, p):
    up, lo = ind.donchian(df, p["entry"])
    return {f"{p['entry']}-bar high": up, f"{p['entry']}-bar low": lo}


STRATEGIES: dict[str, Strategy] = {s.key: s for s in [
    Strategy("buy_hold", "Buy & Hold", "Benchmark", "Buy on day one and hold to the end.", [], buy_hold,
             title=lambda p: "Buy & Hold"),
    Strategy("sma_cross", "SMA Crossover", "Trend",
             "Long while the fast simple moving average is above the slow one (golden/death cross).",
             [Param("fast", 50, 2, 300, label="Fast SMA"), Param("slow", 200, 5, 400, label="Slow SMA")],
             sma_cross, _ov_ma("sma"), lambda p: f"SMA {p['fast']}/{p['slow']} crossover"),
    Strategy("ema_cross", "EMA Crossover", "Trend",
             "Long while the fast exponential moving average is above the slow one.",
             [Param("fast", 12, 2, 200, label="Fast EMA"), Param("slow", 26, 5, 400, label="Slow EMA")],
             ema_cross, _ov_ma("ema"), lambda p: f"EMA {p['fast']}/{p['slow']} crossover"),
    Strategy("price_sma", "Price vs Moving Average", "Trend",
             "Long while price closes above its N-day simple moving average (e.g. the 200-DMA filter).",
             [Param("period", 200, 5, 400, label="SMA period")], price_sma,
             lambda df, p: {f"SMA {p['period']}": ind.sma(df.Close, p["period"])},
             lambda p: f"Price above {p['period']}-day SMA"),
    Strategy("rsi", "RSI Mean Reversion", "Mean reversion",
             "Buy when RSI drops below the oversold level, exit when it recovers past the exit level.",
             [Param("period", 14, 2, 50, label="RSI period"), Param("lower", 30, 5, 50, label="Oversold"),
              Param("upper", 70, 50, 95, label="Overbought"), Param("exit_level", 50, 20, 90, label="Exit level")],
             rsi_reversion, title=lambda p: f"RSI({p['period']}) {p['lower']}/{p['upper']} reversion"),
    Strategy("macd", "MACD Signal Cross", "Trend",
             "Long while the MACD line is above its signal line.",
             [Param("fast", 12, 2, 50), Param("slow", 26, 5, 100), Param("signal", 9, 2, 50)],
             macd_strategy, title=lambda p: f"MACD({p['fast']},{p['slow']},{p['signal']})"),
    Strategy("bollinger_reversion", "Bollinger Reversion", "Mean reversion",
             "Buy closes below the lower band, exit at the middle band.",
             [Param("period", 20, 5, 100, label="Period"), Param("std", 2.0, 0.5, 4, integer=False, label="Std devs")],
             bollinger_reversion, _ov_boll, lambda p: f"Bollinger({p['period']}, {p['std']}) reversion"),
    Strategy("bollinger_breakout", "Bollinger Breakout", "Breakout",
             "Buy closes above the upper band, exit below the middle band.",
             [Param("period", 20, 5, 100, label="Period"), Param("std", 2.0, 0.5, 4, integer=False, label="Std devs")],
             bollinger_breakout, _ov_boll, lambda p: f"Bollinger({p['period']}, {p['std']}) breakout"),
    Strategy("donchian", "Donchian Breakout (Turtle)", "Breakout",
             "Buy a close above the N-bar high; exit on a close below the M-bar low.",
             [Param("entry", 20, 5, 200, label="Entry channel"), Param("exit", 10, 2, 100, label="Exit channel")],
             donchian_breakout, _ov_donch, lambda p: f"Donchian {p['entry']}/{p['exit']} breakout"),
    Strategy("momentum", "Time-Series Momentum", "Momentum",
             "Long when the trailing N-bar return is positive.",
             [Param("lookback", 126, 5, 400, label="Lookback")], momentum,
             title=lambda p: f"{p['lookback']}-bar momentum"),
    Strategy("supertrend", "Supertrend", "Trend",
             "Follow the ATR-based Supertrend direction.",
             [Param("period", 10, 3, 50, label="ATR period"), Param("mult", 3.0, 1, 6, integer=False, label="Multiplier")],
             supertrend_strategy, title=lambda p: f"Supertrend({p['period']}, {p['mult']})"),
    Strategy("stochastic", "Stochastic Oscillator", "Mean reversion",
             "Buy a %K/%D bullish cross in oversold territory; exit when overbought.",
             [Param("k", 14, 3, 50), Param("d", 3, 1, 20), Param("lower", 20, 5, 40), Param("upper", 80, 60, 95)],
             stochastic_strategy, title=lambda p: f"Stochastic({p['k']},{p['d']})"),
]}


def get(key: str) -> Strategy:
    if key not in STRATEGIES:
        raise ValueError(f"Unknown strategy '{key}'. Available: {', '.join(STRATEGIES)}")
    return STRATEGIES[key]


def catalog() -> list[dict]:
    return [{"key": s.key, "label": s.label, "family": s.family, "description": s.description,
             "params": [{"name": p.name, "default": p.default, "label": p.label or p.name} for p in s.params]}
            for s in STRATEGIES.values()]
