"""Alpha v2.14 C paper tracker: Alpha v2.14 B with a Renko 30 overlay. Paper only.

Identical to v2.14 B (v2.11 replay (B) entry filter, entry-bar check) except the
entry-spot overlay: instead of the 10-point anchor barrier it reads a Renko chart of the
day's 1-minute closes -- 30-point bricks, classic two-brick reversal. A new brick against
the position exits it at that minute's close; a new brick back in its direction re-enters
at the current close. Research: 59-day causal replay (matrix_renko.py) Rs46.9k vs v2.14 B
Rs42.8k, with a deeper drawdown (-Rs9.1k vs -Rs6.6k) -- a watch book, not a live candidate.

Notes carried over from v2.14 B:

Two proven pieces, nothing new:
  * v2.11 replay (B)'s entry filter -- PC50 CALL decisions stay flat; PC50 PUT
    and every PC250/PC400 decision are unchanged.
  * Alpha v2.12 B10's overlay -- the entry-spot stop needs a completed
    one-minute close V212_B10_EXIT_BUFFER points past the anchor, recovery is at
    the anchor itself.

Fills are causal, exactly as in the B10 book (see alpha_v212b10_tracker):
candle-decided events price at the first snapshot after their decision.

Paper only; this module never places broker orders.
"""
from __future__ import annotations

import json
import sqlite3
from datetime import datetime

import pandas as pd

from labs.engine.alpha_v212_tracker import (
    AlphaV212InputError,
    _price_segment,
    build_executable_book,
    replay_v212,
)
from labs.engine.alpha_v212b10_tracker import _status, causal_fill_times
from labs.engine.paper_strategy_tracker import IST
from live.engine import champion_inputs
from live.engine.champion_sim import (
    V212_B10_EXIT_BUFFER, V214_CHECK_ENTRY_BAR, V214C_RENKO_BRICK, V214C_RENKO_REVERSAL,
)
from storage.db import get_conn


STRATEGY_VERSION = (
    "alpha_v2.14c_no_pc50_call_renko30_r2_entrybar_itm200_bidask_causal_next_minute"
)


class AlphaV214CInputError(RuntimeError):
    """Required replay or executable-quote input is incomplete."""


def _ensure_tables(conn: sqlite3.Connection) -> None:
    conn.executescript(
        """
        CREATE TABLE IF NOT EXISTS alpha_v214c_daily (
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
        CREATE TABLE IF NOT EXISTS alpha_v214c_trades (
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


def replay_v214c(trade_date: str, override: dict | None = None,
                 recovery_trace: list | None = None) -> dict:
    try:
        replay = replay_v212(
            trade_date,
            override,
            close_confirmed=True,
            exit_buffer=V212_B10_EXIT_BUFFER,
            suppress_pc50_call_entries=True,
            check_entry_bar=V214_CHECK_ENTRY_BAR,
            renko_brick=V214C_RENKO_BRICK,
            renko_reversal=V214C_RENKO_REVERSAL,
            recovery_trace=recovery_trace,
        )
    except AlphaV212InputError as exc:
        raise AlphaV214CInputError(str(exc)) from exc
    context = dict(replay.get("context") or {})
    context.update(
        {
            "strategy_version": "Alpha v2.14 C (v2.14 B + Renko 30 classic)",
            "decision_filter": "no_pc50_call",
            "pc50_call_entries_allowed": False,
            "stop_rule": "close_confirmed",
            "overlay": "renko",
            "renko_brick_pts": V214C_RENKO_BRICK,
            "renko_reversal_bricks": V214C_RENKO_REVERSAL,
            "check_entry_bar": V214_CHECK_ENTRY_BAR,
            "fill_model": "causal_next_minute",
        }
    )
    return {**replay, "context": context}


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
    conn.execute("DELETE FROM alpha_v214c_trades WHERE trade_date=?", (trade_date,))
    for seq, trade in enumerate(trades, 1):
        conn.execute(
            "INSERT INTO alpha_v214c_trades "
            "(trade_date,seq,status,side,strike,expiry_code,tradingsymbol,entry_ts,"
            "exit_ts,entry_spot,exit_spot,spot_pnl_pts,entry_bid,entry_ask,exit_bid,"
            "exit_ask,option_pnl_pts,gross_rs,charges_rs,net_rs,quote_status,"
            "entry_rule,exit_reason) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                trade_date, seq, trade["status"], trade["side"], trade["strike"],
                trade.get("expiry_code"), trade.get("tradingsymbol"),
                trade["entry_ts"], trade["exit_ts"], trade["entry_spot"],
                trade["exit_spot"], trade["spot_pnl_pts"], trade.get("entry_bid"),
                trade.get("entry_ask"), trade.get("exit_bid"), trade.get("exit_ask"),
                trade.get("option_pnl_pts"), trade.get("gross_rs"),
                trade.get("charges_rs"), trade.get("net_rs"),
                trade["quote_status"], trade.get("entry_rule"), trade["exit_reason"],
            ),
        )
    spot = round(sum(float(trade["spot_pnl_pts"]) for trade in trades), 2)
    gross = round(sum(float(trade["gross_rs"]) for trade in priced), 2)
    charges = round(sum(float(trade["charges_rs"]) for trade in priced), 2)
    net = round(sum(float(trade["net_rs"]) for trade in priced), 2)
    conn.execute(
        "INSERT INTO alpha_v214c_daily "
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
        "strategy_version=excluded.strategy_version,context_json=excluded.context_json,"
        "updated_at=excluded.updated_at",
        (
            trade_date, _status(trades, unavailable), replay["tier"],
            replay["direction"], expiry_code, len(trades), len(priced),
            unavailable, spot, gross, charges, net, STRATEGY_VERSION,
            json.dumps(replay.get("context"), sort_keys=True, default=str),
            datetime.now(IST).isoformat(),
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
    trace: list = []
    replay = replay_v214c(trade_date, override, trace)
    expiry_code = None
    trades: list[dict] = []
    if replay["segments"]:
        try:
            expiry_code, quotes = build_executable_book(trade_date)
        except AlphaV212InputError as exc:
            raise AlphaV214CInputError(str(exc)) from exc
        # Renko re-enters at a new price, so re-entries are the minutes the sim traced.
        reentries = {pd.Timestamp(ts) for ts in trace}
        causal = causal_fill_times(
            replay["segments"], champion_inputs.ohlc_by_minute(trade_date),
            recovered=[pd.Timestamp(s["entry_ts"]) in reentries for s in replay["segments"]])
        trades = [
            _price_segment(segment, expiry_code, quotes) for segment in causal
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
        raise AlphaV214CInputError(
            f"Alpha v2.14 C pricing incomplete for {trade_date}: "
            f"{detail}; existing rows retained"
        )
    if persist:
        conn = connection or get_conn()
        if connection is None or commit:
            _ensure_tables(conn)
        _save(conn, trade_date, replay, expiry_code, trades, commit=commit)
    priced = [trade for trade in trades if trade["quote_status"] == "priced"]
    return {
        "trade_date": trade_date,
        "status": _status(trades, len(unavailable)),
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
    "AlphaV214CInputError",
    "STRATEGY_VERSION",
    "_ensure_tables",
    "replay_v214c",
    "run_day",
]
