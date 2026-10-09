"""Hourly panels for the crypto cross-sectional paper book, from Binance's public data archive.

Why the archive and not the exchange API: the labs server is in the US and Binance answers every
futures API call from there with HTTP 451. The archive (data.binance.vision) is reachable. What
that costs:
  - a day's hourly bars are published as one file a contract, roughly a day after the day ends;
  - funding rates are published once a month. Until a month's file exists its funding is
    ESTIMATED from the hourly premium index (see estimate_funding) and replaced when it does;
  - the exchange's trading status of a contract cannot be read at all.

Panels are wide frames on a gap-free hourly UTC index, row t = what is known at t: ``close`` the
price at t (close of the bar ending at t), ``quote_volume`` / ``taker_buy`` that bar's activity,
``funding`` the rate settled at t (actual), ``premium`` the premium index at t (only for the
contracts and days whose funding has to be estimated). They live under storage/state/crypto_xs.
"""
from __future__ import annotations

import io
import json
import re
import time as _time
import urllib.error
import urllib.parse
import urllib.request
import zipfile
from concurrent.futures import ThreadPoolExecutor
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

import numpy as np
import pandas as pd

from labs.engine.crypto_xs_engine import tradable

ROOT = Path(__file__).resolve().parents[2]
STORE = ROOT / "storage" / "state" / "crypto_xs"
ARCHIVE = "https://data.binance.vision/data/futures/um"
BUCKET = "https://s3-ap-northeast-1.amazonaws.com/data.binance.vision"
DATA_START = "2026-03-01"            # 92 days before the first paper day: volume, age and smoothing warm-up
PROBE = "BTCUSDT"                    # a day or month is "published" once this contract's file exists
MIN_COVERAGE = 0.97                  # ...and this share of yesterday's contracts have theirs (or the next day is out)
LATE_RETRY_DAYS = 5                  # a contract's missing day is asked for again this long
WORKERS = 16
HOUR = pd.Timedelta(hours=1)
KLINE_COLUMNS = ["open_time", "open", "high", "low", "close", "volume", "close_time", "quote_volume", "count",
                 "taker_buy_volume", "taker_buy_quote_volume", "ignore"]
FIELDS = {"close": "close", "quote_volume": "quote_volume", "taker_buy": "taker_buy_quote_volume"}
INTEREST_8H, PREMIUM_CLAMP = 0.0001, 0.0005
DEFAULT_INTERVAL_HOURS = 4           # a contract with no funding history yet (new listings settle 4-hourly)


class ArchiveError(RuntimeError):
    """The archive could not be read (network, or an unexpected answer)."""


# ---------------------------------------------------------------- download ---
def fetch(url: str, tries: int = 4) -> bytes | None:
    """The body, or None when the archive says the file does not exist."""
    last = None
    url = urllib.parse.quote(url, safe=":/?&=")                   # a few contract names are not ASCII
    for attempt in range(tries):
        try:
            req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
            with urllib.request.urlopen(req, timeout=40) as response:
                return response.read()
        except urllib.error.HTTPError as exc:
            if exc.code == 404:
                return None
            last = exc
        except Exception as exc:                                  # timeouts, resets
            last = exc
        _time.sleep(1.5 * (attempt + 1))
    raise ArchiveError(f"{url.rsplit('/', 1)[-1]}: {type(last).__name__}: {last}")


def fetch_many(urls: dict, get=fetch) -> dict:
    """{key: url} -> {key: bytes or None}, a few at a time."""
    keys = list(urls)
    with ThreadPoolExecutor(WORKERS) as pool:
        return dict(zip(keys, pool.map(get, [urls[k] for k in keys])))


