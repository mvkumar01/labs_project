"""Validation checks that decide whether a backtest result is likely to be real.

  walk_forward      settings chosen on a training window only, then scored on the next unseen window; the unseen
                    windows are chained into one out-of-sample equity curve
  random_entries    the same number of trades, with the same holding times and sides, entered at random bars:
                    did the strategy's timing beat chance?
  monthly_stability share of positive months, worst month, longest losing streak, positive years
  bootstrap         confidence ranges for CAGR and Sharpe from resampling whole months
  sharpe_confidence probability the true Sharpe is above zero (Bailey & Lopez de Prado), deflated for the number
                    of strategies tried when a result was picked as the best of several

Everything here only reads prices, signals and the engine's own simulate(); nothing looks past the bar it scores.
"""
from __future__ import annotations

import math
from statistics import NormalDist

import numpy as np
import pandas as pd

_N = NormalDist()
EULER_GAMMA = 0.5772156649


# ----------------------------------------------------------------------------------------------------- helpers
def _sharpe(eq: pd.Series, ppy: float) -> float:
    r = eq.pct_change().dropna()
    return float(r.mean() / r.std() * math.sqrt(ppy)) if len(r) > 5 and r.std() > 0 else 0.0


def _cagr(eq: pd.Series) -> float | None:
    yrs = max((eq.index[-1] - eq.index[0]).days / 365.25, 1 / 365.25)
    return float(((eq.iloc[-1] / eq.iloc[0]) ** (1 / yrs) - 1) * 100) if eq.iloc[0] > 0 and eq.iloc[-1] > 0 else None


def _max_dd(eq: pd.Series) -> float:
    return float((eq / eq.cummax() - 1).min() * 100)


def _grid(strat, p: dict) -> list[dict]:
    """The settings the walk-forward may choose from: the user's, and 0.5x..1.5x of the first two numeric params."""
    numeric = [x for x in strat.params if x.name not in ("exit_level",)]
    if not numeric:
        return [dict(p)]
    mults = (0.5, 0.75, 1.0, 1.25, 1.5)

    def val(prm, m):
        v = min(max(p[prm.name] * m, prm.lo), prm.hi)
        return int(round(v)) if prm.integer else round(v, 2)

    px = numeric[0]
    py = numeric[1] if len(numeric) > 1 else None
    out, seen = [], set()
    for mx in mults:
        for my in (mults if py else (1.0,)):
            q = dict(p)
            q[px.name] = val(px, mx)
            if py:
                q[py.name] = val(py, my)
            if "fast" in q and "slow" in q and q["fast"] >= q["slow"]:
                continue
            k = tuple(sorted(q.items()))
            if k not in seen:
                seen.add(k)
                out.append(q)
    return out


