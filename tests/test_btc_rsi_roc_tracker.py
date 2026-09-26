"""Paper-only BTCUSDT RSI/ROC short book (Strategy Tester btcusdt rank 409).

Exact parity with the Tester's 40 research trades and its what-if money is checked by
research/experiments/2026-09-26_btc_rsi_roc_parity/parity.py against the Tester's own data
(not in this repo); these tests pin the engine mechanics, the money, the data plumbing and
the wiring.
"""

from __future__ import annotations

import sqlite3
import urllib.error
from datetime import date, datetime
from pathlib import Path

import numpy as np
import pandas as pd
import pytest

from labs.engine import btc_rsi_roc_tracker as B

ROOT = Path(__file__).resolve().parents[1]


def _grid(opens, highs, lows, closes, valid=None):
    n = len(opens)
    return {"open": np.array(opens, float), "high": np.array(highs, float),
            "low": np.array(lows, float), "close": np.array(closes, float),
            "valid": np.ones(n, bool) if valid is None else np.array(valid, bool),
            "day_id": np.zeros(n, np.int64), "n": n}


# -- trade walk (short) --------------------------------------------------------------
def test_short_target_is_below_and_exits_at_the_target_price():
    g = _grid([100, 100, 99], [100, 100.5, 99.2], [100, 99.4, 98.4], [100, 99.6, 98.6])
    res = B._simulate(g, 0)
    assert res["reason"] == "target" and res["exit"] == pytest.approx(100 - 1.5)
    assert res["stop"] == pytest.approx(101.0)


def test_stop_wins_when_stop_and_target_share_a_bar():
    g = _grid([100, 100, 100], [100, 100, 101.2], [100, 100, 98.0], [100, 100, 100])
    res = B._simulate(g, 0)
    assert res["reason"] == "stop" and res["exit"] == pytest.approx(101.0)


def test_gap_through_the_stop_exits_at_the_open():
    g = _grid([100, 100, 102], [100, 100, 102.5], [100, 99.9, 101.8], [100, 100, 102])
    res = B._simulate(g, 0)
    assert res["reason"] == "stop" and res["exit"] == 102.0


def test_gap_through_the_target_exits_at_the_open():
    g = _grid([100, 100, 97], [100, 100, 97.2], [100, 99.9, 96.8], [100, 100, 97])
    res = B._simulate(g, 0)
    assert res["reason"] == "target" and res["exit"] == 97.0


def test_invalid_bars_are_skipped_and_the_end_of_data_leaves_it_open():
    g = _grid([100, 100, np.nan, 100.1], [100, 100.2, np.nan, 100.3],
              [100, 99.8, np.nan, 99.9], [100, 100, np.nan, 100.2], [1, 1, 0, 1])
    res = B._simulate(g, 0)
    assert res["status"] == "open" and res["held"] == 2 and res["mark_idx"] == 3


def test_max_hold_exits_at_the_close(monkeypatch):
    monkeypatch.setattr(B, "MAX_HOLD_BARS", 3)
    g = _grid([100] * 6, [100.1] * 6, [99.9] * 6, [100, 100, 100, 99.7, 100, 100])
    res = B._simulate(g, 0)
    assert res["reason"] == "time" and res["exit"] == 99.7 and res["held"] == 3


# -- signal operators ------------------------------------------------------------------
def test_cross_above_needs_the_previous_value_at_or_below_the_threshold():
    x = np.array([np.nan, 29.0, 30.0, 31.0, 32.0, 29.0, 31.0])
    assert B.cross_above(x, 30.0).tolist() == [False, False, False, True, False, False, True]


def test_between_is_inclusive_and_nan_is_false():
    assert B.between(np.array([39.9, 40, 60, 60.1, np.nan]), 40, 60).tolist() == \
        [False, True, True, False, False]


def test_rising_edge_and_cooldown():
    m = np.array([0, 1, 1, 0, 1, 0, 0, 1], bool)
    assert B.rising_edge(m).tolist() == [False, True, False, False, True, False, False, True]
    assert B.rearm(B.rising_edge(m), 3).tolist() == \
        [False, True, False, False, False, False, False, True]


