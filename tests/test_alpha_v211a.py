from __future__ import annotations

import sqlite3

import pandas as pd
import pytest

from labs.engine import alpha_v211a_tracker as tracker
from live.engine import champion_sim


DATE = "2026-07-20"
IST = "Asia/Kolkata"


def _strategy_inputs():
    times = pd.date_range(f"{DATE} 09:15", periods=4, freq="5min")
    adf = pd.DataFrame(
        {
            "timestamp": times,
            "alpha": [0.0, -40.0, -50.0, 0.0],
            "d_pe_sum": [1.0] * 4,
            "d_ce_sum": [1.0] * 4,
            "denom": [2.0] * 4,
            "spot": [100.0, 100.0, 72.0, 100.0],
        }
    )
    bars = {
        ts.strftime("%H:%M"): (100.0, 100.0, 100.0, 100.0)
        for ts in pd.date_range(f"{DATE} 09:15", f"{DATE} 09:34", freq="min")
    }
    # After the 09:20 PUT entry: reach +30 favourable points, then retrace 20.
    bars["09:25"] = (72.0, 75.0, 70.0, 72.0)
    bars["09:26"] = (72.0, 90.0, 70.0, 88.0)
    return adf, champion_sim.OHLC(bars)


def _simulate(*, enable_v211a: bool):
    adf, ohlc = _strategy_inputs()
    return champion_sim.simulate(
        adf,
        {},
        {},
        ohlc,
        DATE,
        True,
        -100.0,
        "PC400",
        "Mon",
        "TRAIL",
        0,
        1000,
        enable_v77_dn_put_filter=False,
        enable_rule3_dn_put=False,
        enable_v211a_low_vix_dn_put_trail=enable_v211a,
    )


def test_v211a_adds_30_20_trail_only_when_enabled() -> None:
    baseline_pnl, baseline = _simulate(enable_v211a=False)
    v211a_pnl, v211a = _simulate(enable_v211a=True)

    assert baseline_pnl == 0.0
    assert baseline[0]["reason"] == "v711_drift_stop"
    assert v211a_pnl == 10.0
    assert v211a[0]["reason"] == "TRAIL"
    assert v211a[0]["exit_spot"] == 90.0
    assert pd.Timestamp(v211a[0]["exit_ts"]).strftime("%H:%M") == "09:25"


def test_v211a_tracker_persists_separate_champion2_ledger(monkeypatch) -> None:
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    entry_ts = pd.Timestamp(f"{DATE} 09:20", tz=IST).isoformat()
    exit_ts = pd.Timestamp(f"{DATE} 09:25", tz=IST).isoformat()
    replay = {
        "tier": "PC400",
        "direction": "DOWN",
        "segments": [
            {
                "pos": "put",
                "entry_ts": entry_ts,
                "exit_ts": exit_ts,
                "entry_spot": 24197.6,
                "exit_spot": 24187.6,
                "pnl": 10.0,
                "entry_rule": "RULE1",
                "reason": "TRAIL",
            }
        ],
        "session_done": True,
        "context": {"vix_open": 16.84, "champion_label": "Champion 2"},
    }
    quotes = {
        (entry_ts, 24400, "pe"): {
            "bid": 199.0,
            "ask": 200.0,
            "tradingsymbol": "NIFTY_TEST_PE",
        },
        (exit_ts, 24400, "pe"): {
            "bid": 215.0,
            "ask": 216.0,
            "tradingsymbol": "NIFTY_TEST_PE",
        },
    }
    monkeypatch.setattr(tracker, "replay_v211a", lambda *_a, **_k: replay)
    monkeypatch.setattr(
        tracker, "build_executable_book", lambda *_a, **_k: ("26723", quotes)
    )

    result = tracker.run_day(DATE, connection=conn)

    assert result["status"] == "traded"
    trade = conn.execute("SELECT * FROM alpha_v211a_trades").fetchone()
    daily = conn.execute("SELECT * FROM alpha_v211a_daily").fetchone()
    assert trade["exit_reason"] == "TRAIL"
    assert trade["option_pnl_pts"] == 15.0
    assert trade["net_rs"] == pytest.approx(975.0 - trade["charges_rs"])
    assert daily["strategy_version"].startswith("alpha_v2.11a_champion2_")


def test_replay_marks_exact_low_vix_gate_in_context(monkeypatch) -> None:
    monkeypatch.setattr(
        tracker,
        "replay_champion_signals",
        lambda *_a, **kwargs: {
            "tier": "PC400",
            "direction": "DOWN",
            "sim_trades": [],
            "session_done": True,
            "context": {"vix_open": 16.5},
            "v211a_low_vix_trail_enabled": kwargs["enable_v211a"],
        },
    )

    replay = tracker.replay_v211a(DATE)

    assert replay["context"]["champion_label"] == "Champion 2"
    assert replay["context"]["pc400_dn_put_low_vix_trail"] is True
    assert replay["context"]["trail_arm_points"] == 30
    assert replay["context"]["trail_retrace_points"] == 20
