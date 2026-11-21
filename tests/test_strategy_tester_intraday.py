"""Strategy Tester intraday engine (strategy_tester/intraday): exact fills on hand-built minute bars, no look-ahead,
the plain-English reader, and the request route - offline (no Kite)."""
from __future__ import annotations

import numpy as np
import pandas as pd
import pytest

from strategy_tester import service
from strategy_tester.intraday import engine as E, rules as R, run as RUN, store

COSTLESS = (0.0, 0.0)


def _day(date: str, path: list[float] | None = None, base: float = 20000.0, seed: int = 0):
    """375 minute bars; `path` sets closes for the first minutes, the rest drift gently."""
    rng = np.random.default_rng(seed)
    n = 375
    close = base + np.cumsum(rng.normal(0, 3, n))
    if path:
        close[: len(path)] = path
        close[len(path):] = path[-1] + np.cumsum(rng.normal(0, 3, n - len(path)))
    open_ = np.r_[base, close[:-1]]
    high = np.maximum(open_, close) + 1
    low = np.minimum(open_, close) - 1
    idx = pd.date_range(f"{date} 09:15", periods=n, freq="min")
    return pd.DataFrame({"Open": open_, "High": high, "Low": low, "Close": close}, index=idx)


def _market(frames):
    return E.Market("NIFTY", pd.concat(frames))


@pytest.fixture(autouse=True)
def no_costs(monkeypatch):
    monkeypatch.setattr(E, "COST_BPS", COSTLESS)


def test_timed_entry_fills_at_that_minute_open_and_time_exit():
    mk = _market([_day("2024-01-01", seed=1), _day("2024-01-02", seed=2)])
    rule = R.Rule(side="long", entry={"type": "time", "at": "09:20"}, exit_time="15:00")
    tr, pnl = E.simulate(mk, rule, np.array([1]))
    t = tr[0]
    assert t["k_in"] == 5 and t["entry_price"] == mk.o[1, 5]
    assert t["k_out"] == E.k_of("15:00") and t["exit_price"] == mk.o[1, E.k_of("15:00")]
    assert pnl[0] == pytest.approx(t["exit_price"] - t["entry_price"])


def test_stop_counts_first_when_a_minute_touches_both():
    d = _day("2024-01-02", seed=3)
    k = 6                                        # entry at 09:21 open
    entry = d["Open"].iloc[k]
    d.iloc[k + 2, d.columns.get_loc("High")] = entry + 50
    d.iloc[k + 2, d.columns.get_loc("Low")] = entry - 50
    mk = _market([_day("2024-01-01"), d])
    rule = R.Rule(side="long", entry={"type": "time", "at": "09:21"}, stop_pts=30, target_pts=40)
    t = E.simulate(mk, rule, np.array([1]))[0][0]
    if t["exit_reason"] == "Stop loss":
        assert t["exit_price"] == pytest.approx(min(entry - 30, d["Open"].iloc[t["k_out"]]) if t["k_out"] > k else entry - 30)
    assert t["k_out"] <= k + 2 and t["exit_reason"] != "Target"


def test_breakout_is_decided_on_the_completed_minute_and_entered_next():
    path = [20000.0] * 15 + [20000.0, 20010.0, 20030.0, 20040.0]     # ORB(15) high ~20001; minute 17 closes above
    mk = _market([_day("2024-01-01"), _day("2024-01-02", path=path, seed=4)])
    rule = R.Rule(side="long", entry={"type": "orb", "minutes": 15})
    t = E.simulate(mk, rule, np.array([1]))[0][0]
    hi = mk.h[1, :15].max()
    first_close_above = int(np.argmax(mk.c[1] > hi))
    assert t["k_in"] == first_close_above + 1 and t["entry_price"] == mk.o[1, first_close_above + 1]


def test_no_look_ahead_in_events_or_trades():
    frames = [_day(f"2024-01-{i:02d}", seed=i) for i in range(1, 9)]
    rule = R.Rule(side="both", entry={"type": "ema_cross", "fast": 5, "slow": 13, "tf": 5}, stop_pts=20, target_pts=30,
                  max_trades=3)
    a = E.simulate(_market(frames), rule, np.arange(1, 8))[0]
    later = [f.copy() for f in frames]
    later[-1] = later[-1] * 1.03                 # change only the last day
    b = E.simulate(_market(later), rule, np.arange(1, 8))[0]
    keep = lambda ts: [x for x in ts if x["day"] < 7]
    assert keep(a) == keep(b)


