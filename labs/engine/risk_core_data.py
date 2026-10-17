"""Inputs of the Risk core (trend x vol) paper book: a frozen research seed plus the server's daily feeds.

Paper only. Nothing here calls a broker order API (Kite is used for daily candles only).

  seed      data/risk_core_seed_*.csv (scripts/build_risk_core_seed.py): the exact inputs of the V2
            Strategy Tester's research run - Kite daily NIFTY 50 / NIFTY BANK / MCX CRUDEOIL continuous,
            Binance USD-M daily klines and funding for BTCUSDT / ETHUSDT, the ECB USD/INR rate
  feeds     only the days after each seed series ends, stored in the labs DB:
              Kite daily candles (historical_data, "day"; crude with continuous=True on the front month),
              final once the day is past or after the close (NSE 15:35, MCX 23:35);
              Binance's public archive (data.binance.vision): the API answers this US server with HTTP 451.
              A UTC day's 1d kline is published about a day later. Funding is published monthly; until a
              month's file exists its funding is ESTIMATED from the hourly premium index
              (crypto_xs_data.estimate_funding, 8-hourly settlement) and replaced when the file appears;
              the ECB rate via frankfurter.dev (no rate on weekends / holidays: the last one carries).
"""
from __future__ import annotations

import json
import sqlite3
import urllib.request
from datetime import date, datetime, time, timedelta, timezone
from pathlib import Path

import numpy as np
import pandas as pd

from labs.engine import crypto_xs_data as cx

BASE_DIR = Path(__file__).resolve().parents[2]
SEED_BARS = BASE_DIR / "data" / "risk_core_seed_bars.csv"
SEED_FUNDING = BASE_DIR / "data" / "risk_core_seed_funding.csv"
SEED_FX = BASE_DIR / "data" / "risk_core_seed_fx.csv"
IST = timezone(timedelta(hours=5, minutes=30))
KITE = {"nifty50": ("NSE", 256265, time(15, 35)), "banknifty": ("NSE", 260105, time(15, 35)),
        "crudeoil": ("MCX", None, time(23, 35))}
BINANCE = {"btcusdt": "BTCUSDT", "ethusdt": "ETHUSDT"}
FX_URL = "https://api.frankfurter.dev/v1/{start}..{end}?base=USD&symbols=INR"
FUNDING_INTERVAL_HOURS = 8


class RiskCoreInputError(RuntimeError):
    """An input the book needs could not be read."""


def ensure_tables(conn: sqlite3.Connection) -> None:
    conn.executescript(
        """
        CREATE TABLE IF NOT EXISTS risk_core_bars (
            sleeve TEXT NOT NULL, trade_date TEXT NOT NULL,
            open REAL, high REAL, low REAL, close REAL NOT NULL,
            source TEXT, fetched_at TEXT,
            PRIMARY KEY (sleeve, trade_date)
        );
        CREATE TABLE IF NOT EXISTS risk_core_funding (
            sleeve TEXT NOT NULL, trade_date TEXT NOT NULL, funding REAL NOT NULL,
            estimated INTEGER NOT NULL DEFAULT 0, fetched_at TEXT,
            PRIMARY KEY (sleeve, trade_date)
        );
        CREATE TABLE IF NOT EXISTS risk_core_fx (
            trade_date TEXT PRIMARY KEY, usd_inr REAL NOT NULL, fetched_at TEXT
        );
        CREATE TABLE IF NOT EXISTS risk_core_state (k TEXT PRIMARY KEY, v TEXT);
        """
    )


# -------------------------------------------------------------------- seed ---
def _seed(path: Path) -> pd.DataFrame:
    return pd.read_csv(path, dtype={"trade_date": str, "sleeve": str})


def seed_ends() -> dict:
    """Last seeded day of every series: sleeve bars, sleeve funding, 'fx'."""
    b = _seed(SEED_BARS).groupby("sleeve").trade_date.max().to_dict()
    f = _seed(SEED_FUNDING).groupby("sleeve").trade_date.max().to_dict()
    return {"bars": b, "funding": f, "fx": _seed(SEED_FX).trade_date.max()}


# --------------------------------------------------------------- Kite feed ---
def _crude_token(kite) -> int:
    rows = [r for r in kite.instruments("MCX") if r.get("name") == "CRUDEOIL" and r.get("instrument_type") == "FUT"]
    today = datetime.now(IST).date()
    live = sorted((r for r in rows if r["expiry"] >= today), key=lambda r: r["expiry"])
    if not live:
        raise RiskCoreInputError("no live MCX CRUDEOIL futures contract")
    return int(live[0]["instrument_token"])


