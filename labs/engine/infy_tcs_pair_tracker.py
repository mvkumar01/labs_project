"""Infosys / TCS divergence pair: paper book.

Paper only. This module never calls a broker order API.

The rule (fixed; from alphaIMB research/experiments/2026-10-10_lead_lag_pairs,
walkforward_infy_tcs.py ``trades_for(df, 60)``), on daily bars of NSE:INFY and NSE:TCS:

  spread  ln(INFY close) - ln(TCS close)
  z       (spread - 60-day mean) / 60-day sd, the window including the day itself (sample sd)
  entry   flat and |z| >= 2 at a close -> at the NEXT day's open, equal rupees on each leg:
          z > 0 (Infosys rich) short INFY / long TCS; z < 0 long INFY / short TCS
  exit    at each close from the entry day on: z back through its average (z x entry z <= 0),
          or 20 trading days held -> both legs out at the NEXT day's open
  no stop-loss, one position at a time; the exit day's close can itself be a new signal
  costs   5 bps per leg per side = 20 bps a round trip

Evidence status: a lead, not a proven edge. Walk-forward on three years of daily data the test
period (Jan 2025 - Oct 2026) made +22% per rupee of one leg in 12 trades, but the 15 months
before it lost 7-9%, all the profit came from seven consecutive winners, and over the full three
years the average trade is not distinguishable from zero.

What this book does and does not model:
  - cash share prices on both legs, fractional shares, NOTIONAL rupees a leg. A real version
    needs stock futures on both legs (a cash short cannot be carried overnight); lot sizes, the
    futures basis and roll costs are not modelled;
  - Kite daily candles as served. Whether they are adjusted for dividends is not confirmed: an
    ex-dividend drop would look like a spread move;
  - a signal is read only from a completed day's close; today's open is used the moment Kite
    shows it. An open trade is marked at the latest price.

The whole ledger is recomputed from the cached candles on every run, so it is idempotent.
"""
from __future__ import annotations

import sqlite3
from datetime import date, datetime, time, timedelta, timezone

import numpy as np
import pandas as pd

from storage.db import get_conn

IST = timezone(timedelta(hours=5, minutes=30))
STRATEGY_VERSION = "infy_tcs_pair_z60_e2_x0_h20_v1"
LEG_A, LEG_B = "INFY", "TCS"
EXCHANGE = "NSE"
WINDOW, ENTRY_Z, MAX_HOLD = 60, 2.0, 20
COST_BPS_PER_LEG_SIDE = 5.0
ROUND_TRIP_COST = 4 * COST_BPS_PER_LEG_SIDE / 1e4
NOTIONAL = 1_000_000.0               # paper rupees on each leg
PAPER_START = "2026-06-01"           # trades entered before this are warm-up, not recorded
FIRST_UNSEEN = "2026-10-12"          # first session after the research data (to 9 Oct 2026)
HISTORY_START = "2025-10-01"         # candles from here: the z window and the position carried into June
CLOSE_FINAL_AFTER = time(15, 35)
REFRESH_MINUTES = 15


class PairInputError(RuntimeError):
    """The daily candles the book needs are unavailable."""


# ------------------------------------------------------------------- rule ---
def zscore(frame: pd.DataFrame) -> pd.Series:
    spread = np.log(frame["a_close"]) - np.log(frame["b_close"])
    return (spread - spread.rolling(WINDOW).mean()) / spread.rolling(WINDOW).std()


