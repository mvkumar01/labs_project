"""Persistent daily PAPER tracker for the live Alpha champion (v2.11).

Replays the COMPLETED 5-min day through `live.engine.champion_sim` — a faithful,
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
from live.engine import champion_inputs, champion_sim
from live.engine.alpha_hybrid import _read_locked_hybrid_state
from config.labs_config import SHARED_LIVE_DIR, UNDERLYINGS

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
    (live.engine.champion_sim — Rule 1/2/3 + v7.6/v7.7/v7.8/v7.9-D2/v7.11 + trail
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
    # Resolve cell context (sgap needs 09:15 open + prev close) BEFORE building
    # inputs so the alpha source (regime vs gemini_c2) + formula are decided per
    # the Run F routing — see champion_inputs.alpha_source.
    ohlc = champion_sim.OHLC(champion_inputs.ohlc_by_minute(trade_date))
    sgap, weekday, use_trail, regime = champion_inputs.day_context(
        trade_date, ohlc, direction, day["vix"])
    range_source, use_abs = champion_inputs.alpha_source(
        tier, direction, day["vix"], sgap, day["biggap"])
    try:
        _, adf, ce_map, pe_map = champion_inputs.build_sim_inputs(
            trade_date, day["lower"], day["upper"], use_abs, range_source=range_source)
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
