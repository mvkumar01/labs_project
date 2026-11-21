"""Event-driven bar backtester + performance analytics."""
from __future__ import annotations

import math
from dataclasses import dataclass, field, asdict

import numpy as np
import pandas as pd

from . import strategies as strat_lib
from .data import PriceData, load

DEFAULT_COSTS_BPS = {  # (commission, slippage) per side, in basis points
    "equity": (5, 2), "etf": (2, 1), "index": (2, 1), "commodity": (3, 3), "forex": (1, 1), "crypto": (10, 5),
}


@dataclass
class BacktestConfig:
    capital: float = 100_000.0
    allow_short: bool = False
    commission_bps: float | None = None
    slippage_bps: float | None = None
    stop_loss_pct: float | None = None
    take_profit_pct: float | None = None
    trailing_stop_pct: float | None = None
    position_size: float = 1.0  # fraction of equity per trade

    def resolved(self, asset_class: str) -> "BacktestConfig":
        c, s = DEFAULT_COSTS_BPS.get(asset_class, (5, 2))
        out = BacktestConfig(**asdict(self))
        if out.commission_bps is None:
            out.commission_bps = c
        if out.slippage_bps is None:
            out.slippage_bps = s
        return out


@dataclass
class RunOutput:
    equity: pd.Series
    position: pd.Series
    trades: list[dict] = field(default_factory=list)


# ======================================================================================
# Core simulation
# ======================================================================================
def simulate(df: pd.DataFrame, target: pd.Series, start_i: int, cfg: BacktestConfig) -> RunOutput:
    o, h, l, c = (df[k].to_numpy(float) for k in ("Open", "High", "Low", "Close"))
    tgt = np.nan_to_num(target.to_numpy(float), nan=0.0)
    idx = df.index
    n = len(df)
    comm = cfg.commission_bps / 1e4
    slip = cfg.slippage_bps / 1e4
    sl = (cfg.stop_loss_pct or 0) / 100
    tp = (cfg.take_profit_pct or 0) / 100
    tr = (cfg.trailing_stop_pct or 0) / 100

    cash = cfg.capital
    units = 0.0
    direction = 0
    entry_px = entry_cost = 0.0
    entry_i = -1
    extreme = 0.0
    lock = None  # after a stop-out, wait for the signal to change before re-entering
    equity = np.empty(n - start_i)
    posarr = np.zeros(n - start_i)
    trades: list[dict] = []

    def close_position(i: int, raw_px: float, reason: str, mark_only: bool = False):
        nonlocal cash, units, direction
        # mark_only: position is still open at the end of the test, so value it at the close
        # without charging exit costs (keeps trade P&L reconciled with the equity curve)
        px = raw_px if mark_only else (raw_px * (1 - slip) if direction > 0 else raw_px * (1 + slip))
        notional = abs(units) * px
        fee = 0.0 if mark_only else notional * comm
        cash += units * px - fee
        gross = (px / entry_px - 1) * direction
        pnl = (px - entry_px) * units - fee - entry_cost
        trades.append({
            "side": "Long" if direction > 0 else "Short",
            "entry_time": idx[entry_i], "exit_time": idx[i],
            "entry_price": entry_px, "exit_price": px,
            "return_pct": (gross - comm * (1 if mark_only else 2)) * 100, "pnl": pnl,
            "bars": i - entry_i, "exit_reason": reason,
        })
        units = 0.0
        direction = 0

    def open_position(i: int, d: int, raw_px: float):
        nonlocal cash, units, direction, entry_px, entry_cost, entry_i, extreme
        px = raw_px * (1 + slip) if d > 0 else raw_px * (1 - slip)
        eq = cash  # flat at this point
        notional = eq * cfg.position_size / (1 + comm)
        units = d * notional / px
        entry_cost = notional * comm
        cash -= units * px + entry_cost
        direction, entry_px, entry_i, extreme = d, px, i, px

    for k, i in enumerate(range(start_i, n)):
        desired = int(np.sign(tgt[i - 1])) if i > 0 else 0
        if lock is not None:
            if desired != lock:
                lock = None
            else:
                desired = 0
        # 1) execute signal changes at the open
        if desired != direction:
            if direction != 0:
                close_position(i, o[i], "Signal")
            if desired != 0:
                open_position(i, desired, o[i])
        # 2) intrabar risk exits
        if direction != 0 and (sl or tp or tr):
            exit_px, reason = None, None
            if direction > 0:
                stops = []
                if sl: stops.append((entry_px * (1 - sl), "Stop loss"))
                if tr: stops.append((extreme * (1 - tr), "Trailing stop"))
                if stops:
                    lvl, why = max(stops, key=lambda s: s[0])
                    if l[i] <= lvl:
                        exit_px, reason = min(o[i], lvl), why
                if exit_px is None and tp and h[i] >= entry_px * (1 + tp):
                    exit_px, reason = max(o[i], entry_px * (1 + tp)), "Take profit"
                if exit_px is None:
                    extreme = max(extreme, h[i])
            else:
                stops = []
                if sl: stops.append((entry_px * (1 + sl), "Stop loss"))
                if tr: stops.append((extreme * (1 + tr), "Trailing stop"))
                if stops:
                    lvl, why = min(stops, key=lambda s: s[0])
                    if h[i] >= lvl:
                        exit_px, reason = max(o[i], lvl), why
                if exit_px is None and tp and l[i] <= entry_px * (1 - tp):
                    exit_px, reason = min(o[i], entry_px * (1 - tp)), "Take profit"
                if exit_px is None:
                    extreme = min(extreme, l[i])
            if exit_px is not None:
                lock = direction
                close_position(i, exit_px, reason)
        equity[k] = cash + units * c[i]
        posarr[k] = direction

    if direction != 0:  # mark open position at the final close
        close_position(n - 1, c[-1], "Open at end", mark_only=True)
        trades[-1]["open"] = True
    return RunOutput(pd.Series(equity, index=idx[start_i:]), pd.Series(posarr, index=idx[start_i:]), trades)


