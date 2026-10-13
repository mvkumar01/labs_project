"""Alpha v2.14 E = Alpha v2.14 C (Renko 30 overlay) that opens no position from 14:00.

A live preset (strategy_version v2.14e) and its paper twin (alpha_v214e). These pin the rule:
what the cut-off stops, what it leaves alone, and that live and paper read the same constants.
"""

from __future__ import annotations

import inspect
import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pandas as pd
import pytest

import live.live_runner as lr
import storage.live_db as live_db
from labs.engine import alpha_v214e_tracker as v214e
from labs.ui.live_routes import STRATEGY_LABELS, STRATEGY_PRESETS
from live import live_executor as ex
from live import live_service as svc
from live.brokers.base import OrderResult, Position
from live.engine import champion_decider, champion_sim
from live.engine.champion_sim import (
    V212_B10_EXIT_BUFFER, V214C_RENKO_BRICK, V214C_RENKO_REVERSAL, V214E_NO_ENTRY_FROM_MINUTE,
)
from live.live_runner import champion_live_policy, entries_closed_now
from storage.live_db import init_live_db

ROOT = Path(__file__).resolve().parents[1]
IST = timezone(timedelta(hours=5, minutes=30))
DATE = "2026-06-18"
hm = lambda ts: pd.Timestamp(ts).strftime("%H:%M")


# -- the rule in the simulator ---------------------------------------------------------
def _inputs(first_bar: str, alphas, closes=None, flat: float = 100.0):
    """Five-minute Alpha marks from `first_bar`; every one-minute candle closes at `flat`
    unless `closes` says otherwise. PC50: Alpha crossing above 25 enters a CALL."""
    times = pd.date_range(f"{DATE} {first_bar}", periods=len(alphas), freq="5min")
    adf = pd.DataFrame({"timestamp": times, "alpha": alphas, "d_pe_sum": 1.0, "d_ce_sum": 1.0,
                        "denom": 100.0, "spot": flat})
    by_minute = {ts.strftime("%H:%M"): (flat, flat, flat, flat)
                 for ts in pd.date_range(times[0], times[-1] + pd.Timedelta(minutes=4), freq="min")}
    for key, close in (closes or {}).items():
        by_minute[key] = (close, close, close, close)
    return adf, champion_sim.OHLC(by_minute)


def _run(first_bar, alphas, closes=None, *, cutoff=V214E_NO_ENTRY_FROM_MINUTE, trace=None, **extra):
    adf, ohlc = _inputs(first_bar, alphas, closes)
    kw = dict(enable_entry_spot_recovery=True, entry_spot_close_confirmed=True,
              entry_spot_exit_buffer=V212_B10_EXIT_BUFFER, entry_spot_check_entry_bar=True,
              entry_spot_renko_brick=V214C_RENKO_BRICK, entry_spot_renko_reversal=V214C_RENKO_REVERSAL,
              entry_spot_recovery_trace=trace, no_entry_from_minute=cutoff)
    kw.update(extra)
    _, segs = champion_sim.simulate(adf, {}, {}, ohlc, DATE, False, 1.0, "PC50", "Thu", "STD", 0, 1000, **kw)
    return segs


def test_cut_off_is_two_in_the_afternoon():
    assert V214E_NO_ENTRY_FROM_MINUTE == 14 * 60


def test_alpha_entry_on_the_1355_mark_is_taken_and_on_the_1400_mark_is_not():
    alphas = [0.0, 30.0, 40.0, 50.0, 100.0]                 # crosses 25 on the second mark
    taken = _run("13:50", alphas)
    assert [(hm(s["entry_ts"]), s["reason"]) for s in taken] == [("13:55", "TGT_ALPHA")]
    assert hm(taken[0]["exit_ts"]) == "14:10"               # exits after 14:00 are untouched
    assert _run("13:55", alphas) == []                      # the same signal on the 14:00 mark
    late = _run("13:55", alphas, cutoff=None)               # v2.14 C takes it
    assert [(hm(s["entry_ts"]), s["reason"]) for s in late] == [("14:00", "TGT_ALPHA")]


def test_every_later_mark_is_closed_too():
    assert _run("14:20", [0.0, 30.0, 40.0, 50.0, 100.0]) == []
    assert _run("15:00", [0.0, 30.0, 40.0, 50.0, 100.0]) == []


