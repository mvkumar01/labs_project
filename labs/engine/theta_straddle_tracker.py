"""Paper-only NIFTY ATM short straddle: sell 09:20, cover 15:15."""
from __future__ import annotations

from datetime import date, datetime, timedelta, timezone
import math
import sqlite3

import pandas as pd

from config.labs_config import SHARED_ARCHIVE_DIR, SHARED_LIVE_DIR, UNDERLYINGS
from labs.engine.charges import short_option_round_trip_charges
from market_data.expiry import select_expiry_code
from market_data.shared_store import load_options_frame
from storage.db import get_conn


IST = timezone(timedelta(hours=5, minutes=30))
SYMBOL = "NIFTY"
LOT_SIZE = int(UNDERLYINGS[SYMBOL]["lot_size"])
STRIKE_STEP = int(UNDERLYINGS[SYMBOL]["strike_step"])
LOTS = 1
QTY = LOT_SIZE * LOTS
ENTRY_TIME = "09:20"
EXIT_TIME = "15:15"
QUOTE_WINDOW_MINUTES = 5
ESTIMATED_MARGIN_RATE = 0.10
MARGIN_METHOD = "estimated_10pct_index_notional_after_straddle_offset"
STRATEGY_VERSION = "nifty_atm_short_straddle_0920_1515_v1"


class ThetaStraddleInputError(RuntimeError):
    """Required market data or executable quotes are unavailable."""


def _ensure_tables(conn: sqlite3.Connection) -> None:
    conn.executescript(
        """
        CREATE TABLE IF NOT EXISTS theta_straddle_daily (
            trade_date TEXT PRIMARY KEY,
            status TEXT NOT NULL,
            expiry_code TEXT,
            strike INTEGER,
            entry_ts TEXT,
            exit_ts TEXT,
            entry_spot REAL,
            lot_size INTEGER NOT NULL,
            lots INTEGER NOT NULL,
            qty INTEGER NOT NULL,
            n_legs INTEGER NOT NULL,
            priced_legs INTEGER NOT NULL,
            capital_required_rs REAL,
            premium_credit_rs REAL,
            gross_rs REAL NOT NULL,
            charges_rs REAL NOT NULL,
            net_rs REAL NOT NULL,
            return_on_capital_pct REAL,
            margin_method TEXT NOT NULL,
            strategy_version TEXT NOT NULL,
            error TEXT,
            updated_at TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS theta_straddle_trades (
            trade_date TEXT NOT NULL,
            leg TEXT NOT NULL,
            option_type TEXT NOT NULL,
            tradingsymbol TEXT,
            expiry_code TEXT NOT NULL,
            strike INTEGER NOT NULL,
            entry_ts TEXT NOT NULL,
            exit_ts TEXT,
            entry_sell_bid REAL NOT NULL,
            exit_buy_ask REAL,
            qty INTEGER NOT NULL,
            premium_credit_rs REAL NOT NULL,
            allocated_capital_rs REAL NOT NULL,
            gross_rs REAL,
            charges_rs REAL,
            net_rs REAL,
            status TEXT NOT NULL,
            exit_reason TEXT,
            PRIMARY KEY (trade_date, leg)
        );
        """
    )
    daily_columns = {
        row[1] for row in conn.execute("PRAGMA table_info(theta_straddle_daily)")
    }
    if "error" not in daily_columns:
        conn.execute("ALTER TABLE theta_straddle_daily ADD COLUMN error TEXT")
    conn.commit()


def _positive(value) -> float | None:
    try:
        value = float(value)
    except (TypeError, ValueError):
        return None
    return value if math.isfinite(value) and value > 0 else None


def _timestamp(trade_date: str, hhmm: str) -> pd.Timestamp:
    return pd.Timestamp(f"{trade_date} {hhmm}", tz="Asia/Kolkata")