def list_symbols(get=fetch) -> list[str]:
    """Every USDT perpetual the archive has ever carried hourly bars for (delisted ones included)."""
    prefix, marker, found = "data/futures/um/daily/klines/", "", []
    while True:
        body = get(f"{BUCKET}?delimiter=/&prefix={prefix}" + (f"&marker={marker}" if marker else ""))
        if body is None:
            raise ArchiveError("archive listing unavailable")
        text = body.decode("utf-8", "replace")
        page = re.findall(r"<Prefix>" + re.escape(prefix) + r"([^<]+)/</Prefix>", text)
        found.extend(page)
        if "<IsTruncated>true</IsTruncated>" not in text or not page:
            break
        marker = re.search(r"<NextMarker>([^<]+)</NextMarker>", text)
        marker = marker.group(1) if marker else f"{prefix}{page[-1]}/"
    return sorted(set(tradable(found)))


def kline_url(dataset: str, symbol: str, period: str, monthly: bool) -> str:
    return f"{ARCHIVE}/{'monthly' if monthly else 'daily'}/{dataset}/{symbol}/1h/{symbol}-1h-{period}.zip"


def funding_url(symbol: str, month: str) -> str:
    return f"{ARCHIVE}/monthly/fundingRate/{symbol}/{symbol}-fundingRate-{month}.zip"


# ------------------------------------------------------------------- parse ---
def _csv(data: bytes, names: list[str]) -> pd.DataFrame:
    with zipfile.ZipFile(io.BytesIO(data)) as archive:
        body = archive.read(archive.namelist()[0])
    if not body[:1].isdigit():                                    # newer files carry a header row
        body = body[body.index(b"\n") + 1:] if b"\n" in body else b""
    if not body.strip():
        return pd.DataFrame(columns=names)
    return pd.read_csv(io.BytesIO(body), header=None, names=names, usecols=range(len(names)))


def _epoch(values) -> pd.DatetimeIndex:
    values = pd.to_numeric(values, errors="coerce")
    return pd.DatetimeIndex(pd.to_datetime(values, unit="us" if values.max() > 1e14 else "ms", utc=True))


def parse_klines(data: bytes) -> pd.DataFrame:
    """Hourly bars stamped with the instant they are complete (open time + 1 hour)."""
    frame = _csv(data, KLINE_COLUMNS)
    if frame.empty:
        return pd.DataFrame(columns=list(FIELDS))
    frame.index = _epoch(frame["open_time"]).floor("h") + HOUR
    frame = frame[~frame.index.duplicated(keep="last")].sort_index()
    return frame[list(FIELDS.values())].apply(pd.to_numeric, errors="coerce").set_axis(list(FIELDS), axis=1)


def parse_funding(data: bytes) -> pd.DataFrame:
    """Settled rates stamped at the settlement hour, with the interval each was for."""
    frame = _csv(data, ["calc_time", "funding_interval_hours", "last_funding_rate"])
    if frame.empty:
        return pd.DataFrame(columns=["rate", "interval"])
    frame.index = _epoch(frame["calc_time"]).round("h")           # stamps jitter a few ms around the hour
    rate = pd.to_numeric(frame["last_funding_rate"], errors="coerce").groupby(level=0).sum()
    interval = pd.to_numeric(frame["funding_interval_hours"], errors="coerce").groupby(level=0).last()
    return pd.DataFrame({"rate": rate, "interval": interval}).sort_index()


# ------------------------------------------------------------------- store ---
def _path(name: str, store: Path) -> Path:
    return store / f"{name}.parquet"


def load_panel(name: str, store: Path = STORE) -> pd.DataFrame | None:
    path = _path(name, store)
    return pd.read_parquet(path) if path.exists() else None


def save_panel(name: str, frame: pd.DataFrame, store: Path = STORE) -> None:
    store.mkdir(parents=True, exist_ok=True)
    partial = _path(name, store).with_suffix(".part")
    frame.to_parquet(partial)
    partial.replace(_path(name, store))


def load_manifest(store: Path = STORE) -> dict:
    path = store / "manifest.json"
    base = {"through": None, "funding_through": None, "late": {}, "intervals": {}, "premium_done": [], "symbols": [],
            "catching_up": False}
    return {**base, **json.loads(path.read_text(encoding="utf-8"))} if path.exists() else base