def update_kite(conn: sqlite3.Connection, kite, now: datetime) -> dict:
    """Daily candles from a week before the last stored (or seeded) day to today; a day is stored only
    once it is final. Returns {sleeve: rows stored}."""
    ends, out, stamp = seed_ends()["bars"], {}, now.isoformat(timespec="seconds")
    for sleeve, (exchange, token, close_at) in KITE.items():
        last = conn.execute("SELECT MAX(trade_date) FROM risk_core_bars WHERE sleeve=?", (sleeve,)).fetchone()[0]
        frm = date.fromisoformat(max(filter(None, (last, ends[sleeve])))) - timedelta(days=7)
        tok = token or _crude_token(kite)
        candles = kite.historical_data(tok, datetime.combine(frm, time(0, 0)), datetime.combine(now.date(), time(23, 59)),
                                       "day", continuous=token is None)
        rows = []
        for c in candles:
            d = pd.Timestamp(c["date"]).date()
            if d < now.date() or now.time() >= close_at:
                rows.append((sleeve, d.isoformat(), float(c["open"]), float(c["high"]), float(c["low"]), float(c["close"]),
                             f"kite {exchange} daily" + (" continuous" if token is None else ""), stamp))
        conn.executemany(
            "INSERT INTO risk_core_bars (sleeve,trade_date,open,high,low,close,source,fetched_at) VALUES (?,?,?,?,?,?,?,?) "
            "ON CONFLICT(sleeve,trade_date) DO UPDATE SET open=excluded.open,high=excluded.high,low=excluded.low,"
            "close=excluded.close,source=excluded.source,fetched_at=excluded.fetched_at", rows)
        out[sleeve] = len(rows)
    conn.commit()
    return out


# ------------------------------------------------------------ Binance feed ---
def _kline_1d_url(symbol: str, day: str) -> str:
    return f"{cx.ARCHIVE}/daily/klines/{symbol}/1d/{symbol}-1d-{day}.zip"


def _parse_1d(data: bytes) -> pd.DataFrame:
    f = cx._csv(data, cx.KLINE_COLUMNS)
    if f.empty:
        return f
    f.index = cx._epoch(f["open_time"]).tz_convert(None).normalize()
    return f[["open", "high", "low", "close"]].apply(pd.to_numeric, errors="coerce")


def update_binance(conn: sqlite3.Connection, now: datetime, get=None) -> dict:
    """Daily klines for every UTC day after the last stored (or seeded) one up to yesterday (UTC) that the
    archive has published; then funding: actual from the monthly files where published, estimated from
    the hourly premium index for the days after."""
    get = get or cx.fetch
    ends, stamp = seed_ends(), now.isoformat(timespec="seconds")
    utc_today = (now - timedelta(hours=5, minutes=30)).date()      # `now` is naive IST
    out = {}
    for sleeve, sym in BINANCE.items():
        last = conn.execute("SELECT MAX(trade_date) FROM risk_core_bars WHERE sleeve=?", (sleeve,)).fetchone()[0]
        start = date.fromisoformat(max(filter(None, (last, ends["bars"][sleeve])))) + timedelta(days=1)
        rows = []
        d = start
        while d < utc_today:
            data = get(_kline_1d_url(sym, d.isoformat()))
            if data is None:                     # not published yet: later days will not be either
                break
            for ts, r in _parse_1d(data).iterrows():
                rows.append((sleeve, ts.date().isoformat(), r.open, r.high, r.low, r.close, "binance archive 1d", stamp))
            d += timedelta(days=1)
        conn.executemany(
            "INSERT OR REPLACE INTO risk_core_bars (sleeve,trade_date,open,high,low,close,source,fetched_at) "
            "VALUES (?,?,?,?,?,?,?,?)", rows)
        out[sleeve] = {"bars": len(rows), **_update_funding(conn, sleeve, sym, ends["funding"][sleeve], utc_today, stamp, get)}
    conn.commit()
    return out