def test_trailing_stop_uses_the_previous_bar_best():
    path = [20000.0] * 5 + [20010, 20040, 20060, 20020, 20000]
    d = _day("2024-01-02", path=path, seed=5)
    mk = _market([_day("2024-01-01"), d])
    rule = R.Rule(side="long", entry={"type": "time", "at": "09:20"}, trail_pts=25)
    t = E.simulate(mk, rule, np.array([1]))[0][0]
    assert t["exit_reason"] == "Trailing stop"
    entry = mk.o[1, 5]
    best_before = (mk.h[1, 5:t["k_out"]] - entry).max()
    assert t["exit_price"] == pytest.approx(min(entry + best_before - 25, mk.o[1, t["k_out"]]))


def test_gap_and_weekday_filters():
    d1, d2 = _day("2024-01-01", base=20000), _day("2024-01-02", base=20300)   # +1.5% gap on Tuesday
    mk = _market([d1, d2])
    rule = R.Rule(side="long", entry={"type": "time", "at": "09:20"}, filters=[{"type": "gap", "min": 1.0, "with_side": False}])
    assert len(E.simulate(mk, rule, np.array([1]))[0]) == 1
    rule.filters = [{"type": "gap", "min": 2.0, "with_side": False}]
    assert len(E.simulate(mk, rule, np.array([1]))[0]) == 0
    rule.filters = [{"type": "weekday", "days": [0]}]
    assert len(E.simulate(mk, rule, np.array([1]))[0]) == 0


@pytest.mark.parametrize("text,check", [
    ("buy nifty at 9:20 stop 30 points target 60 points exit at 3:15 pm",
     lambda r: r.entry == {"type": "time", "at": "09:20"} and r.stop_pts == 30 and r.target_pts == 60 and r.exit_time == "15:15" and r.side == "long"),
    ("nifty 15 min opening range breakout both sides stop 40 target 80",
     lambda r: r.entry == {"type": "orb", "minutes": 15} and r.side == "both"),
    ("sell bank nifty on gap up more than 0.5% at 9:30, stop 100 pts, square off 2:30 pm",
     lambda r: r.symbol == "BANKNIFTY" and r.side == "short" and r.entry["at"] == "09:30" and r.exit_time == "14:30"
     and r.filters == [{"type": "gap", "min": 0.5, "with_side": False}]),
    ("intraday nifty previous day high breakout buy, trail 30 points, max 2 trades",
     lambda r: r.entry == {"type": "prev_day"} and r.trail_pts == 30 and r.max_trades == 2),
    ("trade nifty with the gap at 9:16 target 40 points stop 25 points on tuesdays when vix below 15",
     lambda r: r.side == "gap" and r.entry["at"] == "09:16" and {"type": "weekday", "days": [1]} in r.filters),
])
def test_reader(text, check):
    assert R.is_intraday(text)
    assert check(R.parse(text)[0])


def test_daily_requests_still_go_to_the_daily_engine():
    assert not R.is_intraday("backtest golden cross on nifty since 2015")
    assert not R.is_intraday("rsi 14 below 30 on RELIANCE.NS")


def test_request_route_runs_with_validation(monkeypatch):
    frames = [_day(str(d.date()), seed=i) for i, d in enumerate(pd.bdate_range("2019-01-01", "2024-12-31"))]
    bars = pd.concat(frames)
    monkeypatch.setattr(store, "load", lambda symbol, *a, **k: bars)
    monkeypatch.setattr(store, "daily_vix", lambda *a, **k: pd.Series(dtype=float))
    monkeypatch.setattr(RUN, "_markets", {})
    r = service.query("buy nifty at 9:20 stop 30 points target 60 points exit at 3:15 pm all history")
    x = r["results"][0]
    assert x["mode"] == "intraday" and x["metrics"]["trades"] > 1000
    v = x["validation"]
    assert v["random"] and v["walk_forward"] and len(v["walk_forward"]["folds"]) >= 2
    assert x["robustness"]["sensitivity"]["x_values"] and x["verdict"]["label"] in ("Promising", "Mixed", "Weak")
    # random-walk prices: no edge should be claimed
    assert x["verdict"]["label"] != "Promising"
