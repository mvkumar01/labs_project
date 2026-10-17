"""Risk core (trend x vol): the rule, as pure functions over daily bars.

Paper only. This module never calls a broker order API.

From the V2 Strategy Tester (research/scripts/risk_core.py, agreed 2026-10-16; settings are textbook values,
never fitted). A protection layer, not a profit engine: in research it cut the worst fall of buying and
holding the five markets from -37% to -5%, but earned below cash over the last two years.

Book: notional capital split equally across five sleeves - NIFTY 50, NIFTY BANK, BTCUSDT, ETHUSDT and
MCX CRUDEOIL - rebalanced back to equal on the first day of each month (calendar-day book: crypto trades
every day, an NSE / MCX sleeve contributes nothing on a day its market is shut). In each sleeve, at every
close, the fraction held long for the next day:

  trend   average of nine 0/1 rules on the daily close: close > SMA 20 / 50 / 100 / 200; Donchian 20
          (enter on a close above the previous 20-day high, out on a close below the previous 10-day
          low) and Donchian 55 (55-day high / 27-day low); close above its level 30 / 90 / 180 days ago
  vol     RiskMetrics: variance_t = 0.94 variance_t-1 + 0.06 return_t^2, annualised with sqrt(252)
          (NSE, MCX) or sqrt(365) (crypto)
  target  trend x min(1, 0.15 / vol)
  held    changes only when the target moves by 0.10 or more, or falls to 0

A position decided at day t's close earns day t+1's close-to-close return. Costs per unit of notional
traded, per side: index 0.14%, crypto 0.07% plus Binance funding paid by longs, crude 0.05% plus a 0.1%
a month roll while held; the monthly rebalance pays each sleeve's per-side cost on the weight it moves.
Idle money earns nothing (COSTS); the research book also credited 6% on idle index money and crude
collateral plus index dividends (RESEARCH_COSTS, used only for parity). Crypto returns are computed in
USD and converted with the daily USD/INR rate; crude drops the overnight return on the roll days
(19th-22nd of each month: open-to-close instead), as the research did.
"""
from __future__ import annotations

from dataclasses import dataclass

import numpy as np
import pandas as pd

SLEEVES = ("nifty50", "banknifty", "btcusdt", "ethusdt", "crudeoil")
CRYPTO = ("btcusdt", "ethusdt")
SMA = (20, 50, 100, 200)
DONCHIAN = (20, 55)
TSMOM = (30, 90, 180)
TARGET_VOL, LAMBDA, BAND = 0.15, 0.94, 0.10
ROLL_GAP_DAYS = (19, 22)


@dataclass(frozen=True)
class Cost:
    kind: str                       # cash | futures | perp
    per_side: float
    periods_per_year: int
    cash_rate: float = 0.0
    dividend_yield: float = 0.0
    roll_per_month: float = 0.0


COSTS = {
    "nifty50": Cost("cash", 0.0014, 252),
    "banknifty": Cost("cash", 0.0014, 252),
    "btcusdt": Cost("perp", 0.0007, 365),
    "ethusdt": Cost("perp", 0.0007, 365),
    "crudeoil": Cost("futures", 0.0005, 252, roll_per_month=0.001),
}
# The research book's own cost model (Strategy Tester configs/daily.yaml), for the parity check only.
RESEARCH_COSTS = {
    "nifty50": Cost("cash", 0.0014, 252, cash_rate=0.06, dividend_yield=0.013),
    "banknifty": Cost("cash", 0.0014, 252, cash_rate=0.06, dividend_yield=0.008),
    "btcusdt": Cost("perp", 0.0007, 365),
    "ethusdt": Cost("perp", 0.0007, 365),
    "crudeoil": Cost("futures", 0.0005, 252, cash_rate=0.06, dividend_yield=0.06, roll_per_month=0.001),
}


# ------------------------------------------------------------------- rules ---
def _donchian(close, hi_n, lo_x) -> np.ndarray:
    pos = np.full(len(close), np.nan)
    p = 0.0
    for i in range(len(close)):
        if np.isnan(hi_n[i]) or np.isnan(lo_x[i]):
            continue
        if p == 1.0 and close[i] < lo_x[i]:
            p = 0.0
        if close[i] > hi_n[i]:
            p = 1.0
        pos[i] = p
    return pos