def _update_funding(conn, sleeve, sym, seed_end: str, utc_today: date, stamp: str, get) -> dict:
    first = date.fromisoformat(seed_end) + timedelta(days=1)
    have_actual = conn.execute("SELECT MAX(trade_date) FROM risk_core_funding WHERE sleeve=? AND estimated=0",
                               (sleeve,)).fetchone()[0]
    if have_actual:
        first = max(first, date.fromisoformat(have_actual) + timedelta(days=1))
    actual_rows, actual_through = [], first - timedelta(days=1)
    m = date(first.year, first.month, 1)
    while m < date(utc_today.year, utc_today.month, 1):
        data = get(cx.funding_url(sym, m.strftime("%Y-%m")))
        if data is None:
            break
        f = cx.parse_funding(data)
        daily = f["rate"].groupby(f.index.tz_convert(None).normalize()).sum()
        actual_rows += [(sleeve, ts.date().isoformat(), float(v), 0, stamp) for ts, v in daily.items() if ts.date() >= first]
        nxt = date(m.year + (m.month == 12), m.month % 12 + 1, 1)
        actual_through, m = nxt - timedelta(days=1), nxt
    conn.executemany("INSERT OR REPLACE INTO risk_core_funding (sleeve,trade_date,funding,estimated,fetched_at) "
                     "VALUES (?,?,?,?,?)", actual_rows)
    # estimate the days after the last published month, up to the last stored kline
    last_bar = conn.execute("SELECT MAX(trade_date) FROM risk_core_bars WHERE sleeve=?", (sleeve,)).fetchone()[0]
    est_from = actual_through + timedelta(days=1)
    est_rows = []
    if last_bar and est_from <= date.fromisoformat(last_bar):
        conn.execute("DELETE FROM risk_core_funding WHERE sleeve=? AND estimated=1 AND trade_date<=?",
                     (sleeve, actual_through.isoformat()))
        days = pd.date_range(est_from, date.fromisoformat(last_bar), freq="D")
        pieces = []
        for d in days:
            data = get(cx.kline_url("premiumIndexKlines", sym, d.date().isoformat(), False))
            if data is not None:
                pieces.append(cx.parse_klines(data)["close"])
        if pieces:
            prem = pd.concat(pieces).sort_index()
            prem = prem[~prem.index.duplicated(keep="last")].to_frame(sym)
            est = cx.estimate_funding(prem, {sym: FUNDING_INTERVAL_HOURS}, prem.index.min())[sym].dropna()
            daily = est.groupby(est.index.tz_convert(None).normalize()).sum()
            est_rows = [(sleeve, ts.date().isoformat(), float(v), 1, stamp) for ts, v in daily.items()
                        if est_from <= ts.date() <= date.fromisoformat(last_bar)]
            conn.executemany("INSERT OR REPLACE INTO risk_core_funding (sleeve,trade_date,funding,estimated,fetched_at) "
                             "VALUES (?,?,?,?,?)", est_rows)
    return {"funding_actual": len(actual_rows), "funding_estimated": len(est_rows)}


# ------------------------------------------------------------------ USD/INR ---
def update_fx(conn: sqlite3.Connection, now: datetime, get=None) -> int:
    last = conn.execute("SELECT MAX(trade_date) FROM risk_core_fx").fetchone()[0] or seed_ends()["fx"]
    start = date.fromisoformat(last) - timedelta(days=7)
    url = FX_URL.format(start=start.isoformat(), end=now.date().isoformat())
    body = get(url) if get else urllib.request.urlopen(urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"}),
                                                        timeout=30).read()
    rates = json.loads(body).get("rates", {})
    stamp = now.isoformat(timespec="seconds")
    rows = [(d, float(v["INR"]), stamp) for d, v in rates.items() if "INR" in v]
    conn.executemany("INSERT OR REPLACE INTO risk_core_fx (trade_date, usd_inr, fetched_at) VALUES (?,?,?)", rows)
    conn.commit()
    return len(rows)


# ----------------------------------------------------------------- loading ---
def load_inputs(conn: sqlite3.Connection) -> tuple[dict, dict, pd.Series, dict]:
    """(bars, funding, usd_inr, info): the seed, extended by stored feed days after each seed series ends
    (a feed day never replaces a seeded one)."""
    ends = seed_ends()
    sb, sf, sx = _seed(SEED_BARS), _seed(SEED_FUNDING), _seed(SEED_FX)
    fb = pd.read_sql_query("SELECT sleeve, trade_date, open, high, low, close, source FROM risk_core_bars", conn)
    ff = pd.read_sql_query("SELECT sleeve, trade_date, funding, estimated FROM risk_core_funding", conn)
    fx = pd.read_sql_query("SELECT trade_date, usd_inr FROM risk_core_fx", conn)
    bars, funding, info = {}, {}, {"through": {}, "feed_days": {}, "estimated_funding_days": {}}
    for sleeve in ("nifty50", "banknifty", "btcusdt", "ethusdt", "crudeoil"):
        s = sb[sb.sleeve == sleeve]
        f = fb[(fb.sleeve == sleeve) & (fb.trade_date > ends["bars"][sleeve])]
        d = pd.concat([x for x in (s, f) if len(x)])[["trade_date", "open", "high", "low", "close"]]
        d.index = pd.to_datetime(d.pop("trade_date"))
        bars[sleeve] = d.sort_index().astype(float)
        info["through"][sleeve] = bars[sleeve].index.max().date().isoformat()
        info["feed_days"][sleeve] = len(f)
    for sleeve in BINANCE:
        s = sf[sf.sleeve == sleeve]
        f = ff[(ff.sleeve == sleeve) & (ff.trade_date > ends["funding"][sleeve])]
        x = pd.concat([y[["trade_date", "funding"]] for y in (s, f) if len(y)])
        funding[sleeve] = pd.Series(x.funding.to_numpy(float), index=pd.to_datetime(x.trade_date)).sort_index()
        info["estimated_funding_days"][sleeve] = int(f.estimated.sum()) if len(f) else 0
    x = pd.concat([y for y in (sx, fx[fx.trade_date > ends["fx"]]) if len(y)])
    usd_inr = pd.Series(x.usd_inr.to_numpy(float), index=pd.to_datetime(x.trade_date)).sort_index()
    info["fx_through"] = usd_inr.index.max().date().isoformat()
    if np.isnan(usd_inr.to_numpy()).any():
        raise RiskCoreInputError("USD/INR series has gaps")
    return bars, funding, usd_inr, info
