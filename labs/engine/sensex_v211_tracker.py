"""Paper book: NIFTY Alpha v2.11 signals executed in SENSEX options.

Only the contract layer changes. Signal side and exact entry/exit timestamps
come from ``paper_strategy_tracker.replay_champion_signals``. Each trade buys
one nearest-expiry ATM SENSEX option at the exact-mark ask and sells the same
contract at the exact-mark bid. LTP is never an execution fallback.
"""
from __future__ import annotations

import math
import sqlite3
from datetime import datetime

import pandas as pd

from config.labs_config import SHARED_ARCHIVE_DIR, SHARED_LIVE_DIR, UNDERLYINGS
from labs.engine.paper_strategy_tracker import (
    IST,
    ReplayInputError,
    replay_champion_signals,
)
from market_data.expiry import select_expiry_code
from market_data.shared_store import load_options_frame
from storage.db import get_conn


SYMBOL = "SENSEX"
LOT_SIZE = int(UNDERLYINGS[SYMBOL]["lot_size"])
STRIKE_STEP = int(UNDERLYINGS[SYMBOL]["strike_step"])
STRATEGY_VERSION = "nifty_alpha_v2.11_sensex_atm_v1"


class SensexV211InputError(RuntimeError):
    """Required signal or SENSEX execution input is incomplete."""


def _ensure_tables(conn: sqlite3.Connection) -> None:
    conn.executescript(
        """
        CREATE TABLE IF NOT EXISTS sensex_v211_daily (
            trade_date               TEXT PRIMARY KEY,
            status                   TEXT NOT NULL,
            tier                     TEXT,
            gap_dir                  TEXT,
            expiry_code              TEXT,
            n_trades                 INTEGER NOT NULL,
            option_gross_rs          REAL NOT NULL,
            option_priced_trades     INTEGER NOT NULL,
            option_unavailable_trades INTEGER NOT NULL,
            strategy_version         TEXT NOT NULL,
            updated_at               TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS sensex_v211_trades (
            trade_date       TEXT NOT NULL,
            seq              INTEGER NOT NULL,
            status           TEXT NOT NULL,
            side             TEXT NOT NULL,
            strike           INTEGER NOT NULL,
            tradingsymbol    TEXT,
            expiry_code      TEXT NOT NULL,
            entry_ts         TEXT NOT NULL,
            exit_ts          TEXT NOT NULL,
            entry_sensex     REAL,
            exit_sensex      REAL,
            entry_bid        REAL,
            entry_ask        REAL,
            exit_bid         REAL,
            exit_ask         REAL,
            option_pnl_pts   REAL,
            option_gross_rs  REAL,
            quote_status     TEXT NOT NULL,
            entry_rule       TEXT,
            exit_reason      TEXT NOT NULL,
            PRIMARY KEY (trade_date, seq)
        );
        """
    )
    conn.commit()


def _normalise_timestamp(frame: pd.DataFrame) -> pd.DataFrame:
    frame = frame.copy()
    frame["timestamp"] = pd.to_datetime(frame["timestamp"], errors="coerce")
    frame = frame.dropna(subset=["timestamp"])
    if frame["timestamp"].dt.tz is None:
        frame["timestamp"] = frame["timestamp"].dt.tz_localize(IST)
    else:
        frame["timestamp"] = frame["timestamp"].dt.tz_convert(IST)
    return frame


def _finite_positive(value) -> float | None:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if math.isfinite(number) and number > 0 else None


