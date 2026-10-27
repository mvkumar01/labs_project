"""Crypto trend book (BTC/ETH) and the combined taker + trend book: paper ledgers.

Paper only. This module never calls an exchange order API (it has no exchange credentials at all;
it reads the Binance public-archive panels the taker book keeps, see crypto_xs_data.py).

Handed over from the Crypto_Analysis research project (run XS_FACTORS_20261009_V1, handoff #2,
scripts/run_btc_benchmarks.py rule B2_trend_60d with research_engine/xs_backtest.run) at Vipin's
request. The taker book (crypto_xs) is untouched; these two books sit beside it.

  trend (crypto_xs_trend)   BTCUSDT and ETHUSDT perpetuals, half the capital each, on the taker
                            book's clock: decide at 00:00 UTC from hourly closes at or before it,
                            trade at the 01:00 price, hold to the next 01:00.
                              direction = sign(close[00:00] / close[00:00 sixty days earlier] - 1)
                              vol   = std of hourly log returns over the last 720 hours (480 needed)
                                      x sqrt(24 x 365)
                              scale = min(0.30 / vol, 2.0)
                              weight = 0.5 x direction x scale   (gross can reach 2x capital)
                            result = sum(w x price return) - sum(w x funding settled in the hold)
                                     - 7 bps x |weight change after drift|
  combined (crypto_xs_combo) daily net = 0.5555 x taker net + 0.4445 x trend net, both legs
                            rebalanced daily to those weights. The taker leg is the taker book's
                            research model at unit gross (crypto_xs_daily.net, the COMPOSITE
                            column of the backtest). Inverse-volatility weights fixed on 2021-24
                            data only; never re-fitted.

Two periods, kept apart and never pooled:
  1 Jun - 30 Aug 2026  reproduces the research (trend +7.64 / -5.53 / +19.99%, combined +4.89 /
                       -7.61 / +9.47%, compounded by 00:00 decision date)
  1 Sep 2026 onward    unseen by the research; nothing may be tuned on it.

Funding for the month the archive has not published yet is estimated from the premium index, as
in the taker book; each day says which it carries.
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

SYMBOLS = ("BTCUSDT", "ETHUSDT")
TARGET_VOL, CAP = 0.30, 2.0
LOOKBACK_DAYS, VOL_HOURS, VOL_MIN_HOURS = 60, 720, 480
LEG = 0.5                                  # half the capital per asset
TAKER_WEIGHT, TREND_WEIGHT = 0.5555, 0.4445
COST_BPS = engine.COST_BPS
TREND_KEY, COMBO_KEY = "crypto_xs_trend", "crypto_xs_combo"
TREND_VERSION = "crypto_trend_btc_eth_60d_vol30_cap2_v1"
COMBO_VERSION = "crypto_combo_taker5555_trend4445_v1"
PAPER_START = "2026-06-01"
FIRST_UNSEEN = "2026-09-01"
EQUITY_START = 100_000.0
REFERENCE = {TREND_KEY: {"2026-06": 7.64, "2026-07": -5.53, "2026-08": 19.99},
             COMBO_KEY: {"2026-06": 4.89, "2026-07": -7.61, "2026-08": 9.47}}
REFERENCE_LAST_DAY = "2026-08-30"
RECONCILE_TOLERANCE = 0.05                 # percentage points a month
PREMIUM_STORE = data.ROOT / "storage" / "state" / "crypto_trend"   # this book's own premium cache
CHECK_MINUTES = 30


# -------------------------------------------------------------------- rule ---
def trend_weights(close: pd.DataFrame, decisions: pd.DatetimeIndex) -> pd.DataFrame:
    """The research's B2_trend_60d x 0.5 per asset (run_btc_benchmarks.trend_weights)."""
    daily = close.reindex(decisions)
    vol = np.log(close).diff().rolling(VOL_HOURS, min_periods=VOL_MIN_HOURS).std().reindex(decisions) * np.sqrt(24 * 365)
    scale = (TARGET_VOL / vol).clip(upper=CAP)
    direction = np.sign(daily / daily.shift(LOOKBACK_DAYS) - 1)
    return (LEG * direction * scale).fillna(0.0), direction, scale, vol


