"""MCX GOLD short on a 5-minute CCI spike: paper book.

Paper only. This module never calls a broker order API.

The rule lives in live/engine/gold_cci_engine.py, an exact port of the rule Strategy Tester v2
suggested from its gold runs: short when 5-minute CCI(14) turns above 100 with 15-minute RSI(5)
above 50 and 1-hour DI- above DI+; stop 0.25%, stop to the entry price after a gain of one stop,
target three stops, flat at 23:29. One lot of GOLD (1 kg), one position at a time. Each run
replays the series up to the last completed minute and stores the session's trades and every
trigger with what became of it, so it is idempotent and a restart loses nothing.

Evidence status: the rule was FOUND on the sessions from 10 Mar to 25 Sep 2026. The book is
filled back to PAPER_START (1 Jun 2026) because that was asked for, but only the sessions from
FIRST_UNSEEN (28 Sep 2026) are ones the rule had not seen; the tab reports the two apart.
Nothing in the engine may be tuned from what this book shows.

Data: completed 1-minute Kite candles in the shared MCX candle cache
(labs/engine/crude_macd_st_tracker.py). The Tester ran the whole search on ONE contract,
GOLD26OCTFUT; Kite stopped serving it when it expired on 5 Oct 2026, so its candles to 25 Sep
(all the Tester held) ship with the code in labs/engine/seeds and are loaded into the cache once.
From 28 Sep the book trades the December contract from Kite, joined as the Tester joins contracts
(the price gap at the roll is added to the earlier prices), and each session is replayed on the
series as it stood that day, so a session's own contract is never adjusted.

Costs: MCX futures charges on the fill prices plus one tick (Rs 1) of slippage each side, the
assumption the back test used (about Rs 2,850 a round trip for one lot at Rs 1.5 lakh per 10 g).
"""
from __future__ import annotations

import sqlite3
from datetime import date, datetime, timedelta
from pathlib import Path

import pandas as pd

from labs.engine import crude_macd_st_tracker as mcx
from labs.engine.charges import mcx_futures_round_trip_charges
from live.engine import gold_cci_engine as eng
from storage.db import get_conn

IST = mcx.IST
STRATEGY_VERSION = "gold_cci14_5m_short_rsi5_15m_di7_1h_sl025_be1r_tp3r_v1"
UNDERLYING = "GOLD"
LOTS = 1
QTY = LOTS * eng.LOT_QTY
SLIPPAGE_TICKS = 1.0
PAPER_START = "2026-06-01"           # filled back to here on request
FIRST_UNSEEN = "2026-09-28"          # first session the rule was not found on
FINAL_AFTER = mcx.FINAL_AFTER
LOOKBACK_DAYS = 60                   # calendar days of warm-up replayed before a session
NEXT_CONTRACT_DAYS = 7               # start caching the next contract this many days before a roll
KITE_FROM = "2026-09-21"             # earlier sessions replay on the seeded contract alone: no Kite session
SEED_FILE = Path(__file__).resolve().parent / "seeds" / "gold26oct_1min_to_20260925.parquet"
# The October contract: real expiry 5 Oct 2026, but its candles end on 25 Sep, so the book rolls to
# December on 28 Sep (the date below is what the join treats as its expiry). No Kite token: it is
# never fetched, only read from the seed.
SEEDED = ({"tradingsymbol": "GOLD26OCTFUT", "instrument_token": None, "expiry": "2026-09-28"},)
_MASTER: dict = {"date": None, "rows": []}   # Kite's MCX instrument list, read once a day


class GoldCciInputError(RuntimeError):
    """The contracts or candles a session needs are unavailable."""


# ------------------------------------------------------------------ schema ---
def _ensure_tables(conn: sqlite3.Connection) -> None:
    mcx._ensure_tables(conn)                       # the shared minute-candle cache
    conn.executescript(
        """
        CREATE TABLE IF NOT EXISTS gold_cci_contracts (
            tradingsymbol    TEXT PRIMARY KEY,
            expiry           TEXT NOT NULL,
            instrument_token INTEGER
        );
        CREATE TABLE IF NOT EXISTS gold_cci_daily (
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
        CREATE TABLE IF NOT EXISTS gold_cci_trades (
            trade_date    TEXT NOT NULL,
            seq           INTEGER NOT NULL,
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
            stop_moved    INTEGER NOT NULL DEFAULT 0,
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
        CREATE TABLE IF NOT EXISTS gold_cci_signals (
            trade_date TEXT NOT NULL,
            signal_ts  TEXT PRIMARY KEY,
            outcome    TEXT NOT NULL,
            cci        REAL,
            rsi        REAL,
            plus_di    REAL,
            minus_di   REAL
        );
        """
    )
    conn.commit()