# ======================================================================================
# Analytics
# ======================================================================================
def _years(index: pd.DatetimeIndex) -> float:
    return max((index[-1] - index[0]).days / 365.25, 1 / 365.25)


def periods_per_year(index: pd.DatetimeIndex) -> float:
    return max(len(index) / _years(index), 1.0)


def drawdown(equity: pd.Series) -> pd.Series:
    return equity / equity.cummax() - 1


def _safe(x):
    if x is None:
        return None
    if isinstance(x, (np.floating, np.integer)):
        x = x.item()
    if isinstance(x, float) and (math.isnan(x) or math.isinf(x)):
        return None
    return x


def perf_metrics(equity: pd.Series, capital: float, trades: list[dict] | None = None,
                 position: pd.Series | None = None, bench_equity: pd.Series | None = None) -> dict:
    eq = pd.concat([pd.Series([capital], index=[equity.index[0] - pd.Timedelta(seconds=1)]), equity])
    rets = eq.pct_change().dropna()
    ppy = periods_per_year(equity.index)
    yrs = _years(equity.index)
    total = equity.iloc[-1] / capital - 1
    cagr = (equity.iloc[-1] / capital) ** (1 / yrs) - 1 if equity.iloc[-1] > 0 else -1.0
    vol = rets.std() * math.sqrt(ppy)
    sharpe = rets.mean() / rets.std() * math.sqrt(ppy) if rets.std() > 0 else 0.0
    downside = rets[rets < 0]
    dstd = math.sqrt((downside ** 2).sum() / max(len(rets), 1)) * math.sqrt(ppy)
    sortino = rets.mean() * ppy / dstd if dstd > 0 else 0.0
    dd = drawdown(eq)
    max_dd = dd.min()
    # longest drawdown duration in days
    underwater = dd < 0
    longest, cur_start = 0, None
    for t, u in underwater.items():
        if u and cur_start is None:
            cur_start = t
        elif not u and cur_start is not None:
            longest = max(longest, (t - cur_start).days); cur_start = None
    if cur_start is not None:
        longest = max(longest, (eq.index[-1] - cur_start).days)
    m = {
        "total_return": total * 100, "cagr": cagr * 100, "volatility": vol * 100, "sharpe": sharpe,
        "sortino": sortino, "max_drawdown": max_dd * 100, "max_dd_days": longest,
        "calmar": (cagr / abs(max_dd)) if max_dd < 0 else None,
        "final_equity": equity.iloc[-1], "years": yrs,
        "best_day": rets.max() * 100 if len(rets) else None, "worst_day": rets.min() * 100 if len(rets) else None,
    }
    if trades is not None:
        r = np.array([t["return_pct"] for t in trades]) if trades else np.array([])
        pnl = np.array([t["pnl"] for t in trades]) if trades else np.array([])
        wins, losses = pnl[pnl > 0], pnl[pnl <= 0]
        m.update({
            "trades": len(trades),
            "win_rate": (len(wins) / len(trades) * 100) if trades else None,
            "profit_factor": (wins.sum() / abs(losses.sum())) if len(losses) and losses.sum() != 0 else (None if not len(wins) else float("inf")),
            "avg_trade": r.mean() if len(r) else None,
            "avg_win": r[r > 0].mean() if (r > 0).any() else None,
            "avg_loss": r[r <= 0].mean() if (r <= 0).any() else None,
            "best_trade": r.max() if len(r) else None, "worst_trade": r.min() if len(r) else None,
            "avg_bars": float(np.mean([t["bars"] for t in trades])) if trades else None,
            "trades_per_year": len(trades) / yrs,
        })
        if m["profit_factor"] == float("inf"):
            m["profit_factor"] = None
    if position is not None:
        m["exposure"] = float((position != 0).mean() * 100)
    if bench_equity is not None:
        br = bench_equity.pct_change().reindex(rets.index).fillna(0)
        if br.std() > 0 and rets.std() > 0:
            beta = np.cov(rets, br)[0, 1] / br.var()
            m["beta"] = beta
            m["correlation"] = float(np.corrcoef(rets, br)[0, 1])
            m["alpha"] = (rets.mean() - beta * br.mean()) * ppy * 100
    return {k: _safe(v) for k, v in m.items()}


