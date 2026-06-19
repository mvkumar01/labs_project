"""Persistent daily PAPER tracker for the live Alpha champion (v2.11).

Replays the COMPLETED 5-min day through `labs.engine.champion_sim` — a faithful,
self-contained port of the validated alphaIMB research champion rule stack
(Rule 1/2/3 + v7.6 ALPHA_STALL + v7.7 PC400-DN PUT filter + v7.7 PC400-DN CALL
trail + v7.8 PC50 denom guard + v7.9 D2 + v7.11 drift stop + PC400 spot trail +
wall rejection). The alpha series itself comes from the same alpha_hybrid
pipeline the bot uses (range from alphaIMB's locked hybrid_range_state, or the
backfill override) and is bit-identical to research. Executes on PAPER (no
broker, never places orders), prices fills off the shared-store option LTP,
deducts real charges, and persists a daily row + per-trade rows to labs.db.

Pre-2026-06-19 this ran only the bot's per-bar AlphaSignalEngine (Rules 1/2/3 +
v7.8 + v7.11 + a hand-rolled v22 trail); v7.6 / v7.7 / v7.9-D2 were never wired
into labs, so /labs/live diverged from research on PC400 gap-DN days. Routing
through champion_sim closes that gap.

Run EOD once per day (pa_paper_tracker.py, ~15:40 IST) or intraday (loop). Idempotent per date.

Known live-vs-research residual: PC400 non-carve-out days where research uses
Gemini-c2 alpha (e.g. 06-03) are NOT reproducible from the locked range file and
degrade to regime+std here — by design (see alpha_hybrid docstring). The tracker
faithfully reflects the live bot, which is the point.

Modeling notes (v1, documented for honesty):
- Option = ATM strike (nearest 50 to entry spot) for the side; premium = the
  last shared-store LTP in the entry/exit 5-min bucket. The live bot's exact
  strike pick may differ slightly; PnL direction/magnitude is option-premium
  based (long option: profit = exit_premium - entry_premium).
- pnl_pts (spot move) is also recorded — that's the strategy's validated metric.
- Charges via labs.engine.charges (round-trip, buy-to-open/sell-to-close).
"""
from __future__ import annotations

import math
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pandas as pd

from storage.db import get_conn
from labs.engine.charges import round_trip_charges
from labs.engine import champion_sim
from live.engine.alpha_hybrid import (
    ALPHA_DATA_DIR, _load_baseline, _load_live_data, _prepare_snapshot_frame,
    _previous_trading_days, _read_locked_hybrid_state)
from config.labs_config import SHARED_LIVE_DIR, UNDERLYINGS

VIX_TRAIL_CUTOFF = 17.0   # vix_open < cutoff (or missing) -> TRAIL regime

IST = timezone(timedelta(hours=5, minutes=30))
SYMBOL = "NIFTY"
LOT_SIZE = int(UNDERLYINGS.get("NIFTY", {}).get("lot_size", 65))
PAPER_LOTS = 1                       # tracker trades 1 lot (unit strategy view)
QTY = LOT_SIZE * PAPER_LOTS
STRATEGY_VERSION = "alpha_v2.11"


# ── schema (lazy; never touches existing labs.db tables) ─────────────────────
def _ensure_tables(conn) -> None:
    conn.executescript(
        """
        CREATE TABLE IF NOT EXISTS paper_strategy_daily (
            trade_date   TEXT PRIMARY KEY,
            status       TEXT,
            tier         TEXT,
            gap_dir      TEXT,
            n_trades     INTEGER,
            pnl_pts      REAL,
            gross_rs     REAL,
            charges_rs   REAL,
            net_rs       REAL,
            strategy_version TEXT,
            updated_at   TEXT
        );
        CREATE TABLE IF NOT EXISTS paper_strategy_trades (
            trade_date   TEXT,
            seq          INTEGER,
            side         TEXT,
            strike       INTEGER,
            entry_ts     TEXT,
            exit_ts      TEXT,
            entry_spot   REAL,
            exit_spot    REAL,
            entry_prem   REAL,
            exit_prem    REAL,
            pnl_pts      REAL,
            gross_rs     REAL,
            charges_rs   REAL,
            net_rs       REAL,
            entry_rule   TEXT,
            exit_reason  TEXT,
            PRIMARY KEY (trade_date, seq)
        );
        """
    )
    conn.commit()


