"""Optional repair of derived crypto panels from Binance's public USD-M REST API.

Run on a host where Binance permits API access. Never proxies a restricted host,
never uses exchange credentials, and never calls order endpoints. The archive
updater remains the fallback on PythonAnywhere. Dates are completed UTC days.
"""
from __future__ import annotations

import argparse
import json
import time
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

import numpy as np
import pandas as pd

from labs.engine import crypto_xs_data as data
from labs.engine import crypto_xs_engine as engine

BASE = "https://fapi.binance.com"


def active_symbols(info):
    # The original archive universe also contains stock/commodity perpetuals.
    # Preserve that universe; excluding TRADIFI here would leave held names stale.
    return engine.tradable([s["symbol"] for s in info["symbols"]
                           if s["contractType"] in {"PERPETUAL", "TRADIFI_PERPETUAL"}
                           and s["status"] == "TRADING"])


def public_get(path, params):
    url = BASE + path + "?" + urllib.parse.urlencode(params)
    # Conservative single caller: funding has a shared 500 requests / 5 min cap.
    time.sleep(0.65)
    with urllib.request.urlopen(url, timeout=40) as response:
        return json.load(response)


def history(path, symbol, first, end, get=public_get):
    """Inclusive API bounds, ascending pagination; do not silently truncate."""
    cursor, stop = int(first.timestamp() * 1000), int(end.timestamp() * 1000)
    result = []
    funding = path.endswith("fundingRate")
    while cursor <= stop:
        params = dict(symbol=symbol, startTime=cursor, endTime=stop, limit=1000)
        if not funding:
            params["interval"] = "1h"
        rows = get(path, params)
        if not isinstance(rows, list):
            raise data.ArchiveError(f"Unexpected public API response for {symbol}")
        if not rows:
            break
        stamps = [int(r["fundingTime"] if funding else r[0]) for r in rows]
        if min(stamps) < cursor or max(stamps) > stop:
            raise data.ArchiveError(f"API returned timestamps outside requested range: {symbol}")
        result.extend(rows)
        following = max(stamps) + 1
        if following <= cursor:
            raise data.ArchiveError(f"API pagination did not advance: {symbol}")
        cursor = following
        if len(rows) < 1000:
            break
    return result


def kline_frame(rows):
    frame = pd.DataFrame(rows, columns=data.KLINE_COLUMNS)
    if frame.empty:
        return pd.DataFrame(columns=list(data.FIELDS))
    frame.index = pd.to_datetime(frame.open_time, unit="ms", utc=True) + data.HOUR
    result = frame[list(data.FIELDS.values())].apply(pd.to_numeric).set_axis(list(data.FIELDS), axis=1)
    valid = (np.isfinite(result).all(axis=1) & (result.close > 0) & (result.quote_volume >= 0)
             & (result.taker_buy >= 0)
             & (result.taker_buy <= result.quote_volume + np.maximum(1e-6, result.quote_volume * 1e-9)))
    if not valid.all():
        raise data.ArchiveError("Invalid public kline price or taker/total volume")
    return result


def record_settlements(manifest, panels, info, end):
    """Retain exchange-confirmed terminal events, without inventing actual prices.

    The public delivery-price endpoint documents quarterly settlements and can
    return no record for a delisted perpetual. A pre-settlement traded close is
    an explicitly unverified valuation proxy, never an asserted exchange fill.
    """
    events = manifest.setdefault("settlements", {})
    for item in info["symbols"]:
        symbol = item["symbol"]
        if item["status"] != "SETTLING" or symbol not in panels["close"]:
            continue
        stamp = pd.to_datetime(item["deliveryDate"], unit="ms", utc=True)
        if stamp > end or events.get(symbol, {}).get("verified"):
            continue
        valid = (panels["quote_volume"][symbol] > 0) & (panels["close"].index <= stamp)
        prices = panels["close"].loc[valid, symbol].dropna()
        if not len(prices):
            continue
        events[symbol] = dict(time=stamp.isoformat(), price=float(prices.iloc[-1]),
                              price_time=prices.index[-1].isoformat(), verified=False,
                              source="last traded hourly close before exchangeInfo deliveryDate; estimated settlement")


