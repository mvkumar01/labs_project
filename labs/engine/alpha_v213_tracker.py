"""Alpha v2.13 paper tracker: v2.11 risk authority plus the v2.12 overlay.

Every segment buys one nearest-expiry NIFTY option 200 points ITM at the
executable ask and sells at the executable bid. This module is paper-only and
never places broker orders.
"""
from __future__ import annotations

import json
import sqlite3
from datetime import datetime

import pandas as pd

from labs.engine.alpha_v212_tracker import (
    AlphaV212InputError,
    _price_segment,
    _timestamp_key,
    build_executable_book,
)
from labs.engine.paper_strategy_tracker import (
    IST,
    ReplayInputError,
    _resolve_day,
    _resolve_replay_context,
    _session_over,
)
from live.engine import champion_inputs, champion_sim, champion_v213
from storage.db import get_conn


STRATEGY_VERSION = (
    "alpha_v2.13_v211_risk_authority_entry_spot_recovery_"
    "itm200_bidask_completed1m_nextmark"
)


class AlphaV213InputError(RuntimeError):
    """Required v2.13 replay or executable-quote input is incomplete."""


def _holding_segment(open_state: dict, adf, ohlc, trade_date: str) -> dict:
    """Create a current paper mark without closing the shadow v2.11 trade."""
    entry_ts = champion_sim._local_naive_timestamp(open_state["entry_ts"])
    max_bar_ts = champion_sim._local_naive_timestamp(
        adf.iloc[-1]["timestamp"]
    ) + pd.Timedelta(minutes=4)
    available = []
    for minute, values in ohlc.by_minute.items():
        if values is None or len(values) != 4:
            continue
        bar_ts = pd.Timestamp(f"{trade_date} {minute}")
        if bar_ts <= max_bar_ts:
            available.append((bar_ts, float(values[3])))
    if available:
        bar_ts, mark_spot = max(available, key=lambda item: item[0])
        mark_ts = bar_ts + pd.Timedelta(minutes=1)
    else:
        mark_ts = entry_ts
        mark_spot = float(open_state["entry_spot"])
    if mark_ts <= entry_ts:
        mark_ts = entry_ts
        mark_spot = float(open_state["entry_spot"])
    pos = str(open_state["side"]).lower()
    entry_spot = float(open_state["entry_spot"])
    pnl = mark_spot - entry_spot if pos == "call" else entry_spot - mark_spot
    return {
        "pnl": round(pnl, 2),
        "reason": "EOD",
        "pos": pos,
        "entry_ts": open_state["entry_ts"],
        "exit_ts": mark_ts,
        "entry_alpha": open_state.get("entry_alpha"),
        "exit_alpha": None,
        "entry_spot": entry_spot,
        "signal_entry_spot": float(
            open_state.get("signal_entry_spot", entry_spot)
        ),
        "exit_spot": mark_spot,
        "entry_rule": open_state.get("entry_rule"),
        "tier": open_state.get("tier"),
    }


def _ensure_tables(conn: sqlite3.Connection) -> None:
    conn.executescript(
        """
        CREATE TABLE IF NOT EXISTS alpha_v213_daily (
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
            updated_at TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS alpha_v213_trades (
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
            PRIMARY KEY (trade_date, seq)
        );
        """
    )
    conn.commit()