def _r50(x: float) -> int:
    return int(round(float(x) / 50.0) * 50)


def _session_over(trade_date: str) -> bool:
    """True once trade_date's session is finished — a past day, or today at/after
    15:30 IST. Until then an open position is HELD (marked to market), not squared
    off as a fake eod at the latest completed bar."""
    now = datetime.now(IST)
    today = now.date().isoformat()
    if trade_date < today:
        return True
    if trade_date == today:
        return (now.hour * 60 + now.minute) >= (15 * 60 + 30)
    return False


def _premium_lookup(trade_date: str) -> dict:
    """(bucket_start_iso, strike, 'ce'|'pe') -> last LTP in that 5-min bucket."""
    path = SHARED_LIVE_DIR / trade_date / f"{SYMBOL}_options_1min.csv"
    if not path.exists():
        return {}
    df = pd.read_csv(path)
    if "option_type" in df.columns:
        df["type"] = df["option_type"].astype(str).str.lower()
    if "ltp" not in df.columns or "type" not in df.columns:
        return {}
    df["timestamp"] = pd.to_datetime(df["timestamp"], errors="coerce")
    df = df.dropna(subset=["timestamp"])
    if df["timestamp"].dt.tz is None:
        df["timestamp"] = df["timestamp"].dt.tz_localize(IST)
    else:
        df["timestamp"] = df["timestamp"].dt.tz_convert(IST)
    df["strike"] = pd.to_numeric(df["strike"], errors="coerce")
    df["ltp"] = pd.to_numeric(df["ltp"], errors="coerce")
    df = df.dropna(subset=["strike", "ltp"])
    df["bucket"] = df["timestamp"].dt.floor("5min")
    out: dict = {}
    for (bucket, strike, typ), g in df.groupby(["bucket", "strike", "type"]):
        out[(bucket.isoformat(), int(strike), str(typ))] = float(g.sort_values("timestamp")["ltp"].iloc[-1])
    return out


def _premium(lookup: dict, bar_ts_iso: str, strike: int, otype: str) -> float | None:
    # champion_sim entry/exit ts is the 5-min bucket-start (label=left); match directly.
    key = (pd.Timestamp(bar_ts_iso).isoformat(), int(strike), otype)
    return lookup.get(key)


def _ohlc_by_minute(trade_date: str) -> dict:
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


def _prev_close(trade_date: str) -> float | None:
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


def _build_sim_inputs(trade_date: str, lo: float, hi: float, use_abs: bool):
    """Build (snapshot_df, adf, ce_map, pe_map) for champion_sim from the same
    alpha_hybrid pipeline the bot uses. adf mirrors research build_alpha_regime:
    per-bar alpha + d_pe_sum/d_ce_sum/denom over [lo,hi]; ce_map/pe_map carry
    per-strike OI for the v7.7 PC400-DN PUT filter and v7.9 D2 wall checks."""
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


def _resolve_day(trade_date: str, override: dict | None) -> dict | None:
    """Resolve the day's range + cell context, from the backfill override or the
    locked hybrid state. Returns None for no-trade (SKIP / not locked)."""
    if override is not None:
        if override.get("skip") or override.get("bucket") == "SKIP":
            return None
        return {"lower": float(override["lower"]), "upper": float(override["upper"]),
                "bucket": override["bucket"], "direction": override["direction"],
                "vix": override.get("vix"), "biggap": bool(override.get("pc400_v210_biggap"))}
    state = _read_locked_hybrid_state(trade_date)
    if state is None:
        return None
    return {"lower": float(state["lower"]), "upper": float(state["upper"]),
            "bucket": state.get("bucket") or "PC50", "direction": state.get("direction"),
            "vix": state.get("vix_at_open"), "biggap": bool(state.get("pc400_v210_biggap"))}