def save_manifest(manifest: dict, store: Path = STORE) -> None:
    store.mkdir(parents=True, exist_ok=True)
    partial = store / "manifest.part"
    partial.write_text(json.dumps(manifest, indent=1, sort_keys=True), encoding="utf-8")
    partial.replace(store / "manifest.json")


def _merge(panel: pd.DataFrame | None, pieces: dict, through: date) -> pd.DataFrame:
    """Write {symbol: series} into a wide panel whose index runs gap-free to 00:00 after ``through``."""
    end = pd.Timestamp(through, tz="UTC") + pd.Timedelta(days=1)
    start = pd.Timestamp(DATA_START, tz="UTC") + HOUR
    if panel is not None and len(panel):
        start, end = min(start, panel.index[0]), max(end, panel.index[-1])
    index = pd.date_range(start, end, freq="1h")
    columns = sorted(set(panel.columns if panel is not None else []) | set(pieces))
    out = (panel if panel is not None else pd.DataFrame()).reindex(index=index, columns=columns).astype("float64")
    for symbol, series in pieces.items():
        series = series[~series.index.duplicated(keep="last")]
        series = series[(series.index >= index[0]) & (series.index <= index[-1])].dropna()
        if len(series):
            out.loc[series.index, symbol] = series.to_numpy(dtype="float64")
    return out