def replay_v213(trade_date: str, override: dict | None = None) -> dict:
    day = _resolve_day(trade_date, override)
    if day is None or day.get("bucket") == "SKIP":
        if day is None:
            raise AlphaV213InputError(
                f"No locked range state is available for {trade_date}"
            )
        return {
            "tier": "SKIP",
            "direction": day.get("direction"),
            "segments": [],
            "session_done": _session_over(trade_date),
        }

    tier = day["bucket"]
    ohlc, context, range_source, use_abs, provenance = (
        _resolve_replay_context(trade_date, day)
    )
    try:
        _, adf, ce_map, pe_map = champion_inputs.build_sim_inputs(
            trade_date,
            day["lower"],
            day["upper"],
            use_abs,
            range_source=range_source,
        )
    except Exception as exc:
        raise AlphaV213InputError(
            f"Unable to build v2.13 inputs for {trade_date}: "
            f"{type(exc).__name__}: {exc}"
        ) from exc
    if adf is None or adf.empty:
        raise AlphaV213InputError(f"v2.13 input frame is empty for {trade_date}")
    cutoff = champion_sim.exact_mark_cutoff(trade_date, datetime.now(IST))
    if len(adf) < 2:
        raise AlphaV213InputError(
            f"v2.13 input frame has only {len(adf)} completed bars for {trade_date}"
        )
    session_done = _session_over(trade_date)
    _, segments, open_state = champion_v213.simulate(
        adf,
        ce_map,
        pe_map,
        ohlc,
        trade_date,
        context.use_trail,
        context.sgap,
        tier,
        context.weekday,
        context.regime,
        day["lower"],
        day["upper"],
        close_eod=session_done,
        return_state=True,
        entries_until_ts=cutoff,
    )
    if not session_done and open_state is not None:
        segments.append(_holding_segment(open_state, adf, ohlc, trade_date))
    return {
        "tier": tier,
        "direction": context.direction,
        "segments": segments,
        "session_done": session_done,
        "context": provenance,
    }


def _save(
    conn: sqlite3.Connection,
    trade_date: str,
    replay: dict,
    expiry_code: str | None,
    trades: list[dict],
    *,
    commit: bool,
) -> None:
    priced = [trade for trade in trades if trade["quote_status"] == "priced"]
    unavailable = len(trades) - len(priced)
    status = (
        "partial_unavailable" if trades and unavailable
        else "open" if any(trade["status"] == "open" for trade in trades)
        else "traded" if trades
        else "no_trade"
    )
    conn.execute("DELETE FROM alpha_v213_trades WHERE trade_date=?", (trade_date,))
    for seq, trade in enumerate(trades, 1):
        conn.execute(
            "INSERT INTO alpha_v213_trades "
            "(trade_date,seq,status,side,strike,expiry_code,tradingsymbol,entry_ts,"
            "exit_ts,entry_spot,exit_spot,spot_pnl_pts,entry_bid,entry_ask,exit_bid,"
            "exit_ask,option_pnl_pts,gross_rs,charges_rs,net_rs,quote_status,"
            "entry_rule,exit_reason) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                trade_date,
                seq,
                trade["status"],
                trade["side"],
                trade["strike"],
                trade.get("expiry_code"),
                trade.get("tradingsymbol"),
                trade["entry_ts"],
                trade["exit_ts"],
                trade["entry_spot"],
                trade["exit_spot"],
                trade["spot_pnl_pts"],
                trade.get("entry_bid"),
                trade.get("entry_ask"),
                trade.get("exit_bid"),
                trade.get("exit_ask"),
                trade.get("option_pnl_pts"),
                trade.get("gross_rs"),
                trade.get("charges_rs"),
                trade.get("net_rs"),
                trade["quote_status"],
                trade.get("entry_rule"),
                trade["exit_reason"],
            ),
        )
    spot = round(sum(float(trade["spot_pnl_pts"]) for trade in trades), 2)
    gross = round(sum(float(trade["gross_rs"]) for trade in priced), 2)
    charges = round(sum(float(trade["charges_rs"]) for trade in priced), 2)
    net = round(sum(float(trade["net_rs"]) for trade in priced), 2)
    now = datetime.now(IST).isoformat()
    conn.execute(
        "INSERT INTO alpha_v213_daily "
        "(trade_date,status,tier,gap_dir,expiry_code,n_segments,priced_segments,"
        "unavailable_segments,spot_pnl_pts,gross_rs,charges_rs,net_rs,"
        "strategy_version,context_json,updated_at) "
        "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?) "
        "ON CONFLICT(trade_date) DO UPDATE SET status=excluded.status,"
        "tier=excluded.tier,gap_dir=excluded.gap_dir,expiry_code=excluded.expiry_code,"
        "n_segments=excluded.n_segments,priced_segments=excluded.priced_segments,"
        "unavailable_segments=excluded.unavailable_segments,"
        "spot_pnl_pts=excluded.spot_pnl_pts,gross_rs=excluded.gross_rs,"
        "charges_rs=excluded.charges_rs,net_rs=excluded.net_rs,"
        "strategy_version=excluded.strategy_version,"
        "context_json=excluded.context_json,updated_at=excluded.updated_at",
        (
            trade_date,
            status,
            replay["tier"],
            replay["direction"],
            expiry_code,
            len(trades),
            len(priced),
            unavailable,
            spot,
            gross,
            charges,
            net,
            STRATEGY_VERSION,
            json.dumps(replay.get("context"), sort_keys=True)
            if replay.get("context")
            else None,
            now,
        ),
    )
    if commit:
        conn.commit()