def run(close: pd.DataFrame, funding: pd.DataFrame) -> dict:
    """The trend book over the BTC/ETH panels: market, weights and the per-day frame. The last
    day's hold is still open (complete = False)."""
    close, funding = close[list(SYMBOLS)], funding.reindex(columns=list(SYMBOLS))
    market = engine.build_market(close, funding, close * 0.0 + 1.0)
    w, direction, scale, vol = trend_weights(close, market.decisions)
    weights = w.reindex(columns=market.symbols).to_numpy()
    returns = np.nan_to_num(market.forward_return)
    price_pnl = weights * returns
    funding_pnl = -weights * market.forward_funding
    drifted = np.vstack([np.zeros((1, weights.shape[1])), weights[:-1] * (1.0 + returns[:-1])])
    traded = np.abs(weights - drifted)
    frame = pd.DataFrame({"gross": price_pnl.sum(1), "funding": funding_pnl.sum(1),
                          "cost": traded.sum(1) * COST_BPS / 1e4, "turnover": traded.sum(1),
                          "exposure": np.abs(weights).sum(1), "complete": market.complete}, index=market.decisions)
    frame["net"] = frame["gross"] + frame["funding"] - frame["cost"]
    return {"market": market, "weights": weights, "direction": direction, "scale": scale, "vol": vol,
            "frame": frame, "price_pnl": price_pnl, "funding_pnl": funding_pnl}


def compute(store: Path = data.STORE, get=data.fetch, offline: bool = False) -> dict | None:
    """Run the trend rule over the stored panels. Funding after the last published month is
    estimated from BTC and ETH's premium index (kept in this book's own store, fetched unless
    ``offline``), the taker book's method."""
    panels = data.load(store)
    close = panels["close"]
    if close is None or not len(close) or any(s not in close.columns for s in SYMBOLS):
        return None
    close = close[list(SYMBOLS)]
    manifest = panels["manifest"]
    actual = panels["funding"] if panels["funding"] is not None else pd.DataFrame(index=close.index, columns=list(SYMBOLS))
    actual = actual.reindex(columns=list(SYMBOLS)).astype("float64")
    known = manifest.get("funding_through")
    estimate_from = (pd.Timestamp(known, tz="UTC") + pd.Timedelta(days=1)) if known else close.index[0]
    funding = actual
    if estimate_from <= close.index[-1]:
        first, last = (estimate_from - pd.Timedelta(days=1)).date(), date.fromisoformat(manifest["through"])
        premium = (data.load_panel("premium", PREMIUM_STORE) if offline
                   else data.update_premium(list(SYMBOLS), first, last, PREMIUM_STORE, get))
        if premium is None or not set(SYMBOLS) <= set(premium.columns):
            shared = panels.get("premium")
            premium = shared if shared is not None and set(SYMBOLS) <= set(shared.columns) else premium
        if premium is not None and len(premium):
            premium = premium.reindex(columns=[s for s in SYMBOLS if s in premium.columns])
            guess = data.estimate_funding(premium, manifest.get("intervals", {}), estimate_from)
            for symbol, coverage in manifest.get("api_funding_coverage", {}).items():
                if symbol in guess:
                    covered = (guess.index >= pd.Timestamp(coverage["first"])) & (guess.index <= pd.Timestamp(coverage["through"]))
                    guess.loc[covered, symbol] = np.nan
            index = actual.index.union(guess.index)
            funding = actual.reindex(index)
            funding[guess.columns] = funding[guess.columns].combine_first(guess.reindex(index))
    out = run(close, funding)
    out.update(estimate_from=estimate_from, manifest=manifest)
    return out


