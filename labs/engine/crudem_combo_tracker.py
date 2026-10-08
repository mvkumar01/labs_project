"""CRUDEOILM "consistent" six-member combination: paper book.

Paper only. This module never calls a broker order API.

The rules live in live/engine/crudem_combo_engine.py, an exact port of Strategy Tester v2's saved
combination c8 (run crudem_20261005). Each run replays the rolled CRUDEOILM series up to the last
completed minute and stores today's trades and every member signal, so it is idempotent and a
restart loses nothing.

Evidence status: the combination was CHOSEN on every session from 1 Jun to 6 Oct 2026. Sessions
from PAPER_START (7 Oct 2026) are the first it was not chosen on, and the only ones this book
records. Nothing in the engine may be tuned from what this book shows.

Data: completed 1-minute Kite candles of the front contract and the next one, kept in the shared
MCX candle cache (labs/engine/crude_macd_st_tracker.py). Contracts are joined as the Tester joins
them: a contract's last session is the one before its expiry day, and at a roll the price gap is
added to the earlier contract's prices. Kite serves minute history for listed contracts only, so
each contract's candles stay in the cache after it expires.

Costs: MCX futures charges on the fill prices plus one tick (Rs 1) of slippage each side, the
assumption the back test used (about Rs 80 a round trip for one lot).
"""
from __future__ import annotations

import sqlite3
from datetime import date, datetime, timedelta

import pandas as pd

from labs.engine import crude_macd_st_tracker as mcx
from live.engine import crudem_combo_engine as eng
from labs.engine.charges import mcx_futures_round_trip_charges
from storage.db import get_conn

IST = mcx.IST
STRATEGY_VERSION = "crudem_consistent_combo_c8_v1"
UNDERLYING = "CRUDEOILM"
LOTS = 1
QTY = LOTS * eng.LOT_QTY
SLIPPAGE_TICKS = 1.0
PAPER_START = "2026-10-07"           # first session the combination was not chosen on
FINAL_AFTER = mcx.FINAL_AFTER
LOOKBACK_DAYS = 60                   # calendar days of warm-up replayed before a session
# The September contract was the front month inside the warm-up window when this book started;
# Kite no longer lists it. Its candles are used when the shared cache holds them.
KNOWN_EXPIRED = ({"tradingsymbol": "CRUDEOILM26SEPFUT", "instrument_token": 144870407, "expiry": "2026-09-21"},)
NEXT_CONTRACT_DAYS = 7               # start caching the next contract this many days before a roll
_MASTER: dict = {"date": None, "rows": []}   # Kite's MCX instrument list, read once a day


class CrudemComboInputError(RuntimeError):
    """The contracts or candles a session needs are unavailable."""


