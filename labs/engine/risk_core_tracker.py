"""Risk core (trend x vol): paper book over five markets, daily.

Paper only. This module never calls a broker order API.

The rule and costs are in labs/engine/risk_core_engine.py, the inputs in labs/engine/risk_core_data.py.
Rs 10,00,000 notional in five equal sleeves of Rs 2,00,000 (NIFTY 50, NIFTY BANK, BTCUSDT, ETHUSDT, MCX
CRUDEOIL), rebalanced to equal on the first day of each month, booked from 2020-07-01 on a calendar-day
index. The whole ledger is recomputed from the inputs after every fetch, so it is idempotent; the book
runs through the last day every sleeve has a close for (the Binance archive is about two days late).

Periods: 'research' up to RESEARCH_END (the V2 Strategy Tester's study; its own run stopped crypto at
2026-06-18), 'forward' from FORWARD_FROM. Stated rule: the protection is BROKEN if the forward worst fall
goes beyond BROKEN_DD (-15%, about three times the research worst fall of -5.3%).
"""
from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timedelta, timezone

import numpy as np
import pandas as pd

from labs.engine import risk_core_data as data
from labs.engine import risk_core_engine as rc
from storage.db import get_conn

IST = timezone(timedelta(hours=5, minutes=30))
STRATEGY_VERSION = "risk_core_trend_vol_v1"
CAPITAL = 1_000_000.0
START = "2020-07-01"
RESEARCH_END = "2026-10-15"
FORWARD_FROM = "2026-10-16"
BROKEN_DD = -0.15
CASH_RATE = 0.06                    # the Sharpe is measured above 6% cash, as in the research
REFRESH = timedelta(hours=3)
LABELS = {"nifty50": "NIFTY 50", "banknifty": "BANK NIFTY", "btcusdt": "BTC", "ethusdt": "ETH", "crudeoil": "CRUDE"}


def _ensure_tables(conn: sqlite3.Connection) -> None:
    data.ensure_tables(conn)
    cols = ", ".join(f"{k}_held REAL, {k}_value REAL" for k in rc.SLEEVES)
    conn.execute(
        f"""CREATE TABLE IF NOT EXISTS risk_core_days (
            trade_date TEXT PRIMARY KEY, period TEXT NOT NULL,
            book_return REAL NOT NULL, book_value REAL NOT NULL, net_rs REAL NOT NULL, {cols})""")


def _state(conn, k: str, v=None):
    if v is None:
        row = conn.execute("SELECT v FROM risk_core_state WHERE k=?", (k,)).fetchone()
        return row[0] if row else None
    conn.execute("INSERT OR REPLACE INTO risk_core_state (k, v) VALUES (?, ?)", (k, v))


def rebuild(conn: sqlite3.Connection) -> dict:
    """Recompute the whole ledger from the inputs and store it."""
    bars, funding, usd_inr, info = data.load_inputs(conn)
    res = rc.run(bars, funding, usd_inr, START, rc.COSTS, capital=CAPITAL)
    b = res["book"]
    held = pd.DataFrame({k: f["held"] for k, f in res["sleeves"].items()}).reindex(b.index).ffill().fillna(0.0)
    value = b["book_value"]
    net = value.diff().fillna(value.iloc[0] - CAPITAL)
    rows = []
    for ts in b.index:
        d = ts.date().isoformat()
        rows.append((d, "research" if d <= RESEARCH_END else "forward", float(b.at[ts, "book_return"]), float(value[ts]),
                     float(net[ts]), *[x for k in rc.SLEEVES for x in (float(held.at[ts, k]), float(b.at[ts, f"{k}_value"]))]))
    conn.execute("DELETE FROM risk_core_days")
    marks = ",".join("?" * (5 + 2 * len(rc.SLEEVES)))
    conn.executemany(f"INSERT INTO risk_core_days VALUES ({marks})", rows)
    today = {k: {"held": float(f["held"].iloc[-1]), "as_of": f.index[-1].date().isoformat(),
                 "target": float(f["target"].iloc[-1]) if np.isfinite(f["target"].iloc[-1]) else 0.0,
                 "trend": float(f["trend"].iloc[-1]) if np.isfinite(f["trend"].iloc[-1]) else None,
                 "vol": float(f["vol"].iloc[-1])} for k, f in res["sleeves"].items()}
    _state(conn, "today", json.dumps(today))
    _state(conn, "inputs", json.dumps(info))
    _state(conn, "rebuilt_at", datetime.now(IST).isoformat(timespec="seconds"))
    conn.commit()
    return {"days": len(rows), "through": rows[-1][0] if rows else None, "book_value": rows[-1][3] if rows else None}


def fetch(conn: sqlite3.Connection, now: datetime, kite=None) -> dict:
    """Every feed, each on its own: one failing does not stop the others (the book then runs through the
    last day all sleeves have)."""
    out = {}
    try:
        if kite is None:
            from auth.session_manager import get_kite
            kite = get_kite()
        out["kite"] = data.update_kite(conn, kite, now)
    except Exception as exc:
        out["kite"] = f"error: {type(exc).__name__}: {exc}"
    for name, fn in (("binance", data.update_binance), ("fx", data.update_fx)):
        try:
            out[name] = fn(conn, now)
        except Exception as exc:
            out[name] = f"error: {type(exc).__name__}: {exc}"
    _state(conn, "fetched_at", now.isoformat(timespec="seconds"))
    _state(conn, "last_fetch", json.dumps(out, default=str))
    conn.commit()
    return out