def _normalise_frame(trade_date: str) -> tuple[pd.DataFrame, str]:
    try:
        frame = load_options_frame(
            SYMBOL,
            trade_date,
            live_root=SHARED_LIVE_DIR,
            archive_root=SHARED_ARCHIVE_DIR,
        )
    except Exception as exc:
        raise ThetaStraddleInputError(
            f"Unable to load NIFTY quotes for {trade_date}: {type(exc).__name__}: {exc}"
        ) from exc
    required = {"timestamp", "spot", "strike", "option_type", "expiry", "bid", "ask"}
    missing = required.difference(frame.columns)
    if missing:
        raise ThetaStraddleInputError(f"NIFTY quote data missing columns: {sorted(missing)}")
    frame = frame.copy()
    frame["timestamp"] = pd.to_datetime(frame["timestamp"], errors="coerce")
    frame = frame.dropna(subset=["timestamp"])
    if frame["timestamp"].dt.tz is None:
        frame["timestamp"] = frame["timestamp"].dt.tz_localize("Asia/Kolkata")
    else:
        frame["timestamp"] = frame["timestamp"].dt.tz_convert("Asia/Kolkata")
    frame["type"] = frame["option_type"].astype(str).str.lower().replace(
        {"call": "ce", "put": "pe"}
    )
    frame["expiry"] = frame["expiry"].astype(str).str.upper()
    for column in ("spot", "strike", "bid", "ask"):
        frame[column] = pd.to_numeric(frame[column], errors="coerce")
    frame = frame.dropna(subset=["spot", "strike"])
    expiry_code = select_expiry_code(frame["expiry"].unique(), trade_date, "nearest_weekly")
    if expiry_code is None:
        raise ThetaStraddleInputError(f"No nearest NIFTY expiry for {trade_date}")
    frame = frame[frame["expiry"] == str(expiry_code)].copy()
    if frame.empty:
        raise ThetaStraddleInputError(f"Selected expiry {expiry_code} has no rows")
    return frame.sort_values("timestamp"), str(expiry_code)


def _leg_quotes_at(frame: pd.DataFrame, timestamp: pd.Timestamp, strike: int) -> dict:
    rows = frame[(frame["timestamp"] == timestamp) & (frame["strike"] == strike)]
    result = {}
    for option_type in ("ce", "pe"):
        typed = rows[rows["type"] == option_type]
        if typed.empty:
            continue
        row = typed.iloc[-1]
        result[option_type] = {
            "bid": _positive(row.get("bid")),
            "ask": _positive(row.get("ask")),
            "tradingsymbol": row.get("tradingsymbol"),
        }
    return result


def _entry_snapshot(frame: pd.DataFrame, trade_date: str) -> tuple[pd.Timestamp, float, int, dict]:
    start = _timestamp(trade_date, ENTRY_TIME)
    end = start + pd.Timedelta(minutes=QUOTE_WINDOW_MINUTES)
    candidates = frame[(frame["timestamp"] >= start) & (frame["timestamp"] <= end)]
    for timestamp in candidates["timestamp"].drop_duplicates().sort_values():
        minute = candidates[candidates["timestamp"] == timestamp]
        spot = _positive(minute["spot"].median())
        if spot is None:
            continue
        strike = int(math.floor(spot / STRIKE_STEP + 0.5) * STRIKE_STEP)
        quotes = _leg_quotes_at(frame, timestamp, strike)
        if all(quotes.get(kind, {}).get("bid") is not None for kind in ("ce", "pe")):
            return timestamp, spot, strike, quotes
    raise ThetaStraddleInputError("No common executable CE/PE bid from 09:20 to 09:25")


def _exit_snapshot(
    frame: pd.DataFrame,
    trade_date: str,
    strike: int,
    entry_ts: pd.Timestamp,
    *,
    require_close: bool,
) -> tuple[pd.Timestamp, dict, bool]:
    target = _timestamp(trade_date, EXIT_TIME)
    end = target + pd.Timedelta(minutes=QUOTE_WINDOW_MINUTES)
    closed = frame[(frame["timestamp"] >= target) & (frame["timestamp"] <= end)]
    for timestamp in closed["timestamp"].drop_duplicates().sort_values():
        quotes = _leg_quotes_at(frame, timestamp, strike)
        if all(quotes.get(kind, {}).get("ask") is not None for kind in ("ce", "pe")):
            return timestamp, quotes, True
    if require_close:
        raise ThetaStraddleInputError("No common executable CE/PE ask from 15:15 to 15:20")
    marks = frame[
        (frame["timestamp"] >= entry_ts) & (frame["timestamp"] < target)
    ]
    for timestamp in reversed(list(marks["timestamp"].drop_duplicates().sort_values())):
        quotes = _leg_quotes_at(frame, timestamp, strike)
        if all(quotes.get(kind, {}).get("ask") is not None for kind in ("ce", "pe")):
            return timestamp, quotes, False
    raise ThetaStraddleInputError("No common CE/PE mark after entry")


