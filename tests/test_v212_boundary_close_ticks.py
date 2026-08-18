"""v2.12_closed_confirmed decides on a tick-built 60-second close.

`close1m` never meant "wait for the collector's OHLC row". It means: aggregate
the two-second spot samples inside a clock minute and, at the rollover, treat
that minute's last fresh sample as its close. These tests pin that timing
(decision on the first poll after `:00`), the boundary-only semantics (no
intraminute exit), and the guarantee that the collector's later row for the same
minute cannot produce a second action.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from live.engine import champion_inputs
from live.engine.minute_ticks import MinuteTickAggregator
from live.live_runner import champion_live_policy

IST = timezone(timedelta(hours=5, minutes=30))
DATE = "2026-08-17"
COLLECTOR_DATE = "2026-08-14"   # isolated from DATE so the OHLC cache cannot alias


def _ts(hhmmss: str) -> datetime:
    return datetime.strptime(f"{DATE} {hhmmss}", "%Y-%m-%d %H:%M:%S").replace(
        tzinfo=IST)


def _feed(agg: MinuteTickAggregator, samples):
    """Feed (HH:MM:SS, spot) pairs, returning every FrozenMinute emitted."""
    return [f for f in (agg.add(_ts(t), v) for t, v in samples) if f is not None]


def _minute_stream(minute: str, value, *, start=0, stop=60, step=2):
    """The real cadence: a sample every two seconds across one clock minute."""
    out = []
    for sec in range(start, stop, step):
        spot = value(sec) if callable(value) else value
        out.append((f"{minute}:{sec:02d}", spot))
    return out


# -- the clock itself ----------------------------------------------------
def test_minute_emits_exactly_once_at_rollover() -> None:
    agg = MinuteTickAggregator()
    samples = _minute_stream("09:25", 100.0) + _minute_stream("09:26", 101.0)

    frozen = _feed(agg, samples)

    # 09:25 freezes when 09:26 opens; 09:26 is still in flight.
    assert [f.minute for f in frozen] == ["09:25"]


def test_no_emission_at_any_point_inside_the_minute() -> None:
    agg = MinuteTickAggregator()

    # 30 samples across 09:25 -- not one of them may produce a decision.
    emitted = _feed(agg, _minute_stream("09:25", 100.0))

    assert emitted == []


def test_close_is_the_last_fresh_sample_of_the_ended_minute() -> None:
    agg = MinuteTickAggregator()
    # Ramp so the last sample (09:25:58) is unambiguous, and the extreme
    # values land mid-minute rather than at the close.
    samples = _minute_stream("09:25", lambda s: 100.0 + (s % 10)) + [
        ("09:26:00", 200.0)]

    frozen = _feed(agg, samples)[0]

    assert frozen.close == 100.0 + (58 % 10)     # 09:25:58, the last print
    # Samples land on even seconds, so the ramp peaks at +8, never +9.
    assert frozen.high == 108.0
    assert frozen.low == 100.0
    assert frozen.open == 100.0                  # 09:25:00
    assert frozen.key == f"{DATE}T09:25"


def test_decision_lands_on_first_poll_after_the_boundary() -> None:
    """The whole point: the 09:25 close is available at ~09:26:00, not :49."""
    agg = MinuteTickAggregator()
    samples = _minute_stream("09:25", 100.0) + [("09:26:00", 101.0)]

    frozen = _feed(agg, samples)[0]

    assert frozen.minute == "09:25"
    # Emitted by the sample at 09:26:00 -- the first 2s poll after rollover.
    assert agg.boundary_key(DATE) == f"{DATE}T09:25"
    # And the last tick backing it came from inside 09:25.
    assert frozen.last_ts == _ts("09:25:58")


def test_intraminute_anchor_breach_produces_no_boundary_action() -> None:
    """Spot dives through the anchor mid-minute but recovers by the close.

    Close-confirmed must surface a HOLD-shaped close (above the anchor), which
    is precisely what a tick stop would have got wrong.
    """
    anchor = 100.0
    agg = MinuteTickAggregator()

    def value(sec):
        return 95.0 if sec in (20, 30) else 101.0

    frozen = _feed(agg, _minute_stream("09:25", value) + [("09:26:00", 101.0)])

    assert len(frozen) == 1
    assert frozen[0].low == 95.0          # the breach really happened
    assert frozen[0].close == 101.0       # but the CLOSE is favourable
    assert frozen[0].close > anchor       # CALL rule -> HOLD


# -- CALL / PUT boundary rules -------------------------------------------
@pytest.mark.parametrize(
    "side, anchor, close, expect_exit",
    [
        ("CALL", 100.0, 99.0, True),    # close <= anchor -> EXIT
        ("CALL", 100.0, 100.0, True),   # equality is an adverse close
        ("CALL", 100.0, 101.0, False),  # favourable -> HOLD
        ("PUT", 100.0, 101.0, True),    # close >= anchor -> EXIT
        ("PUT", 100.0, 100.0, True),    # equality is an adverse close
        ("PUT", 100.0, 99.0, False),    # favourable -> HOLD
    ],
)
def test_boundary_close_rule_per_side(side, anchor, close, expect_exit) -> None:
    agg = MinuteTickAggregator()
    frozen = _feed(
        agg,
        _minute_stream("09:25", lambda s: close if s >= 56 else anchor)
        + [("09:26:00", close)],
    )[0]

    hit = (frozen.close <= anchor if side == "CALL"
           else frozen.close >= anchor)

    assert hit is expect_exit


# -- staleness / safety --------------------------------------------------
def test_stale_minute_is_dropped_so_the_strategy_holds() -> None:
    """Sampling died at :10; :58 never arrived. Not a 60-second close."""
    agg = MinuteTickAggregator()
    samples = [("09:25:00", 100.0), ("09:25:10", 95.0), ("09:26:00", 101.0)]

    frozen = _feed(agg, samples)

    assert frozen == []                       # no decision at all
    assert agg.boundary_key(DATE) is None
    assert "09:25" in agg.rejected(DATE)


def test_missing_samples_never_fabricate_a_close() -> None:
    agg = MinuteTickAggregator()
    samples = [("09:25:00", None), ("09:25:30", None), ("09:26:00", 101.0)]

    frozen = _feed(agg, samples)

    assert frozen == []
    assert agg.minutes_for(DATE) == {}


def test_nonpositive_prints_are_rejected_as_feed_artefacts() -> None:
    agg = MinuteTickAggregator()
    samples = _minute_stream("09:25", 100.0) + [
        ("09:25:59", 0.0), ("09:26:00", 101.0)]

    frozen = _feed(agg, samples)[0]

    # The 0.0 print neither becomes the close nor drags the low to zero.
    assert frozen.close == 100.0
    assert frozen.low == 100.0


def test_one_action_per_minute_across_a_multi_minute_stream() -> None:
    agg = MinuteTickAggregator()
    samples = (_minute_stream("09:25", 100.0)
               + _minute_stream("09:26", 99.0)
               + _minute_stream("09:27", 98.0)
               + [("09:28:00", 97.0)])

    frozen = _feed(agg, samples)

    assert [f.minute for f in frozen] == ["09:25", "09:26", "09:27"]
    assert len({f.minute for f in frozen}) == len(frozen)   # never repeated


def test_old_sessions_are_pruned_in_the_always_on_process() -> None:
    """The runner is not restarted between trading days."""
    agg = MinuteTickAggregator()
    for day in ("2026-08-10", "2026-08-11", "2026-08-12", "2026-08-13"):
        for hhmmss, spot in [("09:25:58", 100.0), ("09:26:00", 101.0)]:
            ts = datetime.strptime(
                f"{day} {hhmmss}", "%Y-%m-%d %H:%M:%S").replace(tzinfo=IST)
            agg.add(ts, spot)

    assert agg.boundary_key("2026-08-13") == "2026-08-13T09:25"
    assert agg.minutes_for("2026-08-10") == {}      # dropped
    assert agg.boundary_key("2026-08-10") is None


# -- collector convergence -----------------------------------------------
def test_ohlc_by_minute_gap_fills_only_missing_minutes() -> None:
    extra = {"09:25": (99.0, 99.5, 98.5, 99.0)}
    real = champion_inputs.ohlc_by_minute(DATE)
    merged = champion_inputs.ohlc_by_minute(DATE, extra_minutes=extra)

    for hm, candle in real.items():
        assert merged[hm] == candle, "a collected minute was overwritten"
    if "09:25" not in real:
        assert merged["09:25"] == extra["09:25"]


def test_collector_row_wins_over_the_tick_stand_in(monkeypatch) -> None:
    """Once the real candle lands it replaces the tick-built minute.

    That is what stops the stand-in becoming a second source of record: the
    replay converges on collector truth, and the champion cursor suppresses a
    duplicate exit/re-entry for the minute already acted on.
    """
    import pandas as pd

    # A collector that has published 09:25 but not yet 09:26.
    published = pd.DataFrame({
        "timestamp": [f"{COLLECTOR_DATE} 09:25:00"],
        "open": [10.0], "high": [11.0], "low": [9.0], "close": [10.5],
    })
    monkeypatch.setattr(champion_inputs, "_labs_sig", lambda d: ("test-sig",))
    monkeypatch.setattr(champion_inputs, "_labs_spot_ohlc", lambda d: published)
    monkeypatch.setattr(champion_inputs, "_OHLC_BY_MINUTE_CACHE", {})

    # The runner still offers BOTH minutes, including its stand-in for 09:25.
    merged = champion_inputs.ohlc_by_minute(
        COLLECTOR_DATE,
        extra_minutes={"09:25": (99.0, 99.0, 99.0, 99.0),
                       "09:26": (1.0, 2.0, 0.5, 1.5)})

    assert merged["09:25"] == (10.0, 11.0, 9.0, 10.5)  # collector wins
    assert merged["09:26"] == (1.0, 2.0, 0.5, 1.5)     # gap still filled


def test_extra_minutes_do_not_leak_into_the_shared_cache() -> None:
    extra = {"23:59": (1.0, 1.0, 1.0, 1.0)}
    champion_inputs.ohlc_by_minute(DATE, extra_minutes=extra)

    assert "23:59" not in champion_inputs.ohlc_by_minute(DATE)


# -- other strategies untouched ------------------------------------------
def test_close_confirmed_takes_boundary_clock_but_no_tick_authority() -> None:
    policy = champion_live_policy("v2.12_closed_confirmed")

    assert policy.boundary_tick_close is True    # decides at the rollover
    assert policy.fast_stop_overlay is False     # never intraminute
    assert policy.next_open_fallback is False


def test_v212_and_v213_policies_are_unchanged() -> None:
    v212 = champion_live_policy("v2.12")
    assert (v212.fast_stop_overlay, v212.next_open_fallback) == (False, False)
    assert v212.boundary_tick_close is False     # still collector-clocked

    v213 = champion_live_policy("v2.13")
    assert (v213.fast_stop_overlay, v213.next_open_fallback) == (True, True)
    assert v213.boundary_tick_close is False     # keeps its buffered tick stop