def test_fires_need_both_trigger_states_and_the_gate(monkeypatch):
    t = np.array([0, 1, 1, 1, 0, 1], bool)
    r = np.array([0, 0, 1, 1, 1, 1], bool)
    gate = np.array([1, 1, 1, 1, 1, 0], bool)
    fires = B.fires_from({"rsi_h1_x": t, "roc_m15_x": r, "gate": gate})
    assert np.flatnonzero(fires).tolist() == [2]      # edge at 2; the 5 edge is gated off


def test_rsi_is_wilder_with_an_sma_seed():
    close = np.arange(1.0, 20.0)                      # only gains -> 100 once seeded
    out = B.rsi(close, 7)
    assert np.isnan(out[:7]).all() and out[7] == 100.0


def test_roc_is_percent_change_over_n_bars():
    out = B.roc(np.array([100.0, 101, 102, 110]), 2)
    assert np.isnan(out[:2]).all() and out[2] == pytest.approx(2.0)
    assert out[3] == pytest.approx(100 * (110 / 101 - 1))


# -- grid / timeframes -----------------------------------------------------------------
def _frame(start: str, minutes: int, price=lambda k: 100.0 + k * 0.01) -> pd.DataFrame:
    ts = pd.date_range(start, periods=minutes, freq="min")
    p = np.array([price(k) for k in range(minutes)])
    return pd.DataFrame({"ts": ts, "open": p, "high": p + 0.5, "low": p - 0.5,
                         "close": p, "volume": 1.0})


def test_hourly_value_is_visible_on_the_last_minute_of_its_hour():
    g = B.build_grid(_frame("2026-06-01 00:00", 180))
    h1 = B.resample(g, 60)                            # a grid spans whole UTC days: 24 bins
    state = np.zeros(h1["close"].size, bool)
    state[0] = True                                   # only the 00:00-00:59 bin
    vis = B._broadcast(g, h1, state)
    assert not vis[58] and vis[59] and vis[118] and not vis[119]


def test_grid_is_utc_every_day_and_cut_at_the_first_incomplete_minute():
    f = _frame("2026-06-06 23:58", 5)                 # Sat -> Sun: crypto has no weekend
    g = B.build_grid(f, cutoff=datetime(2026, 6, 7, 0, 2))
    assert list(g["days"]) == [date(2026, 6, 6), date(2026, 6, 7)]
    assert g["partial_last_day"] and g["n"] == 1440 + 2
    assert g["valid"][-2:].all()


def test_live_replay_never_uses_the_future():
    """Every trade closed by a mid-day cutoff equals the same trade of the full replay."""
    rng = np.random.default_rng(7)
    steps = rng.normal(0, 25, 12 * 1440).cumsum()
    f = _frame("2026-05-01 00:00", 12 * 1440, price=lambda k: 60000 + steps[k])
    f["high"] = f["close"] + rng.uniform(0, 40, len(f))
    f["low"] = f["close"] - rng.uniform(0, 40, len(f))
    _, full = B.replay(f)
    assert len(full) > 0
    for cut in (datetime(2026, 5, 6, 13, 7), datetime(2026, 5, 9, 2, 44)):
        _, part = B.replay(f, cutoff=cut)
        closed = part[part.status == "closed"]
        ref = full.set_index("entry_ts")
        for t in closed.itertuples():
            assert ref.loc[t.entry_ts, "exit_ts"] == t.exit_ts
            assert ref.loc[t.entry_ts, "exit_price"] == pytest.approx(t.exit_price)


# -- money -----------------------------------------------------------------------------
def _rates(*pairs):
    s = pd.Series({d: r for d, r in pairs}, dtype="float64")
    s.index = pd.to_datetime(s.index)
    return s