def _dip(first: str, back: str, low: float = 40.0) -> dict:
    """One-minute closes at `low` from `first` up to (not including) `back`: two bricks down
    at `first`, and the brick back up when the price returns to 100 at `back`."""
    return {ts.strftime("%H:%M"): low for ts in pd.date_range(f"{DATE} {first}", f"{DATE} {back}", freq="min")[:-1]}


def test_renko_re_entry_needs_its_candle_to_close_before_1400():
    alphas = [0.0, 30.0, 40.0, 40.0, 40.0, 40.0, 40.0, 40.0]     # CALL on the 13:40 mark, Alpha holds
    trace: list = []
    segs = _run("13:35", alphas, _dip("13:47", "13:58"), trace=trace)     # brick up at 13:58; candle closes 13:59
    assert [(hm(s["entry_ts"]), hm(s["exit_ts"]), s["reason"]) for s in segs[:1]] == [("13:40", "13:47", "ENTRY_SPOT_SL")]
    assert hm(segs[1]["entry_ts"]) == "13:58" and segs[1]["entry_spot"] == 100.0 and [hm(t) for t in trace] == ["13:58"]

    trace = []
    segs = _run("13:35", alphas, _dip("13:47", "13:59"), trace=trace)     # same brick, candle closes 14:00
    assert [(hm(s["entry_ts"]), s["reason"]) for s in segs] == [("13:40", "ENTRY_SPOT_SL")] and trace == []
    segs = _run("13:35", alphas, _dip("13:47", "14:03"))                  # and any later one
    assert [s["reason"] for s in segs] == ["ENTRY_SPOT_SL"]
    again = _run("13:35", alphas, _dip("13:47", "13:59"), cutoff=None)    # v2.14 C re-enters
    assert hm(again[1]["entry_ts"]) == "13:59"


def test_a_position_open_at_1400_keeps_its_renko_exit_but_is_not_re_entered():
    alphas = [0.0, 30.0] + [40.0] * 8                        # CALL on the 13:40 mark
    closes = _dip("14:07", "14:12")                          # out on the bricks at 14:07; brick back at 14:12
    segs = _run("13:35", alphas, closes)
    assert [(hm(s["entry_ts"]), hm(s["exit_ts"]), s["reason"]) for s in segs] == [("13:40", "14:07", "ENTRY_SPOT_SL")]
    twice = _run("13:35", alphas, closes, cutoff=None)
    assert [hm(s["entry_ts"]) for s in twice] == ["13:40", "14:12"]


def test_re_entry_inside_the_same_five_minute_bar_is_also_closed():
    alphas = [0.0, 30.0] + [40.0] * 6
    # out at 13:56, brick back at 13:59: the same five-minute bar, its candle closes at 14:00
    assert [s["reason"] for s in _run("13:35", alphas, _dip("13:56", "13:59"))] == ["ENTRY_SPOT_SL"]
    # a minute earlier it is still allowed
    assert [hm(s["entry_ts"]) for s in _run("13:35", alphas, _dip("13:56", "13:58"))] == ["13:40", "13:58"]


def test_without_the_cut_off_the_simulator_is_unchanged():
    import test_v212_close_confirmed as fx
    key = lambda segs: [(s["reason"], str(s["entry_ts"]), str(s["exit_ts"]), s["pnl"]) for s in segs]
    for stop_close in (99.0, 100.0, 101.0):
        adf, ohlc = fx._inputs(stop_close=stop_close)
        args = (adf, {}, {}, ohlc, fx.DATE, False, 1.0, "PC50", "Thu", "STD", 0, 1000)
        for kw in (dict(enable_entry_spot_recovery=True, entry_spot_close_confirmed=True),
                   dict(enable_entry_spot_recovery=True, entry_spot_close_confirmed=True, entry_spot_exit_buffer=10.0,
                        entry_spot_check_entry_bar=True, entry_spot_renko_brick=30.0),
                   dict(enable_entry_spot_recovery=True), {}):
            assert key(champion_sim.simulate(*args, **kw)[1]) == \
                key(champion_sim.simulate(*args, no_entry_from_minute=None, **kw)[1])
            # a cut-off later than the whole session changes nothing either
            assert key(champion_sim.simulate(*args, **kw)[1]) == \
                key(champion_sim.simulate(*args, no_entry_from_minute=16 * 60, **kw)[1])
    assert inspect.signature(champion_sim.simulate).parameters["no_entry_from_minute"].default is None


