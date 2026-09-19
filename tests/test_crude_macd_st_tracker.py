from __future__ import annotations

import inspect
import sqlite3
from datetime import date, datetime, timedelta
from pathlib import Path

import numpy as np
import pandas as pd
import pytest

from labs.engine import crude_macd_st_tracker as tracker
from labs.engine.charges import mcx_futures_round_trip_charges


# ── helpers ──────────────────────────────────────────────────────────────────
def _session_frame(day: str, closes: np.ndarray, start: str = "09:00") -> pd.DataFrame:
    ts = pd.date_range(f"{day} {start}", periods=len(closes), freq="1min")
    opens = np.r_[closes[0], closes[:-1]]
    return pd.DataFrame({"ts": ts, "open": opens, "high": np.maximum(opens, closes) + 1,
                         "low": np.minimum(opens, closes) - 1, "close": closes,
                         "volume": 10.0})


def _history(days: list[str], seed: int = 7) -> pd.DataFrame:
    rng = np.random.default_rng(seed)
    frames, level = [], 7000.0
    for day in days:
        steps = rng.normal(0, 4, tracker.BARS_PER_DAY)
        closes = np.round(level + np.cumsum(steps))
        level = closes[-1]
        frames.append(_session_frame(day, closes))
    return pd.concat(frames, ignore_index=True)


def _weekdays(start: str, n: int) -> list[str]:
    d, out = date.fromisoformat(start), []
    while len(out) < n:
        if d.weekday() < 5:
            out.append(d.isoformat())
        d += timedelta(days=1)
    return out


def _grid(bars: list[tuple[float, float, float, float]], partial: bool = False) -> dict:
    """A one-day grid made of the given (open, high, low, close) bars."""
    arr = np.array(bars, dtype=float)
    n = len(bars)
    return {"open": arr[:, 0], "high": arr[:, 1], "low": arr[:, 2], "close": arr[:, 3],
            "valid": np.ones(n, dtype=bool), "day_id": np.zeros(n, dtype=np.int64),
            "partial_last_day": partial, "n": n}


# ── exit simulator: the Tester's rules ───────────────────────────────────────
def test_target_hit_exits_at_the_target_price():
    g = _grid([(100, 100, 100, 100), (100, 101, 99, 100), (100, 106, 99, 105)])
    res = tracker._simulate(g, 0, 10.0)            # stop 90, target 105
    assert res["reason"] == "target" and res["exit"] == 105.0 and res["exit_idx"] == 2


def test_stop_wins_when_stop_and_target_share_a_bar():
    g = _grid([(100, 100, 100, 100), (100, 101, 99, 100), (100, 106, 89, 95)])
    res = tracker._simulate(g, 0, 10.0)
    assert res["reason"] == "stop" and res["exit"] == 90.0


def test_gap_through_the_stop_exits_at_the_open():
    g = _grid([(100, 100, 100, 100), (100, 101, 99, 100), (85, 86, 84, 85)])
    res = tracker._simulate(g, 0, 10.0)
    assert res["reason"] == "stop" and res["exit"] == 85.0


def test_gap_through_the_target_exits_at_the_open():
    g = _grid([(100, 100, 100, 100), (100, 101, 99, 100), (110, 111, 80, 90)])
    res = tracker._simulate(g, 0, 10.0)
    assert res["reason"] == "target" and res["exit"] == 110.0


def test_flat_at_the_last_bar_of_the_session():
    g = _grid([(100, 100, 100, 100), (100, 101, 99, 100), (100, 102, 98, 101)])
    res = tracker._simulate(g, 0, 10.0)
    assert res["reason"] == "eod" and res["exit"] == 101.0 and res["exit_idx"] == 2


def test_running_session_leaves_the_trade_open():
    g = _grid([(100, 100, 100, 100), (100, 101, 99, 100), (100, 102, 98, 101)], partial=True)
    res = tracker._simulate(g, 0, 10.0)
    assert res["status"] == "open" and res["mark_idx"] == 2


# ── signal primitives ────────────────────────────────────────────────────────
def test_cross_below_zero_needs_the_previous_value_at_or_above_zero():
    x = np.array([np.nan, 1.0, -1.0, -2.0, 0.0, -0.5, np.nan, -1.0])
    assert tracker.cross_below_zero(x).tolist() == [False, False, True, False, False, True,
                                                    False, False]