# ------------------------------------------------------------------------------------------------ walk-forward
def walk_forward(df: pd.DataFrame, strat, p: dict, start_i: int, cfg, ppy: float, simulate) -> dict | None:
    """Rolling walk-forward. Folds: train 3 years / test 1 year when there are 5+ years, else 40% / 15% of the bars.
    Each fold picks the grid setting with the best training Sharpe, then trades the test window with it from flat.
    Targets are computed once per setting on the whole frame: every indicator here uses past bars only."""
    n = len(df) - start_i
    years = n / ppy
    if n < 200:
        return None
    if years >= 5:
        train, test = int(round(3 * ppy)), int(round(ppy))
    else:
        train, test = int(n * 0.40), int(n * 0.15)
    if test < 20:
        return None
    grid = _grid(strat, p)
    targets = [strat.fn(df, q, cfg.allow_short) for q in grid]
    folds, pieces, chosen = [], [], []
    a = start_i
    while a + train + test <= len(df):
        b, c = a + train, a + train + test
        best_k, best_sh = 0, -1e9
        for k, tgt in enumerate(targets):
            eq = simulate(df.iloc[:b], tgt.iloc[:b], a, cfg).equity
            sh = _sharpe(eq, ppy)
            if sh > best_sh:
                best_k, best_sh = k, sh
        eq_test = simulate(df.iloc[:c], targets[best_k].iloc[:c], b, cfg).equity
        bh = df["Close"].iloc[b:c] / df["Open"].iloc[b]
        folds.append({"train_start": df.index[a].strftime("%Y-%m-%d"), "test_start": df.index[b].strftime("%Y-%m-%d"),
                      "test_end": df.index[c - 1].strftime("%Y-%m-%d"), "params": grid[best_k],
                      "train_sharpe": round(best_sh, 2), "test_sharpe": round(_sharpe(eq_test, ppy), 2),
                      "test_return": round(float(eq_test.iloc[-1] / cfg.capital - 1) * 100, 2),
                      "bh_return": round(float(bh.iloc[-1] - 1) * 100, 2)})
        pieces.append(eq_test / cfg.capital)
        chosen.append(tuple(sorted(grid[best_k].items())))
        a += test
    if not folds:
        return None
    # chain the unseen windows into one curve
    curve, level = [], 1.0
    for piece in pieces:
        curve.append(piece * level)
        level = float(piece.iloc[-1] * level)
    oos = pd.concat(curve) * cfg.capital
    first_b = int(df.index.searchsorted(pd.Timestamp(folds[0]["test_start"])))
    bh = cfg.capital * df["Close"].iloc[first_b:first_b + len(oos)] / df["Open"].iloc[first_b]
    stable = max(chosen.count(x) for x in set(chosen)) / len(chosen)
    return {
        "folds": folds, "train_bars": train, "test_bars": test,
        "oos": {"sharpe": round(_sharpe(oos, ppy), 2), "cagr": _cagr(oos), "max_drawdown": round(_max_dd(oos), 2),
                "total_return": round(float(oos.iloc[-1] / cfg.capital - 1) * 100, 2),
                "start": oos.index[0].strftime("%Y-%m-%d"), "end": oos.index[-1].strftime("%Y-%m-%d")},
        "oos_benchmark": {"sharpe": round(_sharpe(bh, ppy), 2), "cagr": _cagr(bh)},
        "folds_positive": sum(1 for f in folds if f["test_return"] > 0),
        "folds_beat_bh": sum(1 for f in folds if f["test_return"] > f["bh_return"]),
        "param_stability": round(stable * 100, 1),
        "equity": oos,
    }


# --------------------------------------------------------------------------------------------- random entries
def random_entries(df: pd.DataFrame, trades: list[dict], start_i: int, cost_per_trade: float, n: int = 1000,
                   seed: int = 7) -> dict | None:
    """Compare the strategy's average trade with random trades that keep its count, holding times and sides.
    Both are measured open-to-open (entry at a bar's open, exit at the open `bars` later) minus the round-trip cost,
    so the strategy's stops are not credited - the question is only whether its entry timing beats chance."""
    closed = [t for t in trades if not t.get("open") and t["bars"] > 0]
    if len(closed) < 5:
        return None
    o = df["Open"].to_numpy(float)
    idx = df.index
    pos = {ts: i for i, ts in enumerate(idx)}
    durs = np.array([t["bars"] for t in closed])
    sides = np.array([1 if t["side"] == "Long" else -1 for t in closed])
    entries = np.array([pos[pd.Timestamp(t["entry_time"])] for t in closed if pd.Timestamp(t["entry_time"]) in pos])
    if len(entries) != len(closed):
        return None
    actual = float(np.mean(sides * (o[entries + durs] / o[entries] - 1)) - cost_per_trade)
    rng = np.random.default_rng(seed)
    hi = len(o) - 1 - durs                                  # latest start for each trade's own duration
    lo = start_i
    if (hi <= lo).any():
        return None
    starts = lo + (rng.random((n, len(durs))) * (hi - lo + 1)).astype(int)
    rand = (sides * (o[starts + durs] / o[starts] - 1)).mean(axis=1) - cost_per_trade
    return {"trades": len(closed), "actual_avg_trade": actual * 100, "random_median": float(np.median(rand)) * 100,
            "random_p05": float(np.percentile(rand, 5)) * 100, "random_p95": float(np.percentile(rand, 95)) * 100,
            "percentile": float((rand < actual).mean() * 100), "p_value": float((rand >= actual).mean()),
            "samples": n}


# ------------------------------------------------------------------------------------------- monthly + bootstrap
def _monthly_returns(equity: pd.Series, capital: float) -> pd.Series:
    me = equity.groupby([equity.index.year, equity.index.month]).last()
    prev = me.shift(1)
    prev.iloc[0] = capital
    return me / prev - 1


