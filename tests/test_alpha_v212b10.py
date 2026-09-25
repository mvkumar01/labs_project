"""Alpha v2.12 B10: 10-point stop buffer on the close, with honest fills.

Pins the three things that must stay true for paper and live to agree:
  * the rule -- a stop needs the completed close 10 points past the anchor,
    recovery is still at the anchor, and a zero buffer changes nothing;
  * one barrier -- paper and live read the same V212_B10_EXIT_BUFFER constant;
  * causal fills -- every candle-decided event is priced after its decision.
"""

from __future__ import annotations

import inspect

import pandas as pd
import pytest

from labs.engine import alpha_v212_tracker, alpha_v212b10_tracker as b10
from live.engine import champion_sim
from live.engine.champion_sim import V212_B10_EXIT_BUFFER
from live.live_runner import champion_live_policy

import test_v212_close_confirmed as fx


def _simulate(stop_close: float, *, buffer: float, confirmed: bool = True):
    adf, ohlc = fx._inputs(stop_close=stop_close)
    # The shared fixture pins the 09:25 low at 99; keep the candle physical
    # (low <= close <= high) when the close is pushed below it.
    o, h, l, c = ohlc.by_minute["09:25"]
    ohlc.by_minute["09:25"] = (o, max(h, c), min(l, c), c)
    _, segments = champion_sim.simulate(
        adf, {}, {}, ohlc, fx.DATE, False, 1.0, "PC50", "Thu", "STD", 0, 1000,
        enable_entry_spot_recovery=True,
        entry_spot_close_confirmed=confirmed,
        entry_spot_exit_buffer=buffer,
    )
    return segments


def _stopped(segments) -> bool:
    return any(s["reason"] == "ENTRY_SPOT_SL" for s in segments)


# -- the rule ---------------------------------------------------------------
def test_b10_constant_is_ten_points() -> None:
    assert V212_B10_EXIT_BUFFER == 10.0


@pytest.mark.parametrize("close, stops", [
    (99.0, False),    # 1 pt through the anchor: inside the buffer -> HOLD
    (91.0, False),    # 9 pts through: still inside -> HOLD
    (90.0, True),     # exactly anchor - 10 -> STOP
    (85.0, True),     # well past -> STOP
])
def test_call_stop_needs_close_ten_points_past_anchor(close, stops) -> None:
    assert _stopped(_simulate(close, buffer=V212_B10_EXIT_BUFFER)) is stops


@pytest.mark.parametrize("low, close", [
    (88.0, 95.0),     # dipped 12 pts intrabar, closed inside the buffer zone
    (88.0, 101.0),    # dipped 12 pts intrabar, closed back above the anchor
])
def test_intrabar_breach_that_closes_inside_buffer_holds(low, close) -> None:
    """The defining B10 case: only the CLOSE decides, never the intrabar low."""
    adf, ohlc = fx._inputs(stop_close=close)
    o, h, _, _ = ohlc.by_minute["09:25"]
    ohlc.by_minute["09:25"] = (o, max(h, close), low, close)
    _, segments = champion_sim.simulate(
        adf, {}, {}, ohlc, fx.DATE, False, 1.0, "PC50", "Thu", "STD", 0, 1000,
        enable_entry_spot_recovery=True, entry_spot_close_confirmed=True,
        entry_spot_exit_buffer=V212_B10_EXIT_BUFFER)
    assert not _stopped(segments)


def test_zero_buffer_is_byte_for_byte_close_confirmed() -> None:
    for close in (99.0, 100.0, 101.0):
        a = _simulate(close, buffer=0.0)
        adf, ohlc = fx._inputs(stop_close=close)
        o, h, l, c = ohlc.by_minute["09:25"]
        ohlc.by_minute["09:25"] = (o, max(h, c), min(l, c), c)
        _, b = champion_sim.simulate(
            adf, {}, {}, ohlc, fx.DATE, False, 1.0, "PC50", "Thu", "STD", 0, 1000,
            enable_entry_spot_recovery=True, entry_spot_close_confirmed=True)
        assert [(s["reason"], s["pnl"]) for s in a] == \
               [(s["reason"], s["pnl"]) for s in b]