def test_rising_edge_and_cooldown():
    mask = np.array([1, 1, 0, 1, 0, 1, 0, 0, 1], dtype=bool)
    assert tracker.rising_edge(mask).tolist() == [1, 0, 0, 1, 0, 1, 0, 0, 1]
    assert tracker.rearm(tracker.rising_edge(mask), 4).tolist() == [1, 0, 0, 0, 0, 1, 0, 0, 0]


def test_supertrend_starts_down_and_flips_up_on_a_rally():
    close = np.r_[np.full(20, 100.0), np.linspace(100, 160, 20)]
    d = tracker.supertrend_dir(close + 1, close - 1, close)
    finite = d[np.isfinite(d)]
    assert finite[0] == -1.0 and finite[-1] == 1.0


def test_ema_is_seeded_with_the_sma_of_the_first_values():
    out = tracker.ema(np.array([1.0, 2.0, 3.0, 4.0]), 3)
    assert np.isnan(out[:2]).all() and out[2] == 2.0 and out[3] == 3.0


# ── replay ───────────────────────────────────────────────────────────────────
def test_live_replay_never_uses_the_future():
    """Every closed trade seen mid-session is final; an open one exits later."""
    days = _weekdays("2026-07-01", 12)
    frame = _history(days)
    _, final = tracker.replay(frame)
    last = days[-1]
    final = final[final["trade_date"] == last]
    for minutes in range(0, tracker.BARS_PER_DAY, 37):
        cut = datetime.fromisoformat(last) + timedelta(hours=9, minutes=minutes)
        _, live = tracker.replay(frame, cut)
        live = live[live["trade_date"] == last]
        seen = final[final["entry_ts"] < cut]
        closed = live[live["status"] == "closed"]
        assert closed["entry_ts"].tolist() == seen[seen["exit_ts"] < cut]["entry_ts"].tolist()
        assert closed["exit_price"].tolist() == seen[seen["exit_ts"] < cut]["exit_price"].tolist()
        assert live[live["status"] == "open"]["entry_ts"].tolist() == \
            seen[seen["exit_ts"] >= cut]["entry_ts"].tolist()


def test_entries_need_close_above_previous_session_close():
    days = _weekdays("2026-07-01", 15)
    g, trades = tracker.replay(_history(days, seed=3))
    for t in trades.itertuples():
        day_idx = int(np.flatnonzero(g["days"] == date.fromisoformat(t.trade_date))[0])
        i = int(np.flatnonzero(g["ts"] == np.datetime64(t.signal_ts))[0])
        assert g["close"][i] > g["pdc_by_day"][day_idx]


# ── contracts and charges ────────────────────────────────────────────────────
CONTRACTS = [
    {"tradingsymbol": "CRUDEOIL26SEPFUT", "instrument_token": 1, "expiry": date(2026, 9, 21)},
    {"tradingsymbol": "CRUDEOIL26OCTFUT", "instrument_token": 2, "expiry": date(2026, 10, 19)},
]


def test_front_contract_rolls_on_the_expiry_day():
    assert tracker.front_contract(CONTRACTS, date(2026, 6, 1))["tradingsymbol"] == "CRUDEOIL26SEPFUT"
    assert tracker.front_contract(CONTRACTS, date(2026, 9, 18))["tradingsymbol"] == "CRUDEOIL26SEPFUT"
    assert tracker.front_contract(CONTRACTS, date(2026, 9, 21))["tradingsymbol"] == "CRUDEOIL26OCTFUT"


def test_mcx_charges_match_the_tester_model():
    c = mcx_futures_round_trip_charges(8000.0, 8010.0, 100)
    buy, sell = 800_000.0, 801_000.0
    turnover = buy + sell
    brokerage = 40.0                                 # 0.03% capped at ₹20 per order
    txn = turnover * 0.000021
    sebi = turnover * 10 / 1e7
    expected = brokerage + sell * 0.0001 + txn + sebi + buy * 0.00002 \
        + 0.18 * (brokerage + txn + sebi)
    assert c["total"] == round(expected, 2)


