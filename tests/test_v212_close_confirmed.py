from __future__ import annotations

import pandas as pd

from live.engine import champion_sim
from live.live_runner import champion_live_policy


DATE = "2026-06-18"


def _inputs(*, stop_close: float):
    times = pd.date_range(f"{DATE} 09:15", periods=5, freq="5min")
    adf = pd.DataFrame({
        "timestamp": times,
        "alpha": [0.0, 30.0, 40.0, 50.0, 100.0],
        "d_pe_sum": [1.0] * 5,
        "d_ce_sum": [1.0] * 5,
        "denom": [100.0] * 5,
        "spot": [100.0, 100.0, stop_close, 101.0, 110.0],
    })
    by_minute = {}
    for ts in pd.date_range(f"{DATE} 09:15", f"{DATE} 09:39", freq="min"):
        by_minute[ts.strftime("%H:%M")] = (105.0, 106.0, 104.0, 105.0)
    by_minute["09:20"] = (100.0, 101.0, 100.0, 100.0)
    by_minute["09:25"] = (100.0, 102.0, 99.0, stop_close)
    by_minute["09:30"] = (99.0, 102.0, 99.0, 101.0)
    for minute in range(35, 40):
        by_minute[f"09:{minute}"] = (105.0, 111.0, 105.0, 110.0)
    return adf, champion_sim.OHLC(by_minute)


def _simulate(stop_close: float):
    adf, ohlc = _inputs(stop_close=stop_close)
    return champion_sim.simulate(
        adf, {}, {}, ohlc, DATE, False, 1.0, "PC50", "Thu", "STD",
        0, 1000,
        enable_entry_spot_recovery=True,
        entry_spot_close_confirmed=True,
    )


def test_favourable_close_after_anchor_touch_remains_hold() -> None:
    pnl, segments = _simulate(101.0)

    assert pnl == 10.0
    assert [segment["reason"] for segment in segments] == ["TGT_ALPHA"]
    assert pd.Timestamp(segments[0]["entry_ts"]).strftime("%H:%M") == "09:20"


def test_adverse_close_still_exits_and_later_recovers() -> None:
    pnl, segments = _simulate(99.0)

    assert pnl == 9.0
    assert [segment["reason"] for segment in segments] == [
        "ENTRY_SPOT_SL", "TGT_ALPHA"
    ]
    assert pd.Timestamp(segments[0]["exit_ts"]).strftime("%H:%M") == "09:25"
    assert pd.Timestamp(segments[1]["entry_ts"]).strftime("%H:%M") == "09:30"


def test_anchor_equality_is_a_confirmed_adverse_close() -> None:
    _, segments = _simulate(100.0)

    assert segments[0]["reason"] == "ENTRY_SPOT_SL"
    assert segments[0]["exit_spot"] == 100.0


def test_close_confirmed_has_no_tick_or_next_open_authority() -> None:
    policy = champion_live_policy("v2.12_closed_confirmed")

    assert policy.fast_stop_overlay is False
    assert policy.next_open_fallback is False
