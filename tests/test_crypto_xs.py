"""Crypto cross-sectional taker-imbalance paper book: the rule against the research backtest, the
archive reader against a fake archive, and the ledger."""
from __future__ import annotations

import io
import py_compile
import shutil
import sqlite3
import subprocess
import sys
import zipfile
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

import numpy as np
import pandas as pd
import pytest

from labs.engine import crypto_xs_data as data
from labs.engine import crypto_xs_engine as engine
from labs.engine import crypto_xs_tracker as tracker

ROOT = Path(__file__).resolve().parents[1]
FIX = ROOT / "tests" / "fixtures"
UTC = timezone.utc


# ═════════════════════════════════════════════════════════════════ the rule ══
@pytest.fixture(scope="module")
def research():
    """The research project's own hourly panels, 15 Apr - 1 Jul 2026, for every contract that was
    in the top 50 by volume on any day of that window."""
    long = pd.read_parquet(FIX / "crypto_xs_research_panel_20260415_20260701.parquet")
    return {name: long.pivot(index="time", columns="symbol", values=name).astype("float64")
            for name in ("close", "quote_volume", "taker_buy_quote_volume", "funding")}


def test_rule_reproduces_the_research_backtest_for_june(research):
    """Crypto_Analysis run XS_FACTORS_20261009_V1 (S05_taker1, hold 7): every June day to 1e-12,
    the first five days as handed over, and the month compounding to +2.30%."""
    out = engine.run(research["close"], research["funding"], research["quote_volume"], research["taker_buy_quote_volume"])
    frame = out["frame"]
    want = pd.read_csv(FIX / "crypto_xs_research_daily_net_june2026.csv")
    got = frame.loc["2026-06-01":"2026-06-30", "net"]
    assert list(got.index.strftime("%Y-%m-%d")) == list(want.decision_date) and len(got) == 30
    assert np.abs(got.to_numpy() - want.net.to_numpy()).max() < 1e-12
    assert [round(100 * v, 3) for v in got.iloc[:5]] == [4.019, -0.223, 1.049, 0.353, 1.680]
    assert round(100 * (np.prod(1 + got.to_numpy()) - 1), 2) == 2.30
    june = frame.loc["2026-06-01":"2026-06-30"]
    assert (june.exposure.round(9) == 1.0).all() and june.complete.all()
    assert np.abs(out["weights"].sum(1)).max() < 1e-9                       # dollar-neutral every day
    assert june.n_eligible.between(45, 50).all()
    assert not frame.complete.iloc[-1] and frame.complete.iloc[:-1].all()   # the last hold is still open


def test_rank_weights_and_the_seven_day_book():
    signal = np.array([[0.3, -0.2, 0.1, np.nan] + [0.0] * 8, list(np.linspace(-1, 1, 12))])
    eligible = np.ones_like(signal, dtype=bool)
    w = engine.rank_weights(signal, eligible)
    assert abs(w[1].sum()) < 1e-12 and np.abs(w[1]).sum() == pytest.approx(1.0)
    assert w[1, -1] == pytest.approx(-w[1, 0]) and w[1, -1] > 0 and np.all(np.diff(w[1]) > 0)
    assert w[0, 3] == 0.0 and np.abs(w[0]).sum() == pytest.approx(1.0)      # a contract with no signal gets nothing
    assert np.all(engine.rank_weights(signal[:, :9], eligible[:, :9]) == 0)  # fewer than 10 names: no book
    targets = np.vstack([np.eye(12)[k % 12] - np.eye(12)[(k + 6) % 12] for k in range(10)]) / 2
    book = engine.book_weights(targets)
    assert np.allclose(np.abs(book).sum(1), 1.0)
    assert np.allclose(book[8] * np.abs(targets[2:9].mean(0)).sum(), targets[2:9].mean(0))


# ═════════════════════════════════════════════════════════════ fake archive ══
SYMBOLS = [f"C{k:02d}USDT" for k in range(14)]
START = "2026-03-01"
PUBLISHED_THROUGH = date(2026, 5, 10)