# -- live ------------------------------------------------------------------------------
def test_live_policy_is_v214_with_the_renko_overlay_and_the_cut_off():
    p, b = champion_live_policy("v2.14e"), champion_live_policy("v2.14")
    assert (p.entry_spot_renko_brick, p.entry_spot_renko_reversal) == (V214C_RENKO_BRICK, V214C_RENKO_REVERSAL)
    assert p.no_entry_from_minute == V214E_NO_ENTRY_FROM_MINUTE
    for name in ("suppress_pc50_call_entries", "entry_spot_exit_buffer", "boundary_tick_close", "fast_stop_overlay",
                 "next_open_fallback", "entry_spot_check_entry_bar"):
        assert getattr(p, name) == getattr(b, name), name
    assert p.suppress_pc50_call_entries is True and p.boundary_tick_close is True and p.fast_stop_overlay is False


@pytest.mark.parametrize("version", ["v2.11", "v2.11b", "v2.12", "v2.12_closed_confirmed", "v2.12_b10", "v2.14", "v2.13"])
def test_no_other_version_gets_the_overlay_or_the_cut_off(version):
    p = champion_live_policy(version)
    assert p.entry_spot_renko_brick == 0.0 and p.no_entry_from_minute is None
    assert entries_closed_now(p, datetime(2026, 10, 14, 15, 0, tzinfo=IST)) is False


def test_cut_off_clock_is_ist():
    p = champion_live_policy("v2.14e")
    assert entries_closed_now(p, datetime(2026, 10, 14, 13, 59, 59, tzinfo=IST)) is False
    assert entries_closed_now(p, datetime(2026, 10, 14, 14, 0, 0, tzinfo=IST)) is True
    assert entries_closed_now(p, datetime(2026, 10, 14, 8, 29, 59, tzinfo=timezone.utc)) is False    # 13:59:59 IST
    assert entries_closed_now(p, datetime(2026, 10, 14, 8, 30, 0, tzinfo=timezone.utc)) is True


def test_preset_label_and_contract():
    assert STRATEGY_PRESETS["champion_v214e"] == ("champion_replay", "v2.14e")
    assert "Renko 30" in STRATEGY_LABELS["champion_v214e"] and "14:00" in STRATEGY_LABELS["champion_v214e"]
    assert STRATEGY_PRESETS["champion_v214"] == ("champion_replay", "v2.14")          # B is still B
    # a runner that has not loaded this version must not pass the decision-contract gate
    assert ex.LIVE_DECISION_ABI == "alpha-v2.14abe-renko-cutoff-live-v2"
    assert ex.expected_decision_abi("v2.14e") == ex.LIVE_DECISION_ABI


def test_decider_hands_the_overlay_and_the_cut_off_to_the_simulator():
    params = inspect.signature(champion_decider.champion_target).parameters
    assert params["entry_spot_renko_brick"].default == 0.0 and params["no_entry_from_minute"].default is None
    source = inspect.getsource(champion_decider.champion_target)
    for name in ("entry_spot_renko_brick", "entry_spot_renko_reversal", "no_entry_from_minute"):
        assert f"{name}={name}," in source


def test_a_strategy_cannot_be_selected_while_the_runner_is_on_an_earlier_release(monkeypatch):
    """Files on disk change at deploy; the runner process only at its restart."""
    from labs.ui import live_routes
    loaded = {"abi": "alpha-v2.14ab-entrybar-live-v1"}
    monkeypatch.setattr(live_routes.svc, "get_config", lambda u, c, key, *a: loaded["abi"] if key == "runner_decision_abi" else None)
    assert "restarted first" in live_routes._runner_not_ready_for("u", "c", "champion_v214e")
    assert "restarted first" in live_routes._runner_not_ready_for("u", "c", "champion_v214")
    loaded["abi"] = ex.LIVE_DECISION_ABI                      # the restarted runner
    assert live_routes._runner_not_ready_for("u", "c", "champion_v214e") is None
    loaded["abi"] = ""                                        # never claimed by a runner yet
    assert live_routes._runner_not_ready_for("u", "c", "champion_v214e") is None
    loaded["abi"] = ex.PROPOSER_DECISION_ABI                  # coming from the SENSEX runner
    assert live_routes._runner_not_ready_for("u", "c", "champion_v214e") is None
    loaded["abi"] = "alpha-v2.14ab-entrybar-live-v1"
    assert live_routes._runner_not_ready_for("u", "c", "proposer_dt25_v3") is None


