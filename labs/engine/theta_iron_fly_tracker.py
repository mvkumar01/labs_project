"""Paper-only NIFTY ATM iron fly with defined wings and profit target."""
from __future__ import annotations

from datetime import date, datetime
import math
import sqlite3

import pandas as pd

from config.labs_config import UNDERLYINGS
from labs.engine.charges import round_trip_charges, short_option_round_trip_charges
from labs.engine.theta_straddle_tracker import (
    IST,
    ThetaStraddleInputError,
    _normalise_frame,
    _positive,
    _timestamp,
)
from storage.db import get_conn


SYMBOL = "NIFTY"
LOT_SIZE = int(UNDERLYINGS[SYMBOL]["lot_size"])
STRIKE_STEP = int(UNDERLYINGS[SYMBOL]["strike_step"])
LOTS = 1
QTY = LOT_SIZE * LOTS
WING_WIDTH = 400
ENTRY_TIME = "09:20"
ENTRY_WINDOW_MINUTES = 5
EXIT_TIME = "15:00"
EXIT_WINDOW_MINUTES = 5
PROFIT_TARGET_PCT = 0.20
MARGIN_METHOD = "defined_max_loss_wing_width_less_net_credit"
STRATEGY_VERSION = "nifty_atm_iron_fly_400w_20pct_0920_1500_v1"


class ThetaIronFlyInputError(RuntimeError):
    """Required market data or executable four-leg quotes are unavailable."""


