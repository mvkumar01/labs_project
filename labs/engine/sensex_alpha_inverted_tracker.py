"""SENSEX-own Alpha paper book with option execution side inverted.

Alpha calculation, entries, exits and timestamps come from Sensex_alpha.
Only the bought option is swapped: CALL -> PUT and PUT -> CALL.
"""
from __future__ import annotations

import sqlite3
from datetime import datetime

from labs.engine import sensex_alpha_tracker as base
from storage.db import get_conn


STRATEGY_VERSION = "sensex_own_alpha_abs_liquid_expiry_option_inverted_v2"


def _ensure_tables(conn: sqlite3.Connection) -> None:
    conn.executescript(
        """
        CREATE TABLE IF NOT EXISTS sensex_alpha_inverted_daily (
            trade_date TEXT PRIMARY KEY, status TEXT NOT NULL,
            prev_close REAL NOT NULL, range_lower REAL NOT NULL,
            range_upper REAL NOT NULL, latest_mark TEXT, latest_spot REAL,
            latest_alpha REAL, position_side TEXT, n_trades INTEGER NOT NULL,
            spot_pnl_pts REAL NOT NULL, option_gross_rs REAL,
            option_priced_trades INTEGER NOT NULL,
            option_unavailable_trades INTEGER NOT NULL, expiry_code TEXT,
            baseline_date TEXT, liquidity_mode TEXT, liquidity_mark TEXT,
            selected_expiry_type TEXT, weekly_expiry_code TEXT,
            monthly_expiry_code TEXT, weekly_in_band_oi REAL,
            monthly_in_band_oi REAL, strategy_version TEXT NOT NULL,
            updated_at TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS sensex_alpha_inverted_trades (
            trade_date TEXT NOT NULL, seq INTEGER NOT NULL, status TEXT NOT NULL,
            side TEXT NOT NULL, original_side TEXT NOT NULL, strike INTEGER NOT NULL,
            tradingsymbol TEXT, expiry_code TEXT, entry_ts TEXT NOT NULL,
            exit_ts TEXT NOT NULL, entry_alpha REAL NOT NULL, exit_alpha REAL NOT NULL,
            entry_spot REAL NOT NULL, exit_spot REAL NOT NULL, spot_pnl_pts REAL NOT NULL,
            entry_bid REAL, entry_ask REAL, exit_bid REAL, exit_ask REAL,
            option_pnl_pts REAL, option_gross_rs REAL, quote_status TEXT NOT NULL,
            entry_reason TEXT NOT NULL, exit_reason TEXT NOT NULL,
            PRIMARY KEY (trade_date, seq)
        );
        """
    )
    existing = {
        row[1]
        for row in conn.execute(
            "PRAGMA table_info(sensex_alpha_inverted_daily)"
        )
    }
    additions = {
        "liquidity_mode": "TEXT",
        "liquidity_mark": "TEXT",
        "selected_expiry_type": "TEXT",
        "weekly_expiry_code": "TEXT",
        "monthly_expiry_code": "TEXT",
        "weekly_in_band_oi": "REAL",
        "monthly_in_band_oi": "REAL",
    }
    for column, sql_type in additions.items():
        if column not in existing:
            conn.execute(
                f"ALTER TABLE sensex_alpha_inverted_daily "
                f"ADD COLUMN {column} {sql_type}"
            )
    conn.commit()


def _invert_trades(trades: list[dict], quotes: dict, lot_size: int) -> list[dict]:
    result = []
    for source in trades:
        trade = dict(source)
        trade["original_side"] = source["side"]
        trade["side"] = "PUT" if source["side"] == "CALL" else "CALL"
        trade["spot_pnl_pts"] = round(
            base._spot_pnl(trade["side"], trade["entry_spot"], trade["exit_spot"]), 2
        )
        result.append(base._price_trade(trade, quotes, lot_size))
    return result