def repair(store=data.STORE, *, now=None, get=public_get, log=print):
    """Backfill internal holes, append completed days, replace estimated funding.

    New listings receive 40 days of history. Historical missing prices are asked
    for only between a contract's first and last observed bar, never fabricated.
    Each panel is atomic and manifest is published last. Use an isolated store
    and publish only while its normal writer is quiescent.
    """
    now = pd.Timestamp(now or datetime.now(timezone.utc)).tz_convert("UTC")
    end = now.floor("D")  # last complete hourly bar of the previous UTC day
    manifest = data.load_manifest(store)
    panels = {name: data.load_panel(name, store) for name in data.FIELDS}
    close = panels["close"]
    if close is None or close.empty:
        raise ValueError("Seed the archive panels before running an API repair")
    info = get("/fapi/v1/exchangeInfo", {})
    active = active_symbols(info)
    symbols = sorted(set(close.columns) | set(active))
    pieces = {name: {} for name in data.FIELDS}
    fetched, errors = {}, {}
    for n, symbol in enumerate(symbols):
        series = close[symbol].dropna() if symbol in close else pd.Series(dtype=float)
        if series.empty:
            if symbol not in active:
                continue
            first = end - pd.Timedelta(days=40)
        else:
            observed = close.loc[series.index[0]:series.index[-1], symbol]
            holes = observed[observed.isna()].index
            first = min(holes[0] if len(holes) else end + data.HOUR, series.index[-1] + data.HOUR)
            if symbol not in active and not len(holes):
                continue
        if first > end:
            continue
        try:
            rows = history("/fapi/v1/klines", symbol, first - data.HOUR,
                           end - pd.Timedelta(milliseconds=1), get)
            bars = kline_frame(rows)
            if len(bars):
                for name in data.FIELDS:
                    pieces[name][symbol] = bars[name]
                fetched[symbol] = len(bars)
        except Exception as exc:
            errors[symbol] = str(exc)[:200]
        if n % 50 == 0:
            log(f"[crypto API] prices {n + 1}/{len(symbols)}; errors {len(errors)}", flush=True)
    through = (end - pd.Timedelta(days=1)).date()
    for name in data.FIELDS:
        panels[name] = data._merge(panels[name], pieces[name], through)
    # Do not advertise a new day if the active universe is only partly available.
    last = panels["close"].reindex(columns=active).loc[end]
    if len(active) and last.notna().mean() < data.MIN_COVERAGE:
        raise data.ArchiveError("API repair has insufficient end-of-day price coverage; panels not published")
    for name in data.FIELDS:
        data.save_panel(name, panels[name], store)
    manifest["through"] = max(manifest["through"], through.isoformat())
    manifest["symbols"] = symbols
    manifest["api_prices_checked_at"] = now.isoformat()
    record_settlements(manifest, panels, info, end)
    data.save_manifest(manifest, store)

    close = panels["close"]
    actual = data.load_panel("funding", store)
    out = engine.run(close, actual.reindex(index=close.index, columns=close.columns)
                     if actual is not None else close * np.nan, panels["quote_volume"], panels["taker_buy"])
    known = pd.Timestamp(manifest["funding_through"], tz="UTC") + pd.Timedelta(days=1)
    exposed = out["market"].decisions >= known - pd.Timedelta(hours=25)
    held = np.abs(out["weights"][exposed]).sum(0) > 1e-12
    funding_pieces, coverage = {}, manifest.get("api_funding_coverage", {})
    for symbol in close.columns[held]:
        checked = coverage.get(symbol)
        if checked and pd.Timestamp(checked["first"]) <= known and pd.Timestamp(checked["through"]) >= end:
            continue
        try:
            rows = history("/fapi/v1/fundingRate", symbol, known, end, get)
            if rows:
                stamps = pd.to_datetime([r["fundingTime"] for r in rows], unit="ms", utc=True).round("h")
                funding_pieces[symbol] = pd.Series([float(r["fundingRate"]) for r in rows], index=stamps).groupby(level=0).sum()
                coverage[symbol] = {"first": known.isoformat(), "through": end.isoformat()}
        except Exception as exc:
            errors["funding:" + symbol] = str(exc)[:200]
    actual = data._merge(actual, funding_pieces, through)
    data.save_panel("funding", actual, store)
    manifest.update(api_funding_coverage=coverage, api_checked_at=now.isoformat(), api_errors=errors)
    data.save_manifest(manifest, store)
    report = dict(through=manifest["through"], price_contracts=len(fetched),
                  funding_contracts=len(funding_pieces), errors=errors)
    log(json.dumps(report), flush=True)
    return report


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--store", type=Path, default=data.STORE)
    args = parser.parse_args()
    repair(args.store)
