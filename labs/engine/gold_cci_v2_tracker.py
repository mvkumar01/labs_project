"""Isolated GOLD CCI V2 paper ledger. V1 and the GOLDM dry runner are separate."""
from __future__ import annotations

from datetime import date, datetime
from pathlib import Path
import math
import sqlite3
import pandas as pd

from labs.engine import gold_cci_tracker as v1
from labs.services import margin_estimates
from live.engine import gold_cci_v2_engine as eng
from storage.db import get_conn

BASE_DIR = Path(__file__).resolve().parents[2]
IST = v1.IST
PAPER_START = "2026-10-29"
STRATEGY_VERSION = "gold_cci_v2_original_entries_daily_net3_v1"
MARGIN_SOURCE = "estimated_nrml_quote"


def _ensure_tables(conn):
    v1._ensure_tables(conn)  # Shared contracts/candles; never writes V1 trades.
    conn.executescript("""
        CREATE TABLE IF NOT EXISTS gold_cci_v2_daily (
            trade_date TEXT PRIMARY KEY, status TEXT NOT NULL, tradingsymbol TEXT, expiry TEXT,
            valid_bars INTEGER, n_signals INTEGER DEFAULT 0, n_trades INTEGER DEFAULT 0,
            open_trades INTEGER DEFAULT 0, wins INTEGER DEFAULT 0, gross_rs REAL DEFAULT 0,
            charges_rs REAL DEFAULT 0, slippage_rs REAL DEFAULT 0, net_rs REAL DEFAULT 0,
            qty INTEGER NOT NULL, through_ts TEXT, strategy_version TEXT NOT NULL,
            margin_rate REAL, margin_asof TEXT, margin_contract TEXT,
            capital_rs REAL, daily_target_rs REAL, target_hit INTEGER DEFAULT 0,
            error TEXT, updated_at TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS gold_cci_v2_trades (
            trade_date TEXT NOT NULL, seq INTEGER NOT NULL, direction TEXT NOT NULL,
            tradingsymbol TEXT NOT NULL, signal_ts TEXT NOT NULL, entry_ts TEXT NOT NULL,
            exit_ts TEXT, entry_price REAL NOT NULL, exit_price REAL, stop_price REAL NOT NULL,
            target_price REAL NOT NULL, stop_dist REAL NOT NULL, stop_moved INTEGER DEFAULT 0,
            r_multiple REAL, points REAL, qty INTEGER NOT NULL, gross_rs REAL, charges_rs REAL,
            slippage_rs REAL, net_rs REAL, status TEXT NOT NULL, exit_reason TEXT, bars_held INTEGER,
            margin_rs REAL, margin_source TEXT NOT NULL, margin_asof TEXT, margin_contract TEXT,
            PRIMARY KEY (trade_date,seq)
        );
        CREATE TABLE IF NOT EXISTS gold_cci_v2_signals (
            trade_date TEXT NOT NULL, signal_ts TEXT PRIMARY KEY, outcome TEXT NOT NULL,
            cci REAL, rsi REAL, plus_di REAL, minus_di REAL
        );
    """)
    conn.commit()


def _frozen_quote(conn, session, now, quote=None, kite=None, offline=False):
    row = conn.execute("SELECT margin_rate,margin_asof,margin_contract,capital_rs FROM gold_cci_v2_daily "
                       "WHERE trade_date=?", (session,)).fetchone()
    if row and row[0] is not None:
        return dict(rate=row[0], asof=row[1], contract=row[2], capital=row[3])
    # There is no recorded historical margin for a new past session. Never substitute today's.
    if session != now.date().isoformat():
        return None
    candidate = quote if quote is not None else margin_estimates.cached_quotes().get("quotes", {}).get("GOLD")
    def usable(q):
        try:
            rate = float(q["sides"]["SELL"]["rate"])
            stamp = datetime.fromisoformat(q["asof"])
            stamp = stamp.replace(tzinfo=IST) if stamp.tzinfo is None else stamp.astimezone(IST)
            age = (now.replace(tzinfo=IST) - stamp).total_seconds()
            return math.isfinite(rate) and 0 < rate <= 1 and bool(q["contract"]) and 0 <= age <= 86400 and stamp.date() == now.date()
        except (KeyError, TypeError, ValueError):
            return False
    if not usable(candidate) and not offline and quote is None:
        try:
            candidate = margin_estimates.refresh_quotes(kite=kite).get("quotes", {}).get("GOLD")
        except Exception:
            candidate = None
    if not usable(candidate):
        return None
    return dict(rate=float(candidate["sides"]["SELL"]["rate"]), asof=candidate["asof"],
                contract=candidate["contract"], capital=None)