def run_day(trade_date: str | None = None, override: dict | None = None) -> dict:
    """Replay one day on paper through the faithful research champion engine
    (labs.engine.champion_sim — Rule 1/2/3 + v7.6/v7.7/v7.8/v7.9-D2/v7.11 + trail
    + wall rejection). `override` (backfill) supplies the champion range when the
    live hybrid_range_state isn't available: {lower, upper, bucket, direction,
    vix, pc400_v210_biggap, skip}."""
    trade_date = trade_date or datetime.now(IST).date().isoformat()
    conn = get_conn()
    _ensure_tables(conn)

    day = _resolve_day(trade_date, override)
    if day is None:
        tier = "SKIP" if (override or {}).get("bucket") == "SKIP" or (override or {}).get("skip") else None
        _save_daily(conn, trade_date, status="no_trade", tier=tier,
                    gap_dir=(override or {}).get("direction"), trades=[])
        return {"trade_date": trade_date, "status": "no_trade", "net_rs": 0.0, "n_trades": 0}

    tier, direction = day["bucket"], day["direction"]
    use_abs = (tier == "PC50" and direction == "UP") or day["biggap"]
    try:
        snap, adf, ce_map, pe_map = _build_sim_inputs(trade_date, day["lower"], day["upper"], use_abs)
    except Exception:
        adf = pd.DataFrame()
    if adf is None or adf.empty:
        _save_daily(conn, trade_date, status="no_trade", tier=tier, gap_dir=direction, trades=[])
        return {"trade_date": trade_date, "status": "no_trade", "net_rs": 0.0, "n_trades": 0}

    # Completed-bar discipline for today's live run: drop the in-progress bucket.
    if trade_date == datetime.now(IST).date().isoformat():
        cutoff = pd.Timestamp(datetime.now(IST)) - pd.Timedelta(minutes=5)
        adf = adf[adf["timestamp"] <= cutoff].reset_index(drop=True)
    if len(adf) < 2:
        _save_daily(conn, trade_date, status="no_trade", tier=tier, gap_dir=direction, trades=[])
        return {"trade_date": trade_date, "status": "no_trade", "net_rs": 0.0, "n_trades": 0}

    ohlc = champion_sim.OHLC(_ohlc_by_minute(trade_date))
    prev_close = _prev_close(trade_date)
    day_open = ohlc.day_open()
    if day_open is not None and prev_close is not None:
        sgap = day_open - prev_close
    else:
        sgap = -1.0 if (direction or "").upper() in ("DOWN", "DN") else 1.0
    weekday = datetime.fromisoformat(trade_date).strftime("%a")  # Mon..Fri
    try:
        use_trail = day["vix"] is None or float(day["vix"]) != float(day["vix"]) or float(day["vix"]) < VIX_TRAIL_CUTOFF
    except (TypeError, ValueError):
        use_trail = True
    regime = "TRAIL" if use_trail else "WALL"

    _, sim_trades = champion_sim.simulate(
        adf, ce_map, pe_map, ohlc, trade_date, use_trail, sgap, tier,
        weekday, regime, day["lower"], day["upper"])

    lookup = _premium_lookup(trade_date)
    trades = [_price_trade(lookup, t) for t in sim_trades]

    status = "traded"
    if trades and trades[-1]["exit_reason"] == "EOD" and not _session_over(trade_date):
        # intraday: the final open position isn't squared off — mark to market
        # (net = "if closed now") and surface it as HOLDING, not a fake eod.
        trades[-1]["exit_reason"] = "holding"
        status = "open"
    elif trades and trades[-1]["exit_reason"] == "EOD":
        trades[-1]["exit_reason"] = "eod"

    _save_daily(conn, trade_date, status=status, tier=tier, gap_dir=direction, trades=trades)
    net = round(sum(t["net_rs"] for t in trades), 2)
    return {"trade_date": trade_date, "status": status, "net_rs": net, "n_trades": len(trades)}