# ------------------------------------------------------------------ schema ---
def _ensure_tables(conn: sqlite3.Connection) -> None:
    mcx._ensure_tables(conn)                       # the shared minute-candle cache
    conn.executescript(
        """
        CREATE TABLE IF NOT EXISTS crudem_combo_contracts (
            tradingsymbol    TEXT PRIMARY KEY,
            expiry           TEXT NOT NULL,
            instrument_token INTEGER
        );
        CREATE TABLE IF NOT EXISTS crudem_combo_daily (
            trade_date       TEXT PRIMARY KEY,
            status           TEXT NOT NULL,
            tradingsymbol    TEXT,
            expiry           TEXT,
            valid_bars       INTEGER,
            n_signals        INTEGER NOT NULL DEFAULT 0,
            n_trades         INTEGER NOT NULL DEFAULT 0,
            open_trades      INTEGER NOT NULL DEFAULT 0,
            wins             INTEGER NOT NULL DEFAULT 0,
            gross_rs         REAL NOT NULL DEFAULT 0,
            charges_rs       REAL NOT NULL DEFAULT 0,
            slippage_rs      REAL NOT NULL DEFAULT 0,
            net_rs           REAL NOT NULL DEFAULT 0,
            qty              INTEGER NOT NULL,
            roll_adjustment  REAL,
            through_ts       TEXT,
            strategy_version TEXT NOT NULL,
            error            TEXT,
            updated_at       TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS crudem_combo_trades (
            trade_date    TEXT NOT NULL,
            seq           INTEGER NOT NULL,
            cid           INTEGER NOT NULL,
            direction     TEXT NOT NULL,
            tradingsymbol TEXT NOT NULL,
            signal_ts     TEXT NOT NULL,
            entry_ts      TEXT NOT NULL,
            exit_ts       TEXT,
            entry_price   REAL NOT NULL,
            exit_price    REAL,
            stop_price    REAL NOT NULL,
            target_price  REAL NOT NULL,
            stop_dist     REAL NOT NULL,
            r_multiple    REAL,
            points        REAL,
            qty           INTEGER NOT NULL,
            gross_rs      REAL,
            charges_rs    REAL,
            slippage_rs   REAL,
            net_rs        REAL,
            status        TEXT NOT NULL,
            exit_reason   TEXT,
            bars_held     INTEGER,
            PRIMARY KEY (trade_date, seq)
        );
        CREATE TABLE IF NOT EXISTS crudem_combo_signals (
            trade_date TEXT NOT NULL,
            signal_ts  TEXT NOT NULL,
            cid        INTEGER NOT NULL,
            outcome    TEXT NOT NULL,
            blocked_by INTEGER,
            PRIMARY KEY (signal_ts, cid)
        );
        """
    )
    conn.commit()


# --------------------------------------------------------------- contracts ---
def _as_date(value) -> date:
    return value if isinstance(value, date) and not isinstance(value, datetime) else date.fromisoformat(str(value)[:10])


def remember_contracts(conn: sqlite3.Connection, kite=None) -> list[dict]:
    """Every CRUDEOILM future ever seen (Kite's master forgets a contract once it expires)."""
    rows = list(KNOWN_EXPIRED)
    if kite is not None:
        today = datetime.now(IST).date()
        if _MASTER["date"] != today or not _MASTER["rows"]:
            _MASTER.update(date=today, rows=[
                {"tradingsymbol": r["tradingsymbol"], "instrument_token": int(r["instrument_token"]),
                 "expiry": _as_date(r["expiry"]).isoformat()}
                for r in kite.instruments("MCX")
                if r.get("name") == UNDERLYING and r.get("instrument_type") == "FUT"])
        rows += _MASTER["rows"]
    conn.executemany(
        "INSERT INTO crudem_combo_contracts (tradingsymbol, expiry, instrument_token) VALUES (?,?,?) "
        "ON CONFLICT(tradingsymbol) DO UPDATE SET expiry=excluded.expiry, "
        "instrument_token=COALESCE(excluded.instrument_token, instrument_token)",
        [(r["tradingsymbol"], r["expiry"], r.get("instrument_token")) for r in rows])
    conn.commit()
    cur = conn.execute("SELECT tradingsymbol, expiry, instrument_token FROM crudem_combo_contracts ORDER BY expiry")
    return [{"tradingsymbol": s, "expiry": e, "instrument_token": t} for s, e, t in cur.fetchall()]


def session_frame(conn: sqlite3.Connection, session: date, now: datetime, kite=None) -> tuple[pd.DataFrame, list[dict]]:
    """The rolled, back-adjusted 1-minute series a session is replayed on, and its roll table."""
    contracts = remember_contracts(conn, kite)
    start = session - timedelta(days=LOOKBACK_DAYS)
    today = now.date()
    relevant = [c for c in contracts if _as_date(c["expiry"]) >= start]
    live = [c for c in relevant if _as_date(c["expiry"]) >= today]
    # the front contract, and from a week before its expiry the next one too: the next contract's
    # candles on the roll day set the adjustment, and it becomes the front on the expiry day
    wanted = live[:1]
    if len(live) > 1 and (_as_date(live[0]["expiry"]) - session).days <= NEXT_CONTRACT_DAYS:
        wanted = live[:2]
    for c in wanted:
        if kite is not None and c.get("instrument_token"):
            mcx.ensure_minute_bars(kite, c, start, session, now, conn)
    frames = []
    for c in relevant:
        frame = mcx.load_minute_bars(c["tradingsymbol"], start, session, conn)
        if len(frame):
            frames.append((c["tradingsymbol"], _as_date(c["expiry"]), frame))
    if not frames:
        raise CrudemComboInputError(f"No cached {UNDERLYING} candles for {start}..{session}")
    return eng.stitch(frames, today)