# ------------------------------------------------------------------ update ---
def _months(first: date, last: date) -> list[str]:
    out, cursor = [], date(first.year, first.month, 1)
    while cursor <= last:
        out.append(cursor.strftime("%Y-%m"))
        cursor = date(cursor.year + cursor.month // 12, cursor.month % 12 + 1, 1)
    return out


def _month_end(month: str) -> date:
    y, m = int(month[:4]), int(month[5:])
    return date(y + m // 12, m % 12 + 1, 1) - timedelta(days=1)


def _add_klines(panels: dict, got: dict, through: date) -> dict:
    """got: {(symbol, period): bytes or None}. Returns {symbol: periods found}."""
    pieces = {name: {} for name in FIELDS}
    seen: dict = {}
    for (symbol, period), data in got.items():
        if data is None:
            continue
        bars = parse_klines(data)
        seen.setdefault(symbol, []).append(period)
        for name in FIELDS:
            pieces[name].setdefault(symbol, []).append(bars[name])
    for name in FIELDS:
        panels[name] = _merge(panels.get(name), {s: pd.concat(parts) for s, parts in pieces[name].items()}, through)
    return seen


def update(store: Path = STORE, today: date | None = None, get=fetch, log=print) -> dict:
    """Bring the stored panels up to the last day the archive has published. Safe to call any
    time: it only ever adds bars, and asks again for files that were missing."""
    today = today or datetime.now(timezone.utc).date()
    manifest = load_manifest(store)
    panels: dict = {}

    def stored() -> dict:                                         # the panels are read only when there is something to add
        if not panels:
            panels.update({name: load_panel(name, store) for name in FIELDS})
        return panels

    first = manifest["through"] is None
    added_days: list[str] = []

    if first:
        symbols = list_symbols(get)
        start = date.fromisoformat(DATA_START)
        months = [m for m in _months(start, today) if _month_end(m) < today]
        have_month = fetch_many({m: kline_url("klines", PROBE, m, True) for m in months}, get)
        whole = []
        for m in months:                                          # whole months, as far as they run unbroken
            if have_month[m] is None:
                break
            whole.append(m)
        log(f"[crypto_xs] first build: {len(symbols)} contracts, months {whole}")
        got = fetch_many({(s, m): kline_url("klines", s, m, True) for s in symbols for m in whole}, get)
        through = _month_end(whole[-1]) if whole else start - timedelta(days=1)
        _add_klines(panels, got, through)
        manifest.update(through=through.isoformat(), symbols=symbols, catching_up=True)
        for name in FIELDS:                                       # keep the big download even if a later step fails
            save_panel(name, panels[name], store)
        save_manifest(manifest, store)

    through = date.fromisoformat(manifest["through"])
    day = through + timedelta(days=1)
    wide = bool(manifest.get("catching_up"))                      # the days after the last whole month: ask for every contract
    fresh: list[str] = []
    looked, waiting = False, False
    while day < today:
        tag = day.isoformat()
        if get(kline_url("klines", PROBE, tag, False)) is None:
            break
        stored()
        if not looked and not wide:                               # new listings, looked up once a run
            looked = True
            listed = list_symbols(get)
            fresh = [s for s in listed if s not in manifest["symbols"]]
            manifest["symbols"] = sorted(set(manifest["symbols"]) | set(listed))
            if fresh:
                back = [(day - timedelta(days=k)).isoformat() for k in range(1, 15)]
                _add_klines(panels, fetch_many({(s, d): kline_url("klines", s, d, False) for s in fresh for d in back}, get), through)
        expected = _live_symbols(panels["close"], through, days=1)
        # a contract is asked for until a week after its last bar
        asked = sorted(set(manifest["symbols"]) if wide else set(_live_symbols(panels["close"], through, days=7)) | set(fresh))
        got = fetch_many({(s, tag): kline_url("klines", s, tag, False) for s in asked}, get)
        present = {s for (s, _), data in got.items() if data is not None}
        if expected and len(present & set(expected)) < MIN_COVERAGE * len(expected):
            # half published - unless the archive has already moved on to the next day: then this is all there is
            if get(kline_url("klines", PROBE, (day + timedelta(days=1)).isoformat(), False)) is None:
                log(f"[crypto_xs] {tag}: only {len(present & set(expected))}/{len(expected)} contracts published yet - waiting")
                waiting = True
                break
        _add_klines(panels, got, day)
        recent = set(_live_symbols(panels["close"], through, days=7)) | set(fresh)
        missing = sorted(recent - present)
        if missing:
            manifest["late"][tag] = missing
        through, day = day, day + timedelta(days=1)
        added_days.append(tag)
        manifest["through"] = through.isoformat()

    if not waiting:
        manifest["catching_up"] = False                           # level with the archive: from here, live contracts only

    # files that were missing when their day was first read: ask again for a few days
    retried = 0
    for tag in sorted(manifest["late"]):
        if (today - date.fromisoformat(tag)).days > LATE_RETRY_DAYS:
            del manifest["late"][tag]
            continue
        got = fetch_many({(s, tag): kline_url("klines", s, tag, False) for s in manifest["late"][tag]}, get)
        if not any(v is not None for v in got.values()):
            continue
        found = _add_klines(stored(), got, through)
        retried += len(found)
        manifest["late"][tag] = [s for s in manifest["late"][tag] if s not in found]
        if not manifest["late"][tag]:
            del manifest["late"][tag]

    changed = bool(first or added_days or retried)
    funded = _update_funding(stored, manifest, store, through, get, log)
    if changed:
        for name in FIELDS:
            save_panel(name, panels[name], store)
    save_manifest(manifest, store)
    return {"through": manifest["through"], "added_days": added_days, "late_filled": retried, "funding_months": funded,
            "funding_through": manifest["funding_through"], "changed": bool(changed or funded)}


def _live_symbols(close: pd.DataFrame | None, through: date, days: int = 1) -> list[str]:
    """Contracts with a bar in the last ``days`` stored days."""
    if close is None or not len(close):
        return []
    end = pd.Timestamp(through, tz="UTC") + pd.Timedelta(days=1)
    span = close.loc[(close.index > end - pd.Timedelta(days=days)) & (close.index <= end)]
    return sorted(span.columns[span.notna().any()])


def _update_funding(stored, manifest: dict, store: Path, through: date, get, log) -> list[str]:
    """Actual funding, a month at a time, for every month the archive has published."""
    funding = None
    done_to = manifest["funding_through"]
    start = date.fromisoformat(DATA_START) if done_to is None else date.fromisoformat(done_to) + timedelta(days=1)
    months = [m for m in _months(start, through) if _month_end(m) <= through]
    added = []
    for month in months:
        if get(funding_url(PROBE, month)) is None:
            break
        if not added:
            funding = load_panel("funding", store)
        close = stored()["close"]
        lo, hi = pd.Timestamp(f"{month}-01", tz="UTC"), pd.Timestamp(_month_end(month), tz="UTC") + pd.Timedelta(days=1)
        alive = sorted(close.columns[close.loc[(close.index > lo) & (close.index <= hi)].notna().any()])
        got = fetch_many({s: funding_url(s, month) for s in alive}, get)
        pieces = {}
        for symbol, data in got.items():
            if data is None:
                continue
            rows = parse_funding(data)
            if len(rows):
                pieces[symbol] = rows["rate"]
                last = rows["interval"].dropna()
                if len(last):
                    manifest["intervals"][symbol] = int(last.iloc[-1])
        funding = _merge(funding, pieces, _month_end(month))
        manifest["funding_through"] = _month_end(month).isoformat()
        added.append(month)
        log(f"[crypto_xs] funding {month}: {len(pieces)}/{len(alive)} contracts")
    if added:
        save_panel("funding", funding, store)
    return added


# ------------------------------------------------- funding not yet published ---
def estimate_funding(premium: pd.DataFrame, intervals: dict, start: pd.Timestamp) -> pd.DataFrame:
    """Funding from ``start`` on, estimated from the hourly premium index with Binance's formula:
    rate = (P + clamp(0.01% - P, +-0.05%)) x hours / 8, P = the mean premium over the interval,
    settled on the hours divisible by the contract's interval. An approximation: the exchange
    averages 5-second samples with rising weights and applies per-contract caps, and an interval
    can change. On Jun-Aug 2026 it tracked the book's actual daily funding with correlation 0.98
    and a mean error of 0.02% of equity a day (the funding line itself averages 0.06% a day)."""
    out = pd.DataFrame(np.nan, index=premium.index, columns=premium.columns)
    hours = premium.index[premium.index >= start]
    for symbol in premium.columns:
        step = int(intervals.get(symbol) or DEFAULT_INTERVAL_HOURS)
        average = premium[symbol].rolling(step, min_periods=max(1, step // 2)).mean()
        rate = (average + (INTEREST_8H - average).clip(-PREMIUM_CLAMP, PREMIUM_CLAMP)) * step / 8.0
        settle = hours[(hours.hour % step) == 0]
        out.loc[settle, symbol] = rate.reindex(settle).to_numpy()
    return out


def update_premium(symbols: list[str], first: date, last: date, store: Path = STORE, get=fetch) -> pd.DataFrame | None:
    """Hourly premium index for these contracts over [first, last] (daily files), added to the store."""
    manifest = load_manifest(store)
    done = set(manifest["premium_done"])
    days = [(first + timedelta(days=k)).isoformat() for k in range((last - first).days + 1)]
    want = {(s, d): kline_url("premiumIndexKlines", s, d, False) for s in symbols for d in days if f"{s}|{d}" not in done}
    premium = load_panel("premium", store)
    if want:
        got = fetch_many(want, get)
        pieces: dict = {}
        for (symbol, day), data in got.items():
            if data is None:
                continue
            pieces.setdefault(symbol, []).append(parse_klines(data)["close"])
            done.add(f"{symbol}|{day}")
        premium = _merge(premium, {s: pd.concat(parts) for s, parts in pieces.items()}, last)
        save_panel("premium", premium, store)
        floor = (first - timedelta(days=45)).isoformat()             # forget pairs long since replaced by actuals
        manifest["premium_done"] = sorted(k for k in done if k.split("|")[1] >= floor)
        save_manifest(manifest, store)
    return premium


def load(store: Path = STORE) -> dict:
    """{close, quote_volume, taker_buy, funding, premium, manifest}; a missing panel is None."""
    out = {name: load_panel(name, store) for name in (*FIELDS, "funding", "premium")}
    out["manifest"] = load_manifest(store)
    return out