def monthly_stability(equity: pd.Series, capital: float, bench: pd.Series | None = None) -> dict:
    mr = _monthly_returns(equity, capital)
    streak = worst_streak = 0
    for v in mr:
        streak = streak + 1 if v < 0 else 0
        worst_streak = max(worst_streak, streak)
    yearly = equity.groupby(equity.index.year).last()
    yprev = yearly.shift(1)
    yprev.iloc[0] = capital
    yr = yearly / yprev - 1
    out = {"months": int(len(mr)), "positive_months": float((mr > 0).mean() * 100) if len(mr) else None,
           "worst_month": float(mr.min() * 100) if len(mr) else None, "best_month": float(mr.max() * 100) if len(mr) else None,
           "longest_losing_streak": int(worst_streak), "years": int(len(yr)), "positive_years": int((yr > 0).sum())}
    if bench is not None and len(mr):
        bm = _monthly_returns(bench, capital).reindex(mr.index)
        up, down = bm > 0, bm <= 0
        out["avg_in_up_months"] = float(mr[up].mean() * 100) if up.any() else None
        out["avg_in_down_months"] = float(mr[down].mean() * 100) if down.any() else None
    return out


def bootstrap(equity: pd.Series, capital: float, n: int = 1000, seed: int = 11) -> dict | None:
    """90% ranges for CAGR and Sharpe from resampling whole months with replacement."""
    mr = _monthly_returns(equity, capital).to_numpy(float)
    if len(mr) < 12:
        return None
    rng = np.random.default_rng(seed)
    s = mr[rng.integers(0, len(mr), (n, len(mr)))]
    growth = np.prod(1 + s, axis=1)
    cagr = (np.where(growth > 0, growth, np.nan) ** (12 / len(mr)) - 1) * 100
    sd = s.std(axis=1, ddof=1)
    sh = np.where(sd > 0, s.mean(axis=1) / np.where(sd > 0, sd, 1) * math.sqrt(12), 0.0)
    return {"months": int(len(mr)), "cagr_p05": float(np.nanpercentile(cagr, 5)), "cagr_p95": float(np.nanpercentile(cagr, 95)),
            "sharpe_p05": float(np.percentile(sh, 5)), "sharpe_p95": float(np.percentile(sh, 95)),
            "prob_cagr_positive": float(np.nanmean(cagr > 0) * 100)}


# ------------------------------------------------------------------------------------------ Sharpe confidence
def sr_stats(equity: pd.Series) -> dict | None:
    """Per-bar Sharpe, skew, kurtosis and length: what the probabilistic Sharpe needs."""
    r = equity.pct_change().dropna().to_numpy(float)
    if len(r) < 30 or r.std() == 0:
        return None
    z = (r - r.mean()) / r.std(ddof=1)
    return {"sr": float(r.mean() / r.std(ddof=1)), "skew": float((z ** 3).mean()), "kurt": float((z ** 4).mean()),
            "T": int(len(r))}


def sharpe_confidence(stats: dict | None, n_trials: int = 1, trial_sharpes: list[float] | None = None,
                      ppy: float = 252.0) -> dict | None:
    """Probabilistic Sharpe ratio (Bailey & Lopez de Prado): P(true Sharpe > SR*), skew and fat tails included.
    SR* = 0 for a single test; when the result was picked as the best of `n_trials`, SR* is the Sharpe the best of
    that many would reach by luck alone (deflated Sharpe ratio), its spread set by the trials' own Sharpes."""
    if not stats:
        return None
    sr, T = stats["sr"], stats["T"]
    sr0 = 0.0
    if n_trials > 1:
        if trial_sharpes and len(trial_sharpes) > 1:
            v = float(np.var(np.asarray(trial_sharpes, float) / math.sqrt(ppy), ddof=1))
        else:
            v = 1.0 / T
        v = max(v, 1e-12)
        sr0 = math.sqrt(v) * ((1 - EULER_GAMMA) * _N.inv_cdf(1 - 1 / n_trials)
                              + EULER_GAMMA * _N.inv_cdf(1 - 1 / (n_trials * math.e)))
    denom = 1 - stats["skew"] * sr + (stats["kurt"] - 1) / 4 * sr ** 2
    if denom <= 0:
        return None
    prob = _N.cdf((sr - sr0) * math.sqrt(T - 1) / math.sqrt(denom))
    return {"probability": float(prob * 100), "trials": int(n_trials), "hurdle_sharpe": float(sr0 * math.sqrt(ppy))}
