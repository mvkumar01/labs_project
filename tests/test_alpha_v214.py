"""Alpha v2.14 = v2.11 replay (B) + B10.

Pins that v2.14 is exactly the two proven pieces combined, in paper and live:
  * B10's close-confirmed 10-point stop on the boundary clock, and
  * v2.11 replay (B)'s PC50 CALL entry suppression,
with neither piece leaking into the books it came from.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pandas as pd
import pytest
from flask import Flask

from labs.engine import alpha_v214_tracker as v214
from labs.ui.live_routes import STRATEGY_LABELS, STRATEGY_PRESETS
from live.engine import champion_sim
from live.engine.champion_sim import V212_B10_EXIT_BUFFER
from live.live_runner import champion_live_policy

import test_v212_close_confirmed as fx

ROOT = Path(__file__).resolve().parents[1]


def _pc50_segments(*, suppress: bool):
    adf, ohlc = fx._inputs(stop_close=101.0)
    _, segments = champion_sim.simulate(
        adf, {}, {}, ohlc, fx.DATE, False, 1.0, "PC50", "Thu", "STD", 0, 1000,
        enable_entry_spot_recovery=True,
        entry_spot_close_confirmed=True,
        entry_spot_exit_buffer=V212_B10_EXIT_BUFFER,
        suppress_pc50_call_entries=suppress,
    )
    return segments


# -- the rule ---------------------------------------------------------------
def test_pc50_call_that_b10_takes_is_suppressed_in_v214() -> None:
    assert any(s["pos"] == "call" for s in _pc50_segments(suppress=False))
    assert not any(s["pos"] == "call" for s in _pc50_segments(suppress=True))


def test_paper_replay_is_b10_plus_the_replay_b_filter(monkeypatch) -> None:
    seen = {}

    def fake_replay(trade_date, override=None, **kwargs):
        seen.update(kwargs)
        return {"tier": "PC50", "direction": "UP", "segments": [],
                "session_done": True, "context": {}}

    monkeypatch.setattr(v214, "replay_v212", fake_replay)
    out = v214.replay_v214("2026-09-15")

    assert seen == {"close_confirmed": True,
                    "exit_buffer": V212_B10_EXIT_BUFFER,
                    "suppress_pc50_call_entries": True,
                    "check_entry_bar": True}
    assert out["context"]["strategy_version"] == \
        "Alpha v2.14 B (v2.11 replay (B) + B10)"


# -- live ---------------------------------------------------------------------
def test_live_policy_for_v214() -> None:
    p = champion_live_policy("v2.14")
    assert p.suppress_pc50_call_entries is True
    assert p.entry_spot_exit_buffer == V212_B10_EXIT_BUFFER
    assert p.boundary_tick_close is True
    assert p.fast_stop_overlay is False
    assert p.next_open_fallback is False
    assert p.entry_spot_check_entry_bar is True


def test_sources_keep_their_own_behaviour() -> None:
    b10, replay_b = champion_live_policy("v2.12_b10"), champion_live_policy("v2.11b")
    assert b10.suppress_pc50_call_entries is False
    assert replay_b.entry_spot_exit_buffer == 0.0
    assert replay_b.boundary_tick_close is False
    assert replay_b.entry_spot_check_entry_bar is False
    for other in ("v2.11", "v2.12", "v2.12_closed_confirmed", "v2.13"):
        assert champion_live_policy(other).entry_spot_check_entry_bar is False


def test_live_preset_and_label() -> None:
    assert STRATEGY_PRESETS["champion_v214"] == ("champion_replay", "v2.14")
    assert "v2.11 replay (B) + B10" in STRATEGY_LABELS["champion_v214"]


def test_runner_treats_v214_as_b10_recovery_replay() -> None:
    source = (ROOT / "live" / "live_runner.py").read_text(encoding="utf-8")
    assert 'v212_b10 = strategy_version in ("v2.12_b10", "v2.14")' in source


# -- paper book ---------------------------------------------------------------
def test_tracker_persists_its_own_ledger_with_causal_fills(monkeypatch) -> None:
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    entry_ts = pd.Timestamp("2026-09-15 09:20")
    stop_ts = pd.Timestamp("2026-09-15 09:25")
    replay = {
        "tier": "PC50", "direction": "DOWN", "session_done": True,
        "context": {"decision_filter": "no_pc50_call"},
        "segments": [dict(pos="put", entry_ts=entry_ts, exit_ts=stop_ts,
                          entry_spot=24197.6, exit_spot=24208.0, pnl=-10.4,
                          entry_rule="RULE1", reason="ENTRY_SPOT_SL")],
    }
    fill_ts = stop_ts + pd.Timedelta(minutes=1)       # stop priced at M + 1
    key = lambda ts: ts.tz_localize(v214.IST).isoformat()
    quotes = {
        # A pre-decision quote at the stop minute must NOT be the fill.
        (key(stop_ts), 24400, "pe"): {"bid": 150.0, "ask": 151.0,
                                      "tradingsymbol": "NIFTY_TEST_PE"},
        (key(entry_ts), 24400, "pe"): {"bid": 199.0, "ask": 200.0,
                                       "tradingsymbol": "NIFTY_TEST_PE"},
        (key(fill_ts), 24400, "pe"): {"bid": 190.0, "ask": 191.0,
                                      "tradingsymbol": "NIFTY_TEST_PE"},
    }
    monkeypatch.setattr(v214, "replay_v214", lambda *_a, **_k: replay)
    monkeypatch.setattr(v214, "build_executable_book",
                        lambda *_a, **_k: ("26723", quotes))
    monkeypatch.setattr(v214.champion_inputs, "ohlc_by_minute", lambda _d: {})

    v214.run_day("2026-09-15", connection=conn)

    trade = conn.execute("SELECT * FROM alpha_v214_trades").fetchone()
    daily = conn.execute("SELECT * FROM alpha_v214_daily").fetchone()
    assert pd.Timestamp(trade["exit_ts"]) == fill_ts.tz_localize(v214.IST)
    assert trade["exit_bid"] == 190.0
    assert daily["strategy_version"] == v214.STRATEGY_VERSION

    from labs.ui.routes import labs_bp
    app = Flask(__name__, template_folder="../templates")
    app.register_blueprint(labs_bp)
    monkeypatch.setattr("storage.db.get_conn", lambda: conn)
    html = app.test_client().get("/labs/live?tab=alpha_v214").get_data(as_text=True)
    assert "Alpha v2.14 B" in html
    assert "v2.11 replay (B) + B10" in html


def test_registered_as_live_tab_and_paper_runner() -> None:
    from labs.ui.routes import LIVE_TABS
    from labs.services.book_overview import BOOKS

    assert LIVE_TABS["alpha_v214"] == "Alpha v2.14 B"
    assert BOOKS["alpha_v214"]["label"] == "Alpha v2.14 B"
    assert LIVE_TABS["alpha_v212b10"] == "Alpha v2.14 A"
    assert BOOKS["alpha_v212b10"]["label"] == "Alpha v2.14 A"
    template = (ROOT / "templates" / "live_strategy.html").read_text(encoding="utf-8")
    assert "'alpha_v214'" in template
    for runner in ("pa_paper_tracker.py", "pa_paper_tracker_loop.py"):
        source = (ROOT / runner).read_text(encoding="utf-8")
        assert "alpha_v214_tracker" in source
        assert '"alpha_v214"' in source