USER_ID, CONN_ID = "user-t", "user-t:angel"


def _live_poll(monkeypatch, tmp_path, *, version: str, now: datetime):
    """One runner poll for a flat, armed account while the replay holds a PUT."""
    monkeypatch.setenv("LIVE_ORDER_PROXY_URL", "http://static.test:1234")
    monkeypatch.setattr(live_db, "LIVE_DB_PATH", tmp_path / "live.db")
    init_live_db()
    conn = live_db.get_live_conn()
    svc.upsert_connection(USER_ID, CONN_ID, broker="angel", account_label="T", account_ref="angel:T",
                          status="connected", conn=conn)
    for k, v in (("mode", "LIVE_ARMED"), ("armed", "1"), ("kill_switch", "0"), ("lots", "1"),
                 ("daily_loss_cap", "50000"), ("decision_engine", "champion_replay"), ("strategy_version", version)):
        svc.set_config(USER_ID, CONN_ID, k, v, conn)
    conn.close()

    class FlatAdapter:
        def __init__(self, **_kwargs):
            self.position = Position(symbol=None, qty=0, side=None)

        def connect(self):
            return None

        def is_connected(self):
            return True

        def account_ref(self):
            return "angel:T"

        def get_position(self):
            return self.position

    day = now.date().isoformat()
    monkeypatch.setattr(lr, "_now_ist", lambda: now)
    monkeypatch.setattr(lr, "_today_ist_iso", lambda: day)
    monkeypatch.setattr(lr, "_now_iso", lambda: now.astimezone(timezone.utc).isoformat())
    monkeypatch.setattr(lr, "market_session_available", lambda _now: True)
    monkeypatch.setattr(lr, "eod_watchdog", lambda _now_t: False)
    monkeypatch.setattr(lr, "notify_telegram", lambda _msg: None)
    monkeypatch.setattr(lr, "_fast_spot", lambda: 23996.6)
    monkeypatch.setattr(lr, "_boundary_minutes", lambda _d: {})
    monkeypatch.setattr(lr, "_boundary_key", lambda _d: None)
    monkeypatch.setattr(lr, "get_latest_alpha", lambda: {
        "timestamp": f"{day}T{now.strftime('%H:%M')}:00+05:30", "alpha": -40.0, "spot": 23996.6})
    monkeypatch.setattr(lr.champion_inputs, "latest_completed_ohlc_minute", lambda _d: f"{day}T{now.strftime('%H:%M')}")
    asked = {}

    def target(*_a, **kwargs):
        asked.update(kwargs)
        return {"position": "PUT", "entry_spot": 23996.6, "entry_rule": "RULE1", "n_closed": 0,
                "last_closed_event_id": None}

    monkeypatch.setattr(lr.champion_decider, "champion_target", target)
    monkeypatch.setattr(lr, "resolve_affordable_option", lambda *_a, **_k: ("NIFTY26O2024200PE", 274.5))
    routed = []

    def fake_route(*_args, **kwargs):
        routed.append(kwargs)
        return OrderResult(broker_order_id="OID1", status="PLACED", avg_fill_price=274.5, raw={})

    monkeypatch.setattr(lr, "_route_order", fake_route)
    lr.process_connection(USER_ID, CONN_ID, adapters={}, reconciled=set(), task_id="test-runner",
                          signal_engines={}, alpha_seen={}, adapter_factory=FlatAdapter)
    conn = live_db.get_live_conn()
    try:
        state = svc.get_trade_state(USER_ID, CONN_ID, conn=conn)
    finally:
        conn.close()
    return routed, asked, state


def test_runner_replays_v214e_with_the_overlay_and_enters_before_1400(monkeypatch, tmp_path):
    routed, asked, state = _live_poll(monkeypatch, tmp_path, version="v2.14e",
                                      now=datetime(2026, 10, 14, 13, 59, 30, tzinfo=IST))
    assert (asked["entry_spot_renko_brick"], asked["entry_spot_renko_reversal"]) == (30.0, 2)
    assert asked["no_entry_from_minute"] == 14 * 60
    assert asked["enable_entry_spot_recovery"] is True and asked["entry_spot_close_confirmed"] is True
    assert asked["suppress_pc50_call_entries"] is True and asked["entry_spot_check_entry_bar"] is True
    assert [r["action"] for r in routed] == ["ENTER"] and state["position"] == "OPEN"