def run_day(
    trade_date: str | None = None,
    *,
    require_close: bool = False,
    persist: bool = True,
    connection: sqlite3.Connection | None = None,
    commit: bool = True,
) -> dict:
    trade_date = trade_date or datetime.now(IST).date().isoformat()
    if date.fromisoformat(trade_date).weekday() >= 5:
        raise ThetaStraddleInputError(f"{trade_date} is not a trading weekday")
    frame, expiry_code = _normalise_frame(trade_date)
    if frame["timestamp"].max() < _timestamp(trade_date, ENTRY_TIME):
        return {"trade_date": trade_date, "status": "waiting", "n_legs": 0}
    entry_ts, entry_spot, strike, entry_quotes = _entry_snapshot(frame, trade_date)
    exit_ts, exit_quotes, closed = _exit_snapshot(
        frame, trade_date, strike, entry_ts, require_close=require_close
    )
    capital = round(entry_spot * QTY * ESTIMATED_MARGIN_RATE, 2)
    legs = []
    for leg, option_type in (("CALL", "ce"), ("PUT", "pe")):
        entry_bid = float(entry_quotes[option_type]["bid"])
        exit_ask = float(exit_quotes[option_type]["ask"])
        gross = round((entry_bid - exit_ask) * QTY, 2)
        charges = short_option_round_trip_charges(entry_bid, exit_ask, QTY)
        legs.append({
            "leg": leg,
            "option_type": option_type,
            "tradingsymbol": entry_quotes[option_type].get("tradingsymbol"),
            "entry_sell_bid": entry_bid,
            "exit_buy_ask": exit_ask,
            "premium_credit_rs": round(entry_bid * QTY, 2),
            "allocated_capital_rs": round(capital / 2, 2),
            "gross_rs": gross,
            "charges_rs": float(charges["raw_total"]),
            "net_rs": gross - float(charges["raw_total"]),
        })
    gross = round(sum(leg["gross_rs"] for leg in legs), 2)
    charges = sum(leg["charges_rs"] for leg in legs)
    net = gross - charges
    result = {
        "trade_date": trade_date,
        "status": "closed" if closed else "open",
        "expiry_code": expiry_code,
        "strike": strike,
        "entry_ts": entry_ts.isoformat(),
        "exit_ts": exit_ts.isoformat(),
        "entry_spot": round(entry_spot, 2),
        "lot_size": LOT_SIZE,
        "lots": LOTS,
        "qty": QTY,
        "n_legs": 2,
        "priced_legs": 2,
        "capital_required_rs": capital,
        "premium_credit_rs": round(sum(leg["premium_credit_rs"] for leg in legs), 2),
        "gross_rs": gross,
        "charges_rs": charges,
        "net_rs": net,
        "return_on_capital_pct": round(100 * net / capital, 4) if capital else None,
        "margin_method": MARGIN_METHOD,
        "strategy_version": STRATEGY_VERSION,
        "legs": legs,
    }
    if not persist:
        return result
    own = connection is None
    conn = connection or get_conn()
    _ensure_tables(conn)
    try:
        conn.execute("DELETE FROM theta_straddle_trades WHERE trade_date=?", (trade_date,))
        for leg in legs:
            conn.execute(
                "INSERT INTO theta_straddle_trades "
                "(trade_date,leg,option_type,tradingsymbol,expiry_code,strike,entry_ts,"
                "exit_ts,entry_sell_bid,exit_buy_ask,qty,premium_credit_rs,"
                "allocated_capital_rs,gross_rs,charges_rs,net_rs,status,exit_reason) "
                "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (
                    trade_date, leg["leg"], leg["option_type"], leg["tradingsymbol"],
                    expiry_code, strike, result["entry_ts"], result["exit_ts"],
                    leg["entry_sell_bid"], leg["exit_buy_ask"], QTY,
                    leg["premium_credit_rs"], leg["allocated_capital_rs"],
                    leg["gross_rs"], leg["charges_rs"], leg["net_rs"],
                    result["status"], "TIME_1515" if closed else None,
                ),
            )
        conn.execute(
            "INSERT INTO theta_straddle_daily "
            "(trade_date,status,expiry_code,strike,entry_ts,exit_ts,entry_spot,lot_size,"
            "lots,qty,n_legs,priced_legs,capital_required_rs,premium_credit_rs,gross_rs,"
            "charges_rs,net_rs,return_on_capital_pct,margin_method,strategy_version,updated_at) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?) "
            "ON CONFLICT(trade_date) DO UPDATE SET status=excluded.status,"
            "expiry_code=excluded.expiry_code,strike=excluded.strike,entry_ts=excluded.entry_ts,"
            "exit_ts=excluded.exit_ts,entry_spot=excluded.entry_spot,lot_size=excluded.lot_size,"
            "lots=excluded.lots,qty=excluded.qty,n_legs=excluded.n_legs,"
            "priced_legs=excluded.priced_legs,capital_required_rs=excluded.capital_required_rs,"
            "premium_credit_rs=excluded.premium_credit_rs,gross_rs=excluded.gross_rs,"
            "charges_rs=excluded.charges_rs,net_rs=excluded.net_rs,"
            "return_on_capital_pct=excluded.return_on_capital_pct,"
            "margin_method=excluded.margin_method,strategy_version=excluded.strategy_version,"
            "error=NULL,updated_at=excluded.updated_at",
            (
                trade_date, result["status"], expiry_code, strike, result["entry_ts"],
                result["exit_ts"], result["entry_spot"], LOT_SIZE, LOTS, QTY, 2, 2,
                capital, result["premium_credit_rs"], gross, charges, net,
                result["return_on_capital_pct"], MARGIN_METHOD, STRATEGY_VERSION,
                datetime.now(IST).isoformat(),
            ),
        )
        if commit:
            conn.commit()
    finally:
        if own:
            conn.close()
    return result