def _price_trade(lookup: dict, t: dict) -> dict:
    """Translate a champion_sim trade into a priced paper trade: option premium
    off the shared-store ATM LTP (spot-move fallback) + round-trip charges. pnl_pts
    is the strategy's validated spot-move metric (t['pnl'])."""
    side = "CALL" if t["pos"] == "call" else "PUT"
    strike = _r50(t["entry_spot"])
    otype = "ce" if side == "CALL" else "pe"
    ets = pd.Timestamp(t["entry_ts"]).isoformat()
    xts = pd.Timestamp(t["exit_ts"]).isoformat()
    ep = _premium(lookup, ets, strike, otype) or 150.0
    xp = _premium(lookup, xts, strike, otype)
    if xp is None:
        move = (t["exit_spot"] - t["entry_spot"]) if side == "CALL" else (t["entry_spot"] - t["exit_spot"])
        xp = max(0.05, ep + 0.5 * move)
    gross_rs = (float(xp) - float(ep)) * QTY
    ch = round_trip_charges(ep, xp, QTY)
    return dict(
        side=side, strike=strike, entry_ts=ets, exit_ts=xts,
        entry_spot=round(t["entry_spot"], 1), exit_spot=round(t["exit_spot"], 1),
        entry_prem=round(ep, 2), exit_prem=round(xp, 2),
        pnl_pts=round(float(t["pnl"]), 1), gross_rs=round(gross_rs, 2),
        charges_rs=ch["total"], net_rs=round(gross_rs - ch["total"], 2),
        entry_rule=t["entry_rule"], exit_reason=t["reason"],
    )


def _save_daily(conn, trade_date, *, status, tier, gap_dir, trades) -> None:
    pnl_pts = round(sum(t["pnl_pts"] for t in trades), 1)
    gross = round(sum(t["gross_rs"] for t in trades), 2)
    charges = round(sum(t["charges_rs"] for t in trades), 2)
    net = round(gross - charges, 2)
    now = datetime.now(IST).isoformat()
    conn.execute("DELETE FROM paper_strategy_trades WHERE trade_date = ?", (trade_date,))
    for i, t in enumerate(trades, 1):
        conn.execute(
            "INSERT INTO paper_strategy_trades (trade_date, seq, side, strike, entry_ts, "
            "exit_ts, entry_spot, exit_spot, entry_prem, exit_prem, pnl_pts, gross_rs, "
            "charges_rs, net_rs, entry_rule, exit_reason) VALUES "
            "(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (trade_date, i, t["side"], t["strike"], t["entry_ts"], t["exit_ts"],
             t["entry_spot"], t["exit_spot"], t["entry_prem"], t["exit_prem"],
             t["pnl_pts"], t["gross_rs"], t["charges_rs"], t["net_rs"],
             t["entry_rule"], t["exit_reason"]))
    conn.execute(
        "INSERT INTO paper_strategy_daily (trade_date, status, tier, gap_dir, n_trades, "
        "pnl_pts, gross_rs, charges_rs, net_rs, strategy_version, updated_at) "
        "VALUES (?,?,?,?,?,?,?,?,?,?,?) "
        "ON CONFLICT(trade_date) DO UPDATE SET status=excluded.status, tier=excluded.tier, "
        "gap_dir=excluded.gap_dir, n_trades=excluded.n_trades, pnl_pts=excluded.pnl_pts, "
        "gross_rs=excluded.gross_rs, charges_rs=excluded.charges_rs, net_rs=excluded.net_rs, "
        "strategy_version=excluded.strategy_version, updated_at=excluded.updated_at",
        (trade_date, status, tier, gap_dir, len(trades), pnl_pts, gross, charges, net,
         STRATEGY_VERSION, now))
    conn.commit()


if __name__ == "__main__":
    import sys
    d = sys.argv[1] if len(sys.argv) > 1 else None
    print(run_day(d))