def run_day(trade_date: str | None = None, *, kite=None, now: datetime | None = None, force: bool = False,
            connection: sqlite3.Connection | None = None) -> dict:
    """The paper loop's entry point (the date is ignored: the ledger is always rebuilt whole). Fetches at
    most every REFRESH, rebuilds after a fetch or when the ledger is empty."""
    now = (now or datetime.now(IST)).replace(tzinfo=None)
    own = connection is None
    conn = connection or get_conn()
    _ensure_tables(conn)
    try:
        last = _state(conn, "fetched_at")
        fetched = None
        if force or not last or now - datetime.fromisoformat(last) >= REFRESH:
            fetched = fetch(conn, now, kite)
        empty = not conn.execute("SELECT 1 FROM risk_core_days LIMIT 1").fetchone()
        if fetched is None and not empty:
            return {"status": "up to date"}
        return {**rebuild(conn), "fetched": fetched}
    finally:
        if own:
            conn.close()


# ----------------------------------------------------------------- figures ---
def _worst_fall(r: pd.Series) -> float:
    """Largest fall from a peak, counting the starting value (the forward period's fall from the book's
    value at the research period's last close)."""
    if r.empty:
        return 0.0
    eq = np.concatenate([[1.0], (1 + r).cumprod().to_numpy()])
    return float(min(0.0, (eq / np.maximum.accumulate(eq) - 1).min()))


def summary(conn: sqlite3.Connection) -> dict:
    """Research / forward figures, the rolling two-year Sharpe and the protection status ({} before the
    first build)."""
    if not conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='risk_core_days'").fetchone():
        return {}
    d = pd.read_sql_query("SELECT * FROM risk_core_days ORDER BY trade_date", conn)
    if d.empty:
        return {}
    r = pd.Series(d.book_return.to_numpy(), index=pd.to_datetime(d.trade_date))
    res, fwd = r[r.index <= RESEARCH_END], r[r.index >= FORWARD_FROM]
    s = rc.stats(res, 365, CASH_RATE)
    yr = (1 + res).groupby(res.index.year).prod() - 1
    last2 = r[r.index > r.index.max() - pd.Timedelta(days=730)]
    roll = (last2.mean() - CASH_RATE / 365) / last2.std() * np.sqrt(365) if last2.std() > 0 else float("nan")
    fwd_dd = _worst_fall(fwd)
    value_at = lambda day: float(d[d.trade_date <= day].book_value.iloc[-1]) if (d.trade_date <= day).any() else CAPITAL
    today = json.loads(_state(conn, "today") or "{}")
    inputs = json.loads(_state(conn, "inputs") or "{}")
    return {
        "capital": CAPITAL, "through": d.trade_date.iloc[-1], "book_value": float(d.book_value.iloc[-1]),
        "net_total": float(d.book_value.iloc[-1] - CAPITAL),
        "research": {"from": START, "to": min(RESEARCH_END, d.trade_date.iloc[-1]), "net_rs": value_at(RESEARCH_END) - CAPITAL,
                     "cagr": s.get("cagr"), "sharpe": s.get("sharpe"), "worst_fall": s.get("max_dd"),
                     "worst_year": float(yr.min()) if len(yr) else None, "years_up": int((yr > 0).sum()), "years": len(yr)},
        "forward": {"from": FORWARD_FROM, "days": len(fwd), "net_rs": float(d.book_value.iloc[-1] - value_at(RESEARCH_END)) if len(fwd) else 0.0,
                    "worst_fall": fwd_dd, "return": float((1 + fwd).prod() - 1) if len(fwd) else 0.0},
        "rolling_2y_sharpe": float(roll), "broken": fwd_dd < BROKEN_DD, "broken_at": BROKEN_DD,
        "status": "BROKEN" if fwd_dd < BROKEN_DD else "holding",
        "sleeves": [{"key": k, "label": LABELS[k], **today.get(k, {}), "value": float(d[f"{k}_value"].iloc[-1])} for k in rc.SLEEVES],
        "inputs": inputs, "fetched_at": _state(conn, "fetched_at"), "last_fetch": _state(conn, "last_fetch"),
    }


def tab_data(conn: sqlite3.Connection, date_clause: str = "", date_params=()) -> tuple:
    """(month rows newest first, year rows, stats) for the /labs/live tab."""
    _ensure_tables(conn)
    d = pd.read_sql_query("SELECT trade_date, period, book_return, book_value, net_rs FROM risk_core_days ORDER BY trade_date", conn)
    if d.empty:
        return [], [], {}
    d["month"] = d.trade_date.str[:7]
    months = []
    for m, g in d.groupby("month"):
        eq = (1 + g.book_return).cumprod()
        months.append({"month": m, "period": "forward" if g.trade_date.max() >= FORWARD_FROM and g.trade_date.min() >= FORWARD_FROM
                       else ("mixed" if g.trade_date.max() >= FORWARD_FROM else "research"),
                       "net_rs": float(g.net_rs.sum()), "ret": float(eq.iloc[-1] - 1),
                       "worst_fall": float(min(0.0, (eq / eq.cummax() - 1).min())), "value": float(g.book_value.iloc[-1])})
    d["year"] = d.trade_date.str[:4]
    years = [{"year": y, "net_rs": float(g.net_rs.sum()), "ret": float((1 + g.book_return).prod() - 1)} for y, g in d.groupby("year")]
    return months[::-1], years[::-1], summary(conn)