def build_sensex_book(trade_date: str) -> tuple[str, dict, dict]:
    """Return nearest-expiry exact-mark quotes and SENSEX spot by mark."""
    try:
        frame = load_options_frame(
            SYMBOL,
            trade_date,
            live_root=SHARED_LIVE_DIR,
            archive_root=SHARED_ARCHIVE_DIR,
        )
    except Exception as exc:
        raise SensexV211InputError(
            f"Unable to load SENSEX execution data for {trade_date}: "
            f"{type(exc).__name__}: {exc}"
        ) from exc
    required = {
        "timestamp", "spot", "strike", "option_type", "expiry", "bid", "ask"
    }
    missing = required.difference(frame.columns)
    if missing:
        raise SensexV211InputError(
            f"SENSEX execution data missing columns: {sorted(missing)}"
        )
    frame = _normalise_timestamp(frame)
    frame["type"] = frame["option_type"].astype(str).str.lower()
    for column in ("spot", "strike", "bid", "ask"):
        frame[column] = pd.to_numeric(frame[column], errors="coerce")
    frame = frame.dropna(subset=["spot", "strike"])
    frame["expiry"] = frame["expiry"].astype(str).str.upper()
    expiry_code = select_expiry_code(
        frame["expiry"].dropna().unique(), trade_date, "nearest_weekly"
    )
    if expiry_code is None:
        raise SensexV211InputError(f"No nearest SENSEX expiry for {trade_date}")
    exact = frame[
        (frame["expiry"] == str(expiry_code))
        & (frame["timestamp"].dt.minute % 5 == 0)
        & (frame["timestamp"].dt.second == 0)
    ].copy()
    if exact.empty:
        raise SensexV211InputError(f"No exact five-minute SENSEX quotes for {trade_date}")
    exact = (
        exact.sort_values("timestamp")
        .groupby(["timestamp", "strike", "type"], as_index=False)
        .last()
    )
    quotes: dict = {}
    spot_by_mark: dict = {}
    for row in exact.itertuples(index=False):
        mark = pd.Timestamp(row.timestamp).isoformat()
        spot_by_mark[mark] = float(row.spot)
        quotes[(mark, int(row.strike), str(row.type))] = {
            "bid": _finite_positive(row.bid),
            "ask": _finite_positive(row.ask),
            "tradingsymbol": getattr(row, "tradingsymbol", None),
        }
    return str(expiry_code), quotes, spot_by_mark


def _price_signal(
    signal: dict, expiry_code: str, quotes: dict, spot_by_mark: dict,
    session_done: bool,
) -> dict:
    side = "CALL" if signal["pos"] == "call" else "PUT"
    option_type = "ce" if side == "CALL" else "pe"
    entry_ts = pd.Timestamp(signal["entry_ts"]).isoformat()
    exit_ts = pd.Timestamp(signal["exit_ts"]).isoformat()
    entry_sensex = spot_by_mark.get(entry_ts)
    exit_sensex = spot_by_mark.get(exit_ts)
    if entry_sensex is None:
        raise SensexV211InputError(f"SENSEX spot missing at signal entry {entry_ts}")
    strike = int(round(float(entry_sensex) / STRIKE_STEP) * STRIKE_STEP)
    entry = quotes.get((entry_ts, strike, option_type), {})
    exit_ = quotes.get((exit_ts, strike, option_type), {})
    entry_bid, entry_ask = entry.get("bid"), entry.get("ask")
    exit_bid, exit_ask = exit_.get("bid"), exit_.get("ask")
    quote_status = "priced"
    option_points = option_rupees = None
    if entry_ask is None:
        quote_status = "entry_ask_unavailable"
    elif entry_bid is not None and entry_ask < entry_bid:
        quote_status = "entry_market_crossed"
    elif exit_bid is None:
        quote_status = "exit_bid_unavailable"
    elif exit_ask is not None and exit_ask < exit_bid:
        quote_status = "exit_market_crossed"
    else:
        option_points = round(exit_bid - entry_ask, 2)
        option_rupees = round(option_points * LOT_SIZE, 2)

    exit_reason = str(signal.get("reason") or "unknown")
    status = "closed"
    if exit_reason == "EOD":
        if session_done:
            exit_reason = "eod"
        else:
            exit_reason = "holding"
            status = "open"
    return {
        "status": status,
        "side": side,
        "strike": strike,
        "tradingsymbol": entry.get("tradingsymbol"),
        "expiry_code": expiry_code,
        "entry_ts": entry_ts,
        "exit_ts": exit_ts,
        "entry_sensex": entry_sensex,
        "exit_sensex": exit_sensex,
        "entry_bid": entry_bid,
        "entry_ask": entry_ask,
        "exit_bid": exit_bid,
        "exit_ask": exit_ask,
        "option_pnl_pts": option_points,
        "option_gross_rs": option_rupees,
        "quote_status": quote_status,
        "entry_rule": signal.get("entry_rule"),
        "exit_reason": exit_reason,
    }