def _zip(name: str, text: str) -> bytes:
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        archive.writestr(name, text)
    return buffer.getvalue()


class FakeArchive:
    """Hourly bars for 14 contracts from 1 March, served as the archive serves them: monthly and
    daily kline files, monthly funding files, daily premium-index files and the bucket listing."""

    def __init__(self):
        hours = pd.date_range(f"{START} 00:00", "2026-06-03 23:00", freq="1h", tz="UTC")          # bar open times
        rng = np.random.RandomState(7)
        self.bars, self.funding, self.premium = {}, {}, {}
        for k, symbol in enumerate(SYMBOLS):
            close = 100 * (k + 1) * np.exp(np.cumsum(rng.normal(0, 0.004, len(hours))))
            volume = (k + 1) * 1e6 * (1 + rng.rand(len(hours)))
            buy = volume * np.clip(0.5 + 0.02 * np.sin(k + np.arange(len(hours)) / 37.0) + rng.normal(0, 0.03, len(hours)), 0.05, 0.95)
            self.bars[symbol] = pd.DataFrame({"close": close, "quote_volume": volume, "taker_buy": buy}, index=hours)
            self.premium[symbol] = pd.Series(0.0002 * np.sin(k + np.arange(len(hours)) / 50.0) + 0.0001, index=hours)
            step = 4 if k % 2 else 8
            settle = hours[hours.hour % step == 0]
            self.funding[symbol] = (pd.Series(0.0001 * (1 + (k % 3)) * step / 8, index=settle), step)
        self.through = PUBLISHED_THROUGH
        self.funding_months = {"2026-03", "2026-04"}
        self.withheld: set = set()                # (symbol, day) not published yet
        self.listed = list(SYMBOLS)
        self.calls: list[str] = []

    def _klines(self, frame: pd.DataFrame, header: bool) -> str:
        ms = frame.index.asi8 // (1_000_000 if frame.index.unit == "ns" else 1)
        price = frame.close.to_numpy()
        out = pd.DataFrame({"open_time": ms, "open": price, "high": price, "low": price, "close": price, "volume": 1,
                            "close_time": ms + 3599999, "quote_volume": frame.quote_volume.to_numpy(), "count": 10,
                            "taker_buy_volume": 1, "taker_buy_quote_volume": frame.taker_buy.to_numpy(), "ignore": 0})
        return out.to_csv(index=False, header=header, float_format="%.17g", lineterminator="\n")

    def __call__(self, url: str):
        self.calls.append(url)
        if url.startswith(data.BUCKET):
            return ("<ListBucketResult><IsTruncated>false</IsTruncated>" + "".join(
                f"<CommonPrefixes><Prefix>data/futures/um/daily/klines/{s}/</Prefix></CommonPrefixes>"
                for s in self.listed + ["USDCUSDT", "BTCUSDT_260925"]) + "</ListBucketResult>").encode()
        name = url.rsplit("/", 1)[-1][:-4]
        if "/fundingRate/" in url:
            symbol, month = name.split("-fundingRate-")
            symbol = "C00USDT" if symbol == data.PROBE else symbol
            if month not in self.funding_months or symbol not in self.funding:
                return None
            series, step = self.funding[symbol]
            rows = series[series.index.strftime("%Y-%m") == month]
            return _zip(name + ".csv", "calc_time,funding_interval_hours,last_funding_rate\n" + "".join(
                f"{int(t.timestamp() * 1000) + 3},{step},{v}\n" for t, v in rows.items()))
        symbol, period = name.split("-1h-")
        symbol = "C00USDT" if symbol == data.PROBE else symbol
        if symbol not in self.bars or (symbol not in self.listed and symbol != "C00USDT"):
            return None
        monthly = "/monthly/" in url
        last = date.fromisoformat(period) if not monthly else data._month_end(period)
        if last > self.through or (symbol, period) in self.withheld:
            return None
        if "/premiumIndexKlines/" in url:
            series = self.premium[symbol]
            frame = pd.DataFrame({"close": series, "quote_volume": 0.0, "taker_buy": 0.0})
        else:
            frame = self.bars[symbol]
        first = pd.Timestamp(period + "-01" if monthly else period, tz="UTC")
        rows = frame[(frame.index >= first) & (frame.index < pd.Timestamp(last, tz="UTC") + pd.Timedelta(days=1))]
        return _zip(name + ".csv", self._klines(rows, header=not monthly)) if len(rows) else None


