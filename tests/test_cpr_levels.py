"""Alpha-CPR: level math, stop/target resolution, and flag isolation.

The isolation tests matter most — `champion_sim.simulate()` is shared with the
real-money runner, so the new flags must be inert unless explicitly passed.
"""
from __future__ import annotations

import inspect

import pytest

from labs.engine.cpr_levels import (
    CprInputError, compute_levels, resolve_stop_target, prev_session_hlc)


# ── level math ───────────────────────────────────────────────────────────────
def test_compute_levels_matches_textbook_pivots():
    h, l, c = 100.0, 80.0, 90.0
    lv = compute_levels(h, l, c)
    p = (h + l + c) / 3.0            # 90
    assert lv == sorted(lv)
    assert len(lv) == 9
    for expected in (p, (h + l) / 2.0, 2 * p - (h + l) / 2.0,
                     2 * p - h, p - (h - l), l - 2 * (h - p),
                     2 * p - l, p + (h - l), h + 2 * (p - l)):
        assert any(abs(x - expected) < 1e-9 for x in lv), expected


def test_compute_levels_rejects_bad_input():
    with pytest.raises(CprInputError):
        compute_levels(0, 80, 90)
    with pytest.raises(CprInputError):
        compute_levels(80, 100, 90)      # high below low


# ── stop / target resolution ─────────────────────────────────────────────────
def test_call_takes_nearest_below_as_stop_and_above_as_target():
    lv = [100.0, 110.0, 120.0, 130.0]
    stop, target = resolve_stop_target(lv, 115.0, "call")
    assert (stop, target) == (110.0, 120.0)


def test_put_mirrors_call():
    lv = [100.0, 110.0, 120.0, 130.0]
    stop, target = resolve_stop_target(lv, 115.0, "put")
    assert (stop, target) == (120.0, 110.0)


def test_min_dist_walks_out_to_the_next_level():
    lv = [90.0, 114.0, 116.0, 140.0]
    # Without a floor the 114/116 pair is picked despite being ~1 pt away.
    assert resolve_stop_target(lv, 115.0, "call") == (114.0, 116.0)
    # With a 20-pt floor both walk out past the unexecutable pair.
    assert resolve_stop_target(lv, 115.0, "call", min_dist=20.0) == (90.0, 140.0)


def test_min_dist_drops_the_side_when_nothing_is_far_enough():
    """A level inside the floor is skipped even if it is the only one — the
    exit then cannot fire, rather than silently using an unexecutable stop."""
    lv = [100.0, 130.0]                      # both only 15 pts from 115
    assert resolve_stop_target(lv, 115.0, "call", min_dist=20.0) == (None, None)


def test_missing_side_returns_none_rather_than_guessing():
    stop, target = resolve_stop_target([100.0, 110.0], 150.0, "call")
    assert stop == 110.0 and target is None


def test_no_levels_is_not_an_error():
    assert resolve_stop_target([], 150.0, "call") == (None, None)


# ── prev-session resolution must fail loudly ─────────────────────────────────
def test_prev_session_missing_raises(tmp_path):
    with pytest.raises(CprInputError):
        prev_session_hlc("2026-06-01", spot_dir=tmp_path)


def test_prev_session_picks_latest_strictly_before(tmp_path):
    for d, hi in (("2026-05-28", 101), ("2026-05-29", 202), ("2026-06-01", 303)):
        (tmp_path / f"{d}_NIFTY_spot_1min.csv").write_text(
            "timestamp,open,high,low,close,volume\n"
            f"{d} 09:15:00,1,{hi},5,50,0\n")
    hi, lo, cl, prev = prev_session_hlc("2026-06-01", spot_dir=tmp_path)
    assert prev == "2026-05-29" and hi == 202.0 and lo == 5.0 and cl == 50.0


# ── isolation from the shared real-money engine ──────────────────────────────
def test_cpr_flags_default_off_in_champion_sim():
    from live.engine import champion_sim
    sig = inspect.signature(champion_sim.simulate)
    assert sig.parameters["no_alpha_exits"].default is False
    assert sig.parameters["enable_cpr_sl"].default is False
    assert sig.parameters["enable_cpr_tp"].default is False
    assert sig.parameters["cpr_levels"].default is None
    assert sig.parameters["cpr_min_dist"].default == 0.0


def test_live_runner_never_enables_cpr_flags():
    """The real-money path must not pass any Alpha-CPR flag."""
    import live.live_runner as lr
    src = inspect.getsource(lr)
    for flag in ("no_alpha_exits", "enable_cpr_sl", "enable_cpr_tp",
                 "cpr_levels", "cpr_min_dist"):
        assert flag not in src, f"live_runner must not reference {flag}"


def test_no_alpha_exits_gates_every_non_cpr_exit():
    """Source guard: each v2.11 exit branch is behind `not no_alpha_exits`."""
    from live.engine import champion_sim
    src = inspect.getsource(champion_sim.simulate)
    # tier alpha SL/TP
    assert "if not exited and not no_alpha_exits:" in src
    # trail, wall, drift, stall
    assert "and not no_alpha_exits" in src
    assert src.count("not no_alpha_exits") >= 5
