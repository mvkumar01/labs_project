from __future__ import annotations

import inspect
import sqlite3
from pathlib import Path

import pandas as pd
import pytest
from flask import Flask

from labs.engine import (
    alpha_v211b_backfill,
    alpha_v211b_tracker as tracker,
    paper_strategy_tracker,
)
from labs.ui.routes import labs_bp
from live.engine import champion_sim


DATE = "2026-07-20"
IST = "Asia/Kolkata"
ROOT = Path(__file__).resolve().parents[1]


def _simulate(tier: str, *, suppress: bool):
    times = pd.date_range(f"{DATE} 09:15", periods=4, freq="5min")
    adf = pd.DataFrame(
        {
            "timestamp": times,
            "alpha": [0.0, 40.0, -40.0, -40.0],
            "d_pe_sum": [1.0] * 4,
            "d_ce_sum": [1.0] * 4,
            "denom": [2.0] * 4,
            "spot": [100.0, 100.0, 90.0, 80.0],
        }
    )
    bars = {
        "09:15": (100.0, 100.0, 100.0, 100.0),
        "09:20": (100.0, 100.0, 100.0, 100.0),
        "09:25": (90.0, 90.0, 90.0, 90.0),
        "09:30": (80.0, 80.0, 80.0, 80.0),
    }
    return champion_sim.simulate(
        adf,
        {},
        {},
        champion_sim.OHLC(bars),
        DATE,
        True,
        0.0,
        tier,
        "Mon",
        "TRAIL",
        0,
        1000,
        suppress_pc50_call_entries=suppress,
    )


def test_pc50_call_gate_stays_flat_and_allows_later_put() -> None:
    _, baseline = _simulate("PC50", suppress=False)
    _, replay_b = _simulate("PC50", suppress=True)

    assert [trade["pos"] for trade in baseline] == ["call"]
    assert [trade["pos"] for trade in replay_b] == ["put"]
    assert pd.Timestamp(replay_b[0]["entry_ts"]).strftime("%H:%M") == "09:25"


def test_pc50_call_gate_does_not_change_pc250() -> None:
    _, baseline = _simulate("PC250", suppress=False)
    _, replay_b = _simulate("PC250", suppress=True)

    assert baseline == replay_b
    assert [trade["pos"] for trade in replay_b] == ["call"]


def test_replay_v211b_enables_only_the_decision_gate(monkeypatch) -> None:
    def fake_replay(*_args, **kwargs):
        assert kwargs == {"suppress_pc50_call_entries": True}
        return {
            "tier": "PC50",
            "direction": "UP",
            "sim_trades": [],
            "session_done": True,
            "context": {"vix_open": 16.5},
        }

    monkeypatch.setattr(tracker, "replay_champion_signals", fake_replay)

    replay = tracker.replay_v211b(DATE)

    assert replay["context"]["decision_filter"] == "no_pc50_call"
    assert replay["context"]["pc50_call_entries_allowed"] is False


def test_tracker_persists_separate_replay_b_ledger(monkeypatch) -> None:
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    paper_strategy_tracker._ensure_tables(conn)
    entry_ts = pd.Timestamp(f"{DATE} 09:20", tz=IST).isoformat()
    exit_ts = pd.Timestamp(f"{DATE} 09:25", tz=IST).isoformat()
    replay = {
        "tier": "PC50",
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
                "reason": "EOD",
            }
        ],
        "session_done": True,
        "context": {"decision_filter": "no_pc50_call"},
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
    monkeypatch.setattr(tracker, "replay_v211b", lambda *_a, **_k: replay)
    monkeypatch.setattr(
        tracker, "build_executable_book", lambda *_a, **_k: ("26723", quotes)
    )

    result = tracker.run_day(DATE, connection=conn)

    assert result["status"] == "traded"
    trade = conn.execute("SELECT * FROM alpha_v211b_trades").fetchone()
    daily = conn.execute("SELECT * FROM alpha_v211b_daily").fetchone()
    assert trade["side"] == "PUT"
    assert trade["net_rs"] == pytest.approx(975.0 - trade["charges_rs"])
    assert daily["strategy_version"] == tracker.STRATEGY_VERSION

    app = Flask(__name__, template_folder="../templates")
    app.register_blueprint(labs_bp)
    monkeypatch.setattr("storage.db.get_conn", lambda: conn)
    html = app.test_client().get(
        "/labs/live?tab=alpha_v211b"
    ).get_data(as_text=True)
    assert "Alpha 2.11 - champion replay (B)" in html
    assert "PC50 CALL decisions do not enter a trade" in html
    assert "PUT" in html


def test_backfill_accepts_legacy_primary_schema(monkeypatch) -> None:
    conn = sqlite3.connect(":memory:")
    conn.execute(
        "CREATE TABLE paper_strategy_daily (trade_date TEXT PRIMARY KEY)"
    )
    monkeypatch.setattr(
        alpha_v211b_backfill,
        "_historical_ranges",
        lambda *_args: {DATE: {"bucket": "PC50"}},
    )

    pending, ranges = alpha_v211b_backfill._pending(DATE, DATE, conn)

    columns = {
        row[1] for row in conn.execute("PRAGMA table_info(paper_strategy_daily)")
    }
    assert "context_json" in columns
    assert pending == [DATE]
    assert DATE in ranges


def test_registered_as_live_tab_and_paper_runner() -> None:
    routes_source = (ROOT / "labs" / "ui" / "routes.py").read_text(
        encoding="utf-8"
    )
    template = (ROOT / "templates" / "live_strategy.html").read_text(
        encoding="utf-8"
    )
    assert '"alpha_v211b"' in inspect.getsource(__import__("labs.ui.routes", fromlist=["live_strategy"]).live_strategy)
    assert "alpha_v211b_backfill" in routes_source
    assert "tab='alpha_v211b'" in template
    assert "PC50 CALL decisions do not enter a trade" in template
    for runner in ("pa_paper_tracker.py", "pa_paper_tracker_loop.py"):
        source = (ROOT / runner).read_text(encoding="utf-8")
        assert "alpha_v211b_tracker" in source
        assert '"alpha_v211b"' in source
