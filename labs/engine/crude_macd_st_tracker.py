"""Paper-only CRUDEOIL futures book: MACD-histogram + Supertrend flip, long, 1 lot.

Rule (Strategy Tester run crudeoil/ui_20260919_084538_9143, rank 194)::

    macd(12,26,9)@5m hist x< 0  &  supertrend(7,2)@5m dir x< 0
    gate: 1-min close > previous session close
    long, stop 1.5 x ATR(14)@5m, target 0.5R, no time stop, flat at session end

The replay is a port of the Tester's engine so that paper results match its research
numbers: a gap-filled 1-minute MCX grid (09:00-23:29), 5-minute bins anchored at the
session open, TradingView-seeded EMA/RMA computed on valid bins only, a 5-minute value
visible from the close of its bin, entries on the rising edge of the AND of both trigger
states with a 30-bar cooldown, fill at the next 1-minute bar's open, stop and target
checked on 1-minute highs/lows (stop wins inside a bar, a gap through either exits at the
open), one position at a time, flat at the last bar of the session. Charges are Zerodha's
MCX futures schedule, no slippage. The book never calls a broker order API.

Data: completed 1-minute candles of the front CRUDEOIL future are pulled from Kite
historical_data into ``crude_minute_bars``. The front contract for a session is the nearest
listed expiry strictly after that session, so a contract is rolled on its expiry day. Kite
lists only unexpired contracts, so history before September 2026 replays on CRUDEOIL26SEPFUT,
exactly as the research did.
"""
from __future__ import annotations

from datetime import date, datetime, time, timedelta, timezone
import math
import sqlite3
import time as _time

import numpy as np
import pandas as pd

from labs.engine.charges import mcx_futures_round_trip_charges
from storage.db import get_conn


IST = timezone(timedelta(hours=5, minutes=30))
STRATEGY_VERSION = "crudeoil_macd12269_st72_5m_pdc_long_sl15atr_tp05r_v1"
EXCHANGE = "MCX"
UNDERLYING = "CRUDEOIL"
LOTS = 1
LOT_QTY = 100                    # CRUDEOIL: 1 lot = 100 barrels
QTY = LOTS * LOT_QTY

SESSION_OPEN_MIN = 9 * 60        # 09:00, first bar
SESSION_CLOSE_MIN = 23 * 60 + 29 # 23:29, last bar (the Tester's MCX session)
BARS_PER_DAY = SESSION_CLOSE_MIN - SESSION_OPEN_MIN + 1
TF = 5
MIN_VALID_FRAC = 0.5             # sessions with fewer valid bars take no entries
MACD_FAST, MACD_SLOW, MACD_SIGNAL = 12, 26, 9
ST_N, ST_MULT = 7, 2.0
ATR_N = 14
STOP_ATR_MULT = 1.5
TARGET_R = 0.5
COOLDOWN_BARS = 30
MAX_HOLD_BARS = 870
REPLAY_LOOKBACK_DAYS = 60        # calendar days of history replayed before a session
FINAL_AFTER = time(23, 40)       # a session is frozen once the loop runs past this
KITE_CHUNK_DAYS = 55             # Kite minute history is limited to 60 days per call
NS_PER_MIN = 60_000_000_000


class CrudeInputError(RuntimeError):
    """Required market data is unavailable."""