def _period_end(s: pd.Series, kind: str) -> pd.Series:
    """Resample to month/year end; 'ME'/'YE' need pandas >= 2.2, older pandas uses 'M'/'Y'."""
    for alias in (("ME", "M") if kind == "M" else ("YE", "Y")):
        try:
            return s.resample(alias).last()
        except ValueError:
            continue
    raise ValueError(f"Cannot resample to {kind}")


def monthly_table(equity: pd.Series, capital: float) -> dict:
    me = _period_end(equity, "M")
    prev = me.shift(1)
    prev.iloc[0] = capital
    mr = (me / prev - 1) * 100
    ye = _period_end(equity, "Y")
    yprev = ye.shift(1)
    yprev.iloc[0] = capital
    yr = (ye / yprev - 1) * 100
    rows = []
    for y in sorted(set(mr.index.year)):
        months = [None] * 12
        for t, v in mr[mr.index.year == y].items():
            months[t.month - 1] = _safe(round(float(v), 2))
        tot = yr[yr.index.year == y]
        rows.append({"year": int(y), "months": months, "total": _safe(round(float(tot.iloc[0]), 2)) if len(tot) else None})
    return {"rows": rows}


def _time(ts: pd.Timestamp, intraday: bool):
    return int(ts.timestamp()) if intraday else ts.strftime("%Y-%m-%d")


