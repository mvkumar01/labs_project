"""Alpha-CPR paper tracker: v2.11 alpha entries, CPR structural exits.

Alpha is an ENTRY TRIGGER ONLY. Every v2.11 alpha exit (tier SL/TP, ALPHA_STALL)
and every v2.11 spot exit (TRAIL, WALL_REJ, PC250 spot TP/SL, v7.11 drift) is
switched off. A position leaves only on the prev-day CPR level above or below
its entry spot, or at EOD.

Position size varies so the rupee risk per trade is ~constant:

    lots = round(RISK_BUDGET / (delta_spot_SL * SIZING_DELTA * LOT_SIZE))
    clamped to [1, MAX_LOTS]

`SIZING_DELTA` (0.60) is a *sizing* assumption carried over from the research
brief, not a pricing model. P&L is priced from real executable quotes exactly
like v2.12 / v2.13 — nearest-expiry 200-point ITM, ask-in / bid-out, statutory
charges — so this tab is directly comparable with the others on the page.

Research: alphaIMB `research/experiments/2026-08-06_cpr_sl_lot_sizing`
(variant E, min_dist 20, cap 15: +288.0 spot pts, PF 1.67 over Apr 1 - Jun 18
2026). Paper-trade candidate only — PF on a points basis is ~1.2, and the
research edge leaned on the sizing overlay. This module is paper-only and never
places broker orders.
"""
from __future__ import annotations

import json
import sqlite3
from datetime import datetime

from labs.engine import cpr_levels as cpr
from labs.engine.alpha_v212_tracker import (
    AlphaV212InputError,
    ITM_DISTANCE,
    QTY,
    _quote_at_or_after,
    _r50,
    _timestamp_key,
    build_executable_book,
)
from labs.engine.charges import round_trip_charges
from labs.engine.paper_strategy_tracker import (
    IST,
    ReplayInputError,
    _resolve_day,
    _resolve_replay_context,
    _session_over,
)
from live.engine import champion_inputs, champion_sim
from storage.db import get_conn

# ── strategy configuration ───────────────────────────────────────────────────
CPR_MIN_DIST = 20.0      # spot pts; levels nearer than this are walked out
RISK_BUDGET = 9750.0     # Rs per trade (50 pts x 5 lots x Rs 39)
MAX_LOTS = 15
MIN_LOTS = 1
SIZING_DELTA = 0.60
LOT_SIZE = QTY           # 65

STRATEGY_VERSION = (
    "alpha_cpr_v1_entry_alpha_exit_cpr_both_"
    f"mindist{int(CPR_MIN_DIST)}_risk{int(RISK_BUDGET)}_cap{MAX_LOTS}_"
    "itm200_bidask"
)


class AlphaCprInputError(RuntimeError):
    """Required Alpha-CPR replay, CPR-level or quote input is incomplete."""


def lots_for(delta_spot_sl) -> int:
    """Risk-normalised lot count. No stop level -> the cap (cannot size)."""
    try:
        d = float(delta_spot_sl)
    except (TypeError, ValueError):
        return MAX_LOTS
    if d <= 0:
        return MAX_LOTS
    raw = RISK_BUDGET / (d * SIZING_DELTA * LOT_SIZE)
    return int(max(MIN_LOTS, min(MAX_LOTS, round(raw))))


def _delta_spot_sl(segment: dict):
    stop = segment.get("cpr_sl")
    if stop is None:
        return None
    return round(abs(float(segment["entry_spot"]) - float(stop)), 2)