def replay(frame: pd.DataFrame) -> dict:
    """Run the rule over daily bars (index = date; a_open, a_close, b_open, b_close).

    A row may have no close yet (today, before the close): it can fill an order at its open but
    gives no signal. Returns ``{"z", "trades", "pending"}``: z per day; every trade (closed, or
    open and marked at the last price); and the order due at the next open, if any."""
    z = zscore(frame)
    zv, idx = z.to_numpy(), frame.index
    ao, bo = frame["a_open"].to_numpy(), frame["b_open"].to_numpy()
    ac, bc = frame["a_close"].to_numpy(), frame["b_close"].to_numpy()
    n = len(frame)
    trades, pending, i = [], None, WINDOW          # the reference starts at the first full window + 1

    def has_open(k):
        return k < n and np.isfinite(ao[k]) and np.isfinite(bo[k])

    def signal_ok(k):
        return np.isfinite(zv[k])

    while i < n:
        if not signal_ok(i) or abs(zv[i]) < ENTRY_Z:
            i += 1
            continue
        side = -float(np.sign(zv[i]))                    # +1 = long INFY / short TCS
        entry = i + 1
        base = {"signal_date": idx[i], "z": float(zv[i]), "side": side, "long": LEG_A if side > 0 else LEG_B,
                "short": LEG_B if side > 0 else LEG_A}
        if not has_open(entry):
            pending = {**base, "action": "enter"}
            break
        j, reason = entry, None
        while j < n and signal_ok(j):
            if zv[j] * zv[i] <= 0:
                reason = "back to average"
                break
            if j - entry + 1 >= MAX_HOLD:
                reason = "20-day limit"
                break
            j += 1
        row = {**base, "entry_date": idx[entry], "a_entry": float(ao[entry]), "b_entry": float(bo[entry]),
               "entry_i": entry}
        exit_ = j + 1
        if reason is not None and has_open(exit_):
            gross = side * ((ao[exit_] / ao[entry] - 1) - (bo[exit_] / bo[entry] - 1))
            trades.append({**row, "status": "closed", "exit_date": idx[exit_], "a_exit": float(ao[exit_]),
                           "b_exit": float(bo[exit_]), "days": exit_ - entry, "gross": float(gross),
                           "net": float(gross - ROUND_TRIP_COST), "reason": reason, "exit_signal_date": idx[j],
                           "exit_z": float(zv[j])})
            i = exit_                                    # the exit day's close can be a new signal
            continue
        # still open: mark at the last price known (a close, else that day's open)
        last = n - 1
        a_mark = ac[last] if np.isfinite(ac[last]) else ao[last]
        b_mark = bc[last] if np.isfinite(bc[last]) else bo[last]
        gross = side * ((a_mark / ao[entry] - 1) - (b_mark / bo[entry] - 1))
        trades.append({**row, "status": "open", "exit_date": None, "a_exit": float(a_mark), "b_exit": float(b_mark),
                       "days": last - entry, "gross": float(gross), "net": float(gross - ROUND_TRIP_COST),
                       "reason": reason, "mark_date": idx[last],
                       "exit_signal_date": idx[j] if reason else None, "exit_z": float(zv[j]) if reason else None})
        if reason is not None:
            pending = {**base, "action": "exit", "reason": reason}
        break
    return {"z": z, "trades": trades, "pending": pending}


# ------------------------------------------------------------------ schema ---
def _ensure_tables(conn: sqlite3.Connection) -> None:
    conn.executescript(
        """
        CREATE TABLE IF NOT EXISTS pair_daily_bars (
            symbol TEXT NOT NULL, trade_date TEXT NOT NULL, open REAL, high REAL, low REAL, close REAL,
            volume REAL, final INTEGER NOT NULL DEFAULT 0, fetched_at TEXT NOT NULL,
            PRIMARY KEY (symbol, trade_date)
        );
        CREATE TABLE IF NOT EXISTS infy_tcs_pair_days (
            trade_date TEXT PRIMARY KEY, a_open REAL, a_close REAL, b_open REAL, b_close REAL, final INTEGER NOT NULL,
            spread REAL, z REAL, position TEXT, event TEXT, mtm_rs REAL, updated_at TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS infy_tcs_pair_trades (
            entry_date TEXT PRIMARY KEY, signal_date TEXT NOT NULL, signal_z REAL NOT NULL, long_leg TEXT NOT NULL,
            short_leg TEXT NOT NULL, a_entry REAL NOT NULL, b_entry REAL NOT NULL, exit_date TEXT, a_exit REAL,
            b_exit REAL, exit_signal_date TEXT, exit_z REAL, days INTEGER, gross_pct REAL, net_pct REAL,
            gross_rs REAL, costs_rs REAL, net_rs REAL, status TEXT NOT NULL, exit_reason TEXT, notional REAL NOT NULL,
            strategy_version TEXT NOT NULL, updated_at TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS infy_tcs_pair_state (
            k TEXT PRIMARY KEY, v TEXT
        );
        """
    )
    conn.commit()


