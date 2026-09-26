"""Paper-only BTCUSDT book: RSI(1h) + ROC(15m) crosses, RSI(5m) band gate, short 0.01 BTC.

Rule (Strategy Tester run btcusdt/ui_20260923_162834_bfa8, rank 409, strategy f6041879c2acf239)::

    rsi(n=7)@1h rsi x> 30  &  roc(n=10)@15m roc x> 0
    gate: rsi(n=14)@5m rsi in [40, 60]
    short, stop 1% of entry, target 1.5R, no time stop, max hold 1,440 bars, 30-bar cooldown

The replay is a port of the Tester's engine, so paper results match its research:
a gap-filled 1-minute UTC grid (00:00-23:59, every day -- crypto is continuous), 5m/15m/1h
bins anchored at 00:00 UTC, indicators computed on valid bins only (Wilder RSI with a
TradingView-style SMA seed; ROC = 100 x (close / close[n] - 1)), each condition evaluated on
its own timeframe and visible on a 1-minute bar once its bin has closed by that bar's
close. Entries fire on the rising edge of the AND of both trigger states, filtered by the
gate, then a 30-bar cooldown; fill at the next 1-minute bar's open; stop and target are
checked on 1-minute highs/lows (stop wins inside a bar, a gap through either exits at the
open); one position at a time, a signal on the exit bar may enter on the next bar.

Money (the Tester's what-if at 0.01 BTC): Binance spot fee 0.1% of each leg's value, one
tick (0.01 USDT) of slippage per side. Prices stay in USDT; rupee figures use the ECB
USD/INR reference rate (frankfurter.dev) on the trade's exit date (UTC), capital on its
entry date; weekends/holidays use the last published rate. USDT is taken as USD.

Data: completed 1-minute klines from Binance's public REST API (no key), stored in
``btc_minute_bars`` (UTC). The book never calls an order API.
"""
from __future__ import annotations

from datetime import date, datetime, timedelta, timezone
import json
import math
import sqlite3
import urllib.error
import urllib.parse
import urllib.request

import numpy as np
import pandas as pd

from storage.db import get_conn


IST = timezone(timedelta(hours=5, minutes=30))
UTC = timezone.utc
STRATEGY_VERSION = "btcusdt_rsi7h1x30_roc10m15x0_rsi14m5_40_60_short_sl1pct_tp15r_v1"
RESEARCH = "Strategy Tester btcusdt/ui_20260923_162834_bfa8 rank 409"
SYMBOL = "BTCUSDT"
QTY = 0.01                        # BTC per trade (what-if size)
TICK = 0.01                       # USDT
FEE_PCT = 0.1                     # Binance spot, each side
SLIPPAGE_TICKS = 1.0              # per side

BARS_PER_DAY = 1440               # UTC 00:00-23:59, continuous
DIRECTION = -1                    # short
RSI_H1_N, RSI_H1_THR = 7, 30.0
ROC_M15_N, ROC_M15_THR = 10, 0.0
RSI_M5_N, RSI_M5_LO, RSI_M5_HI = 14, 40.0, 60.0
STOP_PCT = 1.0
TARGET_R = 1.5
COOLDOWN_BARS = 30
MAX_HOLD_BARS = 1440
BOOK_START = date(2026, 6, 1)     # first entry date kept in the ledger
WARMUP_DAYS = 30                  # replay anchor = BOOK_START - WARMUP_DAYS (fixed, see below)
NS_PER_MIN = 60_000_000_000

# api.binance.com refuses some regions (HTTP 451); data-api.binance.vision serves the same
# public market data. The first endpoint that answers is used.
KLINE_ENDPOINTS = (
    "https://api.binance.com/api/v3/klines",
    "https://data-api.binance.vision/api/v3/klines",
)
FX_URL = "https://api.frankfurter.dev/v1/{start}..{end}"
FX_SOURCE = "ECB reference rate via frankfurter.dev (weekends/holidays use the previous rate)"


class BtcInputError(RuntimeError):
    """Required market or FX data is unavailable."""