def _price_cpr_segment(segment: dict, expiry_code: str, quotes: dict) -> dict:
    """Mirror of alpha_v212_tracker._price_segment with variable lots."""
    side = "CALL" if segment["pos"] == "call" else "PUT"
    option_type = "ce" if side == "CALL" else "pe"
    entry_spot = float(segment["entry_spot"])
    strike = (
        _r50(entry_spot - ITM_DISTANCE) if side == "CALL"
        else _r50(entry_spot + ITM_DISTANCE)
    )
    entry_ts, entry = _quote_at_or_after(
        quotes, _timestamp_key(segment["entry_ts"]), strike, option_type)
    exit_ts, exit_ = _quote_at_or_after(
        quotes, _timestamp_key(segment["exit_ts"]), strike, option_type)
    entry_bid, entry_ask = entry.get("bid"), entry.get("ask")
    exit_bid, exit_ask = exit_.get("bid"), exit_.get("ask")

    dsl = _delta_spot_sl(segment)
    lots = lots_for(dsl)
    qty = LOT_SIZE * lots

    quote_status = "priced"
    option_points = gross = charges = net = None
    if entry_bid is None or entry_ask is None:
        quote_status = "entry_book_unavailable"
    elif entry_ask < entry_bid:
        quote_status = "entry_market_crossed"
    elif exit_bid is None or exit_ask is None:
        quote_status = "exit_book_unavailable"
    elif exit_ask < exit_bid:
        quote_status = "exit_market_crossed"
    else:
        option_points = round(exit_bid - entry_ask, 2)
        gross = round(option_points * qty, 2)
        detail = round_trip_charges(entry_ask, exit_bid, qty)
        charges = float(detail.get("raw_total", detail["total"]))
        net = gross - charges
    return {
        "status": "closed",
        "side": side,
        "strike": strike,
        "expiry_code": expiry_code,
        "tradingsymbol": entry.get("tradingsymbol"),
        "entry_ts": entry_ts,
        "exit_ts": exit_ts,
        "entry_spot": entry_spot,
        "exit_spot": float(segment["exit_spot"]),
        "spot_pnl_pts": float(segment["pnl"]),
        "entry_bid": entry_bid,
        "entry_ask": entry_ask,
        "exit_bid": exit_bid,
        "exit_ask": exit_ask,
        "option_pnl_pts": option_points,
        "gross_rs": gross,
        "charges_rs": charges,
        "net_rs": net,
        "quote_status": quote_status,
        "entry_rule": segment.get("entry_rule"),
        "exit_reason": str(segment.get("reason") or "unknown"),
        "lots": lots,
        "cpr_sl": segment.get("cpr_sl"),
        "cpr_tp": segment.get("cpr_tp"),
        "delta_spot_sl": dsl,
        "risk_rs": (round(dsl * lots * SIZING_DELTA * LOT_SIZE, 2)
                    if dsl is not None else None),
    }


def _ensure_tables(conn: sqlite3.Connection) -> None:
    conn.executescript(
        """
        CREATE TABLE IF NOT EXISTS alpha_cpr_daily (
            trade_date TEXT PRIMARY KEY,
            status TEXT NOT NULL,
            tier TEXT,
            gap_dir TEXT,
            expiry_code TEXT,
            n_segments INTEGER NOT NULL,
            priced_segments INTEGER NOT NULL,
            unavailable_segments INTEGER NOT NULL,
            spot_pnl_pts REAL NOT NULL,
            gross_rs REAL NOT NULL,
            charges_rs REAL NOT NULL,
            net_rs REAL NOT NULL,
            strategy_version TEXT NOT NULL,
            context_json TEXT,
            updated_at TEXT NOT NULL,
            cpr_prev_date TEXT,
            avg_lots REAL
        );
        CREATE TABLE IF NOT EXISTS alpha_cpr_trades (
            trade_date TEXT NOT NULL,
            seq INTEGER NOT NULL,
            status TEXT NOT NULL,
            side TEXT NOT NULL,
            strike INTEGER NOT NULL,
            expiry_code TEXT,
            tradingsymbol TEXT,
            entry_ts TEXT NOT NULL,
            exit_ts TEXT NOT NULL,
            entry_spot REAL NOT NULL,
            exit_spot REAL NOT NULL,
            spot_pnl_pts REAL NOT NULL,
            entry_bid REAL,
            entry_ask REAL,
            exit_bid REAL,
            exit_ask REAL,
            option_pnl_pts REAL,
            gross_rs REAL,
            charges_rs REAL,
            net_rs REAL,
            quote_status TEXT NOT NULL,
            entry_rule TEXT,
            exit_reason TEXT NOT NULL,
            lots INTEGER,
            cpr_sl REAL,
            cpr_tp REAL,
            delta_spot_sl REAL,
            risk_rs REAL,
            PRIMARY KEY (trade_date, seq)
        );
        """
    )
    conn.commit()