# ── run_day end to end with a fake Kite ──────────────────────────────────────
class FakeKite:
    def __init__(self, frame: pd.DataFrame):
        self.frame = frame
        self.calls = 0

    def instruments(self, exchange):
        return [{**c, "name": "CRUDEOIL", "instrument_type": "FUT"} for c in CONTRACTS]

    def historical_data(self, token, frm, to, interval):
        self.calls += 1
        rows = self.frame[(self.frame["ts"] >= frm) & (self.frame["ts"] <= to)]
        return [{"date": r.ts.to_pydatetime(), "open": r.open, "high": r.high, "low": r.low,
                 "close": r.close, "volume": r.volume} for r in rows.itertuples()]


def _conn(tmp_path: Path) -> sqlite3.Connection:
    conn = sqlite3.connect(tmp_path / "labs_test.db")
    tracker._ensure_tables(conn)
    return conn


def test_run_day_persists_the_replayed_session(tmp_path, monkeypatch):
    monkeypatch.setitem(tracker._INSTRUMENTS, "date", None)
    days = _weekdays("2026-07-01", 20)
    frame = _history(days, seed=11)
    kite = FakeKite(frame)
    conn = _conn(tmp_path)
    after = datetime.fromisoformat(days[-1]) + timedelta(days=1, hours=10)
    expected = tracker.replay(frame)[1]
    expected = expected[expected["trade_date"] == days[-1]]
    res = tracker.run_day(days[-1], kite=kite, now=after, connection=conn)
    assert res["status"] == "final" and res["n_trades"] == len(expected)
    rows = conn.execute("SELECT entry_ts, exit_price, qty FROM crude_macd_st_trades "
                        "WHERE trade_date=? ORDER BY seq", (days[-1],)).fetchall()
    assert [r[0] for r in rows] == [str(t) for t in expected["entry_ts"]]
    assert all(r[2] == 100 for r in rows)
    calls = kite.calls
    assert tracker.run_day(days[-1], kite=kite, now=after, connection=conn)["skipped"]
    assert kite.calls == calls                        # frozen session is not refetched


def test_run_day_mid_session_stores_only_closed_candles(tmp_path, monkeypatch):
    monkeypatch.setitem(tracker._INSTRUMENTS, "date", None)
    days = _weekdays("2026-07-01", 3)
    frame = _history(days, seed=5)
    conn = _conn(tmp_path)
    now = datetime.fromisoformat(days[-1]) + timedelta(hours=12, minutes=30, seconds=20)
    res = tracker.run_day(days[-1], kite=FakeKite(frame), now=now, connection=conn)
    assert res["status"] == "live"
    last = conn.execute("SELECT MAX(ts) FROM crude_minute_bars").fetchone()[0]
    assert last == f"{days[-1]} 12:29:00"
    covered = {r[0] for r in conn.execute("SELECT trade_date FROM crude_minute_coverage")}
    assert days[-1] not in covered and days[0] in covered


# ── wiring ───────────────────────────────────────────────────────────────────
def test_registered_as_a_live_tab():
    import labs.ui.routes as routes
    assert '"crude_macd_st"' in inspect.getsource(routes.live_strategy)
    assert hasattr(routes, "crude_macd_st_backfill")


def test_paper_loop_runs_the_book_in_the_mcx_session():
    src = Path(__file__).resolve().parents[1].joinpath("pa_paper_tracker_loop.py").read_text(
        encoding="utf-8")
    assert "run_crude_macd_st_live" in src and "_in_mcx_session" in src


def test_tracker_places_no_orders():
    src = inspect.getsource(tracker)
    for forbidden in ("place_order", "live_executor", "place_idempotent"):
        assert forbidden not in src, forbidden