# ------------------------------------------------------------------ storage ---
def _ensure_tables(conn: sqlite3.Connection) -> None:
    conn.executescript(
        """
        CREATE TABLE IF NOT EXISTS btc_minute_bars (
            symbol TEXT NOT NULL,
            ts TEXT NOT NULL,              -- bar open, UTC, 'YYYY-MM-DD HH:MM:00'
            open REAL NOT NULL,
            high REAL NOT NULL,
            low REAL NOT NULL,
            close REAL NOT NULL,
            volume REAL NOT NULL,
            PRIMARY KEY (symbol, ts)
        );
        CREATE TABLE IF NOT EXISTS fx_usd_inr (
            rate_date TEXT PRIMARY KEY,
            rate REAL NOT NULL,
            source TEXT NOT NULL,
            fetched_at TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS btc_rsi_roc_daily (
            trade_date TEXT PRIMARY KEY,   -- UTC day (the research's session)
            status TEXT NOT NULL,          -- live | final | no_session
            tradingsymbol TEXT,
            expiry TEXT,
            valid_bars INTEGER,
            n_signals INTEGER NOT NULL DEFAULT 0,
            n_trades INTEGER NOT NULL DEFAULT 0,
            open_trades INTEGER NOT NULL DEFAULT 0,
            wins INTEGER NOT NULL DEFAULT 0,
            gross_rs REAL NOT NULL DEFAULT 0,
            charges_rs REAL NOT NULL DEFAULT 0,
            slippage_rs REAL NOT NULL DEFAULT 0,
            net_rs REAL NOT NULL DEFAULT 0,
            net_usdt REAL NOT NULL DEFAULT 0,
            qty REAL NOT NULL,
            strategy_version TEXT NOT NULL,
            error TEXT,
            updated_at TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS btc_rsi_roc_trades (
            trade_date TEXT NOT NULL,      -- UTC day of entry
            seq INTEGER NOT NULL,
            tradingsymbol TEXT NOT NULL,
            signal_ts TEXT NOT NULL,       -- UTC
            entry_ts TEXT NOT NULL,        -- UTC
            exit_ts TEXT,                  -- UTC
            entry_price REAL NOT NULL,     -- USDT
            exit_price REAL,               -- USDT (the last close while open)
            stop_price REAL NOT NULL,
            target_price REAL NOT NULL,
            stop_dist REAL NOT NULL,
            r_multiple REAL,
            points REAL,                   -- USDT per BTC, positive = profit (short)
            qty REAL NOT NULL,
            gross_usdt REAL,
            charges_usdt REAL,
            slippage_usdt REAL,
            net_usdt REAL,
            fx_rate REAL,                  -- USD/INR on the exit date (today while open)
            fx_entry_rate REAL,            -- USD/INR on the entry date
            gross_rs REAL,
            charges_rs REAL,
            slippage_rs REAL,
            net_rs REAL,
            notional_rs REAL,
            margin_rs REAL,                -- capital: spot is fully funded, = notional
            margin_source TEXT,
            status TEXT NOT NULL,
            exit_reason TEXT,
            bars_held INTEGER,
            PRIMARY KEY (trade_date, seq)
        );
        """
    )
    conn.commit()


# ------------------------------------------------------------ indicators ---
def _ewm_seeded(x: np.ndarray, alpha: float, n: int) -> np.ndarray:
    """EMA recursion seeded with the SMA of the first n consecutive finite values
    (TradingView ta.rma convention, as in the Tester's indicators.core.ewm_seeded)."""
    out = np.full(x.shape[0], np.nan)
    run, total, seeded, prev = 0, 0.0, False, 0.0
    for i, v in enumerate(x):
        if not seeded:
            if math.isfinite(v):
                run += 1
                total += v
                if run == n:
                    prev = total / n
                    out[i] = prev
                    seeded = True
            else:
                run, total = 0, 0.0
        elif math.isfinite(v):
            prev = alpha * v + (1.0 - alpha) * prev
            out[i] = prev
    return out


def rma(x, n: int) -> np.ndarray:
    return _ewm_seeded(np.asarray(x, dtype=np.float64), 1.0 / n, int(n))


def rsi(close, n: int) -> np.ndarray:
    close = np.asarray(close, dtype=np.float64)
    d = np.diff(close, prepend=np.nan)
    up = np.where(np.isnan(d), np.nan, np.maximum(d, 0.0))
    dn = np.where(np.isnan(d), np.nan, np.maximum(-d, 0.0))
    au, ad = rma(up, n), rma(dn, n)
    with np.errstate(divide="ignore", invalid="ignore"):
        r = 100.0 - 100.0 / (1.0 + au / ad)
    return np.where((ad == 0) & np.isfinite(au), 100.0, r)


def roc(close, n: int) -> np.ndarray:
    close = np.asarray(close, dtype=np.float64)
    prev = np.full_like(close, np.nan)
    if close.size > n:
        prev[n:] = close[:-n]
    with np.errstate(divide="ignore", invalid="ignore"):
        out = 100.0 * (close - prev) / prev
    out[~np.isfinite(out)] = np.nan
    return out


def _on_valid(valid: np.ndarray, func, *arrays) -> np.ndarray:
    """Run an indicator on the valid rows only and scatter it back (NaN elsewhere)."""
    idx = np.flatnonzero(valid)
    out = np.full(valid.shape[0], np.nan)
    if idx.size:
        out[idx] = func(*[np.ascontiguousarray(a[idx]) for a in arrays])
    return out