def _line(series: pd.Series, intraday: bool, digits: int = 4) -> list[dict]:
    s = series.dropna()
    return [{"time": _time(t, intraday), "value": round(float(v), digits)} for t, v in s.items()]


# ======================================================================================
# High-level API
# ======================================================================================
def run_backtest(ticker: str, strategy_key: str, params: dict | None, start: str | None, end: str | None,
                 interval: str = "1d", cfg: BacktestConfig | None = None, robustness: bool = True,
                 detail: bool = True) -> dict:
    strat = strat_lib.get(strategy_key)
    p = strat.clean(params)
    if strategy_key in ("sma_cross", "ema_cross", "macd") and p["fast"] >= p["slow"]:
        p["fast"], p["slow"] = min(p["fast"], p["slow"]), max(p["fast"], p["slow"])
        if p["fast"] == p["slow"]:
            p["slow"] = p["fast"] + 1
    pdata, trade_start = load(ticker, start, end, interval, warmup_bars=strat.warmup(p))
    cfg = (cfg or BacktestConfig()).resolved(pdata.asset_class)
    df = pdata.df
    start_i = int(df.index.searchsorted(trade_start))
    target = strat.fn(df, p, cfg.allow_short)
    out = simulate(df, target, start_i, cfg)

    first_open = df["Open"].iloc[start_i]
    bench = cfg.capital * df["Close"].iloc[start_i:] / first_open
    metrics = perf_metrics(out.equity, cfg.capital, out.trades, out.position, bench)
    bmetrics = perf_metrics(bench, cfg.capital)

    result = {
        "kind": "backtest",
        "symbol": ticker, "name": pdata.name, "asset_class": pdata.asset_class, "interval": interval,
        "strategy": strategy_key, "strategy_label": strat.label, "strategy_name": strat.name(p),
        "strategy_description": strat.description, "family": strat.family, "params": p,
        "config": asdict(cfg),
        "period": {"start": out.equity.index[0].strftime("%Y-%m-%d"), "end": out.equity.index[-1].strftime("%Y-%m-%d")},
        "metrics": metrics, "benchmark": bmetrics,
    }
    if not detail:
        result["series"] = {"equity": _line(out.equity, interval not in ("1d", "1wk", "1mo"), 2)}
        return result

    intraday = interval not in ("1d", "1wk", "1mo")
    px = df.iloc[start_i:]
    result["series"] = {
        "equity": _line(out.equity, intraday, 2),
        "benchmark": _line(bench, intraday, 2),
        "drawdown": _line(drawdown(out.equity) * 100, intraday, 2),
        "benchmark_drawdown": _line(drawdown(bench) * 100, intraday, 2),
        "price": [{"time": _time(t, intraday), "open": round(r.Open, 6), "high": round(r.High, 6),
                   "low": round(r.Low, 6), "close": round(r.Close, 6)} for t, r in px.iterrows()],
        "overlays": {k: _line(v.iloc[start_i:], intraday, 6) for k, v in strat.overlays(df, p).items()},
    }
    markers = []
    for t in out.trades:
        long_ = t["side"] == "Long"
        markers.append({"time": _time(t["entry_time"], intraday), "position": "belowBar" if long_ else "aboveBar",
                        "shape": "arrowUp" if long_ else "arrowDown", "kind": "entry",
                        "text": "Buy" if long_ else "Short"})
        if not t.get("open"):
            markers.append({"time": _time(t["exit_time"], intraday), "position": "aboveBar" if long_ else "belowBar",
                            "shape": "arrowDown" if long_ else "arrowUp", "kind": "exit",
                            "text": "Sell" if long_ else "Cover"})
    markers.sort(key=lambda m: (m["time"], m["kind"] != "exit"))
    result["markers"] = markers
    result["trades"] = [{**t, "entry_time": t["entry_time"].strftime("%Y-%m-%d %H:%M" if intraday else "%Y-%m-%d"),
                         "exit_time": t["exit_time"].strftime("%Y-%m-%d %H:%M" if intraday else "%Y-%m-%d"),
                         "entry_price": _safe(t["entry_price"]), "exit_price": _safe(t["exit_price"]),
                         "return_pct": _safe(t["return_pct"]), "pnl": _safe(t["pnl"])} for t in out.trades]
    result["monthly"] = monthly_table(out.equity, cfg.capital)
    result["benchmark_monthly"] = monthly_table(bench, cfg.capital)
    if robustness and strategy_key != "buy_hold":
        result["robustness"] = robustness_report(df, strat, p, start_i, cfg, out.equity)
    result["verdict"] = strategy_verdict(result)
    return result