def test_dashboard_renders_the_crude_tab(monkeypatch):
    pytest.importorskip("flask")
    from flask import Flask
    from labs.engine import paper_strategy_tracker
    from labs.ui.routes import labs_bp

    conn = sqlite3.connect(":memory:")
    paper_strategy_tracker._ensure_tables(conn)
    tracker._ensure_tables(conn)
    conn.execute(
        "INSERT INTO crude_macd_st_daily (trade_date,status,tradingsymbol,expiry,pdc,valid_bars,"
        "n_signals,n_trades,open_trades,wins,gross_rs,charges_rs,net_rs,qty,strategy_version,"
        "updated_at) VALUES ('2026-07-14','final','CRUDEOIL26SEPFUT','2026-09-21',7500,870,"
        "2,1,0,1,1847.2,196.1,1651.1,100,?, '2026-07-14T23:41:00+05:30')",
        (tracker.STRATEGY_VERSION,))
    conn.execute(
        "INSERT INTO crude_macd_st_trades (trade_date,seq,tradingsymbol,signal_ts,entry_ts,exit_ts,"
        "entry_price,exit_price,stop_price,target_price,stop_dist,r_multiple,points,qty,gross_rs,"
        "charges_rs,net_rs,status,exit_reason,bars_held) VALUES ('2026-07-14',1,'CRUDEOIL26SEPFUT',"
        "'2026-07-14 13:24:00','2026-07-14 13:25:00','2026-07-14 13:26:00',7603,7621.472,"
        "7566.056,7621.472,36.944,0.5,18.472,100,1847.2,196.1,1651.1,'closed','target',2)")
    conn.commit()
    app = Flask(__name__, template_folder="../templates")
    app.register_blueprint(labs_bp)
    monkeypatch.setattr("storage.db.get_conn", lambda: conn)
    html = app.test_client().get("/labs/live?tab=crude_macd_st").get_data(as_text=True)
    assert "CRUDEOIL MACD + Supertrend flip" in html
    assert "&#8377;1,651.10" in html and "target" in html
    assert "Capital deployed" in html


# ── capital deployed ─────────────────────────────────────────────────────────
class MarginKite(FakeKite):
    def __init__(self, frame, margin):
        super().__init__(frame)
        self.margin = margin
        self.margin_calls = 0

    def order_margins(self, orders):
        self.margin_calls += 1
        assert orders[0]["exchange"] == "MCX" and orders[0]["quantity"] == 1
        return [{"total": self.margin}]


def _trades(conn, day):
    return conn.execute("SELECT entry_ts, entry_price, notional_rs, margin_rs, margin_source "
                        "FROM crude_macd_st_trades WHERE trade_date=? ORDER BY seq",
                        (day,)).fetchall()


def _day_with_trades(seed_from=11):
    days = _weekdays("2026-07-01", 20)
    for seed in range(seed_from, seed_from + 50):
        frame = _history(days, seed=seed)
        trades = tracker.replay(frame)[1]
        if (trades["trade_date"] == days[-1]).any():
            return days, frame, trades[trades["trade_date"] == days[-1]]
    raise AssertionError("no synthetic session with a trade")


def test_backfilled_trades_use_the_estimated_margin(tmp_path, monkeypatch):
    monkeypatch.setitem(tracker._INSTRUMENTS, "date", None)
    days, frame, _ = _day_with_trades()
    kite = MarginKite(frame, 250_000.0)
    conn = _conn(tmp_path)
    after = datetime.fromisoformat(days[-1]) + timedelta(days=1, hours=10)
    tracker.run_day(days[-1], kite=kite, now=after, connection=conn)
    rows = _trades(conn, days[-1])
    assert rows and kite.margin_calls == 0           # today's quote never used for history
    for _, entry, notional, margin, source in rows:
        assert notional == round(entry * 100, 2)
        assert margin == round(notional * tracker.EST_MARGIN_RATE, 2)
        assert source == tracker.EST_MARGIN_SOURCE


def test_live_trade_keeps_the_margin_quoted_at_entry(tmp_path, monkeypatch):
    monkeypatch.setitem(tracker._INSTRUMENTS, "date", None)
    days, frame, expected = _day_with_trades()
    first = expected.iloc[0]
    conn = _conn(tmp_path)
    kite = MarginKite(frame, 291_537.5)
    seen = first["entry_ts"].to_pydatetime() + timedelta(minutes=1, seconds=5)
    tracker.run_day(days[-1], kite=kite, now=seen, connection=conn)
    row = _trades(conn, days[-1])[0]
    assert row[3] == 291_537.5 and row[4] == "kite_at_entry"
    kite.margin = 999_999.0                          # a later quote must not replace it
    later = datetime.fromisoformat(days[-1]) + timedelta(hours=23, minutes=35)
    tracker.run_day(days[-1], kite=kite, now=later, connection=conn)
    after_row = [r for r in _trades(conn, days[-1]) if r[0] == row[0]][0]
    assert after_row[3] == 291_537.5 and after_row[4] == "kite_at_entry"
