"""One-minute index bars from Kite, kept as yearly compressed CSVs under labs `data/strategy_tester/minute/`.

  backfill(symbol, start)  fill every missing year from `start` (run on PA, where the labs Kite token lives)
  refresh(symbol)          top up the current year to the last completed session (called lazily by load)
  load(symbol, start, end) the bars as a DataFrame (Open High Low Close, tz-naive IST, minute stamps)

Kite gives index minute history back to 2015 in 60-day requests. Nothing here places orders.
"""
from __future__ import annotations

import logging
import os
import threading
import time as _time
from datetime import date, datetime, time, timedelta, timezone
from pathlib import Path

import pandas as pd

log = logging.getLogger("strategy-tester.store")

ROOT = Path(os.environ.get("ST_MINUTE_DIR") or Path(__file__).resolve().parents[2] / "data" / "strategy_tester" / "minute")
INDICES = {
    "NIFTY": {"token": 256265, "name": "NIFTY 50", "lot": 65, "step": 50},
    "BANKNIFTY": {"token": 260105, "name": "NIFTY BANK", "lot": 30, "step": 100},
    "SENSEX": {"token": 265, "name": "SENSEX", "lot": 20, "step": 100},
}
VIX_TOKEN = 264969
FIRST_YEAR = 2015
CHUNK_DAYS = 55            # Kite allows 60 days of minute candles per request
_refresh_lock = threading.Lock()
_last_refresh: dict[str, float] = {}
_mem: dict[str, tuple[float, pd.DataFrame]] = {}


class StoreUnavailable(RuntimeError):
    pass


def _path(symbol: str, year: int) -> Path:
    return ROOT / f"{symbol}_{year}.csv.gz"


def _kite():
    from labs.simulation.market_data import simulation_kite    # labs data-only session from config/zerodha_token.json
    return simulation_kite()


def _fetch(kite, token: int, start: date, end: date, interval: str = "minute") -> pd.DataFrame:
    rows = []
    d = start
    while d <= end:
        e = min(end, d + timedelta(days=CHUNK_DAYS))
        for attempt in range(4):
            try:
                rows += kite.historical_data(token, datetime.combine(d, time(0, 0)), datetime.combine(e, time(23, 59)),
                                             interval)
                break
            except Exception as exc:                          # rate limit / transient
                if attempt == 3:
                    raise
                log.warning("kite history %s %s..%s retry: %s", token, d, e, exc)
                _time.sleep(2 + attempt * 3)
        _time.sleep(0.35)                                      # stay under Kite's 3 requests a second
        d = e + timedelta(days=1)
    if not rows:
        return pd.DataFrame(columns=["Open", "High", "Low", "Close"])
    f = pd.DataFrame(rows)
    ts = pd.to_datetime(f["date"])
    if getattr(ts.dt, "tz", None) is not None:
        ts = ts.dt.tz_convert("Asia/Kolkata").dt.tz_localize(None)
    out = pd.DataFrame({"Open": f["open"].astype(float).to_numpy(), "High": f["high"].astype(float).to_numpy(),
                        "Low": f["low"].astype(float).to_numpy(), "Close": f["close"].astype(float).to_numpy()},
                       index=pd.DatetimeIndex(ts.to_numpy(), name="ts"))
    return out[~out.index.duplicated(keep="last")].sort_index()


def _save(symbol: str, year: int, frame: pd.DataFrame) -> None:
    ROOT.mkdir(parents=True, exist_ok=True)
    tmp = _path(symbol, year).with_name(_path(symbol, year).name + ".tmp")
    frame.to_csv(tmp, compression="gzip", float_format="%.2f")
    os.replace(tmp, _path(symbol, year))


def _read(symbol: str, year: int) -> pd.DataFrame | None:
    p = _path(symbol, year)
    if not p.is_file():
        return None
    f = pd.read_csv(p, index_col=0, parse_dates=True, compression="gzip")
    f.index.name = "ts"
    return f


