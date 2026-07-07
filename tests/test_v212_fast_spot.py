"""Unit tests for the v2.12 per-poll (tick) fast spot-stop decision.

evaluate_v212_fast_spot is pure (state + live spot -> decision), mirroring the
PC400 trail test. It owns the entry-spot SL exit and the recovery re-entry for
the live champion v2.12 path; the 5-min replay runs without recovery.
"""
import live.live_runner as lr

f = lr.evaluate_v212_fast_spot


def _state(**overrides):
    s = {
        "position": "NONE", "side": None, "entry_spot": None,
        "recovery_armed": 0, "recovery_level": None, "recovery_side": None,
    }
    s.update(overrides)
    return s


def test_open_call_touch_entry_spot_exits_and_arms_recovery():
    d = f(_state(position="OPEN", side="CALL", entry_spot=24111.65), 24111.65)
    assert d["action"] == "EXIT"
    assert d["reason"] == "ENTRY_SPOT_SL"
    assert d["recovery_side"] == "CALL"
    assert d["recovery_level"] == 24111.65


def test_open_call_above_entry_spot_holds():
    assert f(_state(position="OPEN", side="CALL", entry_spot=24111.65),
             24115.0)["action"] == "HOLD"


def test_open_put_rise_to_entry_spot_exits():
    assert f(_state(position="OPEN", side="PUT", entry_spot=24008.65),
             24009.0)["action"] == "EXIT"


def test_open_put_below_entry_spot_holds():
    assert f(_state(position="OPEN", side="PUT", entry_spot=24008.65),
             24005.0)["action"] == "HOLD"


def test_recovery_call_confirmed_recross_reenters():
    d = f(_state(recovery_armed=1, recovery_level=24111.65, recovery_side="CALL"),
          24112.0, confirm_spot=24113.0)
    assert d["action"] == "ENTER"
    assert d["side"] == "CALL"
    assert d["recovery_level"] == 24111.65


def test_recovery_call_tick_cross_without_confirmation_holds():
    """GAP A: a 2s tick cross alone must NOT re-enter — research v2.12 requires
    the 1-min bar to close favourable; the minute snapshot is the live proxy."""
    d = f(_state(recovery_armed=1, recovery_level=24111.65, recovery_side="CALL"),
          24112.0, confirm_spot=24110.0)          # tick crossed, minute has not
    assert d["action"] == "HOLD"
    assert d["reason"] == "recovery_unconfirmed"
    # No confirm data at all -> also HOLD (never an unconfirmed re-entry).
    assert f(_state(recovery_armed=1, recovery_level=24111.65,
                    recovery_side="CALL"), 24112.0)["action"] == "HOLD"


def test_recovery_call_not_yet_recrossed_holds():
    assert f(_state(recovery_armed=1, recovery_level=24111.65, recovery_side="CALL"),
             24110.0, confirm_spot=24112.0)["action"] == "HOLD"


def test_recovery_put_confirmed_recross_reenters():
    assert f(_state(recovery_armed=1, recovery_level=24008.65, recovery_side="PUT"),
             24007.0, confirm_spot=24006.0)["action"] == "ENTER"


def test_recovery_put_unconfirmed_holds():
    assert f(_state(recovery_armed=1, recovery_level=24008.65, recovery_side="PUT"),
             24007.0, confirm_spot=24009.0)["action"] == "HOLD"


def test_missing_spot_never_stops():
    assert f(_state(position="OPEN", side="CALL", entry_spot=24111.65),
             None)["action"] == "HOLD"


def test_flat_unarmed_holds():
    assert f(_state(), 24000.0)["action"] == "HOLD"
