"""Alpha v2.14 A (formerly v2.12 B10): v2.12 with a 10-point stop buffer, honest fills.

Decisions -- the production champion replay, unchanged except for the
entry-spot overlay's stop barrier (shared constant V212_B10_EXIT_BUFFER, the
same one the live runner uses):
  CALL stop   a completed one-minute candle closes at/below anchor - 10
  PUT  stop   a completed one-minute candle closes at/above anchor + 10
  recovery    a later completed candle crosses back through the anchor itself
Entries, alpha exits, the PC400 40/20 trail and EOD are v2.11/v2.12 as-is.

Fills -- causal, unlike the other alpha books. The option snapshot labelled M is
taken at ~M:00 (its stored spot matches candle M's OPEN in 75-81% of minutes),
while a decision made on candle M is only knowable at M:59. Pricing that
decision at snapshot M books a fill from BEFORE the move that caused it, which
is where most of paper v2.12's booked profit came from. Here every
candle-decided event is priced at the first snapshot after its decision:

  entry-spot stops, recoveries, wall rejections   decided on minute M's close
                                                  -> priced at M + 1
  trail / spot / CPR / drift exits                evaluated across a 5-minute
                                                  window but stamped at its
                                                  start -> breach minute + 1
  alpha exits, signal entries, EOD                decision and price share one
                                                  snapshot -> unchanged

A live order decided at the :00 boundary and filled by :05 lands in that same
next-minute snapshot, so this ledger measures what live can actually achieve.

Window exits carry no minute timestamp, so their decision minute is taken as the
first minute in the window whose range reaches the exit level. For a trail that
can precede the moment it armed, erring slightly in the book's favour.

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
from labs.engine.paper_strategy_tracker import IST
from live.engine import champion_inputs
from live.engine.champion_sim import V212_B10_EXIT_BUFFER, V214_CHECK_ENTRY_BAR
from storage.db import get_conn


STRATEGY_VERSION = (
    "alpha_v2.14a_b10_close_buffer10_entrybar_itm200_bidask_causal_next_minute"
)

_ONE_MINUTE = pd.Timedelta(minutes=1)
# Decided on one completed minute's close.
_CLOSE_OF_MINUTE = {"ENTRY_SPOT_SL", "WALL_REJ"}
# Evaluated over a 5-minute window of one-minute bars, stamped at its start.
_WINDOW_EXITS = {
    "TRAIL", "SL_SPOT", "TP_SPOT", "CPR_SL", "CPR_TP", "v711_drift_stop",
}


class AlphaV212B10InputError(RuntimeError):
    """Required replay or executable-quote input is incomplete."""


def _ensure_tables(conn: sqlite3.Connection) -> None:
    conn.executescript(
        """
        CREATE TABLE IF NOT EXISTS alpha_v212b10_daily (
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
        CREATE TABLE IF NOT EXISTS alpha_v212b10_trades (
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


def _recovery_flags(segments: list[dict]) -> list[bool]:
    """True where a segment re-enters after an entry-spot stop on its anchor."""
    flags: list[bool] = []
    previous = None
    for segment in segments:
        flags.append(bool(
            previous is not None
            and previous.get("reason") == "ENTRY_SPOT_SL"
            and previous.get("pos") == segment.get("pos")
            and abs(float(previous["entry_spot"])
                    - float(segment["entry_spot"])) < 0.01
        ))
        previous = segment
    return flags


def _window_decision_minute(segment: dict, by_minute: dict):
    """First minute of the 5-minute window whose range reaches the exit level."""
    start = pd.Timestamp(segment["exit_ts"])
    level = float(segment["exit_spot"])
    for offset in range(5):
        minute = start + pd.Timedelta(minutes=offset)
        bar = by_minute.get(minute.strftime("%H:%M"))
        if bar is None:
            continue
        _, high, low, _ = bar
        if low - 0.01 <= level <= high + 0.01:
            return minute
    return None


def causal_fill_times(segments: list[dict], by_minute: dict) -> list[dict]:
    """Copies of `segments` with fills moved to the first causal snapshot."""
    out: list[dict] = []
    for segment, recovered in zip(segments, _recovery_flags(segments)):
        shifted = dict(segment)
        reason = shifted.get("reason")
        if reason in _CLOSE_OF_MINUTE:
            shifted["exit_ts"] = pd.Timestamp(shifted["exit_ts"]) + _ONE_MINUTE
        elif reason in _WINDOW_EXITS:
            decided = _window_decision_minute(shifted, by_minute)
            if decided is None:
                # Unlocatable: assume the latest minute the window allows.
                decided = pd.Timestamp(shifted["exit_ts"]) + 4 * _ONE_MINUTE
            shifted["exit_ts"] = decided + _ONE_MINUTE
        if recovered:
            shifted["entry_ts"] = pd.Timestamp(shifted["entry_ts"]) + _ONE_MINUTE
        out.append(shifted)
    return out


def replay_v212b10(trade_date: str, override: dict | None = None) -> dict:
    try:
        replay = replay_v212(
            trade_date,
            override,
            close_confirmed=True,
            exit_buffer=V212_B10_EXIT_BUFFER,
            check_entry_bar=V214_CHECK_ENTRY_BAR,
        )
    except AlphaV212InputError as exc:
        raise AlphaV212B10InputError(str(exc)) from exc
    context = dict(replay.get("context") or {})
    context.update(
        {
            "strategy_version": "Alpha v2.14 A",
            "stop_rule": "close_confirmed",
            "exit_buffer_pts": V212_B10_EXIT_BUFFER,
            "check_entry_bar": V214_CHECK_ENTRY_BAR,
            "fill_model": "causal_next_minute",
        }
    )
    return {**replay, "context": context}


def _status(trades: list[dict], unavailable: int) -> str:
    return (
        "partial_unavailable" if trades and unavailable
        else "open" if any(trade["status"] == "open" for trade in trades)
        else "traded" if trades
        else "no_trade"
    )


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
    conn.execute(
        "DELETE FROM alpha_v212b10_trades WHERE trade_date=?", (trade_date,))
    for seq, trade in enumerate(trades, 1):
        conn.execute(
            "INSERT INTO alpha_v212b10_trades "
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
        "INSERT INTO alpha_v212b10_daily "
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
    replay = replay_v212b10(trade_date, override)
    expiry_code = None
    trades: list[dict] = []
    if replay["segments"]:
        try:
            expiry_code, quotes = build_executable_book(trade_date)
        except AlphaV212InputError as exc:
            raise AlphaV212B10InputError(str(exc)) from exc
        causal = causal_fill_times(
            replay["segments"], champion_inputs.ohlc_by_minute(trade_date))
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
        raise AlphaV212B10InputError(
            f"Alpha v2.14 A pricing incomplete for {trade_date}: "
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
    "AlphaV212B10InputError",
    "STRATEGY_VERSION",
    "_ensure_tables",
    "causal_fill_times",
    "replay_v212b10",
    "run_day",
]