# --------------------------------------------------------------- contracts ---
def _as_date(value) -> date:
    return value if isinstance(value, date) and not isinstance(value, datetime) else date.fromisoformat(str(value)[:10])


def seed_cache(conn: sqlite3.Connection) -> int:
    """Load the October contract's candles into the shared cache, once. Returns rows written."""
    symbol = SEEDED[0]["tradingsymbol"]
    have = conn.execute("SELECT COUNT(*) FROM crude_minute_bars WHERE tradingsymbol=?", (symbol,)).fetchone()[0]
    if have or not SEED_FILE.is_file():
        return 0
    f = pd.read_parquet(SEED_FILE)
    ts = pd.to_datetime(f["ts"])
    rows = list(zip([symbol] * len(f), ts.dt.strftime("%Y-%m-%d %H:%M:%S"), f["open"].astype(float),
                    f["high"].astype(float), f["low"].astype(float), f["close"].astype(float), f["volume"].astype(float)))
    conn.executemany(
        "INSERT OR IGNORE INTO crude_minute_bars (tradingsymbol,ts,open,high,low,close,volume) VALUES (?,?,?,?,?,?,?)", rows)
    stamp = datetime.now(IST).isoformat(timespec="seconds")
    conn.executemany(
        "INSERT OR REPLACE INTO crude_minute_coverage (tradingsymbol,trade_date,fetched_at) VALUES (?,?,?)",
        [(symbol, d, stamp) for d in sorted(set(ts.dt.strftime("%Y-%m-%d")))])
    conn.commit()
    return len(rows)


def remember_contracts(conn: sqlite3.Connection, kite=None) -> list[dict]:
    """Every GOLD future the book knows: the seeded one and whatever Kite lists (its master forgets a
    contract once it expires, so they are kept here)."""
    rows = list(SEEDED)
    if kite is not None:
        today = datetime.now(IST).date()
        if _MASTER["date"] != today or not _MASTER["rows"]:
            _MASTER.update(date=today, rows=[
                {"tradingsymbol": r["tradingsymbol"], "instrument_token": int(r["instrument_token"]),
                 "expiry": _as_date(r["expiry"]).isoformat()}
                for r in kite.instruments("MCX")
                if r.get("name") == UNDERLYING and r.get("instrument_type") == "FUT"])
        rows += [r for r in _MASTER["rows"] if r["tradingsymbol"] != SEEDED[0]["tradingsymbol"]]
    conn.executemany(
        "INSERT INTO gold_cci_contracts (tradingsymbol, expiry, instrument_token) VALUES (?,?,?) "
        "ON CONFLICT(tradingsymbol) DO UPDATE SET expiry=excluded.expiry, "
        "instrument_token=COALESCE(excluded.instrument_token, instrument_token)",
        [(r["tradingsymbol"], r["expiry"], r.get("instrument_token")) for r in rows])
    conn.commit()
    cur = conn.execute("SELECT tradingsymbol, expiry, instrument_token FROM gold_cci_contracts ORDER BY expiry")
    return [{"tradingsymbol": s, "expiry": e, "instrument_token": t} for s, e, t in cur.fetchall()]


def session_frame(conn: sqlite3.Connection, session: date, now: datetime, kite=None) -> tuple[pd.DataFrame, list[dict]]:
    """The rolled 1-minute series a session is replayed on, as it stood on that session, and its
    roll table. The front contract on a day is the first one that expires after it."""
    seed_cache(conn)
    contracts = remember_contracts(conn, kite)
    start = session - timedelta(days=LOOKBACK_DAYS)
    relevant = [c for c in contracts if _as_date(c["expiry"]) >= start]
    ahead = [c for c in relevant if _as_date(c["expiry"]) > session]
    # the front contract, and from a week before its expiry the next one too: the next contract's
    # candles on the roll day set the adjustment, and it becomes the front on the expiry day
    wanted = ahead[:1]
    if len(ahead) > 1 and (_as_date(ahead[0]["expiry"]) - session).days <= NEXT_CONTRACT_DAYS:
        wanted = ahead[:2]
    for c in wanted:
        if kite is not None and c.get("instrument_token"):
            mcx.ensure_minute_bars(kite, c, start, session, now, conn)
    frames = []
    for c in relevant:
        frame = mcx.load_minute_bars(c["tradingsymbol"], start, session, conn)
        if len(frame):
            frames.append((c["tradingsymbol"], _as_date(c["expiry"]), frame))
    if not frames:
        raise GoldCciInputError(f"No cached {UNDERLYING} candles for {start}..{session}")
    return eng.stitch(frames, session)


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
            "seq": seq, "direction": "long" if long_ else "short",
            "signal_ts": str(t["signal_ts"]), "entry_ts": str(t["entry_ts"]),
            "exit_ts": None if t["exit_ts"] is None else str(t["exit_ts"]),
            "entry_price": float(t["entry_price"]), "exit_price": float(t["exit_price"]),
            "stop_price": float(t["stop_price"]), "target_price": float(t["target_price"]),
            "stop_dist": float(t["stop_dist"]), "stop_moved": int(bool(t["stop_moved"])),
            "points": float(t["points"]), "r_multiple": float(t["points"] / t["stop_dist"]),
            "gross_rs": round(gross, 2), "charges_rs": round(charges, 2), "slippage_rs": slippage,
            "net_rs": round(gross - charges - slippage, 2), "status": t["status"],
            "exit_reason": t["reason"], "bars_held": int(t["bars_held"]),
        })
    return out


