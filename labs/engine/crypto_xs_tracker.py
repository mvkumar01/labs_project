"""Crypto cross-sectional taker-imbalance book: paper ledger.

Paper only. This module never calls an exchange order API (it has no exchange credentials at all;
it reads Binance's public data archive).

The rule is in crypto_xs_engine.py and is fixed: each day at 00:00 UTC rank the 50 most liquid
Binance USDT perpetuals by their last 24 hours' taker buy/sell imbalance, hold a dollar-neutral
book (long the most bought, short the most sold) that is the average of the last 7 daily targets,
gross exposure 1, traded at the 01:00 price. Handed over from the Crypto_Analysis research
project (run XS_FACTORS_20261009_V1, spec S05_taker1 / hold 7 / direction +1).

Two periods, kept apart and never pooled:
  1 Jun - 31 Aug 2026   the stretch the research backtest covers. This ledger must reproduce it
                        (June +2.30%, July -9.61%, August to the 30th +1.04%, compounded).
  1 Sep 2026 onward     not seen by the research. No parameter may be tuned on it.

What the ledger can and cannot know (see crypto_xs_data.py):
  - it runs about two days behind: a day's result needs the next day's 01:00 price, and the
    archive publishes a day's bars roughly a day after it ends;
  - funding for a month is ESTIMATED until Binance publishes that month's file, then replaced
    by the actual rates; each day says which it carries;
  - whether a contract was tradable at the rebalance is judged from the bar itself (a price and
    traded volume in the hour to 01:00), not from the exchange's status flag.

The whole ledger is recomputed from the stored panels whenever new data arrives (idempotent).
A day's row changes afterwards only if late data arrives: a contract's file published late, or
estimated funding giving way to the actual rates.
"""
from __future__ import annotations

import sqlite3
import threading
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

import numpy as np
import pandas as pd

from labs.engine import crypto_xs_data as data
from labs.engine import crypto_xs_engine as engine
from storage.db import get_conn

STRATEGY_VERSION = "crypto_xs_taker24_top50_h7_v1"
PAPER_START = "2026-06-01"
FIRST_UNSEEN = "2026-09-01"           # the research panels end 31 Aug 2026
EQUITY_START = 100_000.0              # paper dollars; gross exposure = equity
CONTRIBUTION_FLAG = 0.03              # one contract moving the book this much of equity in a day
CHECK_MINUTES = 30
REFERENCE = {"2026-06": 2.30, "2026-07": -9.61, "2026-08": 1.04}      # research backtest, % compounded
REFERENCE_LAST_DAY = "2026-08-30"     # its last complete hold (the panels stop at 1 Sep 00:00)
RECONCILE_TOLERANCE = 0.05            # percentage points a month


# ------------------------------------------------------------------ schema ---
def _ensure_tables(conn: sqlite3.Connection) -> None:
    conn.executescript(
        """
        CREATE TABLE IF NOT EXISTS crypto_xs_daily (
            decision_date TEXT PRIMARY KEY, complete INTEGER NOT NULL, n_eligible INTEGER, n_held INTEGER,
            exposure REAL, long_exposure REAL, turnover REAL, gross REAL, funding REAL, cost REAL, net REAL,
            equity REAL, funding_source TEXT, top_symbol TEXT, top_contribution REAL, flags TEXT,
            strategy_version TEXT NOT NULL, updated_at TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS crypto_xs_weights (
            decision_date TEXT NOT NULL, symbol TEXT NOT NULL, weight REAL NOT NULL, target REAL, signal REAL,
            entry_price REAL, exit_price REAL, price_return REAL, funding_rate REAL, price_pnl REAL,
            funding_pnl REAL, traded REAL, tradable INTEGER, ended INTEGER,
            PRIMARY KEY (decision_date, symbol)
        );
        CREATE TABLE IF NOT EXISTS crypto_xs_state (k TEXT PRIMARY KEY, v TEXT);
        """
    )
    conn.commit()