def test_runner_buys_nothing_from_1400_even_if_the_replay_holds_a_position(monkeypatch, tmp_path):
    """Armed late, an entry that never filled, a restart: the replay's earlier position is
    not bought after the cut-off."""
    routed, asked, state = _live_poll(monkeypatch, tmp_path, version="v2.14e",
                                      now=datetime(2026, 10, 14, 14, 0, 0, tzinfo=IST))
    assert asked["no_entry_from_minute"] == 14 * 60
    assert routed == [] and (state.get("position") or "NONE") != "OPEN"


def test_runner_leaves_v214_b_alone_after_1400(monkeypatch, tmp_path):
    routed, asked, _ = _live_poll(monkeypatch, tmp_path, version="v2.14",
                                  now=datetime(2026, 10, 14, 14, 30, 0, tzinfo=IST))
    assert asked["entry_spot_renko_brick"] == 0.0 and asked["no_entry_from_minute"] is None
    assert [r["action"] for r in routed] == ["ENTER"]


# -- paper twin ------------------------------------------------------------------------
def test_book_is_v214c_with_the_cut_off(monkeypatch):
    from labs.engine import alpha_v214c_tracker as v214c
    seen, seen_c = {}, {}

    def fake(store):
        def run(trade_date, override=None, **kwargs):
            store.update(kwargs)
            return {"tier": "PC50", "direction": "DOWN", "segments": [], "session_done": True, "context": {}}
        return run

    monkeypatch.setattr(v214e, "replay_v212", fake(seen))
    monkeypatch.setattr(v214c, "replay_v212", fake(seen_c))
    out = v214e.replay_v214e("2026-09-15", None, [])
    v214c.replay_v214c("2026-09-15", None, [])
    assert seen["no_entry_from_minute"] == 14 * 60 and "no_entry_from_minute" not in seen_c
    assert {k: v for k, v in seen.items() if k not in ("no_entry_from_minute", "recovery_trace")} == \
        {k: v for k, v in seen_c.items() if k != "recovery_trace"}
    assert out["context"]["no_entry_from"] == "14:00" and out["context"]["overlay"] == "renko"
    assert v214e.STRATEGY_VERSION != v214c.STRATEGY_VERSION and "no_entry_from_1400" in v214e.STRATEGY_VERSION


def test_book_keeps_its_own_tables():
    conn = sqlite3.connect(":memory:")
    v214e._ensure_tables(conn)
    tables = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    assert {"alpha_v214e_daily", "alpha_v214e_trades"} <= tables and not any("v214c" in t for t in tables)


def test_registered_as_a_paper_tab_and_runner():
    from labs.services.book_overview import BOOKS
    from labs.ui.routes import LIVE_TABS
    assert LIVE_TABS["alpha_v214e"] == "Alpha v2.14 E" and BOOKS["alpha_v214e"]["label"] == "Alpha v2.14 E"
    template = (ROOT / "templates" / "live_strategy.html").read_text(encoding="utf-8")
    assert "'alpha_v214e'" in template and "no position from 14:00" in template
    for runner in ("pa_paper_tracker.py", "pa_paper_tracker_loop.py"):
        source = (ROOT / runner).read_text(encoding="utf-8")
        assert "alpha_v214e_tracker" in source and '"alpha_v214e"' in source


def test_patch_script_is_idempotent_and_compiles(tmp_path):
    import py_compile
    import shutil
    import subprocess
    import sys
    files = ("live/engine/champion_sim.py", "labs/ui/routes.py", "templates/live_strategy.html",
             "pa_paper_tracker_loop.py", "labs/services/book_overview.py")
    for rel in files:
        target = tmp_path / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy(ROOT / rel, target)
    run = subprocess.run([sys.executable, str(ROOT / "scripts" / "patch_alpha_v214e.py"), str(tmp_path)],
                         capture_output=True, text=True)
    assert run.returncode == 0 and run.stdout.count("0 edits applied") == 5, run.stdout + run.stderr
    for rel in files:
        if rel.endswith(".py"):
            py_compile.compile(str(tmp_path / rel), doraise=True)