def _save(
    conn: sqlite3.Connection, trade_date: str, replay: dict,
    expiry_code: str | None, trades: list[dict], *, commit: bool,
) -> None:
    now = datetime.now(IST).isoformat()
    priced = [trade for trade in trades if trade["quote_status"] == "priced"]
    unavailable = len(trades) - len(priced)
    if trades and trades[-1]["status"] == "open":
        status = "open"
    elif trades and unavailable:
        status = "partial_unavailable"
    elif trades:
        status = "traded"
    else:
        status = "no_trade"
    conn.execute("DELETE FROM sensex_v211_trades WHERE trade_date=?", (trade_date,))
    for seq, trade in enumerate(trades, 1):
        conn.execute(
            "INSERT INTO sensex_v211_trades "
            "(trade_date,seq,status,side,strike,tradingsymbol,expiry_code,entry_ts,"
            "exit_ts,entry_sensex,exit_sensex,entry_bid,entry_ask,exit_bid,exit_ask,"
            "option_pnl_pts,option_gross_rs,quote_status,entry_rule,exit_reason) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                trade_date, seq, trade["status"], trade["side"], trade["strike"],
                trade.get("tradingsymbol"), trade["expiry_code"], trade["entry_ts"],
                trade["exit_ts"], trade.get("entry_sensex"), trade.get("exit_sensex"),
                trade.get("entry_bid"), trade.get("entry_ask"), trade.get("exit_bid"),
                trade.get("exit_ask"), trade.get("option_pnl_pts"),
                trade.get("option_gross_rs"), trade["quote_status"],
                trade.get("entry_rule"), trade["exit_reason"],
            ),
        )
    gross = round(sum(float(trade["option_gross_rs"]) for trade in priced), 2)
    conn.execute(
        "INSERT INTO sensex_v211_daily "
        "(trade_date,status,tier,gap_dir,expiry_code,n_trades,option_gross_rs,"
        "option_priced_trades,option_unavailable_trades,strategy_version,updated_at) "
        "VALUES (?,?,?,?,?,?,?,?,?,?,?) "
        "ON CONFLICT(trade_date) DO UPDATE SET status=excluded.status,tier=excluded.tier,"
        "gap_dir=excluded.gap_dir,expiry_code=excluded.expiry_code,"
        "n_trades=excluded.n_trades,option_gross_rs=excluded.option_gross_rs,"
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
    trades: list[dict] = []
    if signals:
        expiry_code, quotes, spot_by_mark = build_sensex_book(trade_date)
        trades = [
            _price_signal(
                signal, expiry_code, quotes, spot_by_mark, replay["session_done"]
            )
            for signal in signals
        ]
    unavailable = [trade for trade in trades if trade["quote_status"] != "priced"]
    if require_all_quotes and unavailable:
        details = "; ".join(
            f"#{index + 1} {trade['quote_status']}"
            for index, trade in enumerate(unavailable)
        )
        raise SensexV211InputError(
            f"SENSEX v2.11 pricing incomplete for {trade_date}: {details}; "
            "existing rows retained"
        )
    if persist:
        conn = connection or get_conn()
        if connection is None or commit:
            _ensure_tables(conn)
        _save(conn, trade_date, replay, expiry_code, trades, commit=commit)
    priced = [trade for trade in trades if trade["quote_status"] == "priced"]
    status = "open" if trades and trades[-1]["status"] == "open" else (
        "partial_unavailable" if unavailable else ("traded" if trades else "no_trade")
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
