"""Run an intraday Rule and build the same result shape the Strategy Tester page draws for daily backtests, with
intraday versions of the validation checks:

  random entries   the strategy's trades re-entered at the SAME minute of day and held the SAME number of minutes,
                   on random days, same sides and costs (does the day/condition selection beat chance?)
  walk-forward     stop / target picked on the previous 3 years' daily P&L only, traded on the next year
  sensitivity      Sharpe of daily P&L over stop x target at 0.5x .. 1.5x
  confidence       probabilistic Sharpe on daily P&L; monthly stability; bootstrap of months
"""
from __future__ import annotations

import math
import time as _time

import numpy as np
import pandas as pd

from .. import engine as daily_engine
from .. import validation as V
from . import engine as E
from . import store
from .rules import Rule

CAPITAL = 200_000.0                      # rupees: roughly one NIFTY futures lot's margin
_markets: dict[str, tuple[float, E.Market]] = {}


def market(symbol: str) -> E.Market:
    hit = _markets.get(symbol)
    if hit and _time.time() - hit[0] < 900:
        return hit[1]
    bars = store.load(symbol)
    try:
        vix = store.daily_vix()
    except Exception:
        vix = None
    mk = E.Market(symbol, bars, vix)
    _markets[symbol] = (_time.time(), mk)
    return mk


def _daily_equity(mk: E.Market, days: np.ndarray, pnl_pts: np.ndarray, rs_per_pt: float) -> pd.Series:
    return pd.Series(CAPITAL + np.cumsum(pnl_pts) * rs_per_pt, index=mk.days[days])


def _sharpe(pnl: np.ndarray) -> float:
    return float(pnl.mean() / pnl.std() * math.sqrt(252)) if len(pnl) > 5 and pnl.std() > 0 else 0.0


def _grid(rule: Rule) -> tuple[list, list]:
    mults = (0.5, 0.75, 1.0, 1.25, 1.5)
    xs = sorted({round(rule.stop_pts * m) for m in mults}) if rule.stop_pts else [None]
    ys = sorted({round(rule.target_pts * m) for m in mults}) if rule.target_pts else [None]
    return xs, ys


def random_entries(mk: E.Market, rule: Rule, trades: list[dict], days: np.ndarray, n: int = 500, seed: int = 7) -> dict | None:
    """Same minute of day, same holding minutes, same side, random day (open to open, round-trip cost)."""
    if len(trades) < 10:
        return None
    k_in = np.array([t["k_in"] for t in trades])
    k_out = np.array([t["k_out"] for t in trades])
    sign = np.array([1 if t["side"] == "Long" else -1 for t in trades])
    o = mk.o
    cost = sum(E.COST_BPS) / 1e4
    actual = float(np.mean([t["net_pts"] for t in trades]))
    rng = np.random.default_rng(seed)
    rd = days[rng.integers(0, len(days), (n, len(trades)))]
    ein, eout = o[rd, k_in], o[rd, k_out]
    rand = (sign * (eout - ein) - (ein + eout) * cost).mean(axis=1)
    # the strategy's own trades measured the same way (open to open, no stops credited) for a like-for-like test
    own = float(np.mean(sign * (o[[t["day"] for t in trades], k_out] - o[[t["day"] for t in trades], k_in])
                        - (o[[t["day"] for t in trades], k_in] + o[[t["day"] for t in trades], k_out]) * cost))
    return {"trades": len(trades), "actual_avg_trade_pts": actual, "own_open_to_open_pts": own,
            "random_median_pts": float(np.median(rand)), "random_p05_pts": float(np.percentile(rand, 5)),
            "random_p95_pts": float(np.percentile(rand, 95)), "percentile": float((rand < own).mean() * 100),
            "p_value": float((rand >= own).mean()), "samples": n,
            # the shared tile shows these in % of the index level
            "actual_avg_trade": own / float(np.mean([t["entry_price"] for t in trades])) * 100,
            "random_median": float(np.median(rand)) / float(np.mean([t["entry_price"] for t in trades])) * 100}


