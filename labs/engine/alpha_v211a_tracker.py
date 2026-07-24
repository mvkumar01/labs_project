"""Alpha v2.11A (Champion 2) paper tracker.

The signal and risk stack is Alpha v2.11 except for one isolated cell:
PC400 + gap-DOWN + PUT when the resolved opening VIX is present and below 17.
That cell uses a NIFTY spot trail armed after 30 favourable points and exits on
a 20-point retrace from the best spot. All other cells retain v2.11 behavior.

Every segment buys one nearest-expiry NIFTY option 200 points ITM at the first
executable ask at/after the event and sells at the first executable bid. Paper
only; this module never places broker orders.
"""
from __future__ import annotations

import json
import sqlite3
from datetime import datetime

from labs.engine.alpha_v212_tracker import (
    AlphaV212InputError,
    _price_segment,
    build_executable_book,
)
from labs.engine.paper_strategy_tracker import (
    IST,
    ReplayInputError,
    replay_champion_signals,
)
from storage.db import get_conn


STRATEGY_VERSION = (
    "alpha_v2.11a_champion2_lowvix_pc400_dn_put_"
    "spottrail_arm30_retrace20_itm200_bidask"
)


class AlphaV211AInputError(RuntimeError):
    """Required replay or executable-quote input is incomplete."""


def _ensure_tables(conn: sqlite3.Connection) -> None:
    conn.executescript(
        """
        CREATE TABLE IF NOT EXISTS alpha_v211a_daily (
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
        CREATE TABLE IF NOT EXISTS alpha_v211a_trades (
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


def replay_v211a(trade_date: str, override: dict | None = None) -> dict:
    try:
        replay = replay_champion_signals(
            trade_date,
            override,
            enable_v211a=True,
        )
    except ReplayInputError as exc:
        raise AlphaV211AInputError(str(exc)) from exc
    context = dict(replay.get("context") or {})
    context.update(
        {
            "strategy_version": "v2.11A",
            "champion_label": "Champion 2",
            "pc400_dn_put_low_vix_trail": bool(
                replay.get("v211a_low_vix_trail_enabled")
            ),
            "trail_arm_points": 30,
            "trail_retrace_points": 20,
        }
    )
    return {
        "tier": replay["tier"],
        "direction": replay["direction"],
        "segments": replay["sim_trades"],
        "session_done": replay["session_done"],
        "context": context,
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
    conn.execute("DELETE FROM alpha_v211a_trades WHERE trade_date=?", (trade_date,))
    for seq, trade in enumerate(trades, 1):
        conn.execute(
            "INSERT INTO alpha_v211a_trades "
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
        "INSERT INTO alpha_v211a_daily "
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
            json.dumps(replay.get("context"), sort_keys=True),
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
    replay = replay_v211a(trade_date, override)
    expiry_code = None
    trades: list[dict] = []
    if replay["segments"]:
        try:
            expiry_code, quotes = build_executable_book(trade_date)
        except AlphaV212InputError as exc:
            raise AlphaV211AInputError(str(exc)) from exc
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
        raise AlphaV211AInputError(
            f"Alpha v2.11A pricing incomplete for {trade_date}: {detail}; "
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
    "AlphaV211AInputError",
    "STRATEGY_VERSION",
    "_ensure_tables",
    "replay_v211a",
    "run_day",
]
