"""Build the inputs champion_sim needs, from the shared store + alphaIMB data.

Shared by the labs paper tracker and the live runner's replay-to-now decider so
both feed the champion engine identical inputs. Pure data assembly — no broker,
no order placement, no labs imports.

  adf      DataFrame[timestamp, alpha, d_pe_sum, d_ce_sum, denom, spot] over the
           cell range, std or abs-denom per use_abs (mirrors build_alpha_regime)
  ce_map   {timestamp -> {strike -> CE OI}}   (v7.7 wall checks)
  pe_map   {timestamp -> {strike -> PE OI}}   (v7.7 DN-PUT filter + v7.9 D2)
  ohlc     champion_sim.OHLC — 1-min open/high/low/close, alphaIMB
           nifty_1min_ohlc.csv where present else shared-store 1-min spot
"""
from __future__ import annotations

import pandas as pd

from config.labs_config import SHARED_LIVE_DIR
from live.engine import champion_sim
from live.engine.alpha_hybrid import (
    ALPHA_DATA_DIR, _load_baseline, _load_live_data, _prepare_snapshot_frame,
    _previous_trading_days)

SYMBOL = "NIFTY"
VIX_TRAIL_CUTOFF = 17.0   # vix_open < cutoff (or missing) -> TRAIL regime


def ohlc_by_minute(trade_date: str) -> dict:
    """{'HH:MM': (open,high,low,close)} mirroring the research data path:
    alphaIMB nifty_1min_ohlc.csv where present, shared-store 1-min spot
    (open=high=low=close=spot) filling any minute not in the OHLC file."""
    out: dict = {}
    try:
        oc = pd.read_csv(ALPHA_DATA_DIR / "analytics" / "nifty_1min_ohlc.csv")
        oc["ts"] = pd.to_datetime(oc["timestamp"]).dt.tz_localize(None)
        oc = oc[oc["ts"].dt.strftime("%Y-%m-%d") == trade_date]
        for _, r in oc.iterrows():
            out[r["ts"].strftime("%H:%M")] = (
                float(r["open"]), float(r["high"]), float(r["low"]), float(r["close"]))
    except Exception:
        pass
    path = SHARED_LIVE_DIR / trade_date / f"{SYMBOL}_options_1min.csv"
    if path.exists():
        sdf = pd.read_csv(path, usecols=["timestamp", "spot"])
        for ts_s, sp in sdf.groupby("timestamp")["spot"].first().items():
            hm = str(ts_s)[11:16]
            if hm and hm not in out:
                out[hm] = (float(sp), float(sp), float(sp), float(sp))
    return out


def prev_close(trade_date: str) -> float | None:
    """Prev-day NIFTY close: vix_history.nifty_close (prior row) if present,
    else the previous trading day's last shared-store 1-min spot."""
    try:
        v = pd.read_csv(ALPHA_DATA_DIR / "analytics" / "vix_history.csv", dtype={"date": str})
        prev = v[v["date"] < trade_date].sort_values("date")
        if not prev.empty and pd.notna(prev.iloc[-1].get("nifty_close")):
            return float(prev.iloc[-1]["nifty_close"])
    except Exception:
        pass
    for pday in _previous_trading_days(trade_date):
        p = SHARED_LIVE_DIR / pday / f"{SYMBOL}_options_1min.csv"
        if p.exists():
            try:
                s = pd.read_csv(p, usecols=["timestamp", "spot"]).groupby("timestamp")["spot"].first()
                if len(s):
                    return float(s.iloc[-1])
            except Exception:
                continue
    return None


def build_sim_inputs(trade_date: str, lo: float, hi: float, use_abs: bool):
    """(snapshot_df, adf, ce_map, pe_map) for champion_sim from the alpha_hybrid
    pipeline the bot uses. adf mirrors research build_alpha_regime; ce_map/pe_map
    carry per-strike OI for the v7.7 filter and v7.9 D2 wall checks."""
    snap = _prepare_snapshot_frame(_load_live_data(trade_date), _load_baseline(trade_date))
    ce_map: dict = {}
    pe_map: dict = {}
    for ts, g in snap[snap["type"] == "ce"].groupby("bucket"):
        ce_map[ts] = dict(zip(g["strike"].astype(int), g["oi"].astype(float)))
    for ts, g in snap[snap["type"] == "pe"].groupby("bucket"):
        pe_map[ts] = dict(zip(g["strike"].astype(int), g["oi"].astype(float)))
    sub = snap[(snap["strike"] >= lo) & (snap["strike"] <= hi)]
    rows = []
    for ts, g in sub.groupby("bucket"):
        d_pe = float(g.loc[g["type"] == "pe", "delta_oi"].sum())
        d_ce = float(g.loc[g["type"] == "ce", "delta_oi"].sum())
        denom = (abs(d_pe) + abs(d_ce)) if use_abs else (d_pe + d_ce)
        alpha = ((d_pe - d_ce) * 100.0 / denom) if denom else 0.0
        rows.append(dict(timestamp=ts, alpha=round(alpha, 2), d_pe_sum=d_pe,
                         d_ce_sum=d_ce, denom=denom, spot=float(g["spot"].iloc[-1])))
    adf = pd.DataFrame(rows).sort_values("timestamp").reset_index(drop=True)
    return snap, adf, ce_map, pe_map


def day_context(trade_date: str, ohlc: "champion_sim.OHLC", direction, vix):
    """Resolve (sgap, weekday, use_trail, regime) for the cell."""
    pc = prev_close(trade_date)
    day_open = ohlc.day_open()
    if day_open is not None and pc is not None:
        sgap = day_open - pc
    else:
        sgap = -1.0 if (direction or "").upper() in ("DOWN", "DN") else 1.0
    weekday = pd.Timestamp(trade_date).strftime("%a")  # Mon..Fri
    try:
        use_trail = vix is None or float(vix) != float(vix) or float(vix) < VIX_TRAIL_CUTOFF
    except (TypeError, ValueError):
        use_trail = True
    return sgap, weekday, use_trail, ("TRAIL" if use_trail else "WALL")