def cross_above(x: np.ndarray, thr: float) -> np.ndarray:
    """x[t-1] <= thr and x[t] > thr (NaN -> False)."""
    prev = np.r_[np.nan, x[:-1]]
    with np.errstate(invalid="ignore"):
        return (prev <= thr) & (x > thr)


def between(x: np.ndarray, lo: float, hi: float) -> np.ndarray:
    with np.errstate(invalid="ignore"):
        return (x >= lo) & (x <= hi)


def rising_edge(mask: np.ndarray) -> np.ndarray:
    out = mask.copy()
    out[1:] &= ~mask[:-1]
    return out


def rearm(mask: np.ndarray, n: int) -> np.ndarray:
    out = np.zeros(mask.shape[0], dtype=bool)
    block = -1
    for i in np.flatnonzero(mask):
        if i > block:
            out[i] = True
            block = i + n
    return out


# ------------------------------------------------------------------ replay ---
def build_grid(frame: pd.DataFrame, cutoff: datetime | None = None) -> dict:
    """Gap-filled 1-minute UTC grid for every day present in ``frame`` (ts naive UTC).

    ``cutoff`` (naive UTC) is the first minute that is not yet complete; the grid ends just
    before it (live replay). Days with no rows at all are skipped, as the Tester does."""
    ts = pd.to_datetime(frame["ts"]).dt.floor("min")
    keep = np.ones(len(frame), dtype=bool)
    if cutoff is not None:
        keep &= (ts < pd.Timestamp(cutoff)).to_numpy()
    frame = frame.loc[keep].assign(ts=ts[keep]).drop_duplicates("ts", keep="last")
    frame = frame.sort_values("ts")
    days = np.array(sorted(frame["ts"].dt.date.unique()), dtype=object)
    n_days = days.size
    n = n_days * BARS_PER_DAY
    arrays = {c: np.full(n, np.nan) for c in ("open", "high", "low", "close")}
    valid = np.zeros(n, dtype=bool)
    if n_days:
        day_index = {d: k for k, d in enumerate(days)}
        pos = (frame["ts"].dt.date.map(day_index).to_numpy(dtype=np.int64) * BARS_PER_DAY
               + (frame["ts"].dt.hour * 60 + frame["ts"].dt.minute).to_numpy())
        for c in arrays:
            arrays[c][pos] = pd.to_numeric(frame[c], errors="coerce").to_numpy(np.float64)
        valid[pos] = True
        valid &= np.isfinite(arrays["open"]) & np.isfinite(arrays["high"]) \
            & np.isfinite(arrays["low"]) & np.isfinite(arrays["close"])
        for c in arrays:
            arrays[c][~valid] = np.nan
    day_id = np.repeat(np.arange(n_days, dtype=np.int64), BARS_PER_DAY)
    bar_in_day = np.tile(np.arange(BARS_PER_DAY, dtype=np.int64), n_days)
    partial = False
    if cutoff is not None and n_days and days[-1] == cutoff.date():
        avail = max(0, min(BARS_PER_DAY, cutoff.hour * 60 + cutoff.minute))
        if avail < BARS_PER_DAY:
            partial = True
            n = (n_days - 1) * BARS_PER_DAY + avail
            for c in arrays:
                arrays[c] = arrays[c][:n]
            valid, day_id, bar_in_day = valid[:n], day_id[:n], bar_in_day[:n]
    day_start = np.array([np.datetime64(d, "m") for d in days], dtype="datetime64[m]") \
        if n_days else np.array([], dtype="datetime64[m]")
    ts_min = (day_start[day_id] + bar_in_day.astype("timedelta64[m]")) \
        if n else np.array([], dtype="datetime64[m]")
    counts = np.bincount(day_id[valid], minlength=n_days) if n else np.zeros(n_days, int)
    return {**arrays, "valid": valid, "day_id": day_id, "bar_in_day": bar_in_day,
            "ts": ts_min, "days": days, "valid_counts": counts,
            "partial_last_day": partial, "n": n}


