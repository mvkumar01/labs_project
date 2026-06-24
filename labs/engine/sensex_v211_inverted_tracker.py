"""Paper book: NIFTY Alpha v2.11 signals with inverted SENSEX option side.

Signal timestamps and exits are identical to the canonical v2.11 replay.
Only execution side is swapped: CALL -> PUT and PUT -> CALL. Execution remains
nearest-expiry ATM SENSEX, ask in / bid out, with no LTP fallback.
"""
from __future__ import annotations

import sqlite3
from datetime import datetime

from labs.engine.paper_strategy_tracker import IST, ReplayInputError, replay_champion_signals
from labs.engine.sensex_v211_tracker import (
    SensexV211InputError,
    _price_signal,
    build_sensex_book,
)
from storage.db import get_conn


STRATEGY_VERSION = "nifty_alpha_v2.11_sensex_atm_inverted_v1"


def _ensure_tables(conn: sqlite3.Connection) -> None:
    conn.executescript(
        """
        CREATE TABLE IF NOT EXISTS sensex_v211_inverted_daily (
            trade_date TEXT PRIMARY KEY, status TEXT NOT NULL, tier TEXT,
            gap_dir TEXT, expiry_code TEXT, n_trades INTEGER NOT NULL,
            option_gross_rs REAL NOT NULL, option_priced_trades INTEGER NOT NULL,
            option_unavailable_trades INTEGER NOT NULL,
            strategy_version TEXT NOT NULL, updated_at TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS sensex_v211_inverted_trades (
            trade_date TEXT NOT NULL, seq INTEGER NOT NULL, status TEXT NOT NULL,
            side TEXT NOT NULL, original_side TEXT NOT NULL, strike INTEGER NOT NULL,
            tradingsymbol TEXT, expiry_code TEXT NOT NULL, entry_ts TEXT NOT NULL,
            exit_ts TEXT NOT NULL, entry_sensex REAL, exit_sensex REAL,
            entry_bid REAL, entry_ask REAL, exit_bid REAL, exit_ask REAL,
            option_pnl_pts REAL, option_gross_rs REAL, quote_status TEXT NOT NULL,
            entry_rule TEXT, exit_reason TEXT NOT NULL,
            PRIMARY KEY (trade_date, seq)
        );
        """
    )
    conn.commit()


def _save(conn, trade_date, replay, expiry_code, trades, *, commit: bool) -> None:
    now = datetime.now(IST).isoformat()
    priced = [trade for trade in trades if trade["quote_status"] == "priced"]
    unavailable = len(trades) - len(priced)
    status = (
        "open" if trades and trades[-1]["status"] == "open"
        else "partial_unavailable" if trades and unavailable
        else "traded" if trades else "no_trade"
    )
    conn.execute(
        "DELETE FROM sensex_v211_inverted_trades WHERE trade_date=?", (trade_date,)
    )
    for seq, trade in enumerate(trades, 1):
        conn.execute(
            "INSERT INTO sensex_v211_inverted_trades "
            "(trade_date,seq,status,side,original_side,strike,tradingsymbol,expiry_code,"
            "entry_ts,exit_ts,entry_sensex,exit_sensex,entry_bid,entry_ask,exit_bid,"
            "exit_ask,option_pnl_pts,option_gross_rs,quote_status,entry_rule,exit_reason) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                trade_date, seq, trade["status"], trade["side"],
                trade["original_side"], trade["strike"], trade.get("tradingsymbol"),
                trade["expiry_code"], trade["entry_ts"], trade["exit_ts"],
                trade.get("entry_sensex"), trade.get("exit_sensex"),
                trade.get("entry_bid"), trade.get("entry_ask"), trade.get("exit_bid"),
                trade.get("exit_ask"), trade.get("option_pnl_pts"),
                trade.get("option_gross_rs"), trade["quote_status"],
                trade.get("entry_rule"), trade["exit_reason"],
            ),
        )
    gross = round(sum(float(trade["option_gross_rs"]) for trade in priced), 2)
    conn.execute(
        "INSERT INTO sensex_v211_inverted_daily "
        "(trade_date,status,tier,gap_dir,expiry_code,n_trades,option_gross_rs,"
        "option_priced_trades,option_unavailable_trades,strategy_version,updated_at) "
        "VALUES (?,?,?,?,?,?,?,?,?,?,?) ON CONFLICT(trade_date) DO UPDATE SET "
        "status=excluded.status,tier=excluded.tier,gap_dir=excluded.gap_dir,"
        "expiry_code=excluded.expiry_code,n_trades=excluded.n_trades,"
        "option_gross_rs=excluded.option_gross_rs,"
        "option_priced_trades=excluded.option_priced_trades,"
        "option_unavailable_trades=excluded.option_unavailable_trades,"
        "strategy_version=excluded.strategy_version,updated_at=excluded.updated_at",
        (
            trade_date, status, replay["tier"], replay["direction"], expiry_code,
            len(trades), gross, len(priced), unavailable, STRATEGY_VERSION, now,
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
        replay = replay_champion_signals(trade_date, override)
    except ReplayInputError as exc:
        raise SensexV211InputError(str(exc)) from exc
    signals = replay["sim_trades"]
    expiry_code = None
    trades = []
    if signals:
        expiry_code, quotes, spot_by_mark = build_sensex_book(trade_date)
        for signal in signals:
            original_side = "CALL" if signal["pos"] == "call" else "PUT"
            inverted = dict(signal)
            inverted["pos"] = "put" if signal["pos"] == "call" else "call"
            trade = _price_signal(
                inverted, expiry_code, quotes, spot_by_mark, replay["session_done"]
            )
            trade["original_side"] = original_side
            trades.append(trade)
    unavailable = [trade for trade in trades if trade["quote_status"] != "priced"]
    if require_all_quotes and unavailable:
        detail = "; ".join(
            f"#{index + 1} {trade['quote_status']}"
            for index, trade in enumerate(unavailable)
        )
        raise SensexV211InputError(
            f"Inverted SENSEX v2.11 pricing incomplete for {trade_date}: {detail}; "
            "existing rows retained"
        )
    if persist:
        conn = connection or get_conn()
        if connection is None or commit:
            _ensure_tables(conn)
        _save(conn, trade_date, replay, expiry_code, trades, commit=commit)
    priced = [trade for trade in trades if trade["quote_status"] == "priced"]
    status = (
        "open" if trades and trades[-1]["status"] == "open"
        else "partial_unavailable" if unavailable
        else "traded" if trades else "no_trade"
    )
    return {
        "trade_date": trade_date,
        "status": status,
        "n_trades": len(trades),
        "option_gross_rs": round(
            sum(float(trade["option_gross_rs"]) for trade in priced), 2
        ),
        "option_priced_trades": len(priced),
        "option_unavailable_trades": len(unavailable),
        "expiry_code": expiry_code,
    }


if __name__ == "__main__":
    import sys
    print(run_day(sys.argv[1] if len(sys.argv) > 1 else None))