def test_money_is_the_testers_what_if_at_0_01_btc():
    # Tester rank 409, trade 1: short 70431.69 -> target 69375.214658
    t = {"entry_price": 70431.69, "exit_price": 69375.214658, "points": 1056.475342,
         "entry_ts": "2026-03-21 19:15:00", "exit_ts": "2026-03-21 23:51:00"}
    m = B.price_trade(t, _rates(("2026-03-20", 80.0)), date(2026, 9, 26))
    gross = 1056.475342 * 0.01
    fees = (69375.214658 + 70431.69) * 0.01 * 0.001
    slip = 2 * 0.01 * 0.01
    assert m["net_usdt"] == pytest.approx(gross - fees - slip, abs=1e-6)
    assert m["net_rs"] == pytest.approx(round((gross - fees - slip) * 80.0, 2))
    assert m["notional_rs"] == pytest.approx(round(70431.69 * 0.01 * 80.0, 2))


def test_fx_uses_the_last_published_rate_on_weekends():
    r = _rates(("2026-09-18", 83.0), ("2026-09-21", 84.0))
    assert B.rate_on(r, "2026-09-20") == 83.0            # Sunday -> Friday's rate
    assert B.rate_on(r, "2026-09-21") == 84.0
    assert B.rate_on(r, "2026-09-01") == 83.0            # before the first -> the first


# -- data plumbing ---------------------------------------------------------------------
def _k(ts: str, p: float) -> list:
    ms = int(pd.Timestamp(ts, tz="UTC").timestamp() * 1000)
    return [ms, str(p), str(p + 1), str(p - 1), str(p), "1.0"]


def test_minute_store_pages_resumes_and_drops_the_forming_minute(monkeypatch):
    conn = sqlite3.connect(":memory:")
    B._ensure_tables(conn)
    pages = [[_k("2026-06-01 00:00", 1), _k("2026-06-01 00:01", 2)],
             [_k("2026-06-01 00:02", 3), _k("2026-06-01 00:03", 4)], []]
    calls = []

    def fake(start_ms, end_ms, limit=1000):
        calls.append(start_ms)
        return pages[len(calls) - 1]

    monkeypatch.setattr(B, "fetch_klines", fake)
    now = datetime(2026, 6, 1, 5, 33, 20, tzinfo=B.IST)    # 00:03:20 UTC: 00:03 still forming
    out = B.ensure_minute_bars(date(2026, 6, 1), now, conn)
    stored = [r[0] for r in conn.execute("SELECT ts FROM btc_minute_bars ORDER BY ts")]
    assert stored == ["2026-06-01 00:00:00", "2026-06-01 00:01:00", "2026-06-01 00:02:00"]
    assert out["complete"] and len(calls) == 2


def test_klines_fall_back_when_binance_refuses_the_region(monkeypatch):
    seen = []

    def fake(url, params, timeout=20.0):
        seen.append(url)
        if "api.binance.com" in url:
            raise urllib.error.HTTPError(url, 451, "restricted", {}, None)
        return [["ok"]]

    monkeypatch.setattr(B, "_http_json", fake)
    assert B.fetch_klines(0, 1) == [["ok"]]
    assert "data-api.binance.vision" in seen[-1]


def test_run_live_rewrites_the_ledger_from_the_book_start(monkeypatch):
    conn = sqlite3.connect(":memory:")
    B._ensure_tables(conn)
    monkeypatch.setattr(B, "ensure_minute_bars",
                        lambda start, now, c, max_pages=None: {"pages": 0, "complete": True,
                                                               "through": "x"})
    frame = _frame("2026-06-01 00:00", 3 * 1440)
    monkeypatch.setattr(B, "load_minute_bars", lambda start, c: frame)
    fires = np.zeros(3 * 1440, bool)
    fires[600] = True                                   # one signal on day 1

    def states(g):
        return {"rsi_h1_x": fires[:g["n"]], "roc_m15_x": fires[:g["n"]],
                "gate": np.ones(g["n"], bool)}

    monkeypatch.setattr(B, "signal_states", states)
    monkeypatch.setattr(B, "ensure_fx", lambda a, b, c: None)
    conn.execute("INSERT INTO fx_usd_inr VALUES ('2026-05-29', 85.0, 'test', 'x')")
    out = B.run_live(datetime(2026, 6, 4, 12, 0, tzinfo=B.IST), connection=conn)
    assert out["status"] == "live" and out["trades"] == 1
    row = conn.execute("SELECT trade_date, status, entry_ts, net_rs, fx_rate FROM "
                       "btc_rsi_roc_trades").fetchone()
    assert row[0] == "2026-06-01" and row[2] == "2026-06-01 10:01:00" and row[4] == 85.0
    days = conn.execute("SELECT trade_date, n_signals, n_trades, strategy_version FROM "
                        "btc_rsi_roc_daily ORDER BY trade_date").fetchall()
    assert days[0][:3] == ("2026-06-01", 1, 1) and days[0][3] == B.STRATEGY_VERSION