def _state(conn: sqlite3.Connection, key: str) -> str | None:
    row = conn.execute("SELECT v FROM crypto_xs_state WHERE k=?", (key,)).fetchone()
    return row[0] if row else None


def _set_state(conn: sqlite3.Connection, **values) -> None:
    conn.executemany("INSERT OR REPLACE INTO crypto_xs_state (k, v) VALUES (?, ?)",
                     [(k, None if v is None else str(v)) for k, v in values.items()])
    conn.commit()


# ----------------------------------------------------------------- compute ---
def compute(store: Path = data.STORE, get=data.fetch, offline: bool = False) -> dict | None:
    """Run the rule over the stored panels. Funding after the last published month is estimated
    from the premium index of the contracts the book holds (fetched here unless ``offline``)."""
    panels = data.load(store)
    close = panels["close"]
    if close is None or not len(close):
        return None
    manifest = panels["manifest"]
    symbols = list(close.columns)
    actual = panels["funding"] if panels["funding"] is not None else pd.DataFrame(index=close.index, columns=symbols, dtype="float64")
    actual = actual.reindex(columns=symbols)
    quote_volume, taker_buy = panels["quote_volume"][symbols], panels["taker_buy"][symbols]
    out = engine.run(close, actual, quote_volume, taker_buy)

    known = manifest.get("funding_through")
    estimate_from = (pd.Timestamp(known, tz="UTC") + pd.Timedelta(days=1)) if known else close.index[0]
    decisions = out["market"].decisions
    exposed = decisions >= estimate_from - pd.Timedelta(hours=25)          # holds that reach into the estimated stretch
    estimated_symbols: list[str] = []
    if exposed.any() and estimate_from <= close.index[-1]:
        held = np.abs(out["weights"][exposed]).sum(0) > 0
        estimated_symbols = [s for s, h in zip(symbols, held) if h]
        first, last = (estimate_from - pd.Timedelta(days=1)).date(), date.fromisoformat(manifest["through"])
        premium = panels["premium"] if offline else data.update_premium(estimated_symbols, first, last, store, get)
        if premium is not None and len(premium):
            premium = premium.reindex(columns=[s for s in estimated_symbols if s in premium.columns])
            guess = data.estimate_funding(premium, manifest.get("intervals", {}), estimate_from)
            index = actual.index.union(guess.index)
            funding = actual.reindex(index)
            funding[guess.columns] = funding[guess.columns].combine_first(guess.reindex(index))
            out = engine.run(close, funding, quote_volume, taker_buy)
    out.update(close=close, quote_volume=quote_volume, estimate_from=estimate_from, manifest=manifest,
               estimated_symbols=estimated_symbols)
    return out