@pytest.fixture
def archive(monkeypatch):
    monkeypatch.setattr(data, "DATA_START", START)
    monkeypatch.setattr(data, "WORKERS", 1)
    monkeypatch.setattr(data, "MIN_COVERAGE", 0.9)               # 14 contracts: one missing is still "published"
    return FakeArchive()


def _update(archive, store, today):
    return data.update(store, today=today, get=archive, log=lambda *_: None)


def test_first_build_then_one_day_at_a_time(archive, tmp_path):
    out = _update(archive, tmp_path, date(2026, 5, 12))
    assert out["through"] == "2026-05-10" and out["funding_through"] == "2026-04-30" and out["changed"]
    assert out["added_days"] == [f"2026-05-{d:02d}" for d in range(1, 11)] and out["funding_months"] == ["2026-03", "2026-04"]
    close = data.load_panel("close", tmp_path)
    assert list(close.columns) == SYMBOLS                                    # the stable pair and the dated future are not perpetuals
    assert close.index[0] == pd.Timestamp("2026-03-01 01:00", tz="UTC") and close.index[-1] == pd.Timestamp("2026-05-11 00:00", tz="UTC")
    assert len(close) == (close.index[-1] - close.index[0]) // pd.Timedelta(hours=1) + 1 and close.notna().all().all()
    # a bar is stamped with the instant it is complete: the 00:00-01:00 bar is the row at 01:00
    bar = archive.bars["C03USDT"].loc[pd.Timestamp("2026-04-07 00:00", tz="UTC")]
    row = pd.Timestamp("2026-04-07 01:00", tz="UTC")
    assert close.at[row, "C03USDT"] == pytest.approx(bar.close)
    assert data.load_panel("taker_buy", tmp_path).at[row, "C03USDT"] == pytest.approx(bar.taker_buy)
    funding = data.load_panel("funding", tmp_path)
    assert funding["C02USDT"].dropna().index.hour.isin([0, 8, 16]).all() and funding["C03USDT"].dropna().index.hour.isin(range(0, 24, 4)).all()
    manifest = data.load_manifest(tmp_path)
    assert manifest["intervals"]["C02USDT"] == 8 and manifest["intervals"]["C03USDT"] == 4 and not manifest["catching_up"]

    calls = len(archive.calls)                                               # nothing new: one probe, no panel read
    again = _update(archive, tmp_path, date(2026, 5, 12))
    assert not again["changed"] and again["added_days"] == [] and len(archive.calls) - calls <= 2

    archive.through = date(2026, 5, 11)                                      # the next day is published
    out = _update(archive, tmp_path, date(2026, 5, 13))
    assert out["added_days"] == ["2026-05-11"] and out["through"] == "2026-05-11"
    assert data.load_panel("close", tmp_path).index[-1] == pd.Timestamp("2026-05-12 00:00", tz="UTC")