# ------------------------------------------------------------------ storage ---
def _ensure_tables(conn: sqlite3.Connection) -> None:
    conn.executescript(
        """
        CREATE TABLE IF NOT EXISTS crude_minute_bars (
            tradingsymbol TEXT NOT NULL,
            ts TEXT NOT NULL,
            open REAL NOT NULL,
            high REAL NOT NULL,
            low REAL NOT NULL,
            close REAL NOT NULL,
            volume REAL NOT NULL,
            PRIMARY KEY (tradingsymbol, ts)
        );
        CREATE TABLE IF NOT EXISTS crude_minute_coverage (
            tradingsymbol TEXT NOT NULL,
            trade_date TEXT NOT NULL,
            fetched_at TEXT NOT NULL,
            PRIMARY KEY (tradingsymbol, trade_date)
        );
        CREATE TABLE IF NOT EXISTS crude_macd_st_daily (
            trade_date TEXT PRIMARY KEY,
            status TEXT NOT NULL,
            tradingsymbol TEXT,
            expiry TEXT,
            pdc REAL,
            valid_bars INTEGER,
            n_signals INTEGER NOT NULL DEFAULT 0,
            n_trades INTEGER NOT NULL DEFAULT 0,
            open_trades INTEGER NOT NULL DEFAULT 0,
            wins INTEGER NOT NULL DEFAULT 0,
            gross_rs REAL NOT NULL DEFAULT 0,
            charges_rs REAL NOT NULL DEFAULT 0,
            net_rs REAL NOT NULL DEFAULT 0,
            qty INTEGER NOT NULL,
            strategy_version TEXT NOT NULL,
            error TEXT,
            updated_at TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS crude_macd_st_trades (
            trade_date TEXT NOT NULL,
            seq INTEGER NOT NULL,
            tradingsymbol TEXT NOT NULL,
            signal_ts TEXT NOT NULL,
            entry_ts TEXT NOT NULL,
            exit_ts TEXT,
            entry_price REAL NOT NULL,
            exit_price REAL,
            stop_price REAL NOT NULL,
            target_price REAL NOT NULL,
            stop_dist REAL NOT NULL,
            r_multiple REAL,
            points REAL,
            qty INTEGER NOT NULL,
            gross_rs REAL,
            charges_rs REAL,
            net_rs REAL,
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
    (TradingView ta.ema / ta.rma convention, as in the Tester)."""
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


def ema(x, n: int) -> np.ndarray:
    return _ewm_seeded(np.asarray(x, dtype=np.float64), 2.0 / (n + 1.0), int(n))


def rma(x, n: int) -> np.ndarray:
    return _ewm_seeded(np.asarray(x, dtype=np.float64), 1.0 / n, int(n))


def true_range(high, low, close) -> np.ndarray:
    prev_close = np.r_[np.nan, close[:-1]]
    tr = np.fmax(high - low, np.fmax(np.abs(high - prev_close), np.abs(low - prev_close)))
    if tr.size:
        tr[0] = high[0] - low[0]
    return tr


def macd_hist(close) -> np.ndarray:
    line = ema(close, MACD_FAST) - ema(close, MACD_SLOW)
    return line - ema(line, MACD_SIGNAL)


def atr(high, low, close, n: int = ATR_N) -> np.ndarray:
    return rma(true_range(high, low, close), n)


def supertrend_dir(high, low, close, n: int = ST_N, mult: float = ST_MULT) -> np.ndarray:
    a = rma(true_range(high, low, close), n)
    d = np.full(close.shape[0], np.nan)
    ub_prev = lb_prev = math.nan
    dir_prev = 1.0
    started = False
    for i in range(close.shape[0]):
        if not math.isfinite(a[i]):
            continue
        hl2 = (high[i] + low[i]) / 2.0
        ub = hl2 + mult * a[i]
        lb = hl2 - mult * a[i]
        if started:
            pc = close[i - 1]
            if not (lb > lb_prev or pc < lb_prev):
                lb = lb_prev
            if not (ub < ub_prev or pc > ub_prev):
                ub = ub_prev
            if dir_prev == 1.0:
                di = -1.0 if close[i] < lb else 1.0
            else:
                di = 1.0 if close[i] > ub else -1.0
        else:
            di = -1.0            # TradingView starts in a downtrend
            started = True
        d[i] = di
        ub_prev, lb_prev, dir_prev = ub, lb, di
    return d


def _on_valid(valid: np.ndarray, func, *arrays) -> np.ndarray:
    """Run an indicator on the valid rows only and scatter it back (NaN elsewhere)."""
    idx = np.flatnonzero(valid)
    out = np.full(valid.shape[0], np.nan)
    if idx.size:
        out[idx] = func(*[np.ascontiguousarray(a[idx]) for a in arrays])
    return out


def cross_below_zero(x: np.ndarray) -> np.ndarray:
    prev = np.r_[np.nan, x[:-1]]
    with np.errstate(invalid="ignore"):
        return (prev >= 0.0) & (x < 0.0)


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
    """Gap-filled 1-minute session grid for every weekday present in ``frame``.

    ``frame`` has ts (naive IST), open, high, low, close. ``cutoff`` is the first minute that
    is not yet complete; the grid of that session ends just before it (live replay).
    """
    ts = pd.to_datetime(frame["ts"]).dt.floor("min")
    minute = (ts.dt.hour * 60 + ts.dt.minute).to_numpy()
    keep = ((minute >= SESSION_OPEN_MIN) & (minute <= SESSION_CLOSE_MIN)
            & (ts.dt.weekday < 5).to_numpy())
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
               + (frame["ts"].dt.hour * 60 + frame["ts"].dt.minute).to_numpy() - SESSION_OPEN_MIN)
        for c in arrays:
            arrays[c][pos] = pd.to_numeric(frame[c], errors="coerce").to_numpy(np.float64)
        valid[pos] = True
        valid &= np.isfinite(arrays["open"]) & np.isfinite(arrays["high"]) \
            & np.isfinite(arrays["low"]) & np.isfinite(arrays["close"])
        for c in arrays:
            arrays[c][~valid] = np.nan
    day_id = np.repeat(np.arange(n_days, dtype=np.int64), BARS_PER_DAY)
    bar_in_day = np.tile(np.arange(BARS_PER_DAY, dtype=np.int64), n_days)
    day_len = np.full(n_days, BARS_PER_DAY, dtype=np.int64)
    partial = False
    if cutoff is not None and n_days and days[-1] == cutoff.date():
        avail = (cutoff.hour * 60 + cutoff.minute) - SESSION_OPEN_MIN
        avail = max(0, min(BARS_PER_DAY, avail))
        if avail < BARS_PER_DAY:
            partial = True
            n = (n_days - 1) * BARS_PER_DAY + avail
            day_len[-1] = avail
            for c in arrays:
                arrays[c] = arrays[c][:n]
            valid, day_id, bar_in_day = valid[:n], day_id[:n], bar_in_day[:n]
    day_start = np.array([np.datetime64(d, "m") for d in days], dtype="datetime64[m]") \
        if n_days else np.array([], dtype="datetime64[m]")
    ts_min = (day_start[day_id] + (SESSION_OPEN_MIN + bar_in_day).astype("timedelta64[m]")) \
        if n else np.array([], dtype="datetime64[m]")
    counts = np.bincount(day_id[valid], minlength=n_days) if n else np.zeros(n_days, int)
    day_ok = counts >= MIN_VALID_FRAC * np.maximum(day_len, 1)
    return {**arrays, "valid": valid, "day_id": day_id, "bar_in_day": bar_in_day,
            "ts": ts_min, "days": days, "day_ok": day_ok, "valid_counts": counts,
            "partial_last_day": partial, "n": n}


