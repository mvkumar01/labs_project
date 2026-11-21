"""'Is X a good investment?': an evidence scorecard built from price history,
fundamentals (equities) and how standard strategies have performed on the instrument."""
from __future__ import annotations

import math

import numpy as np
import pandas as pd

from . import data, engine, indicators as ind


def _ret(close: pd.Series, bars: int) -> float | None:
    if len(close) <= bars:
        return None
    return float(close.iloc[-1] / close.iloc[-1 - bars] - 1) * 100


def _cagr(close: pd.Series, years: int) -> float | None:
    start = close.index[-1] - pd.DateOffset(years=years)
    if close.index[0] > start + pd.Timedelta(days=10):
        return None
    s = close[close.index >= start]
    return float((s.iloc[-1] / s.iloc[0]) ** (1 / years) - 1) * 100


def _clip(x, lo=0.0, hi=100.0):
    return float(max(lo, min(hi, x)))


def analyze(ticker: str) -> dict:
    df = data.get_history(ticker, "1d")
    if len(df) < 60:
        raise ValueError(f"Not enough history for {ticker} to analyse.")
    close = df["Close"]
    ac = data.asset_class(ticker)
    last = float(close.iloc[-1])
    sma50, sma200 = ind.sma(close, 50), ind.sma(close, 200)
    rsi14 = ind.rsi(close, 14)
    rets = close.pct_change().dropna()
    one_y = rets.iloc[-252:]
    three_y = rets.iloc[-756:]
    vol = float(one_y.std() * math.sqrt(252) * 100)
    sharpe3 = float(three_y.mean() / three_y.std() * math.sqrt(252)) if three_y.std() > 0 else 0.0
    c5 = close[close.index >= close.index[-1] - pd.DateOffset(years=5)]
    dd5 = float((c5 / c5.cummax() - 1).min() * 100)
    ath = float(close.max())
    hi52, lo52 = float(close.iloc[-252:].max()), float(close.iloc[-252:].min())

    stats = {
        "price": last, "date": close.index[-1].strftime("%Y-%m-%d"),
        "change_1d": _ret(close, 1), "ret_1m": _ret(close, 21), "ret_3m": _ret(close, 63),
        "ret_6m": _ret(close, 126), "ret_1y": _ret(close, 252),
        "cagr_3y": _cagr(close, 3), "cagr_5y": _cagr(close, 5), "cagr_10y": _cagr(close, 10),
        "volatility_1y": vol, "sharpe_3y": sharpe3, "max_drawdown_5y": dd5,
        "from_ath": (last / ath - 1) * 100, "high_52w": hi52, "low_52w": lo52,
        "range_position_52w": (last - lo52) / (hi52 - lo52) * 100 if hi52 > lo52 else None,
        "sma50": float(sma50.iloc[-1]) if not np.isnan(sma50.iloc[-1]) else None,
        "sma200": float(sma200.iloc[-1]) if not np.isnan(sma200.iloc[-1]) else None,
        "rsi14": float(rsi14.iloc[-1]) if not np.isnan(rsi14.iloc[-1]) else None,
        "history_start": close.index[0].strftime("%Y-%m-%d"),
    }

    # ---- scores ------------------------------------------------------------------------
    factors = []
    s50, s200 = stats["sma50"], stats["sma200"]
    if s200:
        slope = float(sma200.iloc[-1] / sma200.iloc[-21] - 1) if len(sma200.dropna()) > 21 else 0
        trend = (35 if last > s200 else 0) + (30 if s50 and s50 > s200 else 0) + \
                (20 if s50 and last > s50 else 0) + (15 if slope > 0 else 0)
        desc = []
        desc.append(f"Price is {'above' if last > s200 else 'below'} its 200-day average")
        if s50:
            desc.append(f"50-day is {'above' if s50 > s200 else 'below'} the 200-day ({'golden' if s50 > s200 else 'death'} cross regime)")
        desc.append(f"200-day average is {'rising' if slope > 0 else 'falling'}")
        factors.append({"name": "Trend", "score": trend, "weight": 1.0, "detail": "; ".join(desc) + "."})
    r6, r12 = stats["ret_6m"], stats["ret_1y"]
    if r6 is not None and r12 is not None:
        mom = _clip(50 + (0.5 * r6 + 0.5 * r12) * 1.5)
        detail = f"6-month return {r6:+.1f}%, 12-month {r12:+.1f}%."
        if stats["rsi14"] and stats["rsi14"] > 75:
            mom = _clip(mom - 10); detail += f" RSI {stats['rsi14']:.0f} is overbought, so a pullback is common."
        elif stats["rsi14"] and stats["rsi14"] < 25:
            detail += f" RSI {stats['rsi14']:.0f} is deeply oversold."
        factors.append({"name": "Momentum", "score": mom, "weight": 1.0, "detail": detail})
    risk = _clip(100 - (vol - 10) * 2.2 + max(dd5, -60) * 0.4 + 20)
    factors.append({"name": "Risk", "score": risk, "weight": 0.8,
                    "detail": f"Annualised volatility {vol:.1f}%; worst 5-year drawdown {dd5:.1f}%. A higher score means a calmer instrument."})
    perf = _clip(50 + sharpe3 * 30)
    c5y = stats["cagr_5y"]
    factors.append({"name": "Track record", "score": perf, "weight": 1.0,
                    "detail": f"3-year Sharpe ratio {sharpe3:.2f}" + (f"; 5-year CAGR {c5y:+.1f}%." if c5y is not None else ".")})

    fundamentals = {}
    if ac == "equity":
        fundamentals = data.ticker_info(ticker)
        f = fundamentals
        pe, fpe = f.get("trailingPE"), f.get("forwardPE")
        val_pts, val_desc = [], []
        if pe and pe > 0:
            val_pts.append(85 if pe < 12 else 70 if pe < 18 else 55 if pe < 25 else 40 if pe < 35 else 25 if pe < 60 else 10)
            val_desc.append(f"Trailing P/E {pe:.1f}")
        if fpe and pe and fpe > 0 and pe > 0:
            val_pts.append(70 if fpe < pe * 0.85 else 50 if fpe < pe else 35)
            val_desc.append(f"forward P/E {fpe:.1f}")
        peg = f.get("trailingPegRatio") or f.get("pegRatio")
        if peg and peg > 0:
            val_pts.append(80 if peg < 1 else 60 if peg < 1.5 else 40 if peg < 2.5 else 20)
            val_desc.append(f"PEG {peg:.2f}")
        tgt, cur = f.get("targetMeanPrice"), f.get("currentPrice") or last
        if tgt and cur:
            up = (tgt / cur - 1) * 100
            val_pts.append(_clip(50 + up * 2))
            val_desc.append(f"analyst mean target implies {up:+.1f}%")
        if val_pts:
            factors.append({"name": "Valuation", "score": float(np.mean(val_pts)), "weight": 1.0,
                            "detail": ", ".join(val_desc) + "."})
        q_pts, q_desc = [], []
        roe, pm, rg, de = f.get("returnOnEquity"), f.get("profitMargins"), f.get("revenueGrowth"), f.get("debtToEquity")
        if roe is not None:
            q_pts.append(_clip(30 + roe * 250)); q_desc.append(f"ROE {roe * 100:.1f}%")
        if pm is not None:
            q_pts.append(_clip(30 + pm * 250)); q_desc.append(f"net margin {pm * 100:.1f}%")
        if rg is not None:
            q_pts.append(_clip(50 + rg * 200)); q_desc.append(f"revenue growth {rg * 100:+.1f}%")
        if de is not None:
            q_pts.append(_clip(90 - de * 0.4)); q_desc.append(f"debt/equity {de:.0f}%")
        if q_pts:
            factors.append({"name": "Quality & growth", "score": float(np.mean(q_pts)), "weight": 1.0,
                            "detail": ", ".join(q_desc) + "."})

    wsum = sum(f["weight"] for f in factors)
    overall = sum(f["score"] * f["weight"] for f in factors) / wsum if wsum else 50
    for f in factors:
        f["score"] = round(f["score"])
    label = "Favorable" if overall >= 62 else ("Neutral" if overall >= 45 else "Unfavorable")

    # ---- which systematic approaches have worked on it --------------------------------
    start = (close.index[-1] - pd.DateOffset(years=10)).strftime("%Y-%m-%d")
    fit = []
    curves = {}
    for key in ["buy_hold", "sma_cross", "price_sma", "momentum", "donchian", "rsi", "macd"]:
        try:
            r = engine.run_backtest(ticker, key, None, start, None, "1d", robustness=False, detail=False)
        except Exception:
            continue
        m = r["metrics"]
        fit.append({"strategy": key, "name": r["strategy_name"], "cagr": m["cagr"], "sharpe": m["sharpe"],
                    "max_drawdown": m["max_drawdown"], "trades": m.get("trades"), "exposure": m.get("exposure")})
        curves[r["strategy_name"]] = r["series"]["equity"]
    fit.sort(key=lambda x: x["sharpe"] or -9, reverse=True)

    # ---- chart series (5y) -----------------------------------------------------------
    c = df[df.index >= df.index[-1] - pd.DateOffset(years=5)]
    def line(s):
        s = s.reindex(c.index).dropna()
        return [{"time": t.strftime("%Y-%m-%d"), "value": round(float(v), 6)} for t, v in s.items()]
    price = [{"time": t.strftime("%Y-%m-%d"), "open": round(r.Open, 6), "high": round(r.High, 6),
              "low": round(r.Low, 6), "close": round(r.Close, 6)} for t, r in c.iterrows()]

    bull = [f"{f['name']}: {f['detail']}" for f in factors if f["score"] >= 60]
    bear = [f"{f['name']}: {f['detail']}" for f in factors if f["score"] < 60]
    if stats["rsi14"] and stats["rsi14"] >= 70:
        bear.append(f"RSI(14) is {stats['rsi14']:.0f}, which is overbought in the short term. Entries after sharp rallies often see a pullback.")
    if stats["ret_1y"] is not None and stats["ret_1y"] < 0:
        bear.append(f"Down {abs(stats['ret_1y']):.1f}% over the past year.")
    if stats["max_drawdown_5y"] < -35:
        bear.append(f"It fell {abs(stats['max_drawdown_5y']):.0f}% from peak to trough within the last 5 years, so size positions for that.")
    summary = {
        "Favorable": "Most of the evidence (trend, momentum, track record and valuation where available) leans positive.",
        "Neutral": "The evidence is mixed. Some factors support the instrument and others argue for caution.",
        "Unfavorable": "Most of the evidence currently argues for caution.",
    }[label]

    return {
        "kind": "analysis",
        "symbol": ticker, "name": fundamentals.get("longName") or data.display_name(ticker), "asset_class": ac,
        "stats": {k: (None if isinstance(v, float) and (math.isnan(v) or math.isinf(v)) else v) for k, v in stats.items()},
        "fundamentals": fundamentals,
        "factors": factors, "score": round(overall), "label": label, "summary": summary,
        "bull_points": bull, "bear_points": bear,
        "strategy_fit": fit, "strategy_curves": curves,
        "series": {"price": price, "sma50": line(sma50), "sma200": line(sma200)},
    }