def walk_forward(mk: E.Market, rule: Rule, days: np.ndarray, ev, grid_pnl: dict, rs_per_pt: float) -> dict | None:
    years = pd.DatetimeIndex(mk.days[days]).year.to_numpy()
    uy = sorted(set(years))
    if len(uy) < 5 or len(grid_pnl) < 2:
        return None
    folds, pieces = [], []
    chosen = []
    for i in range(3, len(uy)):
        tr_m = (years >= uy[i - 3]) & (years < uy[i])
        te_m = years == uy[i]
        best = max(grid_pnl, key=lambda key: _sharpe(grid_pnl[key][tr_m]))
        te = grid_pnl[best][te_m]
        chosen.append(best)
        dd = days[te_m]
        bh = float(np.sum(mk.o[dd, E.EOD_K] - mk.o[dd, 0]))
        folds.append({"train_start": str(uy[i - 3]), "test_start": str(mk.days[days[te_m][0]].date()),
                      "test_end": str(mk.days[days[te_m][-1]].date()),
                      "params": {"stop": best[0], "target": best[1]},
                      "train_sharpe": round(_sharpe(grid_pnl[best][tr_m]), 2), "test_sharpe": round(_sharpe(te), 2),
                      "test_return": round(float(te.sum() * rs_per_pt / CAPITAL * 100), 2),
                      "bh_return": round(float(bh * rs_per_pt / CAPITAL * 100), 2)})
        pieces.append(te)
    oos = np.concatenate(pieces)
    first = int(np.flatnonzero(years == uy[3])[0])
    eq = pd.Series(CAPITAL + np.cumsum(oos) * rs_per_pt, index=mk.days[days[first:first + len(oos)]])
    stable = max(chosen.count(x) for x in set(chosen)) / len(chosen)
    return {"folds": folds, "train_bars": "3 years", "test_bars": "1 year",
            "oos": {"sharpe": round(_sharpe(oos), 2), "cagr": V._cagr(eq), "max_drawdown": round(V._max_dd(eq), 2),
                    "total_return": round(float(oos.sum() * rs_per_pt / CAPITAL * 100), 2),
                    "start": str(eq.index[0].date()), "end": str(eq.index[-1].date())},
            "oos_benchmark": {"sharpe": None, "cagr": None},
            "folds_positive": sum(1 for f in folds if f["test_return"] > 0),
            "folds_beat_bh": sum(1 for f in folds if f["test_return"] > f["bh_return"]),
            "param_stability": round(stable * 100, 1), "equity": eq}