# ------------------------------------------------------------------ ledger ---
def _priced(trades: list[dict]) -> list[dict]:
    out = []
    for seq, t in enumerate(trades, 1):
        long_ = t["side"] > 0
        buy, sell = (t["entry_price"], t["exit_price"]) if long_ else (t["exit_price"], t["entry_price"])
        gross = t["points"] * QTY
        charges = mcx_futures_round_trip_charges(buy, sell, QTY)["raw_total"]
        slippage = 2 * SLIPPAGE_TICKS * eng.TICK_SIZE * QTY
        out.append({
            "seq": seq, "cid": t["cid"], "direction": "long" if long_ else "short",
            "signal_ts": str(t["signal_ts"]), "entry_ts": str(t["entry_ts"]),
            "exit_ts": None if t["exit_ts"] is None else str(t["exit_ts"]),
            "entry_price": float(t["entry_price"]), "exit_price": float(t["exit_price"]),
            "stop_price": float(t["stop_price"]), "target_price": float(t["target_price"]),
            "stop_dist": float(t["stop_dist"]), "points": float(t["points"]),
            "r_multiple": float(t["points"] / t["stop_dist"]),
            "gross_rs": round(gross, 2), "charges_rs": round(charges, 2), "slippage_rs": slippage,
            "net_rs": round(gross - charges - slippage, 2), "status": t["status"],
            "exit_reason": t["reason"], "bars_held": int(t["bars_held"]),
        })
    return out


def _persist(conn: sqlite3.Connection, trade_date: str, status: str, contract: dict | None, summary: dict,
             trades: list[dict], signals: list[dict], error: str | None = None) -> None:
    now = datetime.now(IST).isoformat(timespec="seconds")
    symbol = (contract or {}).get("contract") or ""
    conn.execute("DELETE FROM crudem_combo_trades WHERE trade_date=?", (trade_date,))
    conn.executemany(
        "INSERT INTO crudem_combo_trades (trade_date,seq,cid,direction,tradingsymbol,signal_ts,entry_ts,exit_ts,"
        "entry_price,exit_price,stop_price,target_price,stop_dist,r_multiple,points,qty,gross_rs,charges_rs,"
        "slippage_rs,net_rs,status,exit_reason,bars_held) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        [(trade_date, t["seq"], t["cid"], t["direction"], symbol, t["signal_ts"], t["entry_ts"], t["exit_ts"],
          t["entry_price"], t["exit_price"], t["stop_price"], t["target_price"], t["stop_dist"], t["r_multiple"],
          t["points"], QTY, t["gross_rs"], t["charges_rs"], t["slippage_rs"], t["net_rs"], t["status"],
          t["exit_reason"], t["bars_held"]) for t in trades])
    conn.execute("DELETE FROM crudem_combo_signals WHERE trade_date=?", (trade_date,))
    conn.executemany(
        "INSERT OR REPLACE INTO crudem_combo_signals (trade_date,signal_ts,cid,outcome,blocked_by) VALUES (?,?,?,?,?)",
        [(trade_date, str(s["signal_ts"]), s["cid"], s["outcome"], s.get("blocked_by")) for s in signals])
    closed = [t for t in trades if t["status"] == "closed"]
    conn.execute(
        "INSERT OR REPLACE INTO crudem_combo_daily (trade_date,status,tradingsymbol,expiry,valid_bars,n_signals,"
        "n_trades,open_trades,wins,gross_rs,charges_rs,slippage_rs,net_rs,qty,roll_adjustment,through_ts,"
        "strategy_version,error,updated_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (trade_date, status, symbol or None, (contract or {}).get("expiry"), summary.get("valid_bars"),
         len(signals), len(trades), sum(t["status"] == "open" for t in trades),
         sum(t["net_rs"] > 0 for t in closed), round(sum(t["gross_rs"] for t in closed), 2),
         round(sum(t["charges_rs"] for t in closed), 2), round(sum(t["slippage_rs"] for t in closed), 2),
         round(sum(t["net_rs"] for t in closed), 2), QTY, (contract or {}).get("adjustment"),
         summary.get("through_ts"), STRATEGY_VERSION, error, now))
    conn.commit()