def test_a_half_published_day_waits_and_a_late_file_is_filled_in(archive, tmp_path):
    _update(archive, tmp_path, date(2026, 5, 12))
    archive.through = date(2026, 5, 11)
    archive.withheld = {(s, "2026-05-11") for s in SYMBOLS[3:9]}             # 6 of 14 not up yet
    out = _update(archive, tmp_path, date(2026, 5, 13))
    assert out["added_days"] == [] and out["through"] == "2026-05-10"
    archive.withheld = {("C05USDT", "2026-05-11")}                           # all but one: the day is taken, one file is owed
    out = _update(archive, tmp_path, date(2026, 5, 13))
    assert out["added_days"] == ["2026-05-11"] and data.load_manifest(tmp_path)["late"] == {"2026-05-11": ["C05USDT"]}
    day = data.load_panel("close", tmp_path).loc["2026-05-11 01:00":"2026-05-12 00:00"]
    assert day["C05USDT"].isna().all() and day["C04USDT"].notna().all()
    archive.withheld, archive.through = set(), date(2026, 5, 12)
    out = _update(archive, tmp_path, date(2026, 5, 14))                      # still asked for the day after, and the gap is filled
    assert out["late_filled"] == 1 and out["added_days"] == ["2026-05-12"] and data.load_manifest(tmp_path)["late"] == {}
    assert data.load_panel("close", tmp_path).loc["2026-05-11 01:00":"2026-05-13 00:00", "C05USDT"].notna().all()
    # six files never come, but the archive moves on to the next day: the day is taken as it is
    archive.through = date(2026, 5, 14)
    archive.withheld = {(s, "2026-05-13") for s in SYMBOLS[3:9]}
    out = _update(archive, tmp_path, date(2026, 5, 16))
    assert out["added_days"] == ["2026-05-13", "2026-05-14"] and data.load_manifest(tmp_path)["late"] == {"2026-05-13": SYMBOLS[3:9]}


def test_a_new_listing_is_picked_up_with_its_first_days(archive, tmp_path):
    archive.listed = SYMBOLS[:13]
    archive.bars["C13USDT"] = archive.bars["C13USDT"].loc["2026-05-09":]     # starts trading on 9 May
    _update(archive, tmp_path, date(2026, 5, 9))                             # archive level with 7 May... and published to the 8th
    assert "C13USDT" not in data.load_panel("close", tmp_path).columns
    archive.listed = list(SYMBOLS)
    archive.through = date(2026, 5, 10)
    archive.through, today = date(2026, 5, 10), date(2026, 5, 12)
    out = _update(archive, tmp_path, today)
    close = data.load_panel("close", tmp_path)
    assert out["through"] == "2026-05-10" and close["C13USDT"].first_valid_index() == pd.Timestamp("2026-05-09 01:00", tz="UTC")
    assert close["C13USDT"].loc["2026-05-09 01:00":].notna().all()


def test_funding_estimate_follows_the_exchange_formula():
    index = pd.date_range("2026-05-01 01:00", periods=48, freq="1h", tz="UTC")
    premium = pd.DataFrame({"A": 0.0003, "B": 0.002, "C": -0.004}, index=index)
    est = data.estimate_funding(premium, {"A": 8, "B": 4}, pd.Timestamp("2026-05-02", tz="UTC"))
    assert est.loc[:"2026-05-01 23:00"].isna().all().all()
    assert list(est["A"].dropna().index.hour.unique()) == [0, 8, 16] and est["A"].dropna().iloc[0] == pytest.approx(0.0001)
    assert est["B"].dropna().iloc[0] == pytest.approx((0.002 - 0.0005) * 4 / 8)        # premium beyond the clamp
    assert sorted(est["C"].dropna().index.hour.unique()) == [0, 4, 8, 12, 16, 20]       # no history: 4-hourly
    assert est["C"].dropna().iloc[0] == pytest.approx((-0.004 + 0.0005) * 4 / 8)


# ════════════════════════════════════════════════════════════════ the ledger ══
@pytest.fixture
def book(tmp_path):
    conn = sqlite3.connect(tmp_path / "labs.db")
    yield conn
    conn.close()


@pytest.fixture
def small_universe(monkeypatch, archive):
    monkeypatch.setattr(tracker, "PAPER_START", "2026-04-20")
    monkeypatch.setattr(tracker, "FIRST_UNSEEN", "2026-05-01")
    return archive