def record_unavailable(
    trade_date: str,
    error: str,
    *,
    connection: sqlite3.Connection | None = None,
) -> None:
    """Persist an auditable no-result day without inventing entry prices."""
    own = connection is None
    conn = connection or get_conn()
    _ensure_tables(conn)
    try:
        conn.execute("DELETE FROM theta_straddle_trades WHERE trade_date=?", (trade_date,))
        conn.execute(
            "INSERT INTO theta_straddle_daily "
            "(trade_date,status,expiry_code,strike,entry_ts,exit_ts,entry_spot,lot_size,"
            "lots,qty,n_legs,priced_legs,capital_required_rs,premium_credit_rs,gross_rs,"
            "charges_rs,net_rs,return_on_capital_pct,margin_method,strategy_version,error,"
            "updated_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?) "
            "ON CONFLICT(trade_date) DO UPDATE SET status=excluded.status,"
            "expiry_code=NULL,strike=NULL,entry_ts=NULL,exit_ts=NULL,entry_spot=NULL,"
            "n_legs=0,priced_legs=0,capital_required_rs=NULL,premium_credit_rs=0,"
            "gross_rs=0,charges_rs=0,net_rs=0,return_on_capital_pct=NULL,"
            "margin_method=excluded.margin_method,strategy_version=excluded.strategy_version,"
            "error=excluded.error,updated_at=excluded.updated_at",
            (
                trade_date, "unavailable", None, None, None, None, None, LOT_SIZE,
                LOTS, QTY, 0, 0, None, 0, 0, 0, 0, None, MARGIN_METHOD,
                STRATEGY_VERSION, str(error)[:500], datetime.now(IST).isoformat(),
            ),
        )
        conn.commit()
    finally:
        if own:
            conn.close()