# --------------------------------------------------------------------- run ---
def run_day(trade_date: str | None = None, *, kite=None, now: datetime | None = None, rebuild: bool = False,
            connection: sqlite3.Connection | None = None) -> dict:
    """Replay one session (to the last completed minute, for today) and store it. Idempotent."""
    now = mcx._to_ist_naive(now or datetime.now(IST))
    session = date.fromisoformat(trade_date) if trade_date else now.date()
    trade_date = session.isoformat()
    if session.weekday() >= 5:
        return {"trade_date": trade_date, "status": "weekend"}
    if session > now.date():
        return {"trade_date": trade_date, "status": "future"}
    if trade_date < PAPER_START:
        return {"trade_date": trade_date, "status": "before_start"}
    own = connection is None
    conn = connection or get_conn()
    _ensure_tables(conn)
    try:
        row = conn.execute("SELECT status FROM crudem_combo_daily WHERE trade_date=?", (trade_date,)).fetchone()
        if row and row[0] in ("final", "no_session") and not rebuild:
            return {"trade_date": trade_date, "status": row[0], "skipped": True}
        if kite is None:
            from auth.session_manager import get_kite
            kite = get_kite()
        frame, rolls = session_frame(conn, session, now, kite)
        in_progress = session == now.date() and now.time() < FINAL_AFTER
        cutoff = now.replace(second=0, microsecond=0) if in_progress else None
        frame = frame[frame["ts"].dt.date <= session]
        contract = next((r for r in rolls if r["first"] <= trade_date <= r["last"]), rolls[-1] if rolls else None)
        if frame[frame["ts"].dt.date == session].empty:
            status = "live" if in_progress else "no_session"
            _persist(conn, trade_date, status, contract, {}, [], [])
            return {"trade_date": trade_date, "status": status, "n_trades": 0}
        out = eng.replay(frame, PAPER_START, cutoff)
        g = out["grid"]
        day_idx = int(next(k for k, d in enumerate(g["days"]) if d == session))
        known = g["ts"][:g["n_known"]]
        summary = {"valid_bars": int(g["valid_counts"][day_idx]),
                   "through_ts": str(pd.Timestamp(known[-1])) if len(known) else None}
        trades = _priced([t for t in out["trades"] if t["trade_date"] == trade_date])
        signals = [s for s in out["signals"] if s["signal_ts"].date() == session]
        status = "live" if in_progress else "final"
        _persist(conn, trade_date, status, contract, summary, trades, signals)
        closed = [t for t in trades if t["status"] == "closed"]
        return {"trade_date": trade_date, "status": status, "tradingsymbol": (contract or {}).get("contract"),
                "n_trades": len(trades), "open": len(trades) - len(closed), "signals": len(signals),
                "net_rs": round(sum(t["net_rs"] for t in closed), 2)}
    finally:
        if own:
            conn.close()


