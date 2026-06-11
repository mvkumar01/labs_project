from pathlib import Path
from tempfile import TemporaryDirectory

import live.live_runner as lr


def _state(**overrides):
    state = {
        "position": "OPEN",
        "side": "CALL",
        "entry_spot": 23250.85,
        "peak_pnl": 0.0,
    }
    state.update(overrides)
    return state


def _bar(**overrides):
    bar = {
        "tier": "PC400",
        "gap_direction": "DOWN",
        "vix_at_open": 18.0,
    }
    bar.update(overrides)
    return bar


def test_pc400_call_gap_down_high_vix_trail_arms_then_fires():
    armed = lr.evaluate_pc400_spot_trail(_state(), _bar(), 23298.55)

    assert armed["exit"] is False
    assert armed["use_trail"] is True
    assert round(armed["peak_pnl"], 2) == 47.70
    assert round(armed["stop"], 2) == 23278.55

    fired = lr.evaluate_pc400_spot_trail(
        _state(peak_pnl=51.65),
        _bar(),
        23280.05,
    )

    assert fired["exit"] is True
    assert fired["reason"] == "v22_trail"
    assert round(fired["stop"], 2) == 23282.50


def test_pc400_call_gap_up_high_vix_does_not_use_trail():
    decision = lr.evaluate_pc400_spot_trail(
        _state(peak_pnl=51.65),
        _bar(gap_direction="UP", vix_at_open=18.0),
        23280.05,
    )

    assert decision["use_trail"] is False
    assert decision["exit"] is False
    assert round(decision["peak_pnl"], 2) == 51.65


def test_pc400_put_gap_up_high_vix_uses_symmetric_trail():
    state = _state(side="PUT", entry_spot=23250.85, peak_pnl=51.65)
    bar = _bar(gap_direction="UP", vix_at_open=18.0)

    fired = lr.evaluate_pc400_spot_trail(state, bar, 23221.65)

    assert fired["exit"] is True
    assert fired["reason"] == "v22_trail"
    assert round(fired["stop"], 2) == 23219.20


def test_non_pc400_position_does_not_use_trail():
    decision = lr.evaluate_pc400_spot_trail(
        _state(peak_pnl=51.65),
        _bar(tier="PC50"),
        23280.05,
    )

    assert decision["use_trail"] is False
    assert decision["exit"] is False


def test_trail_snapshot_recovers_peak_after_restart():
    rows = [
        ("2026-06-11 11:45:00", 23250.85),
        ("2026-06-11 12:16:00", 23298.55),
        ("2026-06-11 12:17:00", 23302.50),
        ("2026-06-11 12:21:00", 23280.05),
    ]
    with TemporaryDirectory() as tmp:
        root = Path(tmp)
        day = root / "2026-06-11"
        day.mkdir()
        path = day / "NIFTY_options_1min.csv"
        with path.open("w", encoding="utf-8", newline="") as handle:
            handle.write("timestamp,tradingsymbol,strike,option_type,ltp,spot\n")
            for ts, spot in rows:
                handle.write(f"{ts},NIFTY2661623050CE,23050,CE,1,{spot}\n")

        old_root = lr.SHARED_LIVE_DIR
        old_cache = dict(lr._SPOT_TRAIL_CACHE)
        try:
            lr.SHARED_LIVE_DIR = root
            lr._SPOT_TRAIL_CACHE.clear()
            state = _state(
                entry_time="2026-06-11T06:15:29+00:00",
                peak_pnl=0.0,
            )
            snapshot = lr.get_spot_trail_snapshot(state)
            decision = lr.evaluate_pc400_spot_trail(state, _bar(), snapshot)
        finally:
            lr.SHARED_LIVE_DIR = old_root
            lr._SPOT_TRAIL_CACHE.clear()
            lr._SPOT_TRAIL_CACHE.update(old_cache)

    assert round(snapshot["peak_pnl"], 2) == 51.65
    assert snapshot["spot"] == 23280.05
    assert decision["exit"] is True
    assert round(decision["stop"], 2) == 23282.50