def _source(day: str, estimate_from: pd.Timestamp, manifest: dict) -> str:
    end = pd.Timestamp(day, tz="UTC") + pd.Timedelta(hours=25)
    if end < estimate_from:
        return "actual"
    coverage = manifest.get("api_funding_coverage", {})
    start = max(pd.Timestamp(day, tz="UTC") + pd.Timedelta(hours=1), estimate_from)
    if all(s in coverage and pd.Timestamp(coverage[s]["first"]) <= start and pd.Timestamp(coverage[s]["through"]) >= end
           for s in SYMBOLS):
        return "actual (public API)"
    return "estimated"


# ------------------------------------------------------------------ ledger ---
def _ensure_tables(conn: sqlite3.Connection) -> None:
    conn.executescript(
        f"""
        CREATE TABLE IF NOT EXISTS {TREND_KEY}_daily (
            decision_date TEXT PRIMARY KEY, complete INTEGER NOT NULL,
            btc_direction REAL, btc_vol REAL, btc_scale REAL, btc_weight REAL, btc_entry REAL, btc_return REAL,
            eth_direction REAL, eth_vol REAL, eth_scale REAL, eth_weight REAL, eth_entry REAL, eth_return REAL,
            exposure REAL, turnover REAL, gross REAL, funding REAL, cost REAL, net REAL, equity REAL,
            funding_source TEXT, flags TEXT, strategy_version TEXT NOT NULL, updated_at TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS {COMBO_KEY}_daily (
            decision_date TEXT PRIMARY KEY, complete INTEGER NOT NULL, taker_net REAL, trend_net REAL,
            exposure REAL, turnover REAL, gross REAL, funding REAL, cost REAL, net REAL, equity REAL,
            funding_source TEXT, flags TEXT, strategy_version TEXT NOT NULL, updated_at TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS {TREND_KEY}_state (k TEXT PRIMARY KEY, v TEXT);
        """
    )
    conn.commit()


def _state(conn: sqlite3.Connection, key: str) -> str | None:
    row = conn.execute(f"SELECT v FROM {TREND_KEY}_state WHERE k=?", (key,)).fetchone()
    return row[0] if row else None


def _set_state(conn: sqlite3.Connection, **values) -> None:
    conn.executemany(f"INSERT OR REPLACE INTO {TREND_KEY}_state (k, v) VALUES (?, ?)",
                     [(k, None if v is None else str(v)) for k, v in values.items()])
    conn.commit()


def _f(v):
    return None if v is None or not np.isfinite(v) else float(v)


def _compound(values) -> float:
    return float(np.prod([1.0 + v for v in values]) - 1.0)