# -- wiring ----------------------------------------------------------------------------
def test_registered_as_a_live_tab_overview_card_and_paper_loop():
    from labs.services.book_overview import BOOKS
    from labs.ui.routes import LIVE_TABS
    assert LIVE_TABS["btc_rsi_roc"] == "BTC RSI/ROC short"
    assert BOOKS["btc_rsi_roc"]["size"] == "0.01 BTC"
    loop = (ROOT / "pa_paper_tracker_loop.py").read_text(encoding="utf-8")
    assert "run_btc_rsi_roc_live" in loop and '"btc_rsi_roc"' in loop
    template = (ROOT / "templates" / "live_strategy.html").read_text(encoding="utf-8")
    assert "active_live_tab == 'btc_rsi_roc'" in template


def test_tracker_places_no_orders():
    for name in ("btc_rsi_roc_tracker.py", "btc_rsi_roc_backfill.py"):
        src = (ROOT / "labs" / "engine" / name).read_text(encoding="utf-8")
        assert "place_order" not in src and "/api/v3/order" not in src


def test_dashboard_renders_the_btc_tab(monkeypatch):
    from flask import Flask
    from labs.ui.routes import labs_bp
    from labs.engine import paper_strategy_tracker
    day = date.today().isoformat()                    # inside the page's default window
    conn = sqlite3.connect(":memory:")
    paper_strategy_tracker._ensure_tables(conn)       # the page reads the NIFTY book first
    B._ensure_tables(conn)
    conn.execute(
        "INSERT INTO btc_rsi_roc_daily (trade_date,status,tradingsymbol,valid_bars,n_signals,"
        "n_trades,open_trades,wins,gross_rs,charges_rs,slippage_rs,net_rs,net_usdt,qty,"
        "strategy_version,updated_at) VALUES (?,'final','BTCUSDT',1440,1,1,0,1,"
        "900,130,0.02,769.98,9.1,0.01,?,'x')", (day, B.STRATEGY_VERSION))
    conn.execute(
        "INSERT INTO btc_rsi_roc_trades (trade_date,seq,tradingsymbol,signal_ts,entry_ts,"
        "exit_ts,entry_price,exit_price,stop_price,target_price,stop_dist,r_multiple,points,"
        "qty,net_usdt,fx_rate,gross_rs,charges_rs,slippage_rs,net_rs,notional_rs,margin_rs,"
        "status,exit_reason,bars_held) VALUES (?,1,'BTCUSDT',?,?,?,70000,68950,70700,68950,"
        "700,1.5,1050,0.01,9.1,84.6,888.3,118.3,0.02,769.98,59220,59220,'closed','target',240)",
        (day, f"{day} 10:00:00", f"{day} 10:01:00", f"{day} 14:00:00"))
    conn.commit()
    app = Flask(__name__, template_folder="../templates")
    app.register_blueprint(labs_bp)
    monkeypatch.setattr("storage.db.get_conn", lambda: conn)
    html = app.test_client().get("/labs/live?tab=btc_rsi_roc").get_data(as_text=True)
    assert "BTC RSI/ROC short" in html
    assert f"{day[5:]} 15:31" in html                   # 10:01 UTC shown as IST
    assert "target" in html and "769.98" in html
