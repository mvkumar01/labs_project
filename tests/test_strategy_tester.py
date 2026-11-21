"""Strategy Tester (strategy_tester/): engine mechanics, data handling and request limits, offline (synthetic prices,
no Yahoo)."""
from __future__ import annotations

import numpy as np
import pandas as pd
import pytest

from strategy_tester import data, engine, service, strategies as S


def _daily(n=1500, seed=3, start="2016-01-01", drift=0.0003):
    rng = np.random.default_rng(seed)
    idx = pd.bdate_range(start, periods=n)
    close = 10_000 * np.exp(np.cumsum(rng.normal(drift, 0.011, n)))
    open_ = np.r_[close[0], close[:-1]] * np.exp(rng.normal(0, 0.003, n))
    high = np.maximum(open_, close) * (1 + rng.uniform(0, 0.006, n))
    low = np.minimum(open_, close) * (1 - rng.uniform(0, 0.006, n))
    return pd.DataFrame({"Open": open_, "High": high, "Low": low, "Close": close, "Volume": 0.0}, index=idx)


@pytest.fixture
def prices(monkeypatch):
    frames = {"^NSEI": _daily(), "RELIANCE.NS": _daily(seed=5), "AAPL": _daily(seed=7)}
    monkeypatch.setattr(data, "get_history", lambda t, interval="1d": frames[t])
    return frames


ZERO = dict(commission_bps=0, slippage_bps=0)


def test_end_date_is_inclusive_and_never_takes_the_next_day(prices):
    df = prices["^NSEI"]
    end = df.index[700]
    p, _ = data.load("^NSEI", str(df.index[300].date()), str(end.date()), "1d")
    assert p.df.index[-1] == end


def test_intraday_bars_stay_on_the_exchange_clock(monkeypatch):
    idx = pd.date_range("2026-11-02 09:15", periods=7, freq="h", tz="Asia/Kolkata")
    raw = pd.DataFrame({"Open": 1.0, "High": 1.0, "Low": 1.0, "Close": 1.0, "Volume": 0.0}, index=idx)
    monkeypatch.setattr(data, "yf", None)
    monkeypatch.setattr(data._direct, "history", lambda t, i: raw)
    out = data._download("^NSEI", "1h")
    assert out.index[0] == pd.Timestamp("2026-11-02 09:15") and out.index.tz is None
    assert data._cache_key("^NSEI", "1h").endswith("_v2")


def test_indian_costs_and_no_overnight_shorts(prices):
    assert engine.default_costs("equity", "RELIANCE.NS") == (11.2, 3)
    assert engine.default_costs("index", "^NSEI") == (1.3, 1)
    assert engine.default_costs("equity", "AAPL") == (5, 2)
    r = engine.run_backtest("RELIANCE.NS", "sma_cross", {"fast": 20, "slow": 50}, "2017-01-01", None, "1d",
                            engine.BacktestConfig(allow_short=True), robustness=False)
    assert r["config"]["allow_short"] is False and r["config"]["commission_bps"] == 11.2
    assert any("short" in n.lower() for n in r["notes"])
    assert all(t["side"] == "Long" for t in r["trades"])
    us = engine.run_backtest("AAPL", "sma_cross", {"fast": 20, "slow": 50}, "2017-01-01", None, "1d",
                             engine.BacktestConfig(allow_short=True), robustness=False)
    assert us["config"]["allow_short"] is True and any(t["side"] == "Short" for t in us["trades"])


def test_matches_an_independent_replay(prices):
    """SMA 20/50 long-only, fills at the next open, zero costs - re-derived without the engine."""
    r = engine.run_backtest("^NSEI", "sma_cross", {"fast": 20, "slow": 50}, "2017-01-02", None, "1d",
                            engine.BacktestConfig(**ZERO), robustness=False, detail=False)
    df = prices["^NSEI"]
    p, ts = data.load("^NSEI", "2017-01-02", None, "1d", warmup_bars=S.get("sma_cross").warmup({"fast": 20, "slow": 50}))
    d = p.df
    i0 = int(d.index.searchsorted(ts))
    f, s = d.Close.rolling(20).mean(), d.Close.rolling(50).mean()
    pos = ((f > s) & f.notna() & s.notna()).astype(float).shift(1).fillna(0)
    held = pos > 0
    prev = pos.shift(1).fillna(0) > 0
    ret = np.where(held, np.where(prev, d.Close / d.Close.shift(1) - 1, d.Close / d.Open - 1), 0.0)
    ret += np.where(~held & prev, d.Open / d.Close.shift(1) - 1, 0.0)
    eq = (1 + pd.Series(ret, index=d.index).iloc[i0:]).cumprod() * 100_000
    assert r["metrics"]["final_equity"] == pytest.approx(eq.iloc[-1], rel=1e-9)
    assert len(df) > 0


