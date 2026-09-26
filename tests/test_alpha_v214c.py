"""Alpha v2.14 C = Alpha v2.14 B with a Renko 30 (classic reversal) overlay. Paper only.

Parity with the research (research/experiments/2026-09-24_s55_vs_paper/matrix_renko.py,
"B renko30 r2") is checked on 59 days outside the test suite; these pin the rule.
"""

from __future__ import annotations

from pathlib import Path

import pandas as pd

from labs.engine import alpha_v212b10_tracker as b10
from labs.engine import alpha_v214c_tracker as v214c
from live.engine import champion_sim
from live.engine.champion_sim import (
    V214C_RENKO_BRICK, V214C_RENKO_REVERSAL, renko_events,
)

import test_v212_close_confirmed as fx

ROOT = Path(__file__).resolve().parents[1]


# -- bricks ---------------------------------------------------------------------------
def test_classic_reversal_needs_two_bricks_against_the_trend():
    s = [(f"k{i}", p) for i, p in enumerate([100, 106, 111, 108, 101, 99, 95, 89, 100])]
    assert renko_events(s, 5.0, 2) == {"k1": "u", "k2": "u", "k5": "d", "k6": "d",
                                       "k7": "d", "k8": "u"}


def test_one_price_can_complete_several_bricks():
    s = [("a", 100.0), ("b", 161.0)]
    assert renko_events(s, 30.0, 2) == {"b": "uu"}


def test_constants_are_30_point_classic():
    assert (V214C_RENKO_BRICK, V214C_RENKO_REVERSAL) == (30.0, 2)


# -- the overlay -----------------------------------------------------------------------
def _run(*, renko: bool, trace=None, closes=None):
    adf, ohlc = fx._inputs(stop_close=101.0)        # PC50 CALL enters on the 09:20 bar
    for key, close in (closes or {}).items():
        ohlc.by_minute[key] = (close, close, close, close)
    kw = dict(enable_entry_spot_recovery=True, entry_spot_close_confirmed=True,
              entry_spot_exit_buffer=10.0, entry_spot_check_entry_bar=True)
    if renko:
        kw.update(entry_spot_renko_brick=30.0, entry_spot_renko_reversal=2,
                  entry_spot_recovery_trace=trace)
    _, segs = champion_sim.simulate(adf, {}, {}, ohlc, fx.DATE, False, 1.0, "PC50", "Thu",
                                    "STD", 0, 1000, **kw)
    return segs


def test_down_brick_exits_the_call_and_up_brick_re_enters_at_the_current_close():
    trace: list = []
    segs = _run(renko=True, trace=trace, closes={"09:22": 60.0, "09:24": 140.0})
    hm = lambda ts: pd.Timestamp(ts).strftime("%H:%M")
    assert segs[0]["reason"] == "ENTRY_SPOT_SL" and hm(segs[0]["exit_ts"]) == "09:22"
    assert segs[0]["exit_spot"] == 60.0
    assert hm(segs[1]["entry_ts"]) == "09:24" and segs[1]["entry_spot"] == 140.0
    assert [hm(t) for t in trace] == ["09:24"]


def test_no_brick_no_overlay_exit():
    segs = _run(renko=True, closes={"09:22": 95.0})    # 10 pts is far from a 30-pt brick
    assert "ENTRY_SPOT_SL" not in [s["reason"] for s in segs]


def test_renko_off_leaves_the_overlay_unchanged():
    key = lambda segs: [(s["reason"], str(s["entry_ts"]), str(s["exit_ts"]), s["pnl"]) for s in segs]
    assert key(_run(renko=False)) == key(fx_run_plain())


def fx_run_plain():
    adf, ohlc = fx._inputs(stop_close=101.0)
    _, segs = champion_sim.simulate(
        adf, {}, {}, ohlc, fx.DATE, False, 1.0, "PC50", "Thu", "STD", 0, 1000,
        enable_entry_spot_recovery=True, entry_spot_close_confirmed=True,
        entry_spot_exit_buffer=10.0, entry_spot_check_entry_bar=True,
        entry_spot_renko_brick=0.0)
    return segs


# -- paper book ------------------------------------------------------------------------
def test_book_is_v214b_with_the_renko_overlay(monkeypatch):
    seen = {}

    def fake(trade_date, override=None, **kwargs):
        seen.update(kwargs)
        return {"tier": "PC50", "direction": "DOWN", "segments": [], "session_done": True,
                "context": {}}

    monkeypatch.setattr(v214c, "replay_v212", fake)
    trace: list = []
    out = v214c.replay_v214c("2026-09-15", None, trace)
    assert seen["close_confirmed"] is True and seen["suppress_pc50_call_entries"] is True
    assert seen["check_entry_bar"] is True
    assert (seen["renko_brick"], seen["renko_reversal"]) == (30.0, 2)
    assert seen["recovery_trace"] is trace
    assert out["context"]["overlay"] == "renko"


def test_traced_re_entries_are_priced_at_the_next_snapshot():
    T = lambda hm: pd.Timestamp(f"2026-09-15 {hm}")
    segs = [dict(pos="put", entry_ts=T("09:55"), exit_ts=T("10:02"), reason="ENTRY_SPOT_SL",
                 entry_spot=23390.0, exit_spot=23430.0, pnl=-40.0),
            dict(pos="put", entry_ts=T("10:11"), exit_ts=T("11:00"), reason="TGT_ALPHA",
                 entry_spot=23380.0, exit_spot=23300.0, pnl=80.0)]
    # a new re-entry price: the anchor rule would not see it as a re-entry
    assert b10.causal_fill_times(segs, {})[1]["entry_ts"] == T("10:11")
    out = b10.causal_fill_times(segs, {}, recovered=[False, True])
    assert out[1]["entry_ts"] == T("10:12") and out[0]["exit_ts"] == T("10:03")


def test_registered_as_a_paper_tab_and_runner_but_not_live():
    from labs.services.book_overview import BOOKS
    from labs.ui.live_routes import STRATEGY_PRESETS
    from labs.ui.routes import LIVE_TABS
    assert LIVE_TABS["alpha_v214c"] == "Alpha v2.14 C"
    assert BOOKS["alpha_v214c"]["label"] == "Alpha v2.14 C"
    assert not any("v2.14c" in v[1] or "v214c" in k for k, v in STRATEGY_PRESETS.items())
    template = (ROOT / "templates" / "live_strategy.html").read_text(encoding="utf-8")
    assert "'alpha_v214c'" in template
    for runner in ("pa_paper_tracker.py", "pa_paper_tracker_loop.py"):
        source = (ROOT / runner).read_text(encoding="utf-8")
        assert "alpha_v214c_tracker" in source and '"alpha_v214c"' in source