def rebuild(conn: sqlite3.Connection, store: Path = data.STORE, get=data.fetch, offline: bool = False) -> dict:
    """Recompute the ledger from the stored panels and write it."""
    _ensure_tables(conn)
    out = compute(store, get, offline)
    if out is None:
        return {"error": "no panels stored yet"}
    market, frame, weights = out["market"], out["frame"], out["weights"]
    symbols, close, quote_volume = market.symbols, out["close"], out["quote_volume"]
    stamp = datetime.now(timezone.utc).isoformat(timespec="seconds")
    entries = market.decisions + pd.Timedelta(hours=engine.LAG_HOURS)
    entry_volume = quote_volume.reindex(entries).to_numpy()
    exit_price = np.full(market.entry_price.shape, np.nan)
    exit_price[:-1] = market.entry_price[1:]
    contribution = out["price_pnl"] + out["funding_pnl"]
    start = int(np.searchsorted(market.decisions, pd.Timestamp(PAPER_START, tz="UTC")))
    daily, lines, equity = [], [], EQUITY_START
    for i in range(start, len(market.decisions)):
        day = market.decisions[i].strftime("%Y-%m-%d")
        row, complete = frame.iloc[i], bool(market.complete[i])
        held = np.flatnonzero((np.abs(weights[i]) > 1e-12) | (out["traded"][i] > 1e-12))
        tradable = np.isfinite(market.entry_price[i]) & (np.nan_to_num(entry_volume[i]) > 0)
        ended = np.isnan(exit_price[i]) if complete else np.zeros(len(symbols), dtype=bool)
        flags = []
        stuck = [symbols[j] for j in held if not tradable[j]]
        if stuck:
            flags.append("no trading at the rebalance: " + ", ".join(stuck))
        gone = [symbols[j] for j in held if ended[j] and abs(weights[i, j]) > 1e-12]
        if gone:
            flags.append("stopped trading during the hold (closed at the last price): " + ", ".join(gone))
        top = int(np.argmax(np.abs(contribution[i]))) if complete and len(held) else None
        big = [j for j in held if complete and abs(contribution[i, j]) > CONTRIBUTION_FLAG]
        if big:
            flags.append("one contract over 3% of equity: " + ", ".join(f"{symbols[j]} {100 * contribution[i, j]:+.2f}%" for j in big))
        hold_end = market.decisions[i] + pd.Timedelta(hours=engine.LAG_HOURS + 24)
        source = "actual" if hold_end < out["estimate_from"] else "estimated"
        if complete:
            equity *= 1.0 + float(row["net"])
        daily.append((day, int(complete), int(row["n_eligible"]), int((np.abs(weights[i]) > 1e-12).sum()),
                      float(row["exposure"]), float(weights[i][weights[i] > 0].sum()), float(row["turnover"]),
                      float(row["gross"]) if complete else None, float(row["funding"]) if complete else None,
                      float(row["cost"]), float(row["net"]) if complete else None, round(equity, 2) if complete else None,
                      source, symbols[top] if top is not None else None,
                      float(contribution[i, top]) if top is not None else None, "; ".join(flags) or None,
                      STRATEGY_VERSION, stamp))
        for j in held:
            lines.append((day, symbols[j], float(weights[i, j]), float(out["targets"][i, j]), _f(out["signal"][i, j]),
                          _f(market.entry_price[i, j]), _f(exit_price[i, j]) if complete else None,
                          _f(out["returns"][i, j]) if complete else None,
                          float(market.forward_funding[i, j]) if complete else None,
                          float(out["price_pnl"][i, j]) if complete else None,
                          float(out["funding_pnl"][i, j]) if complete else None, float(out["traded"][i, j]),
                          int(tradable[j]), int(ended[j])))
    conn.execute("DELETE FROM crypto_xs_daily")
    conn.execute("DELETE FROM crypto_xs_weights")
    conn.executemany("INSERT INTO crypto_xs_daily (decision_date,complete,n_eligible,n_held,exposure,long_exposure,turnover,"
                     "gross,funding,cost,net,equity,funding_source,top_symbol,top_contribution,flags,strategy_version,"
                     "updated_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)", daily)
    conn.executemany("INSERT INTO crypto_xs_weights (decision_date,symbol,weight,target,signal,entry_price,exit_price,"
                     "price_return,funding_rate,price_pnl,funding_pnl,traded,tradable,ended) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)", lines)
    conn.commit()
    manifest = out["manifest"]
    _set_state(conn, built_at=stamp, data_through=manifest.get("through"), funding_through=manifest.get("funding_through"),
               version=STRATEGY_VERSION, late=sum(len(v) for v in manifest.get("late", {}).values()),
               estimated_contracts=len(out["estimated_symbols"]))
    done = [d for d in daily if d[1]]
    return {"days": len(done), "through": done[-1][0] if done else None, "open": daily[-1][0] if daily and not daily[-1][1] else None,
            "equity": done[-1][11] if done else EQUITY_START, "data_through": manifest.get("through"),
            "funding_through": manifest.get("funding_through"), "reconciles": reconcile(conn)["ok"]}