def _ensure_tables(conn: sqlite3.Connection) -> None:
    conn.executescript(
        """
        CREATE TABLE IF NOT EXISTS theta_iron_fly_daily (
            trade_date TEXT PRIMARY KEY,
            status TEXT NOT NULL,
            expiry_code TEXT,
            atm_strike INTEGER,
            lower_wing_strike INTEGER,
            upper_wing_strike INTEGER,
            entry_ts TEXT,
            exit_ts TEXT,
            entry_spot REAL,
            lot_size INTEGER NOT NULL,
            lots INTEGER NOT NULL,
            qty INTEGER NOT NULL,
            n_legs INTEGER NOT NULL,
            priced_legs INTEGER NOT NULL,
            capital_required_rs REAL,
            net_credit_rs REAL,
            target_rs REAL,
            gross_rs REAL NOT NULL,
            charges_rs REAL NOT NULL,
            net_rs REAL NOT NULL,
            return_on_capital_pct REAL,
            exit_reason TEXT,
            margin_method TEXT NOT NULL,
            strategy_version TEXT NOT NULL,
            error TEXT,
            updated_at TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS theta_iron_fly_trades (
            trade_date TEXT NOT NULL,
            leg TEXT NOT NULL,
            position_side TEXT NOT NULL,
            option_type TEXT NOT NULL,
            tradingsymbol TEXT,
            expiry_code TEXT NOT NULL,
            strike INTEGER NOT NULL,
            entry_ts TEXT NOT NULL,
            exit_ts TEXT,
            entry_price REAL NOT NULL,
            exit_price REAL,
            qty INTEGER NOT NULL,
            premium_cashflow_rs REAL NOT NULL,
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
    conn.commit()


def _leg_specs(atm_strike: int) -> tuple[dict, ...]:
    return (
        {"leg": "SHORT_CALL", "position_side": "SHORT", "option_type": "ce", "strike": atm_strike},
        {"leg": "SHORT_PUT", "position_side": "SHORT", "option_type": "pe", "strike": atm_strike},
        {"leg": "LONG_CALL_WING", "position_side": "LONG", "option_type": "ce", "strike": atm_strike + WING_WIDTH},
        {"leg": "LONG_PUT_WING", "position_side": "LONG", "option_type": "pe", "strike": atm_strike - WING_WIDTH},
    )


def _quotes_at(frame: pd.DataFrame, timestamp: pd.Timestamp, specs: tuple[dict, ...]) -> dict | None:
    minute = frame[frame["timestamp"] == timestamp]
    quotes = {}
    for spec in specs:
        rows = minute[
            (minute["strike"] == spec["strike"])
            & (minute["type"] == spec["option_type"])
        ]
        if rows.empty:
            return None
        row = rows.iloc[-1]
        bid, ask = _positive(row.get("bid")), _positive(row.get("ask"))
        if bid is None or ask is None:
            return None
        quotes[spec["leg"]] = {
            **spec,
            "bid": bid,
            "ask": ask,
            "tradingsymbol": row.get("tradingsymbol"),
        }
    return quotes


def _entry_snapshot(frame: pd.DataFrame, trade_date: str) -> tuple[pd.Timestamp, float, int, dict]:
    start = _timestamp(trade_date, ENTRY_TIME)
    end = start + pd.Timedelta(minutes=ENTRY_WINDOW_MINUTES)
    candidates = frame[(frame["timestamp"] >= start) & (frame["timestamp"] <= end)]
    for timestamp in candidates["timestamp"].drop_duplicates().sort_values():
        minute = candidates[candidates["timestamp"] == timestamp]
        spot = _positive(minute["spot"].median())
        if spot is None:
            continue
        atm = int(math.floor(spot / STRIKE_STEP + 0.5) * STRIKE_STEP)
        quotes = _quotes_at(frame, timestamp, _leg_specs(atm))
        if quotes is not None:
            return timestamp, spot, atm, quotes
    raise ThetaIronFlyInputError(
        "No common executable four-leg quote from 09:20 to 09:25"
    )


def _price_mark(entry_quotes: dict, exit_quotes: dict) -> tuple[list[dict], float, float, float]:
    legs = []
    for leg_name, entry in entry_quotes.items():
        exit_quote = exit_quotes[leg_name]
        if entry["position_side"] == "SHORT":
            entry_price, exit_price = float(entry["bid"]), float(exit_quote["ask"])
            gross = (entry_price - exit_price) * QTY
            charges = short_option_round_trip_charges(
                entry_price, exit_price, QTY
            )["raw_total"]
            premium_cashflow = entry_price * QTY
        else:
            entry_price, exit_price = float(entry["ask"]), float(exit_quote["bid"])
            gross = (exit_price - entry_price) * QTY
            charges = round_trip_charges(entry_price, exit_price, QTY)["raw_total"]
            premium_cashflow = -entry_price * QTY
        legs.append({
            **entry,
            "entry_price": entry_price,
            "exit_price": exit_price,
            "premium_cashflow_rs": premium_cashflow,
            "gross_rs": gross,
            "charges_rs": float(charges),
            "net_rs": gross - float(charges),
        })
    gross = sum(leg["gross_rs"] for leg in legs)
    charges = sum(leg["charges_rs"] for leg in legs)
    return legs, gross, charges, gross - charges


def _exit_snapshot(
    frame: pd.DataFrame,
    trade_date: str,
    entry_ts: pd.Timestamp,
    entry_quotes: dict,
    target_rs: float,
    *,
    require_close: bool,
) -> tuple[pd.Timestamp, list[dict], float, float, float, bool, str | None]:
    target_time = _timestamp(trade_date, EXIT_TIME)
    end = target_time + pd.Timedelta(minutes=EXIT_WINDOW_MINUTES)
    latest = None
    specs = tuple(
        {
            "leg": quote["leg"],
            "position_side": quote["position_side"],
            "option_type": quote["option_type"],
            "strike": quote["strike"],
        }
        for quote in entry_quotes.values()
    )
    timestamps = frame[
        (frame["timestamp"] > entry_ts) & (frame["timestamp"] <= end)
    ]["timestamp"].drop_duplicates().sort_values()
    for timestamp in timestamps:
        quotes = _quotes_at(frame, timestamp, specs)
        if quotes is None:
            continue
        priced = _price_mark(entry_quotes, quotes)
        latest = (timestamp, *priced)
        if priced[3] >= target_rs:
            return timestamp, *priced, True, "PROFIT_TARGET_20PCT"
        if timestamp >= target_time:
            return timestamp, *priced, True, "TIME_1500"
    if require_close:
        raise ThetaIronFlyInputError(
            "No profit target and no common four-leg exit quote from 15:00 to 15:05"
        )
    if latest is None:
        raise ThetaIronFlyInputError("No common four-leg mark after entry")
    return *latest, False, None


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
        raise ThetaIronFlyInputError(f"{trade_date} is not a trading weekday")
    try:
        frame, expiry_code = _normalise_frame(trade_date)
    except ThetaStraddleInputError as exc:
        raise ThetaIronFlyInputError(str(exc)) from exc
    if frame["timestamp"].max() < _timestamp(trade_date, ENTRY_TIME):
        return {"trade_date": trade_date, "status": "waiting", "n_legs": 0}
    entry_ts, entry_spot, atm, entry_quotes = _entry_snapshot(frame, trade_date)
    short_credit = sum(
        quote["bid"] for quote in entry_quotes.values()
        if quote["position_side"] == "SHORT"
    )
    long_debit = sum(
        quote["ask"] for quote in entry_quotes.values()
        if quote["position_side"] == "LONG"
    )
    net_credit_points = short_credit - long_debit
    if net_credit_points <= 0:
        raise ThetaIronFlyInputError("Four-leg entry does not produce a positive net credit")
    net_credit_rs = net_credit_points * QTY
    target_rs = net_credit_rs * PROFIT_TARGET_PCT
    exit_ts, legs, gross, charges, net, closed, exit_reason = _exit_snapshot(
        frame,
        trade_date,
        entry_ts,
        entry_quotes,
        target_rs,
        require_close=require_close,
    )
    capital = (WING_WIDTH - net_credit_points) * QTY
    if capital <= 0:
        raise ThetaIronFlyInputError(
            "Opening net credit must be smaller than the protective-wing width"
        )
    status = "closed" if closed else "open"
    result = {
        "trade_date": trade_date,
        "status": status,
        "expiry_code": expiry_code,
        "atm_strike": atm,
        "lower_wing_strike": atm - WING_WIDTH,
        "upper_wing_strike": atm + WING_WIDTH,
        "entry_ts": entry_ts.isoformat(),
        "exit_ts": exit_ts.isoformat(),
        "entry_spot": round(entry_spot, 2),
        "lot_size": LOT_SIZE,
        "lots": LOTS,
        "qty": QTY,
        "n_legs": 4,
        "priced_legs": 4,
        "capital_required_rs": capital,
        "net_credit_rs": net_credit_rs,
        "target_rs": target_rs,
        "gross_rs": gross,
        "charges_rs": charges,
        "net_rs": net,
        "return_on_capital_pct": round(100 * net / capital, 4) if capital else None,
        "exit_reason": exit_reason,
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
        conn.execute("DELETE FROM theta_iron_fly_trades WHERE trade_date=?", (trade_date,))
        for leg in legs:
            conn.execute(
                "INSERT INTO theta_iron_fly_trades "
                "(trade_date,leg,position_side,option_type,tradingsymbol,expiry_code," 
                "strike,entry_ts,exit_ts,entry_price,exit_price,qty,premium_cashflow_rs," 
                "allocated_capital_rs,gross_rs,charges_rs,net_rs,status,exit_reason) "
                "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (
                    trade_date, leg["leg"], leg["position_side"], leg["option_type"],
                    leg.get("tradingsymbol"), expiry_code, leg["strike"],
                    result["entry_ts"], result["exit_ts"], leg["entry_price"],
                    leg["exit_price"], QTY, leg["premium_cashflow_rs"], capital / 4,
                    leg["gross_rs"], leg["charges_rs"], leg["net_rs"], status,
                    exit_reason,
                ),
            )
        conn.execute(
            "INSERT INTO theta_iron_fly_daily "
            "(trade_date,status,expiry_code,atm_strike,lower_wing_strike," 
            "upper_wing_strike,entry_ts,exit_ts,entry_spot,lot_size,lots,qty,n_legs," 
            "priced_legs,capital_required_rs,net_credit_rs,target_rs,gross_rs," 
            "charges_rs,net_rs,return_on_capital_pct,exit_reason,margin_method," 
            "strategy_version,updated_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?) "
            "ON CONFLICT(trade_date) DO UPDATE SET status=excluded.status," 
            "expiry_code=excluded.expiry_code,atm_strike=excluded.atm_strike," 
            "lower_wing_strike=excluded.lower_wing_strike," 
            "upper_wing_strike=excluded.upper_wing_strike,entry_ts=excluded.entry_ts," 
            "exit_ts=excluded.exit_ts,entry_spot=excluded.entry_spot," 
            "capital_required_rs=excluded.capital_required_rs," 
            "net_credit_rs=excluded.net_credit_rs,target_rs=excluded.target_rs," 
            "gross_rs=excluded.gross_rs,charges_rs=excluded.charges_rs," 
            "net_rs=excluded.net_rs,return_on_capital_pct=excluded.return_on_capital_pct," 
            "exit_reason=excluded.exit_reason,margin_method=excluded.margin_method," 
            "strategy_version=excluded.strategy_version,error=NULL,updated_at=excluded.updated_at",
            (
                trade_date, status, expiry_code, atm, atm - WING_WIDTH,
                atm + WING_WIDTH, result["entry_ts"], result["exit_ts"],
                result["entry_spot"], LOT_SIZE, LOTS, QTY, 4, 4, capital,
                net_credit_rs, target_rs, gross, charges, net,
                result["return_on_capital_pct"], exit_reason, MARGIN_METHOD,
                STRATEGY_VERSION, datetime.now(IST).isoformat(),
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
    own = connection is None
    conn = connection or get_conn()
    _ensure_tables(conn)
    try:
        conn.execute("DELETE FROM theta_iron_fly_trades WHERE trade_date=?", (trade_date,))
        conn.execute(
            "INSERT INTO theta_iron_fly_daily "
            "(trade_date,status,lot_size,lots,qty,n_legs,priced_legs,gross_rs," 
            "charges_rs,net_rs,margin_method,strategy_version,error,updated_at) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?) "
            "ON CONFLICT(trade_date) DO UPDATE SET status=excluded.status," 
            "expiry_code=NULL,atm_strike=NULL,lower_wing_strike=NULL," 
            "upper_wing_strike=NULL,entry_ts=NULL,exit_ts=NULL,entry_spot=NULL," 
            "n_legs=0,priced_legs=0,capital_required_rs=NULL,net_credit_rs=0," 
            "target_rs=0,gross_rs=0,charges_rs=0,net_rs=0," 
            "return_on_capital_pct=NULL,exit_reason=NULL," 
            "margin_method=excluded.margin_method,strategy_version=excluded.strategy_version," 
            "error=excluded.error,updated_at=excluded.updated_at",
            (
                trade_date, "unavailable", LOT_SIZE, LOTS, QTY, 0, 0, 0, 0, 0,
                MARGIN_METHOD, STRATEGY_VERSION, str(error)[:500],
                datetime.now(IST).isoformat(),
            ),
        )
        conn.commit()
    finally:
        if own:
            conn.close()