def test_no_look_ahead(prices, monkeypatch):
    base = prices["^NSEI"]
    cut = base.index[900]
    bumped = base.copy()
    bumped.loc[bumped.index > cut, ["Open", "High", "Low", "Close"]] *= 1.3
    for key in ("sma_cross", "rsi", "donchian", "supertrend", "bollinger_reversion"):
        a = engine.run_backtest("^NSEI", key, {}, "2017-01-02", None, "1d", engine.BacktestConfig(**ZERO),
                                robustness=False, detail=False)
        monkeypatch.setattr(data, "get_history", lambda t, interval="1d": bumped)
        b = engine.run_backtest("^NSEI", key, {}, "2017-01-02", None, "1d", engine.BacktestConfig(**ZERO),
                                robustness=False, detail=False)
        monkeypatch.setattr(data, "get_history", lambda t, interval="1d": prices[t])
        ea = {x["time"]: x["value"] for x in a["series"]["equity"] if pd.Timestamp(x["time"]) <= cut}
        eb = {x["time"]: x["value"] for x in b["series"]["equity"] if pd.Timestamp(x["time"]) <= cut}
        assert ea == eb, key


def test_stop_before_target_and_gap_fills():
    idx = pd.date_range("2024-01-01", periods=6, freq="D")
    bars = pd.DataFrame({"Open": [100, 100, 100, 80, 100, 100], "High": [100, 101, 112, 82, 101, 101],
                         "Low": [100, 99, 89, 78, 99, 99], "Close": [100, 100, 100, 81, 100, 100], "Volume": 0},
                        index=idx)
    one = pd.Series(1.0, index=idx)
    o = engine.simulate(bars, one, 1, engine.BacktestConfig(**ZERO, stop_loss_pct=10, take_profit_pct=10))
    assert (o.trades[0]["exit_reason"], o.trades[0]["exit_price"]) == ("Stop loss", 90)
    assert len(o.trades) == 1                                  # waits for a fresh signal after the stop
    o2 = engine.simulate(bars.iloc[[0, 1, 3, 4]], one.iloc[:4].set_axis(bars.index[[0, 1, 3, 4]]), 1,
                         engine.BacktestConfig(**ZERO, stop_loss_pct=10))
    assert o2.trades[0]["exit_price"] == 80                    # gap through the stop fills at the open


def test_query_respects_job_limit_and_time_budget(prices, monkeypatch):
    monkeypatch.setattr(service, "MAX_JOBS", 3)
    r = service.query("which strategy works best on nifty since 2017")
    assert len(r["results"]) == 3 and any("Only the first 3" in n for n in r["spec"]["notes"])
    monkeypatch.setattr(service, "TIME_BUDGET_S", -1.0)
    with pytest.raises(service.ServiceError):
        service.query("which strategy works best on nifty since 2017")


def test_rate_limit(monkeypatch):
    from flask import Flask
    from strategy_tester import flask_app
    app = Flask(__name__)
    app.register_blueprint(flask_app.bp)
    monkeypatch.setattr(flask_app, "RATE_LIMIT", 2)
    monkeypatch.setattr(flask_app, "_hits", {})
    monkeypatch.setattr(service, "query", lambda prompt: {"ok": True})
    c = app.test_client()
    codes = [c.post("/labs/strategy-tester/api/query", json={"prompt": "x"},
                    headers={"X-Forwarded-For": "1.2.3.4"}).status_code for _ in range(3)]
    assert codes == [200, 200, 429]
    assert c.post("/labs/strategy-tester/api/query", json={"prompt": "x"},
                  headers={"X-Forwarded-For": "5.6.7.8"}).status_code == 200