def _save(conn, trade_date, context, bars, trades, config, *, commit: bool) -> None:
    now = datetime.now(base.IST).isoformat()
    status = "open" if trades and trades[-1]["status"] == "open" else (
        "traded" if trades else "no_trade"
    )
    conn.execute(
        "DELETE FROM sensex_alpha_inverted_trades WHERE trade_date=?", (trade_date,)
    )
    for seq, trade in enumerate(trades, 1):
        conn.execute(
            "INSERT INTO sensex_alpha_inverted_trades "
            "(trade_date,seq,status,side,original_side,strike,tradingsymbol,expiry_code,"
            "entry_ts,exit_ts,entry_alpha,exit_alpha,entry_spot,exit_spot,spot_pnl_pts,"
            "entry_bid,entry_ask,exit_bid,exit_ask,option_pnl_pts,option_gross_rs,"
            "quote_status,entry_reason,exit_reason) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,"
            "?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                trade_date, seq, trade["status"], trade["side"],
                trade["original_side"], trade["strike"], trade.get("tradingsymbol"),
                trade["expiry_code"], trade["entry_ts"], trade["exit_ts"],
                trade["entry_alpha"], trade["exit_alpha"], trade["entry_spot"],
                trade["exit_spot"], trade["spot_pnl_pts"], trade.get("entry_bid"),
                trade.get("entry_ask"), trade.get("exit_bid"), trade.get("exit_ask"),
                trade.get("option_pnl_pts"), trade.get("option_gross_rs"),
                trade["quote_status"], trade["entry_reason"], trade["exit_reason"],
            ),
        )
    priced = [trade for trade in trades if trade["quote_status"] == "priced"]
    latest = bars.iloc[-1]
    position_side = trades[-1]["side"] if status == "open" else None
    conn.execute(
        "INSERT INTO sensex_alpha_inverted_daily "
        "(trade_date,status,prev_close,range_lower,range_upper,latest_mark,latest_spot,"
        "latest_alpha,position_side,n_trades,spot_pnl_pts,option_gross_rs,"
        "option_priced_trades,option_unavailable_trades,expiry_code,baseline_date,"
        "liquidity_mode,liquidity_mark,selected_expiry_type,weekly_expiry_code,"
        "monthly_expiry_code,weekly_in_band_oi,monthly_in_band_oi,"
        "strategy_version,updated_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,"
        "?,?,?,?,?,?,?,?,?) "
        "ON CONFLICT(trade_date) DO UPDATE SET status=excluded.status,"
        "prev_close=excluded.prev_close,range_lower=excluded.range_lower,"
        "range_upper=excluded.range_upper,latest_mark=excluded.latest_mark,"
        "latest_spot=excluded.latest_spot,latest_alpha=excluded.latest_alpha,"
        "position_side=excluded.position_side,n_trades=excluded.n_trades,"
        "spot_pnl_pts=excluded.spot_pnl_pts,option_gross_rs=excluded.option_gross_rs,"
        "option_priced_trades=excluded.option_priced_trades,"
        "option_unavailable_trades=excluded.option_unavailable_trades,"
        "expiry_code=excluded.expiry_code,baseline_date=excluded.baseline_date,"
        "liquidity_mode=excluded.liquidity_mode,"
        "liquidity_mark=excluded.liquidity_mark,"
        "selected_expiry_type=excluded.selected_expiry_type,"
        "weekly_expiry_code=excluded.weekly_expiry_code,"
        "monthly_expiry_code=excluded.monthly_expiry_code,"
        "weekly_in_band_oi=excluded.weekly_in_band_oi,"
        "monthly_in_band_oi=excluded.monthly_in_band_oi,"
        "strategy_version=excluded.strategy_version,updated_at=excluded.updated_at",
        (
            trade_date, status, context["prev_close"], context["range_lower"],
            context["range_upper"], latest["timestamp"].isoformat(),
            float(latest["spot"]), float(latest["alpha"]), position_side, len(trades),
            round(sum(float(t["spot_pnl_pts"]) for t in trades), 2),
            round(sum(float(t["option_gross_rs"]) for t in priced), 2),
            len(priced), len(trades) - len(priced), context["expiry_code"],
            context["baseline_date"], context.get("liquidity_mode"),
            context.get("liquidity_mark"), context.get("selected_expiry_type"),
            context.get("weekly_expiry_code"), context.get("monthly_expiry_code"),
            context.get("weekly_in_band_oi"), context.get("monthly_in_band_oi"),
            STRATEGY_VERSION, now,
        ),
    )
    if commit:
        conn.commit()


def run_day(
    trade_date: str | None = None,
    *,
    persist: bool = True,
    connection: sqlite3.Connection | None = None,
    commit: bool = True,
) -> dict:
    trade_date = trade_date or datetime.now(base.IST).date().isoformat()
    config = base.load_config()
    context, bars, quotes = base.build_alpha_bars(trade_date, config)
    if len(bars) < 2:
        raise base.SensexReplayInputError(
            f"Only {len(bars)} completed SENSEX Alpha marks for {trade_date}"
        )
    if base._session_over(trade_date, config["eod_exit"]):
        required = base.pd.Timestamp(
            f"{trade_date} {config['eod_exit']}", tz=base.IST
        )
        if base.pd.Timestamp(bars.iloc[-1]["timestamp"]) < required:
            raise base.SensexReplayInputError(
                f"Completed session {trade_date} lacks hard-EOD mark {config['eod_exit']}"
            )
    original = base.simulate(bars, quotes, context, config)
    trades = _invert_trades(original, quotes, config["lot_size"])
    if persist:
        conn = connection or get_conn()
        if connection is None or commit:
            _ensure_tables(conn)
        _save(conn, trade_date, context, bars, trades, config, commit=commit)
    priced = [trade for trade in trades if trade["quote_status"] == "priced"]
    return {
        "trade_date": trade_date,
        "status": "open" if trades and trades[-1]["status"] == "open" else (
            "traded" if trades else "no_trade"
        ),
        "latest_alpha": float(bars.iloc[-1]["alpha"]),
        "n_trades": len(trades),
        "spot_pnl_pts": round(sum(t["spot_pnl_pts"] for t in trades), 2),
        "option_gross_rs": round(sum(t["option_gross_rs"] for t in priced), 2),
        "option_priced_trades": len(priced),
        "option_unavailable_trades": len(trades) - len(priced),
        "expiry_code": context["expiry_code"],
        "selected_expiry_type": context.get("selected_expiry_type"),
        "weekly_in_band_oi": context.get("weekly_in_band_oi"),
        "monthly_in_band_oi": context.get("monthly_in_band_oi"),
    }


if __name__ == "__main__":
    import sys
    print(run_day(sys.argv[1] if len(sys.argv) > 1 else None))