def replay_cpr(trade_date: str, override: dict | None = None) -> dict:
    """v2.11 entries + CPR-only exits for one session."""
    day = _resolve_day(trade_date, override)
    if day is None:
        explicitly_skipped = override is not None and (
            override.get("bucket") == "SKIP" or override.get("skip"))
        if not explicitly_skipped:
            raise AlphaCprInputError(
                f"No locked range state is available for {trade_date}")
        return {"tier": "SKIP", "direction": override.get("direction"),
                "segments": [], "session_done": _session_over(trade_date),
                "cpr_prev_date": None}

    tier = day["bucket"]
    ohlc, context, range_source, use_abs, provenance = (
        _resolve_replay_context(trade_date, day))
    try:
        levels, prev_date = cpr.levels_for(trade_date, "NIFTY")
    except cpr.CprInputError as exc:
        raise AlphaCprInputError(
            f"CPR levels unavailable for {trade_date}: {exc}") from exc
    try:
        _, adf, ce_map, pe_map = champion_inputs.build_sim_inputs(
            trade_date, day["lower"], day["upper"], use_abs,
            range_source=range_source)
    except Exception as exc:
        raise AlphaCprInputError(
            f"Unable to build Alpha-CPR inputs for {trade_date}: "
            f"{type(exc).__name__}: {exc}") from exc
    if adf is None or adf.empty:
        raise AlphaCprInputError(f"Alpha-CPR input frame is empty for {trade_date}")
    if len(adf) < 2:
        raise AlphaCprInputError(
            f"Alpha-CPR input frame has only {len(adf)} completed bars "
            f"for {trade_date}")

    cutoff = champion_sim.exact_mark_cutoff(trade_date, datetime.now(IST))
    session_done = _session_over(trade_date)
    _, segments = champion_sim.simulate(
        adf, ce_map, pe_map, ohlc, trade_date, context.use_trail, context.sgap,
        tier, context.weekday, context.regime, day["lower"], day["upper"],
        no_alpha_exits=True,
        cpr_levels=levels,
        enable_cpr_sl=True,
        enable_cpr_tp=True,
        cpr_min_dist=CPR_MIN_DIST,
        close_eod=session_done,
        entries_until_ts=cutoff,
    )
    return {"tier": tier, "direction": context.direction, "segments": segments,
            "session_done": session_done, "context": provenance,
            "cpr_prev_date": prev_date}


def _save(conn, trade_date, replay, expiry_code, trades, *, commit):
    priced = [t for t in trades if t["quote_status"] == "priced"]
    unavailable = len(trades) - len(priced)
    status = ("partial_unavailable" if trades and unavailable
              else "open" if any(t["status"] == "open" for t in trades)
              else "traded" if trades else "no_trade")
    conn.execute("DELETE FROM alpha_cpr_trades WHERE trade_date=?", (trade_date,))
    for seq, t in enumerate(trades, 1):
        conn.execute(
            "INSERT INTO alpha_cpr_trades "
            "(trade_date,seq,status,side,strike,expiry_code,tradingsymbol,entry_ts,"
            "exit_ts,entry_spot,exit_spot,spot_pnl_pts,entry_bid,entry_ask,exit_bid,"
            "exit_ask,option_pnl_pts,gross_rs,charges_rs,net_rs,quote_status,"
            "entry_rule,exit_reason,lots,cpr_sl,cpr_tp,delta_spot_sl,risk_rs) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (trade_date, seq, t["status"], t["side"], t["strike"],
             t.get("expiry_code"), t.get("tradingsymbol"), t["entry_ts"],
             t["exit_ts"], t["entry_spot"], t["exit_spot"], t["spot_pnl_pts"],
             t.get("entry_bid"), t.get("entry_ask"), t.get("exit_bid"),
             t.get("exit_ask"), t.get("option_pnl_pts"), t.get("gross_rs"),
             t.get("charges_rs"), t.get("net_rs"), t["quote_status"],
             t.get("entry_rule"), t["exit_reason"], t.get("lots"),
             t.get("cpr_sl"), t.get("cpr_tp"), t.get("delta_spot_sl"),
             t.get("risk_rs")))
    spot = round(sum(float(t["spot_pnl_pts"]) for t in trades), 2)
    gross = round(sum(float(t["gross_rs"]) for t in priced), 2)
    charges = round(sum(float(t["charges_rs"]) for t in priced), 2)
    net = round(sum(float(t["net_rs"]) for t in priced), 2)
    avg_lots = (round(sum(int(t.get("lots") or 0) for t in trades) / len(trades), 2)
                if trades else None)
    conn.execute(
        "INSERT INTO alpha_cpr_daily "
        "(trade_date,status,tier,gap_dir,expiry_code,n_segments,priced_segments,"
        "unavailable_segments,spot_pnl_pts,gross_rs,charges_rs,net_rs,"
        "strategy_version,context_json,updated_at,cpr_prev_date,avg_lots) "
        "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?) "
        "ON CONFLICT(trade_date) DO UPDATE SET status=excluded.status,"
        "tier=excluded.tier,gap_dir=excluded.gap_dir,expiry_code=excluded.expiry_code,"
        "n_segments=excluded.n_segments,priced_segments=excluded.priced_segments,"
        "unavailable_segments=excluded.unavailable_segments,"
        "spot_pnl_pts=excluded.spot_pnl_pts,gross_rs=excluded.gross_rs,"
        "charges_rs=excluded.charges_rs,net_rs=excluded.net_rs,"
        "strategy_version=excluded.strategy_version,"
        "context_json=excluded.context_json,updated_at=excluded.updated_at,"
        "cpr_prev_date=excluded.cpr_prev_date,avg_lots=excluded.avg_lots",
        (trade_date, status, replay["tier"], replay["direction"], expiry_code,
         len(trades), len(priced), unavailable, spot, gross, charges, net,
         STRATEGY_VERSION,
         json.dumps(replay.get("context"), sort_keys=True)
         if replay.get("context") else None,
         datetime.now(IST).isoformat(), replay.get("cpr_prev_date"), avg_lots))
    if commit:
        conn.commit()


