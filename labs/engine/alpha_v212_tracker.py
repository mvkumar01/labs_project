"""Alpha v2.12 paper tracker: v2.11 plus entry-spot stop/recovery.

Every signal segment buys one nearest-expiry NIFTY option 200 points ITM at
the exact-minute ask and sells at the exact-minute bid. Recovery-cancel events
are telemetry only and are not priced. Paper only; this module never places
orders.
"""
from __future__ import annotations

import json
import math
import sqlite3
from datetime import datetime

import pandas as pd

from config.labs_config import SHARED_ARCHIVE_DIR, SHARED_LIVE_DIR, UNDERLYINGS
from labs.engine.charges import round_trip_charges
from labs.engine.paper_strategy_tracker import (
    IST,
    ITM_DISTANCE,
    ReplayInputError,
    _resolve_day,
    _resolve_replay_context,
    _r50,
    _session_over,
)
from live.engine import champion_inputs, champion_sim
from market_data.expiry import select_expiry_code
from market_data.shared_store import load_options_frame
from storage.db import get_conn


SYMBOL = "NIFTY"
LOT_SIZE = int(UNDERLYINGS[SYMBOL]["lot_size"])
QTY = LOT_SIZE
STRATEGY_VERSION = "alpha_v2.12_entry_spot_recovery_itm200_bidask"


class AlphaV212InputError(RuntimeError):
    """Required replay or executable-quote input is incomplete."""