def run_live(now: datetime | None = None) -> dict:
    """The paper loop's entry point: freeze earlier sessions left 'live', then replay today."""
    now = now or datetime.now(IST)
    today = mcx._to_ist_naive(now).date().isoformat()
    conn = get_conn()
    _ensure_tables(conn)
    try:
        stale = [r[0] for r in conn.execute(
            "SELECT trade_date FROM crudem_combo_daily WHERE status='live' AND trade_date<? ORDER BY trade_date",
            (today,))]
    finally:
        conn.close()
    for session in stale:
        run_day(session, now=now)
    result = run_day(today, now=now)
    return {**result, "frozen": stale} if stale else result


def run_backfill(*, start_date: str = PAPER_START, end_date: str | None = None, limit: int = 10,
                 rebuild: bool = False) -> dict:
    """Replay sessions from the book's start that have no final row yet (oldest first)."""
    now = datetime.now(IST)
    today = mcx._to_ist_naive(now).date()
    first = max(date.fromisoformat(start_date), date.fromisoformat(PAPER_START))
    last = min(date.fromisoformat(end_date) if end_date else today, today)
    conn = get_conn()
    _ensure_tables(conn)
    try:
        done = {r[0] for r in conn.execute(
            "SELECT trade_date FROM crudem_combo_daily WHERE status IN ('final','no_session')")}
    finally:
        conn.close()
    sessions = [first + timedelta(days=k) for k in range((last - first).days + 1)]
    pending = [d.isoformat() for d in sessions if d.weekday() < 5 and (rebuild or d.isoformat() not in done)]
    completed, errors = [], {}
    for session in pending[:max(1, min(int(limit), 30))]:
        try:
            completed.append(run_day(session, now=now, rebuild=rebuild))
        except Exception as exc:                                  # noqa: BLE001
            errors[session] = f"{type(exc).__name__}: {exc}"
    return {"done": completed, "errors": errors, "remaining": max(0, len(pending) - len(completed) - len(errors))}


# ---------------------------------------------------------------- dashboard ---
def dry_run_data(limit: int = 60) -> dict | None:
    """What the real-time runner (live/crudem_runner.py, phase 0: dry run, no orders) has recorded
    in live.db, for a side-by-side with this replayed book. None when it has recorded nothing."""
    try:
        from storage.live_db import get_live_conn
        conn = get_live_conn()
    except Exception:
        return None
    try:
        cur = conn.execute(
            "SELECT t.trade_date, t.cid, t.direction, t.entry_ts, t.exit_ts, t.entry_price, t.exit_price, "
            "t.stop_price, t.target_price, t.exit_reason, t.net_rs, o.delay_s, o.bar_open "
            "FROM live_crudem_trades t LEFT JOIN live_crudem_orders o ON o.trade_ref = t.trade_ref AND o.kind = 'entry' "
            "WHERE t.book = 'dry' ORDER BY t.entry_ts DESC LIMIT ?", (int(limit),))
        cols = [c[0] for c in cur.description]
        trades = [dict(zip(cols, r)) for r in cur.fetchall()]
    except Exception:                                   # the runner has not created its tables yet
        return None
    finally:
        conn.close()
    if not trades:
        return None
    closed = [t for t in trades if t["exit_ts"]]
    delays = [t["delay_s"] for t in trades if t["delay_s"] is not None]
    slips = [(t["entry_price"] - t["bar_open"]) * (1 if t["direction"] == "long" else -1)
             for t in trades if t["bar_open"] is not None]
    return {"trades": trades, "closed": len(closed),
            "net_total": round(sum(float(t["net_rs"] or 0) for t in closed), 2),
            "avg_delay": sum(delays) / len(delays) if delays else None,
            "avg_slip": sum(slips) / len(slips) if slips else None, "slip_n": len(slips)}