def run(rule: Rule, start: str | None, end: str | None, validate: bool = True) -> dict:
    meta = store.INDICES[rule.symbol]
    mk = market(rule.symbol)
    days = mk.slice_days(start, end)
    if len(days) < 20:
        raise ValueError(f"Not enough {rule.symbol} minute history in the requested period.")
    rs_per_pt = meta["lot"] * max(rule.lots, 1)
    ev = E.events(mk, rule)
    trades, pnl = E.simulate(mk, rule, days, ev)
    equity = _daily_equity(mk, days, pnl, rs_per_pt)
    # benchmark: buy at 09:15, sell at 15:25 every day, same costs - the plain intraday alternative
    cost = sum(E.COST_BPS) / 1e4
    bench_pts = mk.o[days, E.EOD_K] - mk.o[days, 0] - (mk.o[days, E.EOD_K] + mk.o[days, 0]) * cost
    bench = pd.Series(CAPITAL + np.cumsum(bench_pts) * rs_per_pt, index=mk.days[days])
    out_trades = [{"side": t["side"], "entry_time": E.stamp(mk, t["day"], t["k_in"]), "exit_time": E.stamp(mk, t["day"], t["k_out"]),
                   "entry_price": t["entry_price"], "exit_price": t["exit_price"],
                   "return_pct": t["net_pts"] / t["entry_price"] * 100, "pnl": t["net_pts"] * rs_per_pt,
                   "points": t["net_pts"], "bars": t["k_out"] - t["k_in"], "exit_reason": t["exit_reason"]} for t in trades]
    metrics = daily_engine.perf_metrics(equity, CAPITAL, out_trades, pd.Series((pnl != 0).astype(float), index=equity.index), bench)
    metrics.update({"points_total": float(pnl.sum()), "points_per_trade": float(np.mean([t["net_pts"] for t in trades])) if trades else None,
                    "days_traded": int((pnl != 0).sum()), "winning_days": float((pnl[pnl != 0] > 0).mean() * 100) if (pnl != 0).any() else None,
                    "worst_day_pts": float(pnl.min()), "best_day_pts": float(pnl.max()), "rs_per_point": rs_per_pt})
    bmetrics = daily_engine.perf_metrics(bench, CAPITAL)
    name = E.describe(rule)
    result = {
        "kind": "backtest", "mode": "intraday", "symbol": rule.symbol, "name": meta["name"], "asset_class": "index",
        "interval": "1d", "strategy": "intraday_rule", "strategy_label": "Intraday rule", "strategy_name": name,
        "strategy_description": "Intraday rule on one-minute bars (Kite history): decided on completed minutes, entered at "
                                "the next minute's open, stop counted first when a minute touches stop and target, "
                                f"futures costs ~{sum(E.COST_BPS):.1f} bps a side. P&L in index points x {rs_per_pt} "
                                f"(1 lot = {meta['lot']}), on Rs {CAPITAL:,.0f} capital.",
        "family": "Intraday", "params": {k: v for k, v in rule.to_dict().items() if v not in (None, [], {})},
        "rule": rule.to_dict(),
        "config": {"capital": CAPITAL, "allow_short": rule.side != "long", "commission_bps": E.COST_BPS[0],
                   "slippage_bps": E.COST_BPS[1], "stop_loss_pct": None, "take_profit_pct": None,
                   "trailing_stop_pct": None, "position_size": 1.0},
        "period": {"start": str(mk.days[days[0]].date()), "end": str(mk.days[days[-1]].date())},
        "metrics": metrics, "benchmark": bmetrics,
        "notes": ["Benchmark (B&H) here = buy at 09:15 and sell at 15:25 every day, 1 lot, same costs."],
    }
    # daily candles + markers for the price chart (minute bars for years would be too many)
    candles = [{"time": str(mk.days[d].date()), "open": round(float(mk.o[d, 0]), 2), "high": round(float(mk.h[d].max()), 2),
                "low": round(float(mk.l[d].min()), 2), "close": round(float(mk.c[d, -1]), 2)} for d in days]
    markers = []
    for t in out_trades[-400:]:
        long_ = t["side"] == "Long"
        markers.append({"time": t["entry_time"].strftime("%Y-%m-%d"), "position": "belowBar" if long_ else "aboveBar",
                        "shape": "arrowUp" if long_ else "arrowDown", "kind": "entry", "text": "Buy" if long_ else "Short"})
    result["series"] = {"equity": daily_engine._line(equity, False, 2), "benchmark": daily_engine._line(bench, False, 2),
                        "drawdown": daily_engine._line(daily_engine.drawdown(equity) * 100, False, 2),
                        "benchmark_drawdown": daily_engine._line(daily_engine.drawdown(bench) * 100, False, 2),
                        "price": candles, "overlays": {}}
    result["markers"] = sorted(markers, key=lambda m: m["time"])
    result["trades"] = [{**t, "entry_time": t["entry_time"].strftime("%Y-%m-%d %H:%M"),
                         "exit_time": t["exit_time"].strftime("%Y-%m-%d %H:%M")} for t in out_trades]
    result["monthly"] = daily_engine.monthly_table(equity, CAPITAL)
    result["benchmark_monthly"] = daily_engine.monthly_table(bench, CAPITAL)
    val: dict = {"monthly": V.monthly_stability(equity, CAPITAL, bench), "sr_stats": V.sr_stats(equity)}
    val["confidence"] = V.sharpe_confidence(val["sr_stats"], 1, None, 252.0)
    val["random"] = random_entries(mk, rule, trades, days)
    if validate:
        xs, ys = _grid(rule)
        grid_pnl = {}
        cells = []
        for y in ys:
            row = []
            for x in xs:
                key = (x, y)
                if x == rule.stop_pts and y == rule.target_pts:
                    p = pnl
                else:
                    p = E.simulate(mk, rule, days, ev, stop=x if x is not None else rule.stop_pts,
                                   target=y if y is not None else rule.target_pts)[1]
                grid_pnl[key] = p
                row.append(round(_sharpe(p), 2))
            cells.append(row)
        if len(grid_pnl) > 1:
            flat = [v for r_ in cells for v in r_]
            base = _sharpe(pnl)
            stab = sum(1 for v in flat if v > 0 and (base <= 0 or v >= 0.5 * base)) / len(flat)
            result["robustness"] = {"split": None, "sensitivity": {
                "x_param": "Stop (pts)", "y_param": "Target (pts)" if rule.target_pts else None,
                "x_values": xs, "y_values": ys if rule.target_pts else [], "sharpe": cells,
                "base": {"x": rule.stop_pts, "y": rule.target_pts}, "stability": stab * 100}}
            wf = walk_forward(mk, rule, days, ev, grid_pnl, rs_per_pt)
            if wf:
                result["series"]["walk_forward"] = daily_engine._line(wf.pop("equity"), False, 2)
            val["walk_forward"] = wf
        val["bootstrap"] = V.bootstrap(equity, CAPITAL)
    result["validation"] = val
    result["verdict"] = daily_engine.strategy_verdict(result)
    return result