def rebuild(conn: sqlite3.Connection, store: Path = data.STORE, get=data.fetch, offline: bool = False) -> dict:
    """Recompute both ledgers: the trend book from the panels, the combined book from it and the
    taker book's stored daily results (crypto_xs_daily, its research model at unit gross)."""
    _ensure_tables(conn)
    out = compute(store, get, offline)
    if out is None:
        return {"error": "no BTC/ETH panels stored yet"}
    market, frame, w = out["market"], out["frame"], out["weights"]
    stamp = datetime.now(timezone.utc).isoformat(timespec="seconds")
    start = int(np.searchsorted(market.decisions, pd.Timestamp(PAPER_START, tz="UTC")))
    trend, equity = {}, EQUITY_START
    cols = {s: market.symbols.index(s) for s in SYMBOLS}
    for i in range(start, len(market.decisions)):
        ts = market.decisions[i]
        day = ts.strftime("%Y-%m-%d")
        row, complete = frame.iloc[i], bool(market.complete[i])
        if complete:
            equity *= 1.0 + float(row["net"])
        per = []
        for s in SYMBOLS:
            j = cols[s]
            per += [_f(out["direction"].at[ts, s]), _f(out["vol"].at[ts, s]), _f(out["scale"].at[ts, s]), float(w[i, j]),
                    _f(market.entry_price[i, j]), _f(market.forward_return[i, j]) if complete else None]
        flags = "volatility history short" if any(not np.isfinite(out["vol"].at[ts, s]) for s in SYMBOLS) else None
        trend[day] = (day, int(complete), *per, float(row["exposure"]), float(row["turnover"]),
                      float(row["gross"]) if complete else None, float(row["funding"]) if complete else None,
                      float(row["cost"]), float(row["net"]) if complete else None,
                      round(equity, 2) if complete else None, _source(day, out["estimate_from"], out["manifest"]),
                      flags, TREND_VERSION, stamp)
    conn.execute(f"DELETE FROM {TREND_KEY}_daily")
    conn.executemany(f"INSERT INTO {TREND_KEY}_daily VALUES ({','.join('?' * 25)})", list(trend.values()))

    taker = {}
    if conn.execute("SELECT 1 FROM sqlite_master WHERE name='crypto_xs_daily'").fetchone():
        for r in conn.execute("SELECT decision_date, complete, exposure, turnover, gross, funding, cost, net, funding_source "
                              "FROM crypto_xs_daily WHERE decision_date>=?", (PAPER_START,)):
            taker[r[0]] = r
    combo, equity = [], EQUITY_START
    for day, t in trend.items():
        k = taker.get(day)
        if k is None:
            continue
        complete = bool(t[1]) and bool(k[1]) and k[7] is not None and t[19] is not None
        mix = lambda a, b: TAKER_WEIGHT * a + TREND_WEIGHT * b  # noqa: E731
        net = mix(k[7], t[19]) if complete else None
        if complete:
            equity *= 1.0 + net
        sources = {str(k[8] or "estimated"), t[21]}
        source = "actual" if sources == {"actual"} else ("estimated" if any(s.startswith("estimated") for s in sources)
                                                       else "actual (public API)")
        combo.append((day, int(complete), k[7] if complete else None, t[19] if complete else None,
                      mix(k[2] or 0.0, t[14]), mix(k[3] or 0.0, t[15]),
                      mix(k[4], t[16]) if complete else None, mix(k[5], t[17]) if complete else None,
                      mix(k[6] or 0.0, t[18]), net, round(equity, 2) if complete else None, source, None,
                      COMBO_VERSION, stamp))
    conn.execute(f"DELETE FROM {COMBO_KEY}_daily")
    conn.executemany(f"INSERT INTO {COMBO_KEY}_daily VALUES ({','.join('?' * 15)})", combo)
    conn.commit()
    manifest = out["manifest"]
    taker_built = conn.execute("SELECT v FROM crypto_xs_state WHERE k='built_at'").fetchone() \
        if conn.execute("SELECT 1 FROM sqlite_master WHERE name='crypto_xs_state'").fetchone() else None
    _set_state(conn, built_at=stamp, data_through=manifest.get("through"), funding_through=manifest.get("funding_through"),
               taker_built_at=taker_built[0] if taker_built else None, version=TREND_VERSION)
    done = [d for d in trend.values() if d[1]]
    return {"trend_days": len(done), "through": done[-1][0] if done else None,
            "combo_days": sum(1 for c in combo if c[1]), "data_through": manifest.get("through"),
            "reconciles": {k: reconcile(conn, k)["ok"] for k in (TREND_KEY, COMBO_KEY)}}


def reconcile(conn: sqlite3.Connection, key: str) -> dict:
    """June - August against the research's months (compounded %)."""
    rows = conn.execute(f"SELECT decision_date, net FROM {key}_daily WHERE complete=1 AND decision_date<=? ORDER BY 1",
                        (REFERENCE_LAST_DAY,)).fetchall()
    months = {}
    for day, net in rows:
        months.setdefault(day[:7], []).append(net)
    got = {m: round(100 * _compound(v), 2) for m, v in months.items()}
    want = REFERENCE[key]
    full = bool(rows) and rows[-1][0] == REFERENCE_LAST_DAY
    ok = full and all(m in got and abs(got[m] - v) <= RECONCILE_TOLERANCE for m, v in want.items())
    return {"ok": bool(ok), "got": got, "want": want, "through": REFERENCE_LAST_DAY}


# --------------------------------------------------------------------- run ---
_worker: threading.Thread | None = None
_last: dict = {}