def run_day(trade_date: str | None = None, override: dict | None = None, *,
            persist: bool = True, require_all_quotes: bool = False,
            connection: sqlite3.Connection | None = None,
            commit: bool = True) -> dict:
    trade_date = trade_date or datetime.now(IST).date().isoformat()
    try:
        replay = replay_cpr(trade_date, override)
    except ReplayInputError as exc:
        raise AlphaCprInputError(str(exc)) from exc
    expiry_code = None
    trades = []
    if replay["segments"]:
        try:
            expiry_code, quotes = build_executable_book(trade_date)
        except AlphaV212InputError as exc:
            raise AlphaCprInputError(str(exc)) from exc
        trades = [_price_cpr_segment(s, expiry_code, quotes)
                  for s in replay["segments"]]
        if trades and not replay["session_done"] and trades[-1]["exit_reason"] == "EOD":
            trades[-1]["status"] = "open"
            trades[-1]["exit_reason"] = "holding"
    unavailable = [t for t in trades if t["quote_status"] != "priced"]
    if require_all_quotes and unavailable:
        detail = "; ".join(f"#{i + 1} {t['quote_status']}"
                           for i, t in enumerate(unavailable))
        raise AlphaCprInputError(
            f"Alpha-CPR pricing incomplete for {trade_date}: {detail}; "
            "existing rows retained")
    if persist:
        conn = connection or get_conn()
        if connection is None or commit:
            _ensure_tables(conn)
        _save(conn, trade_date, replay, expiry_code, trades, commit=commit)
    priced = [t for t in trades if t["quote_status"] == "priced"]
    return {
        "trade_date": trade_date,
        "status": ("partial_unavailable" if trades and unavailable
                   else "open" if any(t["status"] == "open" for t in trades)
                   else "traded" if trades else "no_trade"),
        "n_segments": len(trades),
        "priced_segments": len(priced),
        "unavailable_segments": len(unavailable),
        "spot_pnl_pts": round(sum(t["spot_pnl_pts"] for t in trades), 2),
        "gross_rs": round(sum(t["gross_rs"] for t in priced), 2),
        "charges_rs": round(sum(t["charges_rs"] for t in priced), 2),
        "net_rs": round(sum(t["net_rs"] for t in priced), 2),
        "expiry_code": expiry_code,
        "cpr_prev_date": replay.get("cpr_prev_date"),
    }


if __name__ == "__main__":
    import sys
    print(run_day(sys.argv[1] if len(sys.argv) > 1 else None))


__all__ = [
    "AlphaCprInputError", "CPR_MIN_DIST", "MAX_LOTS", "RISK_BUDGET",
    "STRATEGY_VERSION", "_ensure_tables", "lots_for", "replay_cpr", "run_day",
]