def test_buffer_defaults_leave_every_other_book_unchanged() -> None:
    sig = inspect.signature(champion_sim.simulate).parameters
    assert sig["entry_spot_exit_buffer"].default == 0.0
    replay = inspect.signature(alpha_v212_tracker.replay_v212).parameters
    assert replay["close_confirmed"].default is False
    assert replay["exit_buffer"].default == 0.0


# -- one barrier, shared by paper and live ------------------------------------
def test_paper_book_uses_the_shared_constant(monkeypatch) -> None:
    seen = {}

    def fake_replay(trade_date, override=None, **kwargs):
        seen.update(kwargs)
        return {"tier": "PC50", "direction": "UP", "segments": [],
                "session_done": True, "context": {}}

    monkeypatch.setattr(b10, "replay_v212", fake_replay)
    b10.replay_v212b10("2026-09-15")

    assert seen == {"close_confirmed": True,
                    "exit_buffer": V212_B10_EXIT_BUFFER,
                    "check_entry_bar": True}


def test_live_policy_for_b10() -> None:
    p = champion_live_policy("v2.12_b10")
    assert p.entry_spot_exit_buffer == V212_B10_EXIT_BUFFER
    assert p.boundary_tick_close is True       # decides at the :00 boundary
    assert p.fast_stop_overlay is False        # never an intraminute tick stop
    assert p.next_open_fallback is False
    assert p.entry_spot_check_entry_bar is True    # v2.14 A: entry-bar fix


@pytest.mark.parametrize("version", [
    "v2.11", "v2.11b", "v2.12", "v2.12_closed_confirmed", "v2.13"])
def test_no_other_live_version_gets_a_buffer(version) -> None:
    assert champion_live_policy(version).entry_spot_exit_buffer == 0.0


# -- causal fills ---------------------------------------------------------------
T = lambda hm: pd.Timestamp(f"2026-09-15 {hm}")


def _seg(reason, entry, exit_, exit_spot, pos="put", anchor=23393.2):
    return dict(pos=pos, entry_ts=T(entry), exit_ts=T(exit_), reason=reason,
                entry_spot=anchor, exit_spot=exit_spot, pnl=0.0)


def test_stop_and_recovery_move_to_the_next_minute() -> None:
    segs = [_seg("ENTRY_SPOT_SL", "09:55", "10:02", 23404.0),
            _seg("TGT_ALPHA", "10:02", "11:00", 23300.0)]
    out = b10.causal_fill_times(segs, {})

    assert out[0]["entry_ts"] == T("09:55")          # signal entry: unchanged
    assert out[0]["exit_ts"] == T("10:03")           # stop decided on 10:02 close
    assert out[1]["entry_ts"] == T("10:03")          # recovery: next minute
    assert out[1]["exit_ts"] == T("11:00")           # alpha exit: unchanged


def test_trail_moves_to_its_real_breach_minute() -> None:
    bars = {"10:35": (23360, 23362, 23352, 23355),
            "10:36": (23355, 23358, 23350, 23357),
            "10:37": (23357, 23364, 23356, 23363),
            "10:38": (23363, 23372, 23362, 23371),   # first touch of 23371.45
            "10:39": (23371, 23374, 23369, 23370)}
    out = b10.causal_fill_times(
        [_seg("TRAIL", "09:55", "10:35", 23371.45)], bars)

    assert out[0]["exit_ts"] == T("10:39")           # breach 10:38 -> fill 10:39


def test_eod_and_alpha_exits_are_never_shifted() -> None:
    segs = [_seg("SL_ALPHA", "09:55", "11:00", 23420.0),
            _seg("EOD", "11:05", "15:25", 23172.0)]
    out = b10.causal_fill_times(segs, {})
    assert [s["exit_ts"] for s in out] == [T("11:00"), T("15:25")]