def robustness_report(df: pd.DataFrame, strat, p: dict, start_i: int, cfg: BacktestConfig,
                      equity: pd.Series) -> dict:
    # 1) in-sample vs out-of-sample (chronological 70/30 split of the same run)
    split = int(len(equity) * 0.7)
    def seg_sharpe(e: pd.Series):
        r = e.pct_change().dropna()
        return float(r.mean() / r.std() * math.sqrt(periods_per_year(e.index))) if len(r) > 5 and r.std() > 0 else 0.0
    def seg_cagr(e: pd.Series):
        yrs = _years(e.index)
        return float(((e.iloc[-1] / e.iloc[0]) ** (1 / yrs) - 1) * 100) if e.iloc[0] > 0 and e.iloc[-1] > 0 else None
    ins, oos = equity.iloc[:split], equity.iloc[split - 1:]
    split_info = {
        "split_date": equity.index[split].strftime("%Y-%m-%d"),
        "in_sample": {"sharpe": seg_sharpe(ins), "cagr": seg_cagr(ins)},
        "out_of_sample": {"sharpe": seg_sharpe(oos), "cagr": seg_cagr(oos)},
    }
    # 2) parameter sensitivity grid over the first two numeric params
    grid = None
    numeric = [x for x in strat.params if x.name not in ("exit_level",)]
    if numeric:
        mults = [0.5, 0.75, 1.0, 1.25, 1.5]
        px = numeric[0]
        py = numeric[1] if len(numeric) > 1 else None
        def val(prm, m):
            v = p[prm.name] * m
            v = min(max(v, prm.lo), prm.hi)
            return int(round(v)) if prm.integer else round(v, 2)
        xs = sorted(set(val(px, m) for m in mults))
        ys = sorted(set(val(py, m) for m in mults)) if py else [None]
        cells = []
        base_df = df
        for yv in ys:
            row = []
            for xv in xs:
                q = dict(p); q[px.name] = xv
                if py:
                    q[py.name] = yv
                if "fast" in q and "slow" in q and q["fast"] >= q["slow"]:
                    row.append(None); continue
                tgt = strat.fn(base_df, q, cfg.allow_short)
                eq = simulate(base_df, tgt, start_i, cfg).equity
                row.append(round(seg_sharpe(eq), 2))
            cells.append(row)
        flat = [v for r in cells for v in r if v is not None]
        base = seg_sharpe(equity)
        stable = sum(1 for v in flat if v > 0 and (base <= 0 or v >= 0.5 * base)) / len(flat) if flat else None
        grid = {"x_param": px.label or px.name, "y_param": (py.label or py.name) if py else None,
                "x_values": xs, "y_values": ys if py else [], "sharpe": cells,
                "base": {"x": p[px.name], "y": p[py.name] if py else None},
                "stability": stable * 100 if stable is not None else None}
    return {"split": split_info, "sensitivity": grid}