def _persist(conn, day, status, contract, summary, trades, signals, quote, error=None):
    closed = [t for t in trades if t["status"] == "closed"]
    stamp = datetime.now(IST).isoformat(timespec="seconds")
    symbol = (contract or {}).get("contract") or ""
    # One transaction: a failed write cannot leave a half-replaced book.
    with conn:
        conn.execute("DELETE FROM gold_cci_v2_trades WHERE trade_date=?", (day,))
        conn.execute("DELETE FROM gold_cci_v2_signals WHERE trade_date=?", (day,))
        for t in trades:
            row = {k: t[k] for k in ("seq", "direction", "signal_ts", "entry_ts", "exit_ts", "entry_price", "exit_price",
                "stop_price", "target_price", "stop_dist", "stop_moved", "r_multiple", "points", "gross_rs", "charges_rs",
                "slippage_rs", "net_rs", "status", "exit_reason", "bars_held")}
            row.update(trade_date=day, tradingsymbol=symbol, qty=eng.QTY,
                       margin_rs=round(t["entry_price"] * eng.QTY * quote["rate"], 2),
                       margin_source=MARGIN_SOURCE, margin_asof=quote["asof"], margin_contract=quote["contract"])
            conn.execute(f"INSERT INTO gold_cci_v2_trades ({','.join(row)}) VALUES ({','.join('?' for _ in row)})", tuple(row.values()))
        conn.executemany("INSERT INTO gold_cci_v2_signals VALUES (?,?,?,?,?,?,?)",
            [(day, str(s["signal_ts"]), s["outcome"], s.get("cci"), s.get("rsi"), s.get("plus_di"), s.get("minus_di")) for s in signals])
        row = dict(trade_date=day, status=status, tradingsymbol=symbol or None, expiry=(contract or {}).get("expiry"),
            valid_bars=summary.get("valid_bars"), n_signals=len(signals), n_trades=len(trades),
            open_trades=len(trades)-len(closed), wins=sum(t["net_rs"] > 0 for t in closed),
            gross_rs=round(sum(t["gross_rs"] for t in closed), 2), charges_rs=round(sum(t["charges_rs"] for t in closed), 2),
            slippage_rs=round(sum(t["slippage_rs"] for t in closed), 2), net_rs=round(sum(t["net_rs"] for t in closed), 2),
            qty=eng.QTY, through_ts=summary.get("through_ts"), strategy_version=STRATEGY_VERSION,
            margin_rate=(quote or {}).get("rate"), margin_asof=(quote or {}).get("asof"), margin_contract=(quote or {}).get("contract"),
            capital_rs=summary.get("capital_rs", (quote or {}).get("capital")), daily_target_rs=summary.get("target_rs"),
            target_hit=int(summary.get("target_hit", False)), error=error, updated_at=stamp)
        conn.execute(f"INSERT OR REPLACE INTO gold_cci_v2_daily ({','.join(row)}) VALUES ({','.join('?' for _ in row)})", tuple(row.values()))


def run_day(trade_date=None, *, kite=None, now=None, rebuild=False, connection=None, offline=False, margin_quote=None):
    now = v1.mcx._to_ist_naive(now or datetime.now(IST))
    session = date.fromisoformat(trade_date) if trade_date else now.date()
    day = session.isoformat()
    if day < PAPER_START:
        return {"trade_date": day, "status": "before_start"}
    if session > now.date() or session.weekday() >= 5:
        return {"trade_date": day, "status": "future" if session > now.date() else "weekend"}
    own = connection is None
    conn = connection or get_conn()
    try:
        _ensure_tables(conn)
        prior = conn.execute("SELECT status FROM gold_cci_v2_daily WHERE trade_date=?", (day,)).fetchone()
        if prior and prior[0] in ("final", "no_session") and not rebuild:
            return {"trade_date": day, "status": prior[0], "skipped": True}
        quote = _frozen_quote(conn, day, now, margin_quote, kite, offline)
        if quote is None:
            error = "Fresh session margin quote unavailable; 3% net target cannot be calculated"
            _persist(conn, day, "blocked_margin", None, {}, [], [], None, error)
            return {"trade_date": day, "status": "blocked_margin", "error": error}
        if kite is None and not offline:
            from auth.session_manager import get_kite
            kite = get_kite()
        frame, rolls = v1.session_frame(conn, session, now, kite)
        frame = frame[frame["ts"].dt.date <= session]
        contract = next((r for r in rolls if r["first"] <= day <= r["last"]), rolls[-1] if rolls else None)
        in_progress = session == now.date() and now.time() < v1.FINAL_AFTER
        if frame[frame["ts"].dt.date == session].empty:
            status = "live" if in_progress else "no_session"
            _persist(conn, day, status, contract, {}, [], [], quote)
            return {"trade_date": day, "status": status, "n_trades": 0}
        cutoff = now.replace(second=0, microsecond=0) if in_progress else None
        out = eng.replay(frame, session, end=session, cutoff=cutoff, margin_rate=quote["rate"],
                         capital_by_day={day: quote["capital"]} if quote["capital"] is not None else None)
        g = out["grid"]
        idx = list(g["days"]).index(session)
        known = g["ts"][:g["n_known"]]
        summary = {**out["daily"].get(day, {}), "valid_bars": int(g["valid_counts"][idx]),
                   "through_ts": str(pd.Timestamp(known[-1])) if len(known) else None}
        trades = v1._priced(out["trades"])
        signals = [s for s in out["signals"] if s["signal_ts"].date() == session]
        status = "live" if in_progress else "final"
        _persist(conn, day, status, contract, summary, trades, signals, quote)
        return {"trade_date": day, "status": status, "n_trades": len(trades),
                "open": sum(t["status"] == "open" for t in trades), "target_hit": summary.get("target_hit", False),
                "net_rs": round(sum(t["net_rs"] for t in trades if t["status"] == "closed"), 2)}
    finally:
        if own:
            conn.close()


def run_live(now=None):
    now = now or datetime.now(IST)
    today = v1.mcx._to_ist_naive(now).date().isoformat()
    conn = get_conn()
    try:
        _ensure_tables(conn)
        stale = [r[0] for r in conn.execute("SELECT trade_date FROM gold_cci_v2_daily WHERE status='live' AND trade_date<? ORDER BY trade_date", (today,))]
    finally:
        conn.close()
    for day in stale:
        run_day(day, now=now)
    return run_day(today, now=now)


def tab_data(conn, date_clause="", date_params=()):
    cur = conn.execute("SELECT * FROM gold_cci_v2_daily WHERE 1=1 " + date_clause + " ORDER BY trade_date DESC", tuple(date_params))
    return [dict(zip([c[0] for c in cur.description], r)) for r in cur.fetchall()]