def trend(d: pd.DataFrame) -> pd.Series:
    """The nine-rule long-only ensemble, 0..1; NaN until every rule has warmed up."""
    c, h, lo = d["close"], d["high"], d["low"]
    rules = []
    for n in SMA:
        m = c.rolling(n).mean()
        rules.append((c > m).astype(float).where(m.notna()))
    for n in DONCHIAN:
        hi_n = h.rolling(n).max().shift(1).to_numpy(np.float64)
        lo_x = lo.rolling(n // 2).min().shift(1).to_numpy(np.float64)
        rules.append(pd.Series(_donchian(c.to_numpy(np.float64), hi_n, lo_x), index=c.index))
    for n in TSMOM:
        s = np.sign(c / c.shift(n) - 1)
        rules.append((s > 0).astype(float).where(s.notna()))
    return pd.concat(rules, axis=1).mean(axis=1, skipna=False)


def volatility(close: pd.Series, periods_per_year: int) -> pd.Series:
    """RiskMetrics exponentially weighted daily volatility, annualised."""
    var = close.pct_change().pow(2).ewm(alpha=1 - LAMBDA, adjust=False).mean()
    return np.sqrt(var * periods_per_year)


def banded(target: pd.Series) -> pd.Series:
    """Keep yesterday's fraction unless the target moved by BAND or more, or fell to 0."""
    out, cur = np.zeros(len(target)), 0.0
    for i, x in enumerate(target.to_numpy(float)):
        x = x if np.isfinite(x) else 0.0
        if x == 0.0 or abs(x - cur) >= BAND:
            cur = x
        out[i] = cur
    return pd.Series(out, index=target.index)


def remove_roll_gaps(d: pd.DataFrame, days=ROLL_GAP_DAYS) -> pd.DataFrame:
    """Kite's continuous futures series is not roll-adjusted: on the roll-window days the return is taken
    open-to-close (dropping the overnight jump between contracts) and the prices are rebuilt from it."""
    c = d["close"]
    r = c.pct_change()
    win = (d.index.day >= days[0]) & (d.index.day <= days[1])
    r = r.where(~win, d["close"] / d["open"] - 1).fillna(0.0)
    f = (c.iloc[0] * (1 + r).cumprod()) / c
    out = d.copy()
    for k in ("open", "high", "low", "close"):
        out[k] = d[k] * f
    return out


def held(d: pd.DataFrame, periods_per_year: int) -> pd.DataFrame:
    """Per day: trend, vol, target and the fraction held into the next day."""
    t = trend(d)
    vol = volatility(d["close"], periods_per_year)
    size = (TARGET_VOL / vol).clip(upper=1.0)
    target = t * size
    return pd.DataFrame({"trend": t, "vol": vol, "target": target, "held": banded(target)})


def sleeve_returns(d: pd.DataFrame, pos: pd.Series, cost: Cost, funding: pd.Series | None = None) -> pd.Series:
    """Daily return of the sleeve: the fraction decided at t's close earns t+1's return."""
    ret = d["close"].pct_change().fillna(0.0)
    p = pos.shift(1).fillna(0.0)
    turn = p.diff().abs().fillna(p.abs())
    ppy = cost.periods_per_year
    rc, q = cost.cash_rate / ppy, cost.dividend_yield / ppy
    if cost.kind == "cash":
        return p * (ret + q) + (1 - p).clip(lower=0) * rc - turn * cost.per_side
    if cost.kind == "futures":
        roll = p.abs() * cost.roll_per_month / (ppy / 12)
        return rc + p * (ret - (rc - q)) - turn * cost.per_side - roll
    if cost.kind == "perp":
        f = funding.reindex(ret.index).fillna(0.0) if funding is not None else 0.0
        return p * ret - turn * cost.per_side - p * f + (1 - p.abs()).clip(lower=0) * rc
    raise ValueError(cost.kind)


def to_inr(r: pd.Series, usd_inr: pd.Series) -> pd.Series:
    """USD returns -> INR returns with the daily USD/INR rate (last published rate on or before each day)."""
    fx = usd_inr.copy()
    fx.index = pd.to_datetime(fx.index)
    f = fx.reindex(pd.date_range(fx.index.min(), r.index.max(), freq="D")).ffill().reindex(r.index)
    return (1 + r) * (1 + f.pct_change().fillna(0.0)) - 1


def calendar(streams: dict) -> pd.DataFrame:
    """Sleeve returns on a calendar-day index over the period every sleeve covers (0 when shut)."""
    lo = max(s.index.min() for s in streams.values())
    hi = min(s.index.max() for s in streams.values())
    idx = pd.date_range(lo, hi, freq="D")
    return pd.DataFrame({k: s.reindex(idx).fillna(0.0) for k, s in streams.items()})


def book(R: pd.DataFrame, per_side: dict, capital: float = 1.0) -> pd.DataFrame:
    """Equal-weight book rebalanced on the first day of each month, paying each sleeve's per-side cost on
    the weight it moves. Returns per day the book return and every sleeve's value (start = capital)."""
    names = list(R.columns)
    cost = np.array([per_side[k] for k in names])
    r = R.to_numpy()
    n, m = r.shape
    v = np.full(m, capital / m)
    months = R.index.to_period("M")
    out, vals = np.zeros(n), np.zeros((n, m))
    for t in range(n):
        if t == 0 or months[t] != months[t - 1]:
            total = v.sum()
            w = np.full(m, 1.0 / m)
            fee = (np.abs(w - v / total) * cost).sum()
            v = w * total * (1.0 - fee)
            prev = total
        else:
            prev = v.sum()
        v = v * (1.0 + r[t])
        out[t] = v.sum() / prev - 1.0
        vals[t] = v
    df = pd.DataFrame(vals, index=R.index, columns=[f"{k}_value" for k in names])
    df.insert(0, "book_return", out)
    df.insert(1, "book_value", vals.sum(axis=1))
    return df


def stats(r: pd.Series, periods_per_year: int = 365, cash_rate: float = 0.06) -> dict:
    """CAGR, Sharpe above cash, worst fall, on a return stream."""
    r = r.dropna()
    if len(r) < 2:
        return {}
    eq = (1 + r).cumprod()
    yrs = len(r) / periods_per_year
    sd = r.std()
    return {"cagr": float(eq.iloc[-1] ** (1 / yrs) - 1) if eq.iloc[-1] > 0 else -1.0,
            "sharpe": float((r.mean() - cash_rate / periods_per_year) / sd * np.sqrt(periods_per_year)) if sd > 0 else float("nan"),
            "max_dd": float((eq / eq.cummax() - 1).min()), "final_multiple": float(eq.iloc[-1])}


def run(bars: dict, funding: dict, usd_inr: pd.Series, start: str, costs: dict = COSTS, capital: float = 1.0) -> dict:
    """bars: sleeve -> daily OHLC (date index; crude as Kite's raw continuous series, history from about
    400 days before ``start``); funding: crypto sleeve -> funding paid by a long per day (USD return units).
    Returns {"book": per-day frame, "sleeves": sleeve -> per-day frame (trend, vol, target, held, return)}."""
    streams, frames = {}, {}
    for k in SLEEVES:
        d = bars[k].sort_index()
        d = d[~d.index.duplicated(keep="last")]
        if k == "crudeoil":
            d = remove_roll_gaps(d)
        c = costs[k]
        h = held(d, c.periods_per_year)
        r = sleeve_returns(d, h["held"], c, funding.get(k))
        keep = d.index >= pd.Timestamp(start)
        r = r[keep]
        if k in CRYPTO:
            r = to_inr(r, usd_inr)
        streams[k] = r
        frames[k] = h[keep].assign(ret=r, close=d["close"][keep])
    b = book(calendar(streams), {k: costs[k].per_side for k in SLEEVES}, capital)
    return {"book": b, "sleeves": frames}