def run_day(
    trade_date: str | None = None,
    override: dict | None = None,
    *,
    persist: bool = True,
    require_all_quotes: bool = False,
    connection: sqlite3.Connection | None = None,
    commit: bool = True,
) -> dict:
    trade_date = trade_date or datetime.now(IST).date().isoformat()
    try:
        replay = replay_v213(trade_date, override)
    except ReplayInputError as exc:
        raise AlphaV213InputError(str(exc)) from exc
    expiry_code = None
    trades = []
    if replay["segments"]:
        try:
            expiry_code, quotes = build_executable_book(trade_date)
        except AlphaV212InputError as exc:
            raise AlphaV213InputError(str(exc)) from exc
        trades = [
            _price_segment(segment, expiry_code, quotes)
            for segment in replay["segments"]
        ]
        if (
            trades
            and not replay["session_done"]
            and trades[-1]["exit_reason"] == "EOD"
        ):
            trades[-1]["status"] = "open"
            trades[-1]["exit_reason"] = "holding"
    unavailable = [
        trade for trade in trades if trade["quote_status"] != "priced"
    ]
    if require_all_quotes and unavailable:
        detail = "; ".join(
            f"#{index + 1} {trade['quote_status']}"
            for index, trade in enumerate(unavailable)
        )
        raise AlphaV213InputError(
            f"Alpha v2.13 pricing incomplete for {trade_date}: {detail}; "
            "existing rows retained"
        )
    if persist:
        conn = connection or get_conn()
        if connection is None or commit:
            _ensure_tables(conn)
        _save(conn, trade_date, replay, expiry_code, trades, commit=commit)
    priced = [trade for trade in trades if trade["quote_status"] == "priced"]
    return {
        "trade_date": trade_date,
        "status": (
            "partial_unavailable" if trades and unavailable
            else "open" if any(trade["status"] == "open" for trade in trades)
            else "traded" if trades
            else "no_trade"
        ),
        "n_segments": len(trades),
        "priced_segments": len(priced),
        "unavailable_segments": len(unavailable),
        "spot_pnl_pts": round(sum(trade["spot_pnl_pts"] for trade in trades), 2),
        "gross_rs": round(sum(trade["gross_rs"] for trade in priced), 2),
        "charges_rs": round(sum(trade["charges_rs"] for trade in priced), 2),
        "net_rs": round(sum(trade["net_rs"] for trade in priced), 2),
        "expiry_code": expiry_code,
    }


if __name__ == "__main__":
    import sys

    print(run_day(sys.argv[1] if len(sys.argv) > 1 else None))


__all__ = [
    "AlphaV213InputError",
    "STRATEGY_VERSION",
    "_ensure_tables",
    "_timestamp_key",
    "build_executable_book",
    "replay_v213",
    "run_day",
]