def _five_minute(g: dict) -> dict:
    """Session-anchored 5-minute bins; an incomplete trailing bin is left out."""
    n = g["n"]
    bins_per_day = -(-BARS_PER_DAY // TF)
    key = g["day_id"] * bins_per_day + g["bar_in_day"] // TF
    change = np.ones(n, dtype=bool)
    change[1:] = key[1:] != key[:-1]
    starts = np.flatnonzero(change)
    ends = np.append(starts[1:], n)
    if g["partial_last_day"] and starts.size and (ends[-1] - starts[-1]) < TF:
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


def _simulate(g: dict, i: int, stop_dist: float) -> dict:
    """Walk one long trade entered at open[i+1] (the Tester's single-exit simulator)."""
    j0 = i + 1
    p = g["open"][j0]
    stop = p - stop_dist
    target = p + TARGET_R * stop_dist
    day = g["day_id"][j0]
    n = g["n"]
    k, last_j, j = 0, -1, j0
    while True:
        if j >= n:
            if g["partial_last_day"] and day == g["day_id"][n - 1]:
                return {"status": "open", "exit_idx": None, "mark_idx": last_j,
                        "stop": stop, "target": target, "entry": p, "held": k}
            return {"status": "closed", "reason": "eod", "exit_idx": last_j,
                    "exit": g["close"][last_j], "stop": stop, "target": target, "entry": p, "held": k}
        if g["day_id"][j] != day:
            return {"status": "closed", "reason": "eod", "exit_idx": last_j,
                    "exit": g["close"][last_j], "stop": stop, "target": target, "entry": p, "held": k}
        if not g["valid"][j]:
            j += 1
            continue
        k += 1
        last_j = j
        o = g["open"][j]
        common = {"status": "closed", "exit_idx": j, "stop": stop, "target": target,
                  "entry": p, "held": k}
        if o <= stop:
            return {**common, "reason": "stop", "exit": o}
        if o >= target:
            return {**common, "reason": "target", "exit": o}
        if g["low"][j] <= stop:
            return {**common, "reason": "stop", "exit": stop}
        if g["high"][j] >= target:
            return {**common, "reason": "target", "exit": target}
        if k >= MAX_HOLD_BARS:
            return {**common, "reason": "time", "exit": g["close"][j]}
        j += 1


def replay(frame: pd.DataFrame, cutoff: datetime | None = None) -> tuple[dict, pd.DataFrame]:
    """Replay the rule over ``frame``; returns (grid, trades) with one row per taken trade."""
    g = build_grid(frame, cutoff)
    empty = pd.DataFrame(columns=["signal_ts", "entry_ts", "exit_ts", "entry_price", "exit_price",
                                  "stop_price", "target_price", "stop_dist", "r_multiple",
                                  "points", "status", "exit_reason", "bars_held", "trade_date"])
    n = g["n"]
    if n < 2:
        g["fires"] = np.zeros(n, dtype=bool)
        return g, empty
    five = _five_minute(g)
    v5 = five["valid"]
    hist = _on_valid(v5, macd_hist, five["close"])
    st_dir = _on_valid(v5, supertrend_dir, five["high"], five["low"], five["close"])
    atr5 = _on_valid(v5, atr, five["high"], five["low"], five["close"])
    # a 5-minute value is usable on a 1-minute bar once its bin has closed by that bar's close
    base_close = (g["ts"] + np.timedelta64(1, "m")).astype("datetime64[ns]").astype(np.int64)
    htf_close = five["close_ts"].astype("datetime64[ns]").astype(np.int64)
    mp = np.searchsorted(htf_close, base_close, side="right") - 1
    has = mp >= 0
    safe = np.maximum(mp, 0)
    macd_x = np.where(has, cross_below_zero(hist)[safe], False) & g["valid"]
    st_x = np.where(has, cross_below_zero(st_dir)[safe], False) & g["valid"]
    atr_b = np.where(has, atr5[safe], np.nan)
    # previous session close, constant through the day
    n_days = len(g["days"])
    day_close = np.full(n_days, np.nan)
    vidx = np.flatnonzero(g["valid"])
    if vidx.size:
        last_per_day = pd.Series(vidx).groupby(g["day_id"][vidx]).max()
        day_close[last_per_day.index.to_numpy()] = g["close"][last_per_day.to_numpy()]
    pdc = np.r_[np.nan, day_close[:-1]][g["day_id"]] if n_days else np.array([])
    with np.errstate(invalid="ignore"):
        gate = g["close"] > pdc
    fires = rearm(rising_edge(macd_x & st_x) & gate, COOLDOWN_BARS)
    g["fires"], g["pdc_by_day"] = fires, np.r_[np.nan, day_close[:-1]]

    rows = []
    busy_until = -1
    for i in np.flatnonzero(fires):
        if i < busy_until or i + 1 >= n:
            continue
        nxt = i + 1
        if not (g["valid"][i] and g["valid"][nxt] and g["day_ok"][g["day_id"][nxt]]
                and g["day_id"][nxt] == g["day_id"][i]):
            continue
        dist = STOP_ATR_MULT * atr_b[i]
        if not (dist > 0):
            continue
        res = _simulate(g, i, dist)
        entry_ts = pd.Timestamp(g["ts"][nxt])
        row = {
            "signal_ts": pd.Timestamp(g["ts"][i]), "entry_ts": entry_ts,
            "entry_price": float(res["entry"]), "stop_price": float(res["stop"]),
            "target_price": float(res["target"]), "stop_dist": float(dist),
            "trade_date": entry_ts.date().isoformat(), "bars_held": int(res["held"]),
        }
        if res["status"] == "open":
            mark = g["close"][res["mark_idx"]] if res["mark_idx"] >= 0 else res["entry"]
            row.update({"exit_ts": None, "exit_price": float(mark), "status": "open",
                        "exit_reason": None,
                        "points": float(mark - res["entry"]),
                        "r_multiple": float((mark - res["entry"]) / dist)})
            rows.append(row)
            break                                  # one position at a time; still running
        points = float(res["exit"] - res["entry"])
        row.update({"exit_ts": pd.Timestamp(g["ts"][res["exit_idx"]]),
                    "exit_price": float(res["exit"]), "status": "closed",
                    "exit_reason": res["reason"], "points": points,
                    "r_multiple": points / dist})
        rows.append(row)
        busy_until = res["exit_idx"]
    return g, (pd.DataFrame(rows) if rows else empty)


# -------------------------------------------------------------------- data ---
_INSTRUMENTS: dict = {"date": None, "rows": []}


def _crude_futures(kite) -> list[dict]:
    today = datetime.now(IST).date()
    if _INSTRUMENTS["date"] != today or not _INSTRUMENTS["rows"]:
        rows = [r for r in kite.instruments(EXCHANGE)
                if r.get("name") == UNDERLYING and r.get("instrument_type") == "FUT"]
        _INSTRUMENTS.update(date=today, rows=rows)
    return _INSTRUMENTS["rows"]


def front_contract(contracts: list[dict], session: date) -> dict:
    """Nearest listed expiry strictly after the session (roll on the expiry day)."""
    def _expiry(r):
        e = r["expiry"]
        return e if isinstance(e, date) else date.fromisoformat(str(e)[:10])
    live = sorted((r for r in contracts if _expiry(r) > session), key=_expiry)
    if not live:
        raise CrudeInputError(f"No listed {UNDERLYING} future expiring after {session}")
    pick = live[0]
    return {"tradingsymbol": pick["tradingsymbol"], "instrument_token": int(pick["instrument_token"]),
            "expiry": _expiry(pick).isoformat()}


def _to_ist_naive(value) -> datetime:
    if isinstance(value, str):
        value = datetime.fromisoformat(value)
    if value.tzinfo is not None:
        value = value.astimezone(IST).replace(tzinfo=None)
    return value


def ensure_minute_bars(kite, contract: dict, start: date, end: date, now: datetime,
                       conn: sqlite3.Connection) -> int:
    """Pull completed 1-minute candles for [start, end] that are not stored yet.

    A past session is fetched once and marked covered; the session in progress is
    re-fetched each call and only candles that have closed are stored."""
    symbol = contract["tradingsymbol"]
    covered = {r[0] for r in conn.execute(
        "SELECT trade_date FROM crude_minute_coverage WHERE tradingsymbol=? "
        "AND trade_date>=? AND trade_date<=?", (symbol, start.isoformat(), end.isoformat()))}
    now_naive = _to_ist_naive(now)
    final_cut = datetime.combine(now_naive.date(), FINAL_AFTER)
    wanted = [start + timedelta(days=k) for k in range((end - start).days + 1)]
    wanted = [d for d in wanted if d.weekday() < 5 and d.isoformat() not in covered
              and d <= now_naive.date()]
    stored = 0
    chunk: list[date] = []
    groups: list[list[date]] = []
    for d in wanted:
        if chunk and ((d - chunk[0]).days >= KITE_CHUNK_DAYS or (d - chunk[-1]).days > 7):
            groups.append(chunk)
            chunk = []
        chunk.append(d)
    if chunk:
        groups.append(chunk)
    for group in groups:
        frm = datetime.combine(group[0], time(0, 0))
        to = min(datetime.combine(group[-1], time(23, 59)), now_naive)
        candles = kite.historical_data(contract["instrument_token"], frm, to, "minute")
        minute_now = now_naive.replace(second=0, microsecond=0)
        rows = []
        for c in candles:
            ts = _to_ist_naive(c["date"]).replace(second=0, microsecond=0)
            if ts >= minute_now:          # still forming
                continue
            rows.append((symbol, ts.strftime("%Y-%m-%d %H:%M:%S"), float(c["open"]),
                         float(c["high"]), float(c["low"]), float(c["close"]),
                         float(c.get("volume") or 0)))
        conn.executemany(
            "INSERT INTO crude_minute_bars (tradingsymbol,ts,open,high,low,close,volume) "
            "VALUES (?,?,?,?,?,?,?) ON CONFLICT(tradingsymbol,ts) DO UPDATE SET "
            "open=excluded.open,high=excluded.high,low=excluded.low,close=excluded.close,"
            "volume=excluded.volume", rows)
        stamp = datetime.now(IST).isoformat(timespec="seconds")
        done = [(symbol, d.isoformat(), stamp) for d in group
                if d < now_naive.date() or now_naive >= final_cut]
        conn.executemany(
            "INSERT OR REPLACE INTO crude_minute_coverage (tradingsymbol,trade_date,fetched_at) "
            "VALUES (?,?,?)", done)
        conn.commit()
        stored += len(rows)
        if len(groups) > 1:
            _time.sleep(0.35)             # Kite historical: ~3 requests per second
    return stored


def load_minute_bars(symbol: str, start: date, end: date, conn: sqlite3.Connection) -> pd.DataFrame:
    frame = pd.read_sql_query(
        "SELECT ts,open,high,low,close,volume FROM crude_minute_bars WHERE tradingsymbol=? "
        "AND ts>=? AND ts<? ORDER BY ts", conn,
        params=(symbol, start.isoformat(), (end + timedelta(days=1)).isoformat()))
    frame["ts"] = pd.to_datetime(frame["ts"])
    return frame


# ------------------------------------------------------------------ ledger ---
def _priced(trades: pd.DataFrame) -> list[dict]:
    out = []
    for seq, t in enumerate(trades.itertuples(index=False), start=1):
        gross = float(t.points) * QTY
        charges = mcx_futures_round_trip_charges(t.entry_price, t.exit_price, QTY)["total"]
        out.append({
            "seq": seq, "signal_ts": str(t.signal_ts), "entry_ts": str(t.entry_ts),
            "exit_ts": None if t.exit_ts is None or pd.isna(t.exit_ts) else str(t.exit_ts),
            "entry_price": t.entry_price, "exit_price": t.exit_price,
            "stop_price": round(t.stop_price, 4), "target_price": round(t.target_price, 4),
            "stop_dist": round(t.stop_dist, 4), "r_multiple": round(t.r_multiple, 4),
            "points": round(t.points, 4), "gross_rs": round(gross, 2), "charges_rs": charges,
            "net_rs": round(gross - charges, 2), "status": t.status,
            "exit_reason": t.exit_reason, "bars_held": int(t.bars_held),
        })
    return out


def _persist(conn, trade_date: str, status: str, contract: dict | None, summary: dict,
             trades: list[dict], error: str | None = None) -> None:
    now = datetime.now(IST).isoformat(timespec="seconds")
    conn.execute("DELETE FROM crude_macd_st_trades WHERE trade_date=?", (trade_date,))
    conn.executemany(
        "INSERT INTO crude_macd_st_trades (trade_date,seq,tradingsymbol,signal_ts,entry_ts,exit_ts,"
        "entry_price,exit_price,stop_price,target_price,stop_dist,r_multiple,points,qty,gross_rs,"
        "charges_rs,net_rs,status,exit_reason,bars_held) "
        "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        [(trade_date, t["seq"], contract["tradingsymbol"], t["signal_ts"], t["entry_ts"],
          t["exit_ts"], t["entry_price"], t["exit_price"], t["stop_price"], t["target_price"],
          t["stop_dist"], t["r_multiple"], t["points"], QTY, t["gross_rs"], t["charges_rs"],
          t["net_rs"], t["status"], t["exit_reason"], t["bars_held"]) for t in trades])
    conn.execute(
        "INSERT OR REPLACE INTO crude_macd_st_daily (trade_date,status,tradingsymbol,expiry,pdc,"
        "valid_bars,n_signals,n_trades,open_trades,wins,gross_rs,charges_rs,net_rs,qty,"
        "strategy_version,error,updated_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (trade_date, status, contract and contract["tradingsymbol"], contract and contract["expiry"],
         summary.get("pdc"), summary.get("valid_bars"), summary.get("n_signals", 0),
         len(trades), sum(t["status"] == "open" for t in trades),
         sum(t["status"] == "closed" and t["net_rs"] > 0 for t in trades),
         round(sum(t["gross_rs"] for t in trades), 2),
         round(sum(t["charges_rs"] for t in trades), 2),
         round(sum(t["net_rs"] for t in trades), 2), QTY, STRATEGY_VERSION, error, now))
    conn.commit()