def backfill(symbol: str, start_year: int = FIRST_YEAR, kite=None, force: bool = False) -> dict:
    """Download every year from start_year that is missing (or all with force); the current year up to today."""
    meta = INDICES.get(symbol)
    if not meta:
        raise StoreUnavailable(f"Unknown index '{symbol}'")
    kite = kite or _kite()
    out = {}
    for y in range(start_year, date.today().year + 1):
        if _path(symbol, y).is_file() and not force and y < date.today().year:
            continue
        f = _fetch(kite, meta["token"], date(y, 1, 1), min(date(y, 12, 31), date.today()))
        if len(f):
            _save(symbol, y, f)
        out[y] = len(f)
        log.info("backfill %s %s: %s bars", symbol, y, len(f))
    _mem.pop(symbol, None)
    return out


def refresh(symbol: str, kite=None, min_gap_s: float = 1800) -> int:
    """Top up the current year from the last stored bar. At most once per `min_gap_s` per symbol; silent on failure
    (the stored history is still usable)."""
    with _refresh_lock:
        if _time.time() - _last_refresh.get(symbol, 0) < min_gap_s:
            return 0
        _last_refresh[symbol] = _time.time()
    try:
        if not years(symbol):
            return 0                                           # nothing stored: backfill first, never from a web request
        now = datetime.now(timezone(timedelta(hours=5, minutes=30)))      # exchange clock (PA runs on UTC)
        today = now.date()
        y = today.year
        cur = _read(symbol, y)
        start = (cur.index[-1].date() + timedelta(days=1)) if cur is not None and len(cur) else date(y, 1, 1)
        if start > today:
            return 0
        new = _fetch(kite or _kite(), INDICES[symbol]["token"], start, today)
        if now.time() < time(15, 31):                        # completed sessions only
            new = new[new.index.normalize() < pd.Timestamp(today)]
        if not len(new):
            return 0
        merged = pd.concat([cur, new]) if cur is not None else new
        _save(symbol, y, merged[~merged.index.duplicated(keep="last")].sort_index())
        _mem.pop(symbol, None)
        return len(new)
    except Exception as exc:
        log.warning("minute refresh %s failed: %s", symbol, exc)
        return 0


def years(symbol: str) -> list[int]:
    return sorted(int(p.name.split("_")[1][:4]) for p in ROOT.glob(f"{symbol}_*.csv.gz"))


def load(symbol: str, start: str | None = None, end: str | None = None, auto_refresh: bool = True) -> pd.DataFrame:
    """Minute bars for [start, end] (inclusive dates), 09:15-15:29 only."""
    if symbol not in INDICES:
        raise StoreUnavailable(f"Intraday tests cover {', '.join(INDICES)}; '{symbol}' has no minute history here.")
    if auto_refresh and os.environ.get("ST_MINUTE_REFRESH", "1") == "1":
        refresh(symbol)
    have = years(symbol)
    if not have:
        raise StoreUnavailable(f"No minute history stored for {symbol} yet (backfill has not run).")
    cached = _mem.get(symbol)
    if cached and _time.time() - cached[0] < 900:
        full = cached[1]
    else:
        full = pd.concat([f for y in have if (f := _read(symbol, y)) is not None])
        full = full[(full.index.time >= time(9, 15)) & (full.index.time <= time(15, 29))]
        _mem[symbol] = (_time.time(), full)
    f = full
    if start:
        f = f[f.index >= pd.Timestamp(start)]
    if end:
        f = f[f.index < pd.Timestamp(end).normalize() + pd.Timedelta(days=1)]
    return f


def daily_vix(kite=None) -> pd.Series:
    """India VIX daily opens (cached in the same folder, refreshed twice a day)."""
    p = ROOT / "INDIAVIX_daily.csv.gz"
    if p.is_file() and _time.time() - p.stat().st_mtime < 12 * 3600:
        return pd.read_csv(p, index_col=0, parse_dates=True, compression="gzip")["Open"]
    try:
        f = _fetch(kite or _kite(), VIX_TOKEN, date(FIRST_YEAR, 1, 1), date.today(), "day")
        f.index = pd.DatetimeIndex(f.index).normalize()
        f = f[~f.index.duplicated(keep="last")]
        ROOT.mkdir(parents=True, exist_ok=True)
        f.to_csv(p, compression="gzip")
        return f["Open"]
    except Exception as exc:
        log.warning("VIX history unavailable: %s", exc)
        return pd.read_csv(p, index_col=0, parse_dates=True, compression="gzip")["Open"] if p.is_file() else pd.Series(dtype=float)