def _f(v):
    return None if v is None or not np.isfinite(v) else float(v)


def _compound(values) -> float:
    return float(np.prod([1.0 + v for v in values]) - 1.0)


def reconcile(conn: sqlite3.Connection) -> dict:
    """June - August against the research backtest's months (compounded %)."""
    rows = conn.execute("SELECT decision_date, net FROM crypto_xs_daily WHERE complete=1 AND decision_date<=? ORDER BY 1",
                        (REFERENCE_LAST_DAY,)).fetchall()
    months = {}
    for day, net in rows:
        months.setdefault(day[:7], []).append(net)
    got = {m: round(100 * _compound(v), 2) for m, v in months.items()}
    full = bool(rows) and rows[-1][0] == REFERENCE_LAST_DAY
    ok = full and all(m in got and abs(got[m] - want) <= RECONCILE_TOLERANCE for m, want in REFERENCE.items())
    return {"ok": bool(ok), "got": got, "want": REFERENCE, "through": REFERENCE_LAST_DAY}


# --------------------------------------------------------------------- run ---
def refresh(now: datetime | None = None, *, store: Path = data.STORE, get=data.fetch, force: bool = False,
            connection: sqlite3.Connection | None = None, log=print) -> dict:
    """Fetch whatever the archive has published since the last run and, if anything changed,
    recompute the ledger. Takes minutes the first time (the whole history), seconds after."""
    now = now or datetime.now(timezone.utc)
    own = connection is None
    conn = connection or get_conn()
    _ensure_tables(conn)
    try:
        _set_state(conn, checked_at=now.isoformat(timespec="seconds"))
        fetched = data.update(store, today=now.astimezone(timezone.utc).date(), get=get, log=log)
        stale = _state(conn, "version") != STRATEGY_VERSION or _state(conn, "data_through") != fetched["through"]
        result = rebuild(conn, store, get) if (force or fetched["changed"] or stale) else {"unchanged": True}
        _set_state(conn, error=None)
        return {**result, "added_days": fetched["added_days"], "funding_months": fetched["funding_months"]}
    except Exception as exc:
        _set_state(conn, error=f"{type(exc).__name__}: {exc}"[:300])
        raise
    finally:
        if own:
            conn.close()


_worker: threading.Thread | None = None
_last: dict = {}


def _background() -> None:
    global _last
    try:
        _last = refresh()
    except Exception as exc:
        _last = {"error": f"{type(exc).__name__}: {exc}"[:200]}


def run_live(now: datetime | None = None) -> dict:
    """The paper loop's entry point. Never blocks the loop: when a check is due (every
    CHECK_MINUTES) the fetch and the recompute run on a background thread."""
    global _worker
    now = now or datetime.now(timezone.utc)
    now = now if now.tzinfo is not None else now.replace(tzinfo=timezone(timedelta(hours=5, minutes=30)))
    if _worker is not None and _worker.is_alive():
        return {"status": "updating"}
    conn = get_conn()
    try:
        _ensure_tables(conn)
        checked = _state(conn, "checked_at")
        through = _state(conn, "data_through")
    finally:
        conn.close()
    due = checked is None or (now - datetime.fromisoformat(checked)) >= timedelta(minutes=CHECK_MINUTES)
    if due:
        _worker = threading.Thread(target=_background, name="crypto-xs-refresh", daemon=True)
        _worker.start()
        return {"status": "updating"}
    return {"status": "idle", "data_through": through, **{k: v for k, v in _last.items() if k in ("error", "through", "equity")}}