# -------------------------------------------------------------------- data ---
def _naive(now: datetime | None) -> datetime:
    now = now or datetime.now(IST)
    return now.astimezone(IST).replace(tzinfo=None) if now.tzinfo is not None else now


def fetch_bars(kite, conn: sqlite3.Connection, now: datetime, start: str | None = None) -> int:
    """Pull daily candles for both legs from ``start`` (default: a week before the last stored
    final day) to today, and store them. A day is final once it is past, or after 15:35."""
    today = now.date()
    last = conn.execute("SELECT MAX(trade_date) FROM pair_daily_bars WHERE final=1").fetchone()[0]
    frm = date.fromisoformat(start) if start else (
        date.fromisoformat(last) - timedelta(days=7) if last else date.fromisoformat(HISTORY_START))
    keys = [f"{EXCHANGE}:{LEG_A}", f"{EXCHANGE}:{LEG_B}"]
    quote = kite.ltp(keys)
    stamp, n = datetime.now(IST).isoformat(timespec="seconds"), 0
    for symbol, key in zip((LEG_A, LEG_B), keys):
        token = int(quote[key]["instrument_token"])
        rows = kite.historical_data(token, datetime.combine(frm, time(0, 0)), datetime.combine(today, time(23, 59)), "day")
        out = []
        for c in rows:
            d = pd.Timestamp(c["date"]).date()
            final = int(d < today or now.time() >= CLOSE_FINAL_AFTER)
            out.append((symbol, d.isoformat(), float(c["open"]), float(c["high"]), float(c["low"]), float(c["close"]),
                        float(c.get("volume") or 0), final, stamp))
        conn.executemany(
            "INSERT INTO pair_daily_bars (symbol,trade_date,open,high,low,close,volume,final,fetched_at) "
            "VALUES (?,?,?,?,?,?,?,?,?) ON CONFLICT(symbol,trade_date) DO UPDATE SET open=excluded.open,"
            "high=excluded.high,low=excluded.low,close=excluded.close,volume=excluded.volume,final=excluded.final,"
            "fetched_at=excluded.fetched_at", out)
        n += len(out)
    conn.execute("INSERT OR REPLACE INTO infy_tcs_pair_state (k, v) VALUES ('fetched_at', ?)", (now.isoformat(timespec="seconds"),))
    conn.commit()
    return n


def load_frame(conn: sqlite3.Connection) -> pd.DataFrame:
    """Both legs side by side by date. ``final`` = both closes are final; a day that is not final
    keeps its opens (orders fill there) and its latest price in a_last / b_last, with no close."""
    f = pd.read_sql_query(
        "SELECT symbol, trade_date, open, close, final FROM pair_daily_bars WHERE trade_date >= ? ORDER BY trade_date",
        conn, params=(HISTORY_START,))
    if f.empty:
        raise PairInputError("no daily candles stored for the pair")
    a = f[f.symbol == LEG_A].set_index("trade_date")
    b = f[f.symbol == LEG_B].set_index("trade_date")
    df = pd.DataFrame({"a_open": a.open, "a_last": a.close, "b_open": b.open, "b_last": b.close,
                       "final": (a.final.astype(bool) & b.final.astype(bool))}).dropna(subset=["a_open", "b_open"])
    df["final"] = df["final"].fillna(False).astype(bool)
    df["a_close"] = df["a_last"].where(df["final"])
    df["b_close"] = df["b_last"].where(df["final"])
    return df