def _persist(conn: sqlite3.Connection, trade_date: str, status: str, contract: dict | None, summary: dict,
             trades: list[dict], signals: list[dict], error: str | None = None) -> None:
    now = datetime.now(IST).isoformat(timespec="seconds")
    symbol = (contract or {}).get("contract") or ""
    conn.execute("DELETE FROM gold_cci_trades WHERE trade_date=?", (trade_date,))
    conn.executemany(
        "INSERT INTO gold_cci_trades (trade_date,seq,direction,tradingsymbol,signal_ts,entry_ts,exit_ts,"
        "entry_price,exit_price,stop_price,target_price,stop_dist,stop_moved,r_multiple,points,qty,gross_rs,"
        "charges_rs,slippage_rs,net_rs,status,exit_reason,bars_held) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        [(trade_date, t["seq"], t["direction"], symbol, t["signal_ts"], t["entry_ts"], t["exit_ts"],
          t["entry_price"], t["exit_price"], t["stop_price"], t["target_price"], t["stop_dist"], t["stop_moved"],
          t["r_multiple"], t["points"], QTY, t["gross_rs"], t["charges_rs"], t["slippage_rs"], t["net_rs"],
          t["status"], t["exit_reason"], t["bars_held"]) for t in trades])
    conn.execute("DELETE FROM gold_cci_signals WHERE trade_date=?", (trade_date,))
    conn.executemany(
        "INSERT OR REPLACE INTO gold_cci_signals (trade_date,signal_ts,outcome,cci,rsi,plus_di,minus_di) "
        "VALUES (?,?,?,?,?,?,?)",
        [(trade_date, str(s["signal_ts"]), s["outcome"], s.get("cci"), s.get("rsi"), s.get("plus_di"),
          s.get("minus_di")) for s in signals])
    closed = [t for t in trades if t["status"] == "closed"]
    conn.execute(
        "INSERT OR REPLACE INTO gold_cci_daily (trade_date,status,tradingsymbol,expiry,valid_bars,n_signals,"
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
            connection: sqlite3.Connection | None = None, offline: bool = False) -> dict:
    """Replay one session (to the last completed minute, for today) and store it. Idempotent.
    ``offline`` replays from the cache alone (no Kite session is opened)."""
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
        row = conn.execute("SELECT status FROM gold_cci_daily WHERE trade_date=?", (trade_date,)).fetchone()
        if row and row[0] in ("final", "no_session") and not rebuild:
            return {"trade_date": trade_date, "status": row[0], "skipped": True}
        if kite is None and not offline and trade_date >= KITE_FROM:
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
            "SELECT trade_date FROM gold_cci_daily WHERE status='live' AND trade_date<? ORDER BY trade_date",
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
            "SELECT trade_date FROM gold_cci_daily WHERE status IN ('final','no_session')")}
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
            break                                                 # keep the book in date order
    return {"done": completed, "errors": errors, "remaining": max(0, len(pending) - len(completed) - len(errors))}


# ---------------------------------------------------------------- dashboard ---
def _block(rows: list[dict], trades: list[dict]) -> dict:
    """Figures of one stretch of the book (rows = final/live sessions, trades = their closed trades)."""
    wins = [t for t in trades if float(t["net_rs"] or 0) > 0]
    gross_win = sum(float(t["net_rs"]) for t in wins)
    gross_loss = -sum(float(t["net_rs"]) for t in trades if float(t["net_rs"] or 0) <= 0)
    return {"days": len(rows), "trades": len(trades), "wins": len(wins),
            "net": round(sum(float(t["net_rs"] or 0) for t in trades), 2),
            "pf": round(gross_win / gross_loss, 2) if gross_loss > 0 else None,
            "win_days": sum(1 for r in rows if float(r["net_rs"] or 0) > 0),
            "loss_days": sum(1 for r in rows if float(r["net_rs"] or 0) < 0),
            "worst_day": round(min((float(r["net_rs"] or 0) for r in rows), default=0.0), 2)}


