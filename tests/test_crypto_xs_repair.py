"""Accounting and public-data regressions from the crypto paper-book audit."""
import json
from datetime import date, datetime, timezone

import numpy as np
import pandas as pd
import pytest

from labs.engine import crypto_xs_account as account
from labs.engine import crypto_xs_public_api as api
from labs.engine import crypto_xs_tracker as tracker
from labs.engine import crypto_xs_data as data
from test_crypto_xs import archive, book, small_universe, SYMBOLS


def test_pagination_uses_last_timestamp_plus_one():
    first, end = pd.Timestamp("2026-10-01", tz="UTC"), pd.Timestamp("2026-10-14", tz="UTC")
    stamp = int(first.timestamp() * 1000)
    pages = [[{"fundingTime": stamp + n, "fundingRate": ".001"} for n in range(1000)],
             [{"fundingTime": stamp + 2000, "fundingRate": ".002"}]]
    calls = []
    def get(path, params):
        calls.append(params)
        return pages.pop(0)
    rows = api.history("/fapi/v1/fundingRate", "BTCUSDT", first, end, get)
    assert len(rows) == 1001 and calls[1]["startTime"] == stamp + 1000
    assert calls[0]["endTime"] == int(end.timestamp() * 1000)


def test_public_klines_are_stamped_at_completion():
    stamp = int(pd.Timestamp("2026-10-01", tz="UTC").timestamp() * 1000)
    row = [stamp, "10", "11", "9", "10.5", "100", stamp+3599999, "1050", 10, "55", "577.5", "0"]
    bar = api.kline_frame([row])
    assert bar.index[0] == pd.Timestamp("2026-10-01 01:00", tz="UTC")
    assert bar.iloc[0].taker_buy == 577.5


def test_saved_panels_are_readable_by_the_production_parquet_reader(tmp_path):
    pytest.importorskip("fastparquet")
    original = pd.DataFrame({"BTCUSDT": [10., 11.]}, index=pd.date_range("2026-10-14", periods=2, freq="h", tz="UTC"))
    data.save_panel("close", original, tmp_path)
    restored = pd.read_parquet(tmp_path / "close.parquet", engine="fastparquet")
    pd.testing.assert_frame_equal(restored, original.rename_axis("index"), check_freq=False)


def test_public_api_preserves_held_tradfi_perpetuals():
    info = {"symbols": [dict(symbol=s, contractType=t, status=status) for s, t, status in [
        ("BTCUSDT", "PERPETUAL", "TRADING"), ("NVDAUSDT", "TRADIFI_PERPETUAL", "TRADING"),
        ("TONUSDT", "PERPETUAL", "SETTLING"), ("BTCUSDT_261225", "NEXT_QUARTER", "TRADING")]]}
    assert api.active_symbols(info) == ["BTCUSDT", "NVDAUSDT"]


def test_up_to_date_prices_are_not_downloaded_again(tmp_path):
    index = pd.date_range("2026-03-01 01:00", "2026-05-12", freq="h", tz="UTC")
    for name, value in [("close", 100.), ("quote_volume", 1000.), ("taker_buy", 550.)]:
        data.save_panel(name, pd.DataFrame({"BTCUSDT": value}, index=index), tmp_path)
    manifest = data.load_manifest(tmp_path)
    manifest.update(through="2026-05-11", funding_through="2026-04-30", symbols=["BTCUSDT"])
    data.save_manifest(manifest, tmp_path)
    calls = []
    def get(path, params):
        calls.append(path)
        assert path == "/fapi/v1/exchangeInfo"
        return {"symbols": [dict(symbol="BTCUSDT", contractType="PERPETUAL", status="TRADING")]}
    result = api.repair(tmp_path, now=pd.Timestamp("2026-05-12 06:00", tz="UTC"), get=get, log=lambda *a, **kw: None)
    assert calls == ["/fapi/v1/exchangeInfo"] and result["price_contracts"] == 0


def test_no_phantom_trades_and_missing_settlement_is_provisional(small_universe, tmp_path, book):
    a = small_universe
    a.bars["C06USDT"] = a.bars["C06USDT"].loc[:"2026-05-06 11:00"]
    tracker.refresh(datetime(2026, 5, 12, tzinfo=timezone.utc), store=tmp_path,
                    get=a, connection=book, log=lambda *_: None)
    rows, _, stats = tracker.tab_data(book)
    by_day = {r["decision_date"]: r for r in rows}
    assert stats["account"] and stats["equity_provisional"]
    assert "settlement unknown" in by_day["2026-05-06"]["flags"]
    lines = [json.loads(r[0]) for r in book.execute("SELECT payload FROM crypto_xs_account_weights")]
    frozen = [r for r in lines if r["symbol"] == "C06USDT" and r["decision_date"] >= "2026-05-07"]
    assert frozen and all(r["traded"] == 0 and r["tradable"] == 0 for r in frozen)
    assert len({r["quantity"] for r in frozen}) == 1
    assert all(r["entry_source"] == "last_observed_valuation_no_fill" for r in frozen)
    assert all(r["exit_price"] is not None for r in frozen if r["exit_source"] is not None)
    # The old model remains inspectable, including its original research assumptions.
    reference = tracker.tab_data(book, reference=True)[0]
    assert next(r for r in reference if r["decision_date"] == "2026-05-07")["turnover"] != by_day["2026-05-07"]["turnover"]