# ------------------------------------------------------------------ ledger ---
def rebuild(conn: sqlite3.Connection) -> dict:
    """Recompute the ledger and the day table from the stored candles."""
    _ensure_tables(conn)
    df = load_frame(conn)
    out = replay(df[["a_open", "a_close", "b_open", "b_close"]])
    stamp = datetime.now(IST).isoformat(timespec="seconds")
    trades = [t for t in out["trades"] if str(t["entry_date"]) >= PAPER_START]
    last = df.index[-1]
    for t in trades:
        if t["status"] == "open" and not bool(df.at[last, "final"]):       # mark at the latest traded price
            t["a_exit"], t["b_exit"] = float(df.at[last, "a_last"]), float(df.at[last, "b_last"])
            t["gross"] = t["side"] * ((t["a_exit"] / t["a_entry"] - 1) - (t["b_exit"] / t["b_entry"] - 1))
            t["net"] = t["gross"] - ROUND_TRIP_COST
    conn.execute("DELETE FROM infy_tcs_pair_trades")
    conn.executemany(
        "INSERT INTO infy_tcs_pair_trades (entry_date,signal_date,signal_z,long_leg,short_leg,a_entry,b_entry,exit_date,"
        "a_exit,b_exit,exit_signal_date,exit_z,days,gross_pct,net_pct,gross_rs,costs_rs,net_rs,status,exit_reason,"
        "notional,strategy_version,updated_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        [(str(t["entry_date"]), str(t["signal_date"]), round(t["z"], 4), t["long"], t["short"], t["a_entry"], t["b_entry"],
          str(t["exit_date"]) if t["exit_date"] else None, t["a_exit"], t["b_exit"],
          str(t["exit_signal_date"]) if t.get("exit_signal_date") else None, t.get("exit_z"), int(t["days"]),
          round(100 * t["gross"], 4), round(100 * t["net"], 4), round(t["gross"] * NOTIONAL, 2),
          round(ROUND_TRIP_COST * NOTIONAL, 2), round(t["net"] * NOTIONAL, 2), t["status"], t.get("reason"),
          NOTIONAL, STRATEGY_VERSION, stamp) for t in trades])
    z = out["z"]
    spread = np.log(df["a_close"]) - np.log(df["b_close"])
    rows = []
    for d in df.index[df.index >= PAPER_START]:
        held = next((t for t in out["trades"] if str(t["entry_date"]) <= d and
                     (t["exit_date"] is None or d < str(t["exit_date"]))), None)
        events = [f"enter (long {t['long']})" for t in out["trades"] if str(t["entry_date"]) == d] + \
                 [f"exit ({t['reason']})" for t in out["trades"] if t["exit_date"] is not None and str(t["exit_date"]) == d] + \
                 [f"signal z {t['z']:+.2f}" for t in out["trades"] if str(t["signal_date"]) == d]
        if out["pending"] and str(out["pending"]["signal_date"]) == d and out["pending"]["action"] == "enter":
            events.append(f"signal z {out['pending']['z']:+.2f}")
        rows.append((d, float(df.at[d, "a_open"]), _f(df.at[d, "a_close"]), float(df.at[d, "b_open"]), _f(df.at[d, "b_close"]),
                     int(df.at[d, "final"]), _f(spread.get(d)), _f(z.get(d)),
                     f"long {held['long']} / short {held['short']}" if held else "flat", "; ".join(events) or None,
                     None, stamp))
    conn.execute("DELETE FROM infy_tcs_pair_days")
    conn.executemany("INSERT INTO infy_tcs_pair_days (trade_date,a_open,a_close,b_open,b_close,final,spread,z,position,"
                     "event,mtm_rs,updated_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)", rows)
    p = out["pending"]
    note = None
    if p:
        note = (f"enter at the next open: long {p['long']} / short {p['short']} (z {p['z']:+.2f} at the close of {p['signal_date']})"
                if p["action"] == "enter" else f"exit at the next open: {p['reason']}")
    conn.execute("INSERT OR REPLACE INTO infy_tcs_pair_state (k, v) VALUES ('pending', ?)", (note,))
    conn.commit()
    closed = [t for t in trades if t["status"] == "closed"]
    return {"through": last, "final": bool(df.at[last, "final"]), "trades": len(trades), "closed": len(closed),
            "open": len(trades) - len(closed), "net_rs": round(sum(t["net"] for t in closed) * NOTIONAL, 2),
            "z": _f(z.dropna().iloc[-1]) if z.notna().any() else None, "pending": note}


def _f(v):
    return None if v is None or (isinstance(v, float) and not np.isfinite(v)) or pd.isna(v) else float(v)


