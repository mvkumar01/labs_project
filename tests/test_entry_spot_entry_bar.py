"""The entry-spot overlay's entry blind spot and fill anchor (2026-09-25).

A position entered on Alpha bar T used to be first checked on T+5..T+9: the
overlay scans T..T+4 BEFORE bar T's entry exists. `entry_spot_check_entry_bar`
revisits the entry bar for the overlay alone. `entry_spot_anchor_at_fill`
anchors the position at the entry snapshot's spot instead of candle T's close.
Both default off, leaving every existing book byte-for-byte unchanged.
"""

from __future__ import annotations

import pandas as pd

from live.engine import champion_sim
from live.engine.champion_sim import V212_B10_EXIT_BUFFER

import test_v212_close_confirmed as fx


def _inputs(*, breach_close: float | None = None, entry_close: float = 100.0):
    # fx: a PC50 CALL enters on the 09:20 bar (alpha 0 -> 30); anchor = 100.
    adf, ohlc = fx._inputs(stop_close=101.0)
    o, h, l, _ = ohlc.by_minute["09:20"]
    ohlc.by_minute["09:20"] = (o, max(h, entry_close), min(l, entry_close), entry_close)
    if breach_close is not None:          # inside the entry bar's own window
        ohlc.by_minute["09:21"] = (100.0, 100.0, breach_close, breach_close)
    return adf, ohlc


def _run(adf, ohlc, **flags):
    _, segments = champion_sim.simulate(
        adf, {}, {}, ohlc, fx.DATE, False, 1.0, "PC50", "Thu", "STD", 0, 1000,
        enable_entry_spot_recovery=True, entry_spot_close_confirmed=True,
        entry_spot_exit_buffer=V212_B10_EXIT_BUFFER, **flags)
    return segments


def _key(segments):
    return [(s["reason"], str(s["entry_ts"]), str(s["exit_ts"]), s["pnl"])
            for s in segments]


def test_breach_inside_the_entry_bar_was_invisible() -> None:
    segments = _run(*_inputs(breach_close=85.0))
    assert "ENTRY_SPOT_SL" not in [s["reason"] for s in segments]


def test_entry_bar_check_stops_on_the_breach_minute() -> None:
    segments = _run(*_inputs(breach_close=85.0), entry_spot_check_entry_bar=True)
    stop = segments[0]
    assert stop["reason"] == "ENTRY_SPOT_SL"
    assert pd.Timestamp(stop["exit_ts"]).strftime("%H:%M") == "09:21"
    assert stop["exit_spot"] == 85.0


def test_entry_bar_check_respects_the_buffer() -> None:
    # 9 points through a 10-point buffer: still HOLD, exactly as on later bars.
    segments = _run(*_inputs(breach_close=91.0), entry_spot_check_entry_bar=True)
    assert "ENTRY_SPOT_SL" not in [s["reason"] for s in segments]


def test_entry_bar_check_changes_nothing_without_a_breach() -> None:
    adf, ohlc = _inputs()
    assert _key(_run(adf, ohlc, entry_spot_check_entry_bar=True)) == _key(_run(adf, ohlc))


def test_default_anchor_is_the_entry_candle_close() -> None:
    segments = _run(*_inputs(entry_close=103.0))
    assert segments[0]["entry_spot"] == 103.0


def test_anchor_at_fill_uses_the_snapshot_spot() -> None:
    segments = _run(*_inputs(entry_close=103.0), entry_spot_anchor_at_fill=True)
    assert segments[0]["entry_spot"] == 100.0      # adf spot at the 09:20 mark


def test_flags_default_off() -> None:
    import inspect
    params = inspect.signature(champion_sim.simulate).parameters
    assert params["entry_spot_check_entry_bar"].default is False
    assert params["entry_spot_anchor_at_fill"].default is False