def test_filters_scope_the_account_cards_flags_and_holdings(small_universe, tmp_path, book):
    tracker.refresh(datetime(2026, 5, 12, tzinfo=timezone.utc), store=tmp_path,
                    get=small_universe, connection=book, log=lambda *_: None)
    rows, holdings, stats = tracker.tab_data(book, "AND trade_date BETWEEN ? AND ?", ("2026-04-23", "2026-04-25"))
    assert len(rows) == 3 and stats["research"]["days"] == 3 and stats["unseen"]["days"] == 0
    assert stats["latest"]["decision_date"] == "2026-04-25"
    assert all(r["decision_date"] == "2026-04-25" for r in holdings)
    assert stats["filtered"]


def test_actual_api_funding_needs_every_held_contract_covered():
    coverage = {s: {"first": "2026-10-01T00:00:00+00:00", "through": "2026-10-15T00:00:00+00:00"} for s in ["A", "B"]}
    out = {"estimate_from": pd.Timestamp("2026-10-01", tz="UTC"), "manifest": {"api_funding_coverage": coverage}}
    lines = [{"decision_date": "2026-10-12", "symbol": s, "weight": .5} for s in ["A", "B"]]
    assert tracker.funding_source(out, "2026-10-12", lines) == "actual (public API)"
    del coverage["B"]
    assert tracker.funding_source(out, "2026-10-12", lines) == "estimated / incomplete"


def test_confirmed_delisting_closes_quantity_without_later_rebalances(small_universe, tmp_path, book):
    a = small_universe
    a.bars["C06USDT"] = a.bars["C06USDT"].loc[:"2026-05-06 11:00"]
    tracker.refresh(datetime(2026, 5, 12, tzinfo=timezone.utc), store=tmp_path,
                    get=a, connection=book, log=lambda *_: None)
    manifest = data.load_manifest(tmp_path)
    manifest["settlements"] = {"C06USDT": dict(time="2026-05-06T12:00:00+00:00", price=600., verified=False)}
    data.save_manifest(manifest, tmp_path)
    tracker.rebuild(book, store=tmp_path, offline=True)
    rows, _, stats = tracker.tab_data(book)
    day = next(r for r in rows if r["decision_date"] == "2026-05-06")
    assert day["provisional"] and "automatic settlement" in day["flags"]
    later = [json.loads(r[0]) for r in book.execute("SELECT payload FROM crypto_xs_account_weights WHERE decision_date>'2026-05-06'")]
    assert all(r["quantity"] == 0 and r["traded"] == 0 for r in later if r["symbol"] == "C06USDT")
    assert stats["provisional_days"] == 1 and stats["equity_provisional"]


def test_api_coverage_prevents_phantom_estimated_funding(small_universe, tmp_path, book):
    tracker.refresh(datetime(2026, 5, 12, tzinfo=timezone.utc), store=tmp_path,
                    get=small_universe, connection=book, log=lambda *_: None)
    manifest = data.load_manifest(tmp_path)
    # Override every other funding settlement with a known long interval.
    actual = data.load_panel("funding", tmp_path).reindex(data.load_panel("close", tmp_path).index)
    may = actual.index >= pd.Timestamp("2026-05-01", tz="UTC")
    for symbol in actual:
        actual.loc[may, symbol] = np.nan
        settlements = actual.index[may & (actual.index.hour % 8 == 0)]
        actual.loc[settlements, symbol] = .0002
    data.save_panel("funding", actual, tmp_path)
    manifest["api_funding_coverage"] = {s: {"first": "2026-05-01T00:00:00+00:00", "through": "2026-05-11T00:00:00+00:00"} for s in actual}
    data.save_manifest(manifest, tmp_path)
    out = tracker.compute(tmp_path, offline=True)
    got = out["funding_panel"].loc["2026-05-05", "C03USDT"].dropna()
    assert got.tolist() == [.0002] * 3
    assert got.index.hour.tolist() == [0, 8, 16]