def test_ledger_days_equity_funding_source_and_the_open_hold(small_universe, tmp_path, book):
    archive = small_universe
    out = tracker.refresh(datetime(2026, 5, 12, 6, 0, tzinfo=UTC), store=tmp_path, get=archive, connection=book, log=lambda *_: None)
    assert out["through"] == "2026-05-09" and out["open"] == "2026-05-10" and out["data_through"] == "2026-05-10"
    assert out["funding_through"] == "2026-04-30" and out["reconciles"] is False
    rows, held, stats = tracker.tab_data(book)
    assert rows[0]["decision_date"] == "2026-05-10" and not rows[0]["complete"] and rows[0]["net"] is None and rows[0]["cost"] > 0
    assert rows[-1]["decision_date"] == "2026-04-20" and len(rows) == 21
    done = [r for r in reversed(rows) if r["complete"]]
    for r in done:
        assert r["net"] == pytest.approx(r["gross"] + r["funding"] - r["cost"]) and r["exposure"] == pytest.approx(1.0)
        assert r["n_eligible"] == 14 and r["long_exposure"] == pytest.approx(0.5)
        assert r["cost"] == pytest.approx(r["turnover"] * 7e-4)
    equity = tracker.EQUITY_START * np.cumprod([1 + r["net"] for r in done])
    assert [r["equity"] for r in done] == pytest.approx(list(equity), abs=0.01)
    # actual funding is published to 30 April: a hold that runs into May carries an estimate
    source = {r["decision_date"]: r["funding_source"] for r in done}
    assert source["2026-04-28"] == "actual" and source["2026-04-29"] == "actual" and source["2026-04-30"] == "estimated"
    assert all(v == "estimated" for d, v in source.items() if d >= "2026-04-30")
    assert all(r["funding"] != 0 for r in done)
    assert stats["research"]["days"] == 11 and stats["unseen"]["days"] == 9 and stats["unseen"]["estimated_days"] == 9
    assert stats["research"]["estimated_days"] == 1 and stats["research"]["months"][0]["month"] == "2026-04"
    assert stats["unseen"]["net_pct"] == pytest.approx(100 * (np.prod([1 + r["net"] for r in done if r["decision_date"] >= "2026-05-01"]) - 1), abs=0.006)
    assert len(held) == rows[0]["n_held"] and sum(h["weight"] for h in held) == pytest.approx(0.0, abs=1e-9)
    assert all(h["tradable"] == 1 for h in held)
    # the per-contract lines add up to the day
    day = done[-1]["decision_date"]
    lines = book.execute("SELECT SUM(price_pnl), SUM(funding_pnl), SUM(traded) FROM crypto_xs_weights WHERE decision_date=?", (day,)).fetchone()
    assert lines[0] == pytest.approx(done[-1]["gross"]) and lines[1] == pytest.approx(done[-1]["funding"])
    assert lines[2] == pytest.approx(done[-1]["turnover"])

    # the month's funding is published: the estimate gives way to the actual rates
    archive.funding_months.add("2026-05")
    archive.through = date(2026, 6, 1)
    before = {r["decision_date"]: r["funding"] for r in done}
    out = tracker.refresh(datetime(2026, 6, 3, 6, 0, tzinfo=UTC), store=tmp_path, get=archive, connection=book, log=lambda *_: None)
    assert out["funding_months"] == ["2026-05"] and out["funding_through"] == "2026-05-31" and out["through"] == "2026-05-31"
    rows, _, stats = tracker.tab_data(book)
    after = {r["decision_date"]: r for r in rows}
    assert after["2026-05-05"]["funding_source"] == "actual" and after["2026-05-05"]["funding"] != pytest.approx(before["2026-05-05"])
    assert after["2026-04-25"]["funding"] == pytest.approx(before["2026-04-25"])


