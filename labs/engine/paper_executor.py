"""
Paper trading executor.
Writes fills to SQLite — no real orders are sent.
"""
import json
import uuid
from datetime import datetime
from typing import Any

import pytz

from storage.db import get_conn
from collector.instruments import build_option_symbols
from config.labs_config import UNDERLYINGS

IST = pytz.timezone("Asia/Kolkata")


def _now_ist() -> str:
    return datetime.now(IST).strftime("%Y-%m-%d %H:%M:%S")


def _resolve_contract(kite, bot: dict, params: dict, signal_side: str, spot: float) -> dict | None:
    """
    Determines the option symbol to trade based on bot contract selection params.
    Returns a dict with keys: symbol, strike, expiry, lot_size.
    Returns None if no suitable contract found.
    """
    from collector.instruments import build_option_symbols, _round_strike
    cfg        = UNDERLYINGS[bot["underlying"]]
    step       = cfg["strike_step"]
    lot_size   = cfg["lot_size"]
    strike_mode = params.get("strike_mode", "atm")
    offset_pts  = int(params.get("strike_offset_pts", 0))

    if strike_mode == "atm":
        strike = _round_strike(spot, step)
    elif strike_mode == "itm_N":
        # ITM = lower strike for CE, higher strike for PE
        adj = -offset_pts if signal_side == "CE" else offset_pts
        strike = _round_strike(spot + adj, step)
    else:  # otm_N
        adj = offset_pts if signal_side == "CE" else -offset_pts
        strike = _round_strike(spot + adj, step)

    # Find the matching symbol from instruments list
    symbols = build_option_symbols(kite, bot["underlying"], spot)
    opt_type = signal_side  # CE or PE
    matched = [s for s in symbols if str(strike) in s and s.endswith(opt_type)]
    if not matched:
        return None

    # Pick shortest symbol (nearest expiry)
    symbol = sorted(matched, key=len)[0]

    # Extract expiry from symbol
    suffix   = symbol[len(bot["underlying"]):]
    expiry_s = suffix[:5]  # YYMON

    return {"symbol": symbol, "strike": strike, "expiry": expiry_s, "lot_size": lot_size}


def open_position(
    kite,
    bot: dict,
    params: dict[str, Any],
    signal_side: str,
    bar_close_time: str,
    spot: float,
    signal_value: float | None,
    options_df,
    conn=None,
) -> dict | None:
    """
    Resolves contract, fetches LTP from options_df, inserts position row.
    Returns the new position dict or None on failure.
    """
    contract = _resolve_contract(kite, bot, params, signal_side, spot)
    if contract is None:
        return None

    # Get LTP from latest options snapshot
    from labs.engine.data_loader import latest_ltp
    ltp = latest_ltp(options_df, contract["symbol"])
    if ltp is None or ltp == 0:
        return None

    trade_date  = datetime.now(IST).strftime("%Y-%m-%d")
    position_id = str(uuid.uuid4())
    now_s       = _now_ist()

    position = {
        "position_id": position_id,
        "bot_id":      bot["bot_id"],
        "trade_date":  trade_date,
        "underlying":  bot["underlying"],
        "side":        signal_side,
        "symbol":      contract["symbol"],
        "strike":      contract["strike"],
        "expiry":      contract["expiry"],
        "entry_time":  now_s,
        "entry_ltp":   ltp,
        "entry_spot":  spot,
        "lot_size":    contract["lot_size"],
        "qty":         1,
        "status":      "open",
    }

    close_conn = conn is None
    if conn is None:
        conn = get_conn()
    try:
        with conn:
            conn.execute("""
                INSERT INTO positions VALUES (
                    :position_id, :bot_id, :trade_date, :underlying, :side,
                    :symbol, :strike, :expiry, :entry_time, :entry_ltp,
                    :entry_spot, :lot_size, :qty, :status
                )
            """, position)

            # Log signal
            conn.execute("""
                INSERT INTO signals (signal_id, bot_id, ts, underlying, bar_close,
                    rsi, signal_type, acted)
                VALUES (?, ?, ?, ?, ?, ?, ?, 1)
            """, (str(uuid.uuid4()), bot["bot_id"], bar_close_time,
                  bot["underlying"], spot, signal_value, signal_side))
    finally:
        if close_conn:
            conn.close()

    return position


def close_position(
    position: dict,
    exit_ltp: float,
    exit_spot: float,
    exit_reason: str,
    conn=None,
) -> dict:
    """
    Computes P&L, writes to trades table, removes from positions.
    Returns the completed trade dict.
    """
    now_s      = _now_ist()
    entry_time = datetime.strptime(position["entry_time"], "%Y-%m-%d %H:%M:%S")
    exit_time  = datetime.strptime(now_s, "%Y-%m-%d %H:%M:%S")
    hold_mins  = max(int((exit_time - entry_time).total_seconds() / 60), 0)

    # P&L — long buy: profit = exit LTP - entry LTP
    B       = float(position["entry_ltp"])
    S       = exit_ltp
    Q       = int(position["lot_size"]) * int(position["qty"])
    pnl_pts = S - B
    pnl_rs  = pnl_pts * Q

    # Charges: flat ₹40 + STT + transaction tax
    # Turnover = (B + S) × Q
    turnover = (B + S) * Q
    charges  = round(40 + 0.00053 * turnover + 0.001 * (S * Q), 2)
    net_pnl_rs = round(pnl_rs - charges, 2)

    trade = {
        "trade_id":        str(uuid.uuid4()),
        "bot_id":          position["bot_id"],
        "trade_date":      position["trade_date"],
        "underlying":      position["underlying"],
        "side":            position["side"],
        "symbol":          position["symbol"],
        "strike":          position["strike"],
        "expiry":          position["expiry"],
        "entry_time":      position["entry_time"],
        "exit_time":       now_s,
        "entry_ltp":       float(position["entry_ltp"]),
        "exit_ltp":        exit_ltp,
        "entry_spot":      float(position["entry_spot"]),
        "exit_spot":       exit_spot,
        "lot_size":        int(position["lot_size"]),
        "qty":             int(position["qty"]),
        "pnl_pts":         round(pnl_pts, 2),
        "pnl_rs":          round(pnl_rs, 2),
        "charges":         charges,
        "net_pnl_rs":      net_pnl_rs,
        "exit_reason":     exit_reason,
        "holding_mins":    hold_mins,
        "signal_at_entry": None,
        "created_at":      now_s,
    }

    close_conn = conn is None
    if conn is None:
        conn = get_conn()
    try:
        with conn:
            conn.execute("""
                INSERT INTO trades VALUES (
                    :trade_id, :bot_id, :trade_date, :underlying, :side,
                    :symbol, :strike, :expiry, :entry_time, :exit_time,
                    :entry_ltp, :exit_ltp, :entry_spot, :exit_spot,
                    :lot_size, :qty, :pnl_pts, :pnl_rs, :charges, :net_pnl_rs,
                    :exit_reason, :holding_mins, :signal_at_entry, :created_at
                )
            """, trade)
            conn.execute(
                "DELETE FROM positions WHERE position_id = ?",
                (position["position_id"],)
            )
    finally:
        if close_conn:
            conn.close()

    return trade