def strategy_verdict(r: dict) -> dict:
    m, b = r["metrics"], r["benchmark"]
    rob = r.get("robustness") or {}
    pros, cons = [], []
    score = 50.0
    if r["strategy"] == "buy_hold":
        return {"label": "Benchmark", "score": None, "pros": [], "cons": [],
                "summary": "Buy & hold is the benchmark other strategies are measured against."}
    cagr, bcagr = m.get("cagr") or 0, b.get("cagr") or 0
    sh, bsh = m.get("sharpe") or 0, b.get("sharpe") or 0
    dd, bdd = m.get("max_drawdown") or 0, b.get("max_drawdown") or 0
    if cagr <= 0:
        score -= 25; cons.append(f"Lost money overall ({cagr:.1f}% a year).")
    if sh >= 1.0:
        score += 10; pros.append(f"Strong absolute risk-adjusted return (Sharpe {sh:.2f}).")
    elif sh < 0.3:
        score -= 15; cons.append(f"Low absolute risk-adjusted return (Sharpe {sh:.2f}; 0.5 or more is a common minimum bar).")
    if dd < -40:
        score -= 10; cons.append(f"A {dd:.0f}% drawdown is very hard to sit through in practice.")
    if sh > bsh + 0.1:
        score += 15; pros.append(f"Better risk-adjusted return than buy & hold (Sharpe {sh:.2f} vs {bsh:.2f}).")
    elif sh < bsh - 0.1:
        score -= 15; cons.append(f"Worse risk-adjusted return than buy & hold (Sharpe {sh:.2f} vs {bsh:.2f}).")
    if cagr > bcagr:
        score += 10; pros.append(f"Higher annual return than buy & hold ({cagr:.1f}% vs {bcagr:.1f}%).")
    else:
        cons.append(f"Lower annual return than simply holding ({cagr:.1f}% vs {bcagr:.1f}%).")
    if dd > bdd * 0.75:
        score += 10; pros.append(f"Shallower worst drawdown ({dd:.1f}% vs {bdd:.1f}%).")
    elif dd < bdd:
        score -= 10; cons.append(f"Deeper worst drawdown than buy & hold ({dd:.1f}% vs {bdd:.1f}%).")
    nt = m.get("trades") or 0
    if nt < 10:
        score -= 10; cons.append(f"Only {nt} trades, which is too few to separate skill from luck.")
    elif nt >= 30:
        pros.append(f"{nt} trades gives a reasonable sample size.")
    split = rob.get("split")
    if split:
        o = split["out_of_sample"]["sharpe"]
        if o > 0.3:
            score += 10; pros.append(f"Held up in the most recent 30% of the data (Sharpe {o:.2f} after {split['split_date']}).")
        elif o <= 0:
            score -= 15; cons.append(f"Did not work in the most recent 30% of the data (Sharpe {o:.2f} after {split['split_date']}).")
    sens = rob.get("sensitivity")
    if sens and sens.get("stability") is not None:
        st = sens["stability"]
        if st >= 70:
            score += 10; pros.append(f"Robust to parameter changes ({st:.0f}% of nearby settings also work).")
        elif st < 40:
            score -= 15; cons.append(f"Fragile: only {st:.0f}% of nearby parameter settings work, a sign of curve-fitting.")
    score = max(0, min(100, score))
    oos_ok = not split or split["out_of_sample"]["sharpe"] > 0
    if (sh < 0.5 or not oos_ok) and score >= 65:
        score = 60  # gate: "Promising" needs a decent absolute Sharpe and a working recent period
    if sh < 0:
        score = min(score, 35)
    label = "Promising" if score >= 65 else ("Mixed" if score >= 40 else "Weak")
    summary = {
        "Promising": "The strategy beat or matched buy & hold on a risk-adjusted basis and held up in robustness checks.",
        "Mixed": "There is some evidence for this strategy, but it has real weaknesses. Treat it as a starting point for research, not a ready system.",
        "Weak": "The evidence does not support trading this strategy as configured.",
    }[label]
    return {"label": label, "score": round(score), "pros": pros, "cons": cons, "summary": summary}