# --------------------------------------------------------------------- run ---
def _due(conn: sqlite3.Connection, now: datetime) -> bool:
    """Is a fetch due? Before the first run, when today's open is missing, when today's close has
    just become final, or every REFRESH_MINUTES while a trade is open or an order is pending."""
    today = now.date().isoformat()
    row = conn.execute("SELECT v FROM infy_tcs_pair_state WHERE k='fetched_at'").fetchone()
    if not row or not row[0]:
        return True
    last_fetch = datetime.fromisoformat(row[0])
    if last_fetch.date() != now.date():
        return True
    if now.weekday() >= 5 or now.time() < time(9, 16):
        return False
    have = dict(conn.execute("SELECT symbol, final FROM pair_daily_bars WHERE trade_date=?", (today,)).fetchall())
    if len(have) < 2:
        return (now - last_fetch) >= timedelta(minutes=2)
    if now.time() >= CLOSE_FINAL_AFTER and not all(have.values()):
        return True
    busy = conn.execute("SELECT COUNT(*) FROM infy_tcs_pair_trades WHERE status='open'").fetchone()[0] or \
        (conn.execute("SELECT v FROM infy_tcs_pair_state WHERE k='pending'").fetchone() or [None])[0]
    return bool(busy) and now.time() < CLOSE_FINAL_AFTER and (now - last_fetch) >= timedelta(minutes=REFRESH_MINUTES)


def run_day(trade_date: str | None = None, *, kite=None, now: datetime | None = None, force: bool = False,
            connection: sqlite3.Connection | None = None) -> dict:
    """The paper loop's entry point (the date argument is ignored: the ledger is always rebuilt
    whole). Fetches candles only when due, then recomputes."""
    now = _naive(now)
    own = connection is None
    conn = connection or get_conn()
    _ensure_tables(conn)
    try:
        if force or _due(conn, now):
            if kite is None:
                from auth.session_manager import get_kite
                kite = get_kite()
            fetch_bars(kite, conn, now)
        return rebuild(conn)
    finally:
        if own:
            conn.close()


# ---------------------------------------------------------------- dashboard ---
def tab_data(conn: sqlite3.Connection, date_clause: str = "", date_params=()) -> tuple:
    """(day rows, trades, stats) for the /labs/live tab, newest first."""
    cur = conn.execute(
        "SELECT trade_date,a_open,a_close,b_open,b_close,final,spread,z,position,event,updated_at FROM infy_tcs_pair_days "
        f"WHERE 1=1 {date_clause} ORDER BY trade_date DESC LIMIT 400", tuple(date_params))
    cols = [c[0] for c in cur.description]
    rows = [dict(zip(cols, r)) for r in cur.fetchall()]
    cur = conn.execute("SELECT * FROM infy_tcs_pair_trades ORDER BY entry_date DESC")
    cols = [c[0] for c in cur.description]
    trades = [dict(zip(cols, r)) for r in cur.fetchall()]
    if not rows:
        return rows, trades, {}
    closed = [t for t in trades if t["status"] == "closed"]
    wins = [t for t in closed if float(t["net_rs"] or 0) > 0]
    pending = (conn.execute("SELECT v FROM infy_tcs_pair_state WHERE k='pending'").fetchone() or [None])[0]
    latest_z = next((r for r in rows if r["z"] is not None), None)
    stats = {
        "trades": len(closed), "wins": len(wins), "open_trades": [t for t in trades if t["status"] == "open"],
        "net_total": round(sum(float(t["net_rs"] or 0) for t in closed), 2),
        "net_pct_total": round(sum(float(t["net_pct"] or 0) for t in closed), 2),
        "worst_trade_pct": round(min((float(t["net_pct"] or 0) for t in closed), default=0.0), 2),
        "unseen_trades": sum(1 for t in closed if t["entry_date"] >= FIRST_UNSEEN),
        "unseen_net": round(sum(float(t["net_rs"] or 0) for t in closed if t["entry_date"] >= FIRST_UNSEEN), 2),
        "pending": pending, "latest": rows[0], "latest_z": latest_z, "notional": NOTIONAL,
        "paper_start": PAPER_START, "first_unseen": FIRST_UNSEEN, "window": WINDOW, "entry_z": ENTRY_Z,
        "max_hold": MAX_HOLD, "cost_pct": round(100 * ROUND_TRIP_COST, 2),
        "days_in_trade": sum(1 for r in rows if r["position"] and r["position"] != "flat"), "days": len(rows),
    }
    return rows, trades, stats


if __name__ == "__main__":
    import json
    print(json.dumps(run_day(force=True), indent=2, default=str))