# ---------------------------------------------------------------- dashboard ---
def _period(rows: list[dict]) -> dict:
    """Summary of a run of completed days (oldest first)."""
    if not rows:
        return {"days": 0}
    net = [r["net"] for r in rows]
    curve = np.cumprod([1.0 + v for v in net])
    peak = np.maximum.accumulate(np.concatenate([[1.0], curve]))[1:]
    worst = min(rows, key=lambda r: r["net"])
    best = max(rows, key=lambda r: r["net"])
    months: dict = {}
    for r in rows:
        months.setdefault(r["decision_date"][:7], []).append(r)
    return {
        "days": len(rows), "first": rows[0]["decision_date"], "last": rows[-1]["decision_date"],
        "net_pct": round(100 * (curve[-1] - 1.0), 2), "sum_pct": round(100 * sum(net), 2),
        "gross_pct": round(100 * sum(r["gross"] for r in rows), 2),
        "funding_pct": round(100 * sum(r["funding"] for r in rows), 2),
        "cost_pct": round(100 * sum(r["cost"] for r in rows), 2),
        "up_days": sum(1 for v in net if v > 0), "worst_day": worst["decision_date"], "worst_pct": round(100 * worst["net"], 2),
        "best_day": best["decision_date"], "best_pct": round(100 * best["net"], 2),
        "max_drawdown_pct": round(100 * float((curve / peak - 1.0).min()), 2),
        "turnover": round(float(np.mean([r["turnover"] for r in rows])), 3),
        "flagged": sum(1 for r in rows if r["flags"]),
        "estimated_days": sum(1 for r in rows if r["funding_source"] != "actual"),
        "months": [{"month": m, "days": len(v), "net_pct": round(100 * _compound([r["net"] for r in v]), 2),
                    "gross_pct": round(100 * sum(r["gross"] for r in v), 2),
                    "funding_pct": round(100 * sum(r["funding"] for r in v), 2),
                    "cost_pct": round(100 * sum(r["cost"] for r in v), 2),
                    "estimated_days": sum(1 for r in v if r["funding_source"] != "actual")} for m, v in sorted(months.items())],
    }


def tab_data(conn: sqlite3.Connection, date_clause: str = "", date_params=()) -> tuple:
    """(day rows newest first, the latest book's lines, stats) for the /labs/live tab."""
    clause = date_clause.replace("trade_date", "decision_date")
    cur = conn.execute(f"SELECT * FROM crypto_xs_daily WHERE 1=1 {clause} ORDER BY decision_date DESC LIMIT 600", tuple(date_params))
    cols = [c[0] for c in cur.description]
    rows = [dict(zip(cols, r)) for r in cur.fetchall()]
    cur = conn.execute("SELECT * FROM crypto_xs_daily ORDER BY decision_date")
    every = [dict(zip([c[0] for c in cur.description], r)) for r in cur.fetchall()]
    if not every:
        return rows, [], {}
    done = [r for r in every if r["complete"]]
    latest = every[-1]
    cur = conn.execute("SELECT * FROM crypto_xs_weights WHERE decision_date=? AND ABS(weight)>1e-12 ORDER BY weight DESC",
                       (latest["decision_date"],))
    book = [dict(zip([c[0] for c in cur.description], r)) for r in cur.fetchall()]
    state = dict(conn.execute("SELECT k, v FROM crypto_xs_state").fetchall())
    stats = {
        "research": _period([r for r in done if r["decision_date"] < FIRST_UNSEEN]),
        "unseen": _period([r for r in done if r["decision_date"] >= FIRST_UNSEEN]),
        "reconcile": reconcile(conn), "latest": latest, "last_done": done[-1] if done else None,
        "equity": done[-1]["equity"] if done else EQUITY_START, "equity_start": EQUITY_START,
        "paper_start": PAPER_START, "first_unseen": FIRST_UNSEEN, "state": state,
        "flagged": [r for r in reversed(every) if r["flags"]][:40],
        "top": engine.TOP, "hold_days": engine.HOLD_DAYS, "cost_bps": engine.COST_BPS,
    }
    return rows, book, stats


if __name__ == "__main__":
    import json
    print(json.dumps(refresh(force=True), indent=2, default=str))