def test_flags_a_dominant_contract_and_one_that_stops_trading(small_universe, tmp_path, book):
    archive = small_universe
    jump = pd.Timestamp("2026-05-04 12:00", tz="UTC")
    for symbol in SYMBOLS:                                                   # every contract triples: whichever side holds it, >3% of equity
        archive.bars[symbol].loc[jump:, "close"] *= 3.0 if symbol in ("C00USDT", "C13USDT") else 1.0
    archive.bars["C06USDT"] = archive.bars["C06USDT"].loc[:"2026-05-06 11:00"]   # delisted mid-hold
    tracker.refresh(datetime(2026, 5, 12, 6, 0, tzinfo=UTC), store=tmp_path, get=archive, connection=book, log=lambda *_: None)
    rows, _, stats = tracker.tab_data(book)
    by_day = {r["decision_date"]: r for r in rows}
    hit = by_day["2026-05-04"]
    assert "one contract over 3% of equity" in hit["flags"] and hit["top_symbol"] in ("C00USDT", "C13USDT")
    assert abs(hit["top_contribution"]) > 0.03
    assert "stopped trading during the hold (closed at the last price): C06USDT" in by_day["2026-05-06"]["flags"]
    # its old rankings are still in the seven-day average, but there is no market to trade it in
    assert "no trading at the rebalance: C06USDT" in by_day["2026-05-07"]["flags"]
    line = book.execute("SELECT tradable, entry_price FROM crypto_xs_weights WHERE decision_date='2026-05-07' AND symbol='C06USDT'").fetchone()
    assert line == (0, None)
    assert {f["decision_date"] for f in stats["flagged"]} >= {"2026-05-04", "2026-05-06", "2026-05-07"}
    assert stats["unseen"]["flagged"] >= 3


def test_run_live_never_blocks_the_loop(monkeypatch, tmp_path):
    db = tmp_path / "labs.db"
    monkeypatch.setattr(tracker, "get_conn", lambda: sqlite3.connect(db))
    started = []
    monkeypatch.setattr(tracker, "refresh", lambda *a, **k: started.append(1) or {"through": "2026-05-09", "equity": 1.0})
    now = datetime(2026, 5, 12, 12, 0, tzinfo=UTC)
    assert tracker.run_live(now) == {"status": "updating"}                   # never checked: start one
    tracker._worker.join(5)
    assert started == [1]
    conn = sqlite3.connect(db)
    tracker._set_state(conn, checked_at=now.isoformat(timespec="seconds"), data_through="2026-05-10")
    conn.close()
    out = tracker.run_live(now + timedelta(minutes=10))                      # checked ten minutes ago: nothing to do
    assert out["status"] == "idle" and out["data_through"] == "2026-05-10" and started == [1]
    assert tracker.run_live(now + timedelta(minutes=31)) == {"status": "updating"}
    tracker._worker.join(5)
    assert started == [1, 1]


def test_ui_patch_applies_next_to_the_pair_wiring_and_is_idempotent(tmp_path):
    for rel in ("labs/ui/routes.py", "templates/live_strategy.html", "pa_paper_tracker_loop.py", "templates/_crypto_xs.html"):
        target = tmp_path / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy(ROOT / rel, target)
    for name in ("patch_proposer_px_ui.py", "patch_proposer_v3_ui.py", "patch_infy_tcs_pair_ui.py", "patch_crypto_xs_ui.py"):
        run = subprocess.run([sys.executable, str(ROOT / "scripts" / name), str(tmp_path)], capture_output=True, text=True)
        assert run.returncode == 0, run.stderr
    second = subprocess.run([sys.executable, str(ROOT / "scripts" / "patch_crypto_xs_ui.py"), str(tmp_path)],
                            capture_output=True, text=True)
    assert second.returncode == 0 and second.stdout.count("0 edits applied") == 3
    routes = (tmp_path / "labs/ui/routes.py").read_text(encoding="utf-8")
    assert routes.count('"crypto_xs": "Crypto taker imbalance"') == 1 and routes.count("crypto_xs_stats=crypto_xs_stats") == 1
    loop = (tmp_path / "pa_paper_tracker_loop.py").read_text(encoding="utf-8")
    assert loop.count("res = run_crypto_xs_live(now)") == 1 and loop.count('"crypto_xs": None') == 1
    py_compile.compile(str(tmp_path / "labs/ui/routes.py"), doraise=True)
    py_compile.compile(str(tmp_path / "pa_paper_tracker_loop.py"), doraise=True)
    import jinja2
    for rel in ("templates/live_strategy.html", "templates/_crypto_xs.html"):
        jinja2.Environment().parse((tmp_path / rel).read_text(encoding="utf-8"))
