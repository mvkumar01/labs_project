"""Alpha v2.14 D paper tracker: Alpha v2.14 B plus PC50 gap-up CALLs on a wider range.

v2.14 B keeps every PC50 CALL flat. v2.14 D keeps that for gap-DOWN days, but on
PC50 gap-UP days it takes CALL entries again -- decided by alpha computed over
round50(prev_close) - 50 .. + 100 instead of the locked +-50 range. Everything else
(puts, PC250/PC400, the B10 overlay, the entry-bar check, causal fills) is v2.14 B.

Why: in the 2026-09-27 PC50 range scan (research/experiments/2026-09-27_pc50_range_scan)
pc-50/+100 lifted gap-up calls by Rs8.2k (v2.14 A, Jun-Sep) while hurting gap-down
days on both sides. The older year (Run F, Jul 2025-May 2026) did NOT confirm it --
gap-up calls were the most-hurt bucket there -- so this is a paper watch book.

Calls and puts come from two replays of the same day, so they can overlap in time.
The book holds one position at a time: signal chains (a segment plus its overlay
re-entries) are taken in entry order and a chain that starts while another is still
open is skipped (counted in the context as skipped_overlap_chains).

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
from labs.engine.alpha_v212b10_tracker import _recovery_flags, _status, causal_fill_times
from labs.engine.paper_strategy_tracker import IST
from live.engine import champion_inputs
from labs.engine import entry_structure
from live.engine.champion_sim import V212_B10_EXIT_BUFFER, V214_CHECK_ENTRY_BAR
from storage.db import get_conn


STRATEGY_VERSION = (
    "alpha_v2.14d_b_plus_pc50_up_call_range_m50_p100_b10_close_buffer10_entrybar"
    "_itm200_bidask_causal_next_minute"
)
PC50_UP_CALL_RANGE = (-50, 100)
UP_CALL_RANGE_LABEL = "pc-50/+100"


class AlphaV214DInputError(RuntimeError):
    """Required replay or executable-quote input is incomplete."""


def _ensure_tables(conn: sqlite3.Connection) -> None:
    conn.executescript(
        """
        CREATE TABLE IF NOT EXISTS alpha_v214d_daily (
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
        CREATE TABLE IF NOT EXISTS alpha_v214d_trades (
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
            signal_range TEXT,
            PRIMARY KEY (trade_date, seq)
        );
        """
    )
    entry_structure.ensure_columns(conn, "alpha_v214d_trades")
    conn.commit()


def _chains(segments: list[dict]) -> list[list[dict]]:
    """Split a replay's segments into chains: a segment plus its overlay re-entries."""
    chains: list[list[dict]] = []
    for segment, recovered in zip(segments, _recovery_flags(segments)):
        if recovered and chains:
            chains[-1].append(segment)
        else:
            chains.append([segment])
    return chains


def merge_one_position(base: list[dict], calls: list[dict]) -> tuple[list[dict], int]:
    """Interleave two replays' chains, one position at a time, earliest entry first."""
    chains = sorted(_chains(base) + _chains(calls),
                    key=lambda c: pd.Timestamp(c[0]["entry_ts"]))
    merged: list[dict] = []
    skipped = 0
    open_until = None
    for chain in chains:
        start = pd.Timestamp(chain[0]["entry_ts"])
        if open_until is not None and start < open_until:
            skipped += 1
            continue
        merged.extend(chain)
        open_until = pd.Timestamp(chain[-1]["exit_ts"])
    return merged, skipped


def replay_v214d(trade_date: str, override: dict | None = None) -> dict:
    flags = dict(close_confirmed=True, exit_buffer=V212_B10_EXIT_BUFFER,
                 check_entry_bar=V214_CHECK_ENTRY_BAR)
    try:
        base = replay_v212(trade_date, override, suppress_pc50_call_entries=True, **flags)
        calls: list[dict] = []
        if base["tier"] == "PC50" and base.get("direction") == "UP":
            wide = replay_v212(trade_date, override, suppress_pc50_call_entries=False,
                               range_offsets=PC50_UP_CALL_RANGE, **flags)
            calls = [dict(s, signal_range=UP_CALL_RANGE_LABEL)
                     for s in wide["segments"] if s.get("pos") == "call"]
    except AlphaV212InputError as exc:
        raise AlphaV214DInputError(str(exc)) from exc
    context = dict(base.get("context") or {})
    locked = (f"{context.get('range_lower', '')}-{context.get('range_upper', '')}"
              if context else None)
    segments, skipped = merge_one_position(
        [dict(s, signal_range=locked) for s in base["segments"]], calls)
    context.update(
        {
            "strategy_version": "Alpha v2.14 D (v2.14 B + PC50 gap-up calls on pc-50/+100)",
            "decision_filter": "no_pc50_call_except_gap_up_wide_range",
            "pc50_up_call_range": UP_CALL_RANGE_LABEL,
            "pc50_up_call_segments": sum(1 for s in segments if s.get("pos") == "call"
                                         and s.get("signal_range") == UP_CALL_RANGE_LABEL),
            "skipped_overlap_chains": skipped,
            "stop_rule": "close_confirmed",
            "exit_buffer_pts": V212_B10_EXIT_BUFFER,
            "check_entry_bar": V214_CHECK_ENTRY_BAR,
            "fill_model": "causal_next_minute",
        }
    )
    return {**base, "segments": segments, "context": context}


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
    conn.execute("DELETE FROM alpha_v214d_trades WHERE trade_date=?", (trade_date,))
    for seq, trade in enumerate(trades, 1):
        conn.execute(
            "INSERT INTO alpha_v214d_trades "
            "(trade_date,seq,status,side,strike,expiry_code,tradingsymbol,entry_ts,"
            "exit_ts,entry_spot,exit_spot,spot_pnl_pts,entry_bid,entry_ask,exit_bid,"
            "exit_ask,option_pnl_pts,gross_rs,charges_rs,net_rs,quote_status,"
            "entry_rule,exit_reason,signal_range," + ",".join(entry_structure.FIELDS)
            + ") VALUES (" + ",".join(["?"] * (24 + len(entry_structure.FIELDS))) + ")",
            (
                trade_date, seq, trade["status"], trade["side"], trade["strike"],
                trade.get("expiry_code"), trade.get("tradingsymbol"),
                trade["entry_ts"], trade["exit_ts"], trade["entry_spot"],
                trade["exit_spot"], trade["spot_pnl_pts"], trade.get("entry_bid"),
                trade.get("entry_ask"), trade.get("exit_bid"), trade.get("exit_ask"),
                trade.get("option_pnl_pts"), trade.get("gross_rs"),
                trade.get("charges_rs"), trade.get("net_rs"),
                trade["quote_status"], trade.get("entry_rule"), trade["exit_reason"],
                trade.get("signal_range"),
                *[trade.get(name) for name in entry_structure.FIELDS],
            ),
        )
    spot = round(sum(float(trade["spot_pnl_pts"]) for trade in trades), 2)
    gross = round(sum(float(trade["gross_rs"]) for trade in priced), 2)
    charges = round(sum(float(trade["charges_rs"]) for trade in priced), 2)
    net = round(sum(float(trade["net_rs"]) for trade in priced), 2)
    conn.execute(
        "INSERT INTO alpha_v214d_daily "
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
    replay = replay_v214d(trade_date, override)
    expiry_code = None
    trades: list[dict] = []
    if replay["segments"]:
        try:
            expiry_code, quotes = build_executable_book(trade_date)
        except AlphaV212InputError as exc:
            raise AlphaV214DInputError(str(exc)) from exc
        causal = causal_fill_times(
            replay["segments"], champion_inputs.ohlc_by_minute(trade_date))
        trades = [
            {**_price_segment(segment, expiry_code, quotes),
             "signal_range": segment.get("signal_range")}
            for segment in causal
        ]
        # Observation only: market structure at each fresh Alpha entry.
        entry_structure.annotate(
            trades, replay["segments"], [not f for f in _recovery_flags(replay["segments"])],
            trade_date, replay.get("oi_maps"))
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
        raise AlphaV214DInputError(
            f"Alpha v2.14 D pricing incomplete for {trade_date}: "
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
    "AlphaV214DInputError",
    "PC50_UP_CALL_RANGE",
    "STRATEGY_VERSION",
    "_ensure_tables",
    "merge_one_position",
    "replay_v214d",
    "run_day",
]