def _ensure_tables(conn: sqlite3.Connection) -> None:
    conn.executescript(
        """
        CREATE TABLE IF NOT EXISTS alpha_v212_daily (
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
        CREATE TABLE IF NOT EXISTS alpha_v212_trades (
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
    columns = {
        row[1] for row in conn.execute("PRAGMA table_info(alpha_v212_daily)")
    }
    if "context_json" not in columns:
        conn.execute(
            "ALTER TABLE alpha_v212_daily ADD COLUMN context_json TEXT"
        )
    conn.commit()


def _finite_positive(value) -> float | None:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if math.isfinite(number) and number > 0 else None


def _timestamp_key(value) -> str:
    ts = pd.Timestamp(value)
    if ts.tzinfo is None:
        ts = ts.tz_localize(IST)
    else:
        ts = ts.tz_convert(IST)
    return ts.floor("min").isoformat()


def build_executable_book(trade_date: str) -> tuple[str, dict]:
    """Return nearest-expiry exact-minute bid/ask quotes."""
    try:
        frame = load_options_frame(
            SYMBOL,
            trade_date,
            live_root=SHARED_LIVE_DIR,
            archive_root=SHARED_ARCHIVE_DIR,
        )
    except Exception as exc:
        raise AlphaV212InputError(
            f"Unable to load NIFTY quotes for {trade_date}: "
            f"{type(exc).__name__}: {exc}"
        ) from exc
    required = {
        "timestamp", "strike", "option_type", "expiry", "bid", "ask"
    }
    missing = required.difference(frame.columns)
    if missing:
        raise AlphaV212InputError(
            f"NIFTY quote data missing columns: {sorted(missing)}"
        )
    frame = frame.copy()
    frame["timestamp"] = pd.to_datetime(frame["timestamp"], errors="coerce")
    frame = frame.dropna(subset=["timestamp"])
    if frame["timestamp"].dt.tz is None:
        frame["timestamp"] = frame["timestamp"].dt.tz_localize(IST)
    else:
        frame["timestamp"] = frame["timestamp"].dt.tz_convert(IST)
    frame["type"] = frame["option_type"].astype(str).str.lower()
    frame["expiry"] = frame["expiry"].astype(str).str.upper()
    for column in ("strike", "bid", "ask"):
        frame[column] = pd.to_numeric(frame[column], errors="coerce")
    frame = frame.dropna(subset=["strike"])
    expiry_code = select_expiry_code(
        frame["expiry"].unique(), trade_date, "nearest_weekly"
    )
    if expiry_code is None:
        raise AlphaV212InputError(f"No nearest NIFTY expiry for {trade_date}")
    frame = frame[frame["expiry"] == str(expiry_code)].copy()
    if frame.empty:
        raise AlphaV212InputError(
            f"Selected NIFTY expiry {expiry_code} has no rows for {trade_date}"
        )
    frame = (
        frame.sort_values("timestamp")
        .groupby(["timestamp", "strike", "type"], as_index=False)
        .last()
    )
    quotes = {}
    for row in frame.itertuples(index=False):
        quotes[
            (_timestamp_key(row.timestamp), int(row.strike), str(row.type))
        ] = {
            "bid": _finite_positive(row.bid),
            "ask": _finite_positive(row.ask),
            "tradingsymbol": getattr(row, "tradingsymbol", None),
        }
    return str(expiry_code), quotes


def _price_segment(segment: dict, expiry_code: str, quotes: dict) -> dict:
    side = "CALL" if segment["pos"] == "call" else "PUT"
    option_type = "ce" if side == "CALL" else "pe"
    strike = (
        _r50(float(segment["entry_spot"]) - ITM_DISTANCE)
        if side == "CALL"
        else _r50(float(segment["entry_spot"]) + ITM_DISTANCE)
    )
    entry_ts = _timestamp_key(segment["entry_ts"])
    exit_ts = _timestamp_key(segment["exit_ts"])
    entry = quotes.get((entry_ts, strike, option_type), {})
    exit_ = quotes.get((exit_ts, strike, option_type), {})
    entry_bid, entry_ask = entry.get("bid"), entry.get("ask")
    exit_bid, exit_ask = exit_.get("bid"), exit_.get("ask")
    quote_status = "priced"
    option_points = gross = charges = net = None
    if entry_bid is None or entry_ask is None:
        quote_status = "entry_book_unavailable"
    elif entry_ask < entry_bid:
        quote_status = "entry_market_crossed"
    elif exit_bid is None or exit_ask is None:
        quote_status = "exit_book_unavailable"
    elif exit_ask < exit_bid:
        quote_status = "exit_market_crossed"
    else:
        option_points = round(exit_bid - entry_ask, 2)
        gross = round(option_points * QTY, 2)
        charge_detail = round_trip_charges(entry_ask, exit_bid, QTY)
        # v2.12's promoted June research audit sums exact statutory charges
        # and rounds only the displayed aggregate. Keeping the raw value here
        # reproduces that benchmark while the UI still formats to paise.
        charges = float(charge_detail.get("raw_total", charge_detail["total"]))
        net = gross - charges
    return {
        "status": "closed",
        "side": side,
        "strike": strike,
        "expiry_code": expiry_code,
        "tradingsymbol": entry.get("tradingsymbol"),
        "entry_ts": entry_ts,
        "exit_ts": exit_ts,
        "entry_spot": float(segment["entry_spot"]),
        "exit_spot": float(segment["exit_spot"]),
        "spot_pnl_pts": float(segment["pnl"]),
        "entry_bid": entry_bid,
        "entry_ask": entry_ask,
        "exit_bid": exit_bid,
        "exit_ask": exit_ask,
        "option_pnl_pts": option_points,
        "gross_rs": gross,
        "charges_rs": charges,
        "net_rs": net,
        "quote_status": quote_status,
        "entry_rule": segment.get("entry_rule"),
        "exit_reason": str(segment.get("reason") or "unknown"),
    }


def replay_v212(trade_date: str, override: dict | None = None) -> dict:
    day = _resolve_day(trade_date, override)
    if day is None:
        explicitly_skipped = override is not None and (
            override.get("bucket") == "SKIP" or override.get("skip")
        )
        if not explicitly_skipped:
            raise AlphaV212InputError(
                f"No locked range state is available for {trade_date}"
            )
        return {
            "tier": "SKIP",
            "direction": override.get("direction"),
            "segments": [],
            "session_done": _session_over(trade_date),
        }

    tier = day["bucket"]
    ohlc, context, range_source, use_abs, provenance = (
        _resolve_replay_context(trade_date, day)
    )
    try:
        _, adf, ce_map, pe_map = champion_inputs.build_sim_inputs(
            trade_date,
            day["lower"],
            day["upper"],
            use_abs,
            range_source=range_source,
        )
    except Exception as exc:
        raise AlphaV212InputError(
            f"Unable to build v2.12 inputs for {trade_date}: "
            f"{type(exc).__name__}: {exc}"
        ) from exc
    if adf is None or adf.empty:
        raise AlphaV212InputError(f"v2.12 input frame is empty for {trade_date}")
    # Current-day: keep the in-progress bucket as a STOPS-ONLY partial bar
    # (entry-spot SL + recovery re-entry fire on its 1-min sub-bars; alpha
    # entries/exits stay on completed bars) so labs v2.12 reacts intra-bar like
    # the live bot. Past days -> cutoff None -> byte-identical to before.
    cutoff = None
    if trade_date == datetime.now(IST).date().isoformat():
        cutoff = pd.Timestamp(datetime.now(IST)) - pd.Timedelta(minutes=5)
    if len(adf) < 2:
        raise AlphaV212InputError(
            f"v2.12 input frame has only {len(adf)} completed bars for {trade_date}"
        )
    _, segments = champion_sim.simulate(
        adf,
        ce_map,
        pe_map,
        ohlc,
        trade_date,
        context.use_trail,
        context.sgap,
        tier,
        context.weekday,
        context.regime,
        day["lower"],
        day["upper"],
        enable_entry_spot_recovery=True,
        entries_until_ts=cutoff,
    )
    # Recovery cancellations carry no economic position or option fill.
    economic = [
        segment
        for segment in segments
        if segment.get("reason") != "RECOVERY_CANCEL_ALPHA"
    ]
    return {
        "tier": tier,
        "direction": context.direction,
        "segments": economic,
        "session_done": _session_over(trade_date),
        "context": provenance,
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
    conn.execute("DELETE FROM alpha_v212_trades WHERE trade_date=?", (trade_date,))
    for seq, trade in enumerate(trades, 1):
        conn.execute(
            "INSERT INTO alpha_v212_trades "
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
    spot = round(sum(float(t["spot_pnl_pts"]) for t in trades), 2)
    gross = round(sum(float(t["gross_rs"]) for t in priced), 2)
    charges = round(sum(float(t["charges_rs"]) for t in priced), 2)
    net = round(sum(float(t["net_rs"]) for t in priced), 2)
    now = datetime.now(IST).isoformat()
    conn.execute(
        "INSERT INTO alpha_v212_daily "
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
            json.dumps(replay.get("context"), sort_keys=True)
            if replay.get("context") else None,
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
    try:
        replay = replay_v212(trade_date, override)
    except ReplayInputError as exc:
        raise AlphaV212InputError(str(exc)) from exc
    expiry_code = None
    trades: list[dict] = []
    if replay["segments"]:
        expiry_code, quotes = build_executable_book(trade_date)
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
    unavailable = [trade for trade in trades if trade["quote_status"] != "priced"]
    if require_all_quotes and unavailable:
        detail = "; ".join(
            f"#{index + 1} {trade['quote_status']}"
            for index, trade in enumerate(unavailable)
        )
        raise AlphaV212InputError(
            f"Alpha v2.12 pricing incomplete for {trade_date}: {detail}; "
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
            else "open" if any(t["status"] == "open" for t in trades)
            else "traded" if trades
            else "no_trade"
        ),
        "n_segments": len(trades),
        "priced_segments": len(priced),
        "unavailable_segments": len(unavailable),
        "spot_pnl_pts": round(sum(t["spot_pnl_pts"] for t in trades), 2),
        "gross_rs": round(sum(t["gross_rs"] for t in priced), 2),
        "charges_rs": round(sum(t["charges_rs"] for t in priced), 2),
        "net_rs": round(sum(t["net_rs"] for t in priced), 2),
        "expiry_code": expiry_code,
    }


if __name__ == "__main__":
    import sys

    print(run_day(sys.argv[1] if len(sys.argv) > 1 else None))