def refresh(*, store: Path = data.STORE, get=data.fetch, force: bool = False,
            connection: sqlite3.Connection | None = None) -> dict:
    """Rebuild when the taker book has rebuilt or the panels moved on since the last build."""
    own = connection is None
    conn = connection or get_conn()
    _ensure_tables(conn)
    try:
        manifest = data.load_manifest(store)
        taker_built = conn.execute("SELECT v FROM crypto_xs_state WHERE k='built_at'").fetchone() \
            if conn.execute("SELECT 1 FROM sqlite_master WHERE name='crypto_xs_state'").fetchone() else None
        stale = (force or _state(conn, "version") != TREND_VERSION
                 or _state(conn, "data_through") != manifest.get("through")
                 or _state(conn, "taker_built_at") != (taker_built[0] if taker_built else None))
        result = rebuild(conn, store, get) if stale else {"unchanged": True}
        _set_state(conn, checked_at=datetime.now(timezone.utc).isoformat(timespec="seconds"), error=None)
        return result
    except Exception as exc:
        _set_state(conn, error=f"{type(exc).__name__}: {exc}"[:300])
        raise
    finally:
        if own:
            conn.close()


def _background() -> None:
    global _last
    try:
        _last = refresh()
    except Exception as exc:
        _last = {"error": f"{type(exc).__name__}: {exc}"[:200]}


def run_live(now: datetime | None = None) -> dict:
    """The paper loop's entry point. Never blocks the loop, and waits while the taker book is
    updating (this book reads the taker book's results and panels)."""
    global _worker
    now = now or datetime.now(timezone.utc)
    now = now if now.tzinfo is not None else now.replace(tzinfo=timezone(timedelta(hours=5, minutes=30)))
    from labs.engine import crypto_xs_tracker as taker
    if (_worker is not None and _worker.is_alive()) or (taker._worker is not None and taker._worker.is_alive()):
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
        _worker = threading.Thread(target=_background, name="crypto-trend-refresh", daemon=True)
        _worker.start()
        return {"status": "updating"}
    return {"status": "idle", "data_through": through, **{k: v for k, v in _last.items() if k in ("error", "through")}}


# ---------------------------------------------------------------- dashboard ---
def tab_data(conn: sqlite3.Connection, key: str, date_clause: str = "", date_params=()) -> tuple:
    """(day rows newest first, stats) for the /labs/live tab of either book."""
    from labs.engine.crypto_xs_tracker import _period
    clause = date_clause.replace("trade_date", "decision_date")
    cur = conn.execute(f"SELECT * FROM {key}_daily WHERE 1=1 {clause} ORDER BY decision_date", tuple(date_params))
    cols = [c[0] for c in cur.description]
    every = [dict(zip(cols, r)) for r in cur.fetchall()]
    if not every:
        return [], ({"empty_range": True, "filtered": True} if clause else {})
    done = [r for r in every if r["complete"]]
    state = dict(conn.execute(f"SELECT k, v FROM {TREND_KEY}_state").fetchall())
    stats = {"key": key, "combined": key == COMBO_KEY,
             "research": _period([r for r in done if r["decision_date"] < FIRST_UNSEEN]),
             "unseen": _period([r for r in done if r["decision_date"] >= FIRST_UNSEEN]),
             "reconcile": reconcile(conn, key), "latest": every[-1], "last_done": done[-1] if done else None,
             "equity": done[-1]["equity"] if done else EQUITY_START, "equity_start": EQUITY_START,
             "paper_start": PAPER_START, "first_unseen": FIRST_UNSEEN, "state": state, "filtered": bool(clause),
             "target_vol": TARGET_VOL, "cap": CAP, "lookback": LOOKBACK_DAYS, "cost_bps": COST_BPS,
             "taker_weight": TAKER_WEIGHT, "trend_weight": TREND_WEIGHT}
    return list(reversed(every))[:600], stats


if __name__ == "__main__":
    import json
    print(json.dumps(refresh(force=True), indent=2, default=str))