def tab_data(conn: sqlite3.Connection, date_clause: str = "", date_params=()) -> tuple:
    """(daily rows, trades, stats) for the /labs/live tab, newest first."""
    cur = conn.execute(
        "SELECT trade_date,status,tradingsymbol,expiry,valid_bars,n_signals,n_trades,open_trades,wins,gross_rs,"
        "charges_rs,slippage_rs,net_rs,qty,roll_adjustment,through_ts,error,updated_at FROM gold_cci_daily "
        f"WHERE 1=1 {date_clause} ORDER BY trade_date DESC LIMIT 400", tuple(date_params))
    cols = [c[0] for c in cur.description]
    rows = [dict(zip(cols, r)) for r in cur.fetchall()]
    cur = conn.execute(
        "SELECT trade_date,seq,direction,tradingsymbol,signal_ts,entry_ts,exit_ts,entry_price,exit_price,"
        "stop_price,target_price,stop_dist,stop_moved,r_multiple,points,gross_rs,charges_rs,slippage_rs,net_rs,"
        f"status,exit_reason,bars_held FROM gold_cci_trades WHERE 1=1 {date_clause} "
        "ORDER BY trade_date DESC, seq DESC LIMIT 800", tuple(date_params))
    cols = [c[0] for c in cur.description]
    trades = [dict(zip(cols, r)) for r in cur.fetchall()]
    cur = conn.execute(
        f"SELECT outcome, COUNT(*) FROM gold_cci_signals WHERE 1=1 {date_clause} GROUP BY outcome",
        tuple(date_params))
    outcomes = dict(cur.fetchall())
    if not rows:
        return rows, trades, {}
    closed = [t for t in trades if t["status"] == "closed"]
    days = [r for r in rows if r["status"] in ("final", "live")]
    equity = peak = max_dd = 0.0
    for t in reversed(closed):
        equity += float(t["net_rs"] or 0)
        peak = max(peak, equity)
        max_dd = max(max_dd, peak - equity)
    months: dict[str, dict] = {}
    for r in days:
        m = months.setdefault(r["trade_date"][:7], {"month": r["trade_date"][:7], "days": 0, "trades": 0,
                                                    "net_rs": 0.0, "win_days": 0, "loss_days": 0, "worst_day": 0.0})
        m["days"] += 1
        m["trades"] += int(r["n_trades"] or 0)
        m["net_rs"] += float(r["net_rs"] or 0)
        m["win_days"] += float(r["net_rs"] or 0) > 0
        m["loss_days"] += float(r["net_rs"] or 0) < 0
        m["worst_day"] = min(m["worst_day"], float(r["net_rs"] or 0))
    by_reason: dict[str, dict] = {}
    for t in closed:
        b = by_reason.setdefault(t["exit_reason"] or "?", {"reason": t["exit_reason"] or "?", "n": 0, "net_rs": 0.0})
        b["n"] += 1
        b["net_rs"] += float(t["net_rs"] or 0)
    whole = _block(days, closed)
    stats = {
        **whole, "win_pct": round(100 * whole["wins"] / max(whole["trades"], 1), 1),
        "traded_days": sum(1 for r in days if r["n_trades"]),
        "open_trades": [t for t in trades if t["status"] == "open"],
        "gross_total": round(sum(float(t["gross_rs"] or 0) for t in closed), 2),
        "costs_total": round(sum(float(t["charges_rs"] or 0) + float(t["slippage_rs"] or 0) for t in closed), 2),
        "net_total": whole["net"], "max_dd": round(max_dd, 2),
        "seen": _block([r for r in days if r["trade_date"] < FIRST_UNSEEN],
                       [t for t in closed if t["trade_date"] < FIRST_UNSEEN]),
        "unseen": _block([r for r in days if r["trade_date"] >= FIRST_UNSEEN],
                         [t for t in closed if t["trade_date"] >= FIRST_UNSEEN]),
        "months": [months[k] for k in sorted(months, reverse=True)],
        "by_reason": sorted(by_reason.values(), key=lambda b: -b["n"]),
        "outcomes": outcomes, "paper_start": PAPER_START, "first_unseen": FIRST_UNSEEN, "lots": LOTS, "qty": QTY,
        "exit_desc": eng.EXIT_DESC,
        "first_date": rows[-1]["trade_date"], "last_date": rows[0]["trade_date"], "latest": rows[0],
    }
    return rows, trades, stats


if __name__ == "__main__":
    import json
    import sys
    print(json.dumps(run_day(sys.argv[1] if len(sys.argv) > 1 else None), indent=2, default=str))