def tab_data(conn: sqlite3.Connection, date_clause: str = "", date_params=()) -> tuple:
    """(daily rows, trades, stats) for the /labs/live tab, newest first."""
    cur = conn.execute(
        "SELECT trade_date,status,tradingsymbol,expiry,valid_bars,n_signals,n_trades,open_trades,wins,gross_rs,"
        "charges_rs,slippage_rs,net_rs,qty,roll_adjustment,through_ts,error,updated_at FROM crudem_combo_daily "
        f"WHERE 1=1 {date_clause} ORDER BY trade_date DESC LIMIT 400", tuple(date_params))
    cols = [c[0] for c in cur.description]
    rows = [dict(zip(cols, r)) for r in cur.fetchall()]
    cur = conn.execute(
        "SELECT trade_date,seq,cid,direction,tradingsymbol,signal_ts,entry_ts,exit_ts,entry_price,exit_price,"
        "stop_price,target_price,stop_dist,r_multiple,points,gross_rs,charges_rs,slippage_rs,net_rs,status,"
        f"exit_reason,bars_held FROM crudem_combo_trades WHERE 1=1 {date_clause} "
        "ORDER BY trade_date DESC, seq DESC LIMIT 600", tuple(date_params))
    cols = [c[0] for c in cur.description]
    trades = [dict(zip(cols, r)) for r in cur.fetchall()]
    cur = conn.execute(
        f"SELECT outcome, COUNT(*) FROM crudem_combo_signals WHERE 1=1 {date_clause} GROUP BY outcome",
        tuple(date_params))
    outcomes = dict(cur.fetchall())
    if not rows:
        return rows, trades, {}
    closed = [t for t in trades if t["status"] == "closed"]
    wins = [t for t in closed if float(t["net_rs"] or 0) > 0]
    gross_win = sum(float(t["net_rs"]) for t in wins)
    gross_loss = -sum(float(t["net_rs"]) for t in closed if float(t["net_rs"] or 0) <= 0)
    equity = peak = max_dd = 0.0
    for t in reversed(closed):
        equity += float(t["net_rs"] or 0)
        peak = max(peak, equity)
        max_dd = max(max_dd, peak - equity)
    days = [r for r in rows if r["status"] in ("final", "live")]
    by_member = {}
    for t in closed:
        b = by_member.setdefault(t["cid"], {"cid": t["cid"], "direction": t["direction"], "n": 0, "wins": 0,
                                            "net_rs": 0.0, "rule": eng.MEMBER_BY_CID[t["cid"]].exit_desc})
        b["n"] += 1
        b["wins"] += float(t["net_rs"] or 0) > 0
        b["net_rs"] += float(t["net_rs"] or 0)
    order = [m.cid for m in eng.MEMBERS]
    stats = {
        "days": len(days), "traded_days": sum(1 for r in days if r["n_trades"]),
        "win_days": sum(1 for r in days if float(r["net_rs"] or 0) > 0),
        "trades": len(closed), "wins": len(wins),
        "win_pct": round(100 * len(wins) / max(len(closed), 1), 1),
        "open_trades": [t for t in trades if t["status"] == "open"],
        "gross_total": round(sum(float(t["gross_rs"] or 0) for t in closed), 2),
        "costs_total": round(sum(float(t["charges_rs"] or 0) + float(t["slippage_rs"] or 0) for t in closed), 2),
        "net_total": round(sum(float(t["net_rs"] or 0) for t in closed), 2),
        "pf": round(gross_win / gross_loss, 2) if gross_loss > 0 else None,
        "max_dd": round(max_dd, 2),
        "worst_day": round(min((float(r["net_rs"] or 0) for r in days), default=0.0), 2),
        "by_member": sorted(by_member.values(), key=lambda b: order.index(b["cid"])),
        "outcomes": outcomes, "paper_start": PAPER_START, "lots": LOTS, "dry": dry_run_data(),
        "first_date": rows[-1]["trade_date"], "last_date": rows[0]["trade_date"], "latest": rows[0],
    }
    return rows, trades, stats


if __name__ == "__main__":
    import json
    import sys
    print(json.dumps(run_day(sys.argv[1] if len(sys.argv) > 1 else None), indent=2, default=str))