def resample(g: dict, tf: int) -> dict:
    """Completed ``tf``-minute bins anchored at 00:00 UTC (the last bin of a partial live
    day is dropped until it closes)."""
    n = g["n"]
    bins_per_day = -(-BARS_PER_DAY // tf)
    key = g["day_id"] * bins_per_day + g["bar_in_day"] // tf
    change = np.ones(n, dtype=bool)
    change[1:] = key[1:] != key[:-1]
    starts = np.flatnonzero(change)
    ends = np.append(starts[1:], n)
    if g["partial_last_day"] and starts.size and (ends[-1] - starts[-1]) < tf:
        starts, ends = starts[:-1], ends[:-1]
    m = starts.size
    o, h, l, c = (np.full(m, np.nan) for _ in range(4))
    ok = np.zeros(m, dtype=bool)
    for k in range(m):
        sl = slice(starts[k], ends[k])
        v = g["valid"][sl]
        if not v.any():
            continue
        ok[k] = True
        o[k] = g["open"][sl][v][0]
        h[k] = g["high"][sl][v].max()
        l[k] = g["low"][sl][v].min()
        c[k] = g["close"][sl][v][-1]
    close_ts = (g["ts"][ends - 1] + np.timedelta64(1, "m")) if m else g["ts"][:0]
    return {"open": o, "high": h, "low": l, "close": c, "valid": ok, "close_ts": close_ts}


def _broadcast(g: dict, htf: dict, state: np.ndarray) -> np.ndarray:
    """A higher-timeframe state on each 1-minute bar: the last bin closed by the bar's close."""
    base_close = (g["ts"] + np.timedelta64(1, "m")).astype("datetime64[ns]").astype(np.int64)
    htf_close = htf["close_ts"].astype("datetime64[ns]").astype(np.int64)
    mp = np.searchsorted(htf_close, base_close, side="right") - 1
    out = np.where(mp >= 0, state[np.maximum(mp, 0)], False)
    return out & g["valid"]


def signal_states(g: dict) -> dict:
    """The two trigger states and the gate state on the 1-minute clock."""
    h1, m15, m5 = resample(g, 60), resample(g, 15), resample(g, 5)
    rsi_h1 = _on_valid(h1["valid"], lambda c: rsi(c, RSI_H1_N), h1["close"])
    roc_m15 = _on_valid(m15["valid"], lambda c: roc(c, ROC_M15_N), m15["close"])
    rsi_m5 = _on_valid(m5["valid"], lambda c: rsi(c, RSI_M5_N), m5["close"])
    return {
        "rsi_h1_x": _broadcast(g, h1, cross_above(rsi_h1, RSI_H1_THR)),
        "roc_m15_x": _broadcast(g, m15, cross_above(roc_m15, ROC_M15_THR)),
        "gate": _broadcast(g, m5, between(rsi_m5, RSI_M5_LO, RSI_M5_HI)),
    }


def fires_from(states: dict) -> np.ndarray:
    """rearm(edge(trigger AND trigger) & gate) -- the Tester's rule_fires."""
    both = states["rsi_h1_x"] & states["roc_m15_x"]
    return rearm(rising_edge(both) & states["gate"], COOLDOWN_BARS)


def _simulate(g: dict, i: int) -> dict:
    """Walk one short entered at open[i+1] (the Tester's single-exit simulator)."""
    j0 = i + 1
    p = g["open"][j0]
    d = p * STOP_PCT / 100.0
    stop = p - DIRECTION * d
    target = p + DIRECTION * TARGET_R * d
    n = g["n"]
    k, last_j, j = 0, -1, j0
    while True:
        if j >= n:
            return {"status": "open", "exit_idx": None, "mark_idx": last_j, "stop": stop,
                    "target": target, "entry": p, "stop_dist": d, "held": k}
        if not g["valid"][j]:
            j += 1
            continue
        k += 1
        last_j = j
        o = g["open"][j]
        common = {"status": "closed", "exit_idx": j, "stop": stop, "target": target,
                  "entry": p, "stop_dist": d, "held": k}
        # short: the stop is above, the target below
        if o >= stop:
            return {**common, "reason": "stop", "exit": o}
        if o <= target:
            return {**common, "reason": "target", "exit": o}
        if g["high"][j] >= stop:
            return {**common, "reason": "stop", "exit": stop}
        if g["low"][j] <= target:
            return {**common, "reason": "target", "exit": target}
        if k >= MAX_HOLD_BARS:
            return {**common, "reason": "time", "exit": g["close"][j]}
        j += 1


TRADE_COLUMNS = ["signal_ts", "entry_ts", "exit_ts", "entry_price", "exit_price", "stop_price",
                 "target_price", "stop_dist", "r_multiple", "points", "status", "exit_reason",
                 "bars_held", "trade_date"]


def replay(frame: pd.DataFrame, cutoff: datetime | None = None) -> tuple[dict, pd.DataFrame]:
    """Replay the rule over ``frame``; returns (grid, trades) with one row per taken trade.
    Timestamps are naive UTC."""
    g = build_grid(frame, cutoff)
    empty = pd.DataFrame(columns=TRADE_COLUMNS)
    n = g["n"]
    if n < 2:
        g["fires"] = np.zeros(n, dtype=bool)
        return g, empty
    fires = fires_from(signal_states(g))
    g["fires"] = fires
    rows = []
    busy_until = -1
    for i in np.flatnonzero(fires):
        if i < busy_until or i + 1 >= n:
            continue
        if not g["valid"][i + 1]:
            continue                                   # no fill price: the Tester's R is NaN
        res = _simulate(g, i)
        entry_ts = pd.Timestamp(g["ts"][i + 1])
        row = {"signal_ts": pd.Timestamp(g["ts"][i]), "entry_ts": entry_ts,
               "entry_price": float(res["entry"]), "stop_price": float(res["stop"]),
               "target_price": float(res["target"]), "stop_dist": float(res["stop_dist"]),
               "trade_date": entry_ts.date().isoformat(), "bars_held": int(res["held"])}
        if res["status"] == "open":
            mark = g["close"][res["mark_idx"]] if res["mark_idx"] >= 0 else res["entry"]
            points = DIRECTION * float(mark - res["entry"])
            row.update({"exit_ts": None, "exit_price": float(mark), "status": "open",
                        "exit_reason": None, "points": points,
                        "r_multiple": points / res["stop_dist"]})
            rows.append(row)
            break                                      # one position at a time; still running
        points = DIRECTION * float(res["exit"] - res["entry"])
        row.update({"exit_ts": pd.Timestamp(g["ts"][res["exit_idx"]]),
                    "exit_price": float(res["exit"]), "status": "closed",
                    "exit_reason": res["reason"], "points": points,
                    "r_multiple": points / res["stop_dist"]})
        rows.append(row)
        busy_until = res["exit_idx"]
    return g, (pd.DataFrame(rows) if rows else empty)


# -------------------------------------------------------------------- data ---
def _http_json(url: str, params: dict, timeout: float = 20.0):
    req = urllib.request.Request(f"{url}?{urllib.parse.urlencode(params)}",
                                 headers={"User-Agent": "labs-paper/1.0"})
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.loads(resp.read().decode("utf-8"))


def fetch_klines(start_ms: int, end_ms: int, limit: int = 1000) -> list:
    """One page of 1-minute klines [start_ms, end_ms] from the first endpoint that answers."""
    errors = []
    params = {"symbol": SYMBOL, "interval": "1m", "startTime": int(start_ms),
              "endTime": int(end_ms), "limit": int(limit)}
    for url in KLINE_ENDPOINTS:
        try:
            return _http_json(url, params)
        except (urllib.error.URLError, TimeoutError, ValueError, OSError) as exc:
            errors.append(f"{url.split('/')[2]}: {getattr(exc, 'code', '') or type(exc).__name__}")
    raise BtcInputError("Binance klines unavailable (" + "; ".join(errors) + ")")


def _utc_naive(now: datetime) -> datetime:
    if now.tzinfo is None:
        now = now.replace(tzinfo=IST)
    return now.astimezone(UTC).replace(tzinfo=None)


def ensure_minute_bars(start: date, now: datetime, conn: sqlite3.Connection,
                       max_pages: int | None = None) -> dict:
    """Store completed 1-minute candles from ``start`` (UTC day) up to the last closed minute.

    Resumes after the newest stored bar (and back-fills before the oldest if ``start`` is
    earlier). ``max_pages`` bounds one call (UI backfill); returns pages used and whether
    the store is now complete up to the last closed minute."""
    now_utc = _utc_naive(now).replace(second=0, microsecond=0)   # first incomplete minute
    last_closed = now_utc - timedelta(minutes=1)
    lo, hi = conn.execute("SELECT MIN(ts), MAX(ts) FROM btc_minute_bars WHERE symbol=?",
                          (SYMBOL,)).fetchone()
    begin = datetime.combine(start, datetime.min.time())
    if lo is not None and pd.Timestamp(lo) <= pd.Timestamp(begin):
        cursor = pd.Timestamp(hi).to_pydatetime() + timedelta(minutes=1)
    else:
        cursor = begin                                   # empty or starts later: refill all
    pages = 0
    while cursor <= last_closed:
        if max_pages is not None and pages >= max_pages:
            break
        start_ms = int(cursor.replace(tzinfo=UTC).timestamp() * 1000)
        end_ms = int(last_closed.replace(tzinfo=UTC).timestamp() * 1000)
        page = fetch_klines(start_ms, end_ms)
        pages += 1
        rows = []
        for k in page:
            ts = datetime.fromtimestamp(int(k[0]) / 1000, tz=UTC).replace(tzinfo=None)
            if ts > last_closed:                          # still forming
                continue
            rows.append((SYMBOL, ts.strftime("%Y-%m-%d %H:%M:00"), float(k[1]), float(k[2]),
                         float(k[3]), float(k[4]), float(k[5])))
        conn.executemany(
            "INSERT INTO btc_minute_bars (symbol,ts,open,high,low,close,volume) "
            "VALUES (?,?,?,?,?,?,?) ON CONFLICT(symbol,ts) DO UPDATE SET open=excluded.open,"
            "high=excluded.high,low=excluded.low,close=excluded.close,volume=excluded.volume",
            rows)
        conn.commit()
        if not page:
            break                                          # nothing listed past the cursor
        cursor = datetime.fromtimestamp(int(page[-1][0]) / 1000, tz=UTC).replace(tzinfo=None) \
            + timedelta(minutes=1)
    return {"pages": pages, "complete": cursor > last_closed, "through": str(last_closed)}


def load_minute_bars(start: date, conn: sqlite3.Connection) -> pd.DataFrame:
    frame = pd.read_sql_query(
        "SELECT ts,open,high,low,close,volume FROM btc_minute_bars WHERE symbol=? AND ts>=? "
        "ORDER BY ts", conn, params=(SYMBOL, start.isoformat()))
    frame["ts"] = pd.to_datetime(frame["ts"])
    return frame


# ---------------------------------------------------------------------- fx ---
def ensure_fx(start: date, end: date, conn: sqlite3.Connection) -> None:
    """Cache ECB USD/INR reference rates covering start..end (only what is missing)."""
    have = sorted(r[0] for r in conn.execute("SELECT rate_date FROM fx_usd_inr"))
    lo, hi = start - timedelta(days=10), end
    need = []
    if not have:
        need.append((lo, hi))
    else:
        first, last = date.fromisoformat(have[0]), date.fromisoformat(have[-1])
        if lo < first:
            need.append((lo, first - timedelta(days=1)))
        if hi > last + timedelta(days=3):               # allow for a weekend before refreshing
            need.append((last + timedelta(days=1), hi))
    stamp = datetime.now(IST).isoformat(timespec="seconds")
    for a, b in need:
        if a > b:
            continue
        payload = _http_json(FX_URL.format(start=a.isoformat(), end=b.isoformat()),
                             {"base": "USD", "symbols": "INR"})
        rows = [(d, float(v["INR"]), FX_SOURCE, stamp)
                for d, v in (payload.get("rates") or {}).items() if "INR" in v]
        conn.executemany("INSERT OR REPLACE INTO fx_usd_inr (rate_date,rate,source,fetched_at) "
                         "VALUES (?,?,?,?)", rows)
        conn.commit()


def fx_rates(conn: sqlite3.Connection) -> pd.Series:
    rows = conn.execute("SELECT rate_date, rate FROM fx_usd_inr ORDER BY rate_date").fetchall()
    if not rows:
        raise BtcInputError("No USD/INR rates cached")
    s = pd.Series({r[0]: r[1] for r in rows}, dtype="float64")
    s.index = pd.to_datetime(s.index)
    return s.sort_index()


def rate_on(rates: pd.Series, day) -> float:
    """The rate in force on ``day``: last published on or before it (else the first)."""
    idx = np.searchsorted(rates.index.values, np.datetime64(pd.Timestamp(day).normalize()),
                          side="right") - 1
    return float(rates.to_numpy()[min(max(idx, 0), len(rates) - 1)])


# ------------------------------------------------------------------ ledger ---
def price_trade(t: dict, rates: pd.Series, today_utc: date) -> dict:
    """USDT and rupee money for one trade (the Tester's add_costs + convert_trades 'daily')."""
    entry, exit_ = float(t["entry_price"]), float(t["exit_price"])
    buy, sell = exit_ * QTY, entry * QTY                 # short: sell first, buy back
    gross = float(t["points"]) * QTY
    charges = (buy + sell) * FEE_PCT / 100.0
    slippage = 2.0 * SLIPPAGE_TICKS * TICK * QTY
    net = gross - charges - slippage
    exit_day = pd.Timestamp(t["exit_ts"]).date() if t.get("exit_ts") else today_utc
    r_exit = rate_on(rates, exit_day)
    r_entry = rate_on(rates, pd.Timestamp(t["entry_ts"]).date())
    notional = entry * QTY * r_entry
    return {"gross_usdt": round(gross, 6), "charges_usdt": round(charges, 6),
            "slippage_usdt": round(slippage, 6), "net_usdt": round(net, 6),
            "fx_rate": r_exit, "fx_entry_rate": r_entry,
            "gross_rs": round(gross * r_exit, 2), "charges_rs": round(charges * r_exit, 2),
            "slippage_rs": round(slippage * r_exit, 2), "net_rs": round(net * r_exit, 2),
            "notional_rs": round(notional, 2), "margin_rs": round(notional, 2),
            "margin_source": "spot_fully_funded"}


def _ts(value) -> str | None:
    if value is None or (not isinstance(value, str) and pd.isna(value)):
        return None
    return pd.Timestamp(value).strftime("%Y-%m-%d %H:%M:%S")


def persist(conn: sqlite3.Connection, g: dict, trades: pd.DataFrame, rates: pd.Series,
            today_utc: date, error: str | None = None) -> dict:
    """Rewrite the ledger from BOOK_START: one trades row per entry, one daily row per UTC day."""
    now = datetime.now(IST).isoformat(timespec="seconds")
    book = trades[trades["trade_date"] >= BOOK_START.isoformat()] if len(trades) else trades
    priced, per_day = [], {}
    for t in book.to_dict("records"):
        money = price_trade(t, rates, today_utc)
        day = t["trade_date"]
        seq = per_day.get(day, 0) + 1
        per_day[day] = seq
        priced.append({**t, **money, "seq": seq})
    conn.execute("DELETE FROM btc_rsi_roc_trades WHERE trade_date>=?", (BOOK_START.isoformat(),))
    conn.executemany(
        "INSERT INTO btc_rsi_roc_trades (trade_date,seq,tradingsymbol,signal_ts,entry_ts,exit_ts,"
        "entry_price,exit_price,stop_price,target_price,stop_dist,r_multiple,points,qty,"
        "gross_usdt,charges_usdt,slippage_usdt,net_usdt,fx_rate,fx_entry_rate,gross_rs,charges_rs,"
        "slippage_rs,net_rs,notional_rs,margin_rs,margin_source,status,exit_reason,bars_held) "
        "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        [(t["trade_date"], t["seq"], SYMBOL, _ts(t["signal_ts"]), _ts(t["entry_ts"]),
          _ts(t.get("exit_ts")), t["entry_price"], t["exit_price"], round(t["stop_price"], 4),
          round(t["target_price"], 4), round(t["stop_dist"], 4), round(t["r_multiple"], 4),
          round(t["points"], 4), QTY, t["gross_usdt"], t["charges_usdt"], t["slippage_usdt"],
          t["net_usdt"], t["fx_rate"], t["fx_entry_rate"], t["gross_rs"], t["charges_rs"],
          t["slippage_rs"], t["net_rs"], t["notional_rs"], t["margin_rs"], t["margin_source"],
          t["status"], t["exit_reason"], t["bars_held"]) for t in priced])
    fires_by_day: dict[str, int] = {}
    if g["n"]:
        for i in np.flatnonzero(g["fires"]):
            key = str(g["days"][g["day_id"][i]])
            fires_by_day[key] = fires_by_day.get(key, 0) + 1
    conn.execute("DELETE FROM btc_rsi_roc_daily WHERE trade_date>=?", (BOOK_START.isoformat(),))
    rows = []
    for k, d in enumerate(g["days"]):
        if d < BOOK_START:
            continue
        key = d.isoformat()
        todays = [t for t in priced if t["trade_date"] == key]
        status = "live" if (d == today_utc and g["partial_last_day"]) else "final"
        rows.append((key, status, SYMBOL, None, int(g["valid_counts"][k]),
                     fires_by_day.get(key, 0), len(todays),
                     sum(t["status"] == "open" for t in todays),
                     sum(t["status"] == "closed" and t["net_rs"] > 0 for t in todays),
                     round(sum(t["gross_rs"] for t in todays), 2),
                     round(sum(t["charges_rs"] for t in todays), 2),
                     round(sum(t["slippage_rs"] for t in todays), 2),
                     round(sum(t["net_rs"] for t in todays), 2),
                     round(sum(t["net_usdt"] for t in todays), 6), QTY, STRATEGY_VERSION,
                     error, now))
    conn.executemany(
        "INSERT OR REPLACE INTO btc_rsi_roc_daily (trade_date,status,tradingsymbol,expiry,"
        "valid_bars,n_signals,n_trades,open_trades,wins,gross_rs,charges_rs,slippage_rs,net_rs,"
        "net_usdt,qty,strategy_version,error,updated_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        rows)
    conn.commit()
    return {"trades": len(priced), "open": sum(t["status"] == "open" for t in priced),
            "net_rs": round(sum(t["net_rs"] for t in priced if t["status"] == "closed"), 2)}


def replay_anchor() -> date:
    """Fixed start of every replay. With one position at a time the replay start decides
    which later signals are taken, so it never moves: the ledger is deterministic, and 30
    days of warm-up settle the 1h RSI(7) to float precision long before BOOK_START."""
    return BOOK_START - timedelta(days=WARMUP_DAYS)


def run_live(now: datetime | None = None, *, connection: sqlite3.Connection | None = None,
             max_pages: int | None = None) -> dict:
    """Fetch closed candles, replay from the fixed anchor and rewrite the ledger. Idempotent;
    an open position is marked at the last completed 1-minute close."""
    now = now or datetime.now(IST)
    own = connection is None
    conn = connection or get_conn()
    _ensure_tables(conn)
    try:
        anchor = replay_anchor()
        data = ensure_minute_bars(anchor, now, conn, max_pages=max_pages)
        if not data["complete"]:
            return {"status": "backfilling", **data}
        now_utc = _utc_naive(now)
        frame = load_minute_bars(anchor, conn)
        cutoff = now_utc.replace(second=0, microsecond=0)
        g, trades = replay(frame, cutoff)
        ensure_fx(BOOK_START, now_utc.date(), conn)
        summary = persist(conn, g, trades, fx_rates(conn), now_utc.date())
        return {"status": "live", "through": data["through"], **summary}
    finally:
        if own:
            conn.close()


# --------------------------------------------------------------------- tab ---
def _ist(value) -> str | None:
    if not value:
        return None
    return (pd.Timestamp(value).tz_localize(UTC).tz_convert(IST)
            .strftime("%Y-%m-%d %H:%M"))


def tab_data(conn: sqlite3.Connection, date_clause: str = "", date_params=()) -> tuple:
    """(daily rows, trades, stats) for the /labs/live tab; trade times shown in IST."""
    cur = conn.execute(
        "SELECT trade_date,status,tradingsymbol,valid_bars,n_signals,n_trades,open_trades,wins,"
        "gross_rs,charges_rs,slippage_rs,net_rs,net_usdt,qty,error,updated_at "
        f"FROM btc_rsi_roc_daily WHERE 1=1 {date_clause} ORDER BY trade_date DESC LIMIT 400",
        tuple(date_params))
    cols = [c[0] for c in cur.description]
    rows = [dict(zip(cols, r)) for r in cur.fetchall()]
    cur = conn.execute(
        "SELECT trade_date,seq,signal_ts,entry_ts,exit_ts,entry_price,exit_price,stop_price,"
        "target_price,r_multiple,points,qty,net_usdt,fx_rate,gross_rs,charges_rs,slippage_rs,"
        "net_rs,notional_rs,status,exit_reason,bars_held "
        f"FROM btc_rsi_roc_trades WHERE 1=1 {date_clause} "
        "ORDER BY trade_date DESC, seq DESC LIMIT 500", tuple(date_params))
    cols = [c[0] for c in cur.description]
    trades = [dict(zip(cols, r)) for r in cur.fetchall()]
    for t in trades:
        t["signal_ist"], t["entry_ist"], t["exit_ist"] = (
            _ist(t["signal_ts"]), _ist(t["entry_ts"]), _ist(t["exit_ts"]))
    if not rows:
        return rows, trades, {}
    closed = [t for t in trades if t["status"] == "closed"]
    wins = [t for t in closed if float(t["net_rs"] or 0) > 0]
    equity = peak = max_dd = 0.0
    for t in reversed(closed):
        equity += float(t["net_rs"] or 0)
        peak = max(peak, equity)
        max_dd = max(max_dd, peak - equity)
    stats = {
        "days": len(rows), "trades": len(closed), "wins": len(wins),
        "open_trades": sum(1 for t in trades if t["status"] == "open"),
        "win_pct": round(100 * len(wins) / max(len(closed), 1), 1),
        "targets": sum(1 for t in closed if t["exit_reason"] == "target"),
        "stops": sum(1 for t in closed if t["exit_reason"] == "stop"),
        "gross_total": round(sum(float(t["gross_rs"] or 0) for t in closed), 2),
        "charges_total": round(sum(float(t["charges_rs"] or 0) for t in closed), 2),
        "slippage_total": round(sum(float(t["slippage_rs"] or 0) for t in closed), 2),
        "net_total": round(sum(float(t["net_rs"] or 0) for t in closed), 2),
        "net_usdt": round(sum(float(t["net_usdt"] or 0) for t in closed), 2),
        "open_net": round(sum(float(t["net_rs"] or 0) for t in trades if t["status"] == "open"), 2),
        "avg_r": round(sum(float(t["r_multiple"] or 0) for t in closed) / max(len(closed), 1), 3),
        "max_dd": round(max_dd, 2),
        "first_date": rows[-1]["trade_date"], "last_date": rows[0]["trade_date"],
        "latest": rows[0],
    }
    return rows, trades, stats


if __name__ == "__main__":
    print(json.dumps(run_live(), indent=2, default=str))