def run_day(trade_date: str | None = None, *, kite=None, now: datetime | None = None,
            rebuild: bool = False, connection: sqlite3.Connection | None = None) -> dict:
    """Replay one MCX session (idempotent). During the session only completed 1-minute
    candles are used, so an open position is marked at the last close."""
    now = now or datetime.now(IST)
    now_naive = _to_ist_naive(now)
    session = date.fromisoformat(trade_date) if trade_date else now_naive.date()
    trade_date = session.isoformat()
    if session.weekday() >= 5:
        return {"trade_date": trade_date, "status": "weekend"}
    if session > now_naive.date():
        return {"trade_date": trade_date, "status": "future"}
    own = connection is None
    conn = connection or get_conn()
    _ensure_tables(conn)
    try:
        row = conn.execute("SELECT status FROM crude_macd_st_daily WHERE trade_date=?",
                           (trade_date,)).fetchone()
        if row and row[0] in ("final", "no_session") and not rebuild:
            return {"trade_date": trade_date, "status": row[0], "skipped": True}
        if kite is None:
            from auth.session_manager import get_kite
            kite = get_kite()
        contract = front_contract(_crude_futures(kite), session)
        start = session - timedelta(days=REPLAY_LOOKBACK_DAYS)
        ensure_minute_bars(kite, contract, start, session, now_naive, conn)
        frame = load_minute_bars(contract["tradingsymbol"], start, session, conn)
        in_progress = session == now_naive.date() and now_naive.time() < FINAL_AFTER
        cutoff = now_naive.replace(second=0, microsecond=0) if in_progress else None
        today_rows = frame[frame["ts"].dt.date == session]
        if today_rows.empty:
            status = "live" if in_progress else "no_session"
            _persist(conn, trade_date, status, contract, {}, [])
            return {"trade_date": trade_date, "status": status,
                    "tradingsymbol": contract["tradingsymbol"], "n_trades": 0}
        g, trades = replay(frame, cutoff)
        day_idx = int(np.flatnonzero(g["days"] == session)[0])
        in_day = g["day_id"] == day_idx
        summary = {
            "pdc": None if not math.isfinite(g["pdc_by_day"][day_idx]) else float(g["pdc_by_day"][day_idx]),
            "valid_bars": int(g["valid_counts"][day_idx]),
            "n_signals": int((g["fires"] & in_day).sum()),
        }
        todays = trades[trades["trade_date"] == trade_date] if len(trades) else trades
        priced = _priced(todays)
        status = "live" if in_progress else "final"
        _persist(conn, trade_date, status, contract, summary, priced)
        return {"trade_date": trade_date, "status": status,
                "tradingsymbol": contract["tradingsymbol"], "n_trades": len(priced),
                "open": sum(t["status"] == "open" for t in priced),
                "net_rs": round(sum(t["net_rs"] for t in priced), 2)}
    finally:
        if own:
            conn.close()


def run_live(now: datetime | None = None) -> dict:
    """Loop entry point: freeze any earlier session still marked live, then run today."""
    now = now or datetime.now(IST)
    today = _to_ist_naive(now).date().isoformat()
    conn = get_conn()
    _ensure_tables(conn)
    try:
        stale = [r[0] for r in conn.execute(
            "SELECT trade_date FROM crude_macd_st_daily WHERE status='live' AND trade_date<? "
            "ORDER BY trade_date", (today,))]
    finally:
        conn.close()
    for session in stale:
        run_day(session, now=now)
    result = run_day(today, now=now)
    if stale:
        result = {**result, "frozen": stale}
    return result


if __name__ == "__main__":
    import json
    import sys
    print(json.dumps(run_day(sys.argv[1] if len(sys.argv) > 1 else None), indent=2, default=str))
