from __future__ import annotations

import sqlite3

import pandas as pd
import pytest

from labs.engine import alpha_v213_tracker as tracker
from live.engine import champion_sim, champion_v213


DATE = "2026-07-10"
IST = "Asia/Kolkata"


def _ts(hhmm: str) -> pd.Timestamp:
    return pd.Timestamp(f"{DATE} {hhmm}", tz=IST)


def _ohlc(overrides: dict[str, tuple[float, float, float, float]] = None):
    rows = {
        ts.strftime("%H:%M"): (101.0, 102.0, 101.0, 101.0)
        for ts in pd.date_range(f"{DATE} 09:15", f"{DATE} 10:15", freq="min")
    }
    rows.update(overrides or {})
    return champion_sim.OHLC(rows)


def _base_trade(**updates) -> dict:
    trade = {
        "pos": "call",
        "entry_ts": _ts("09:20"),
        "exit_ts": _ts("10:05"),
        "entry_spot": 100.0,
        "signal_entry_spot": 100.0,
        "exit_spot": 110.0,
        "entry_alpha": 40.0,
        "exit_alpha": 20.0,
        "entry_rule": "RULE1",
        "tier": "PC400",
        "reason": "TRAIL",
    }
    trade.update(updates)
    return trade


def test_overlay_reenters_only_until_authoritative_v211_exit() -> None:
    ohlc = _ohlc(
        {
            "09:25": (100.0, 100.0, 99.0, 99.0),
            "09:26": (99.0, 100.0, 99.0, 99.0),
            "09:30": (99.0, 102.0, 99.0, 101.0),
            "09:31": (102.0, 103.0, 101.0, 102.0),
        }
    )

    segments, open_state = champion_v213.apply_overlay(
        [_base_trade()], None, ohlc, DATE
    )

    assert open_state is None
    assert [segment["reason"] for segment in segments] == [
        "ENTRY_SPOT_SL",
        "TRAIL",
    ]
    assert [pd.Timestamp(segment["exit_ts"]).strftime("%H:%M") for segment in segments] == [
        "09:26",
        "10:05",
    ]
    assert pd.Timestamp(segments[1]["entry_ts"]).strftime("%H:%M") == "09:31"
    assert segments[1]["entry_spot"] == 102.0
    assert segments[1]["signal_entry_spot"] == 100.0


def test_pending_recovery_is_cancelled_by_v211_exit() -> None:
    ohlc = _ohlc(
        {
            "09:25": (100.0, 100.0, 99.0, 99.0),
            "09:26": (99.0, 100.0, 99.0, 99.0),
            "10:10": (99.0, 102.0, 99.0, 101.0),
        }
    )

    segments, open_state = champion_v213.apply_overlay(
        [_base_trade()], None, ohlc, DATE
    )

    assert open_state is None
    assert len(segments) == 1
    assert segments[0]["reason"] == "ENTRY_SPOT_SL"
    assert pd.Timestamp(segments[0]["exit_ts"]).strftime("%H:%M") == "09:26"


def test_open_recovery_keeps_barrier_separate_from_execution_spot() -> None:
    ohlc = _ohlc(
        {
            "09:25": (100.0, 100.0, 99.0, 99.0),
            "09:26": (99.0, 100.0, 99.0, 99.0),
            "09:30": (99.0, 102.0, 99.0, 101.0),
            "09:31": (102.0, 103.0, 101.0, 102.0),
        }
    )
    base_open = {
        "side": "CALL",
        "entry_ts": _ts("09:20"),
        "entry_spot": 100.0,
        "signal_entry_spot": 100.0,
        "entry_alpha": 40.0,
        "entry_rule": "RULE1",
        "tier": "PC400",
        "gap_direction": "UP",
    }

    segments, open_state = champion_v213.apply_overlay(
        [], base_open, ohlc, DATE, max_bar_ts=_ts("09:35")
    )

    assert len(segments) == 1
    assert open_state["entry_spot"] == 102.0
    assert open_state["signal_entry_spot"] == 100.0
    assert pd.Timestamp(open_state["entry_ts"]).strftime("%H:%M") == "09:31"


def test_intraday_holding_mark_does_not_close_shadow_lifecycle() -> None:
    adf = pd.DataFrame({"timestamp": [pd.Timestamp(f"{DATE} 09:30")]})
    ohlc = _ohlc({"09:34": (102.0, 104.0, 101.0, 103.0)})
    open_state = {
        "side": "CALL",
        "entry_ts": _ts("09:31"),
        "entry_spot": 102.0,
        "signal_entry_spot": 100.0,
        "entry_alpha": 40.0,
        "entry_rule": "RULE1",
        "tier": "PC400",
    }

    segment = tracker._holding_segment(open_state, adf, ohlc, DATE)

    assert segment["reason"] == "EOD"
    assert pd.Timestamp(segment["exit_ts"]).strftime("%H:%M") == "09:35"
    assert segment["exit_spot"] == 103.0
    assert segment["pnl"] == 1.0
    assert segment["signal_entry_spot"] == 100.0


def _segment() -> dict:
    return {
        "pos": "call",
        "entry_ts": _ts("09:20"),
        "exit_ts": _ts("10:05"),
        "entry_spot": 24158.8,
        "signal_entry_spot": 24158.8,
        "exit_spot": 24206.0,
        "pnl": 47.2,
        "entry_rule": "RULE1",
        "reason": "TRAIL",
    }


def test_v213_tracker_persists_separate_ledger(monkeypatch) -> None:
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    replay = {
        "tier": "PC400",
        "direction": "UP",
        "segments": [_segment()],
        "session_done": True,
    }
    entry = tracker._timestamp_key(_ts("09:20"))
    exit_ = tracker._timestamp_key(_ts("10:05"))
    quotes = {
        (entry, 23950, "ce"): {
            "bid": 264.2,
            "ask": 264.85,
            "tradingsymbol": "NIFTY2671423950CE",
        },
        (exit_, 23950, "ce"): {
            "bid": 294.65,
            "ask": 295.0,
            "tradingsymbol": "NIFTY2671423950CE",
        },
    }
    monkeypatch.setattr(tracker, "replay_v213", lambda *_a, **_k: replay)
    monkeypatch.setattr(
        tracker, "build_executable_book", lambda *_a, **_k: ("26714", quotes)
    )

    result = tracker.run_day(DATE, connection=conn)

    assert result["status"] == "traded"
    trade = conn.execute("SELECT * FROM alpha_v213_trades").fetchone()
    daily = conn.execute("SELECT * FROM alpha_v213_daily").fetchone()
    assert trade["exit_reason"] == "TRAIL"
    assert trade["net_rs"] == pytest.approx(1937.0 - trade["charges_rs"])
    assert daily["strategy_version"].startswith("alpha_v2.13_")
