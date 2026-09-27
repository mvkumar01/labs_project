"""Alpha v2.14 D = v2.14 B + PC50 gap-up CALLs decided on round50(pc) - 50 .. + 100. Paper only.

The 59-day mirror parity (D differs from B only on PC50 gap-up days) is checked outside
the suite; these pin the rule.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pandas as pd

from labs.engine import alpha_v212_tracker as t212
from labs.engine import alpha_v214d_tracker as v214d

ROOT = Path(__file__).resolve().parents[1]
T = lambda hm: pd.Timestamp(f"2026-09-15 {hm}", tz="Asia/Kolkata")


def seg(pos, a, b, reason="TGT_ALPHA", spot=23000.0, pnl=10.0):
    return dict(pos=pos, entry_ts=T(a), exit_ts=T(b), reason=reason, entry_spot=spot,
                exit_spot=spot + pnl, pnl=pnl, entry_rule="RULE1")


def _fake_replays(monkeypatch, *, tier="PC50", direction="UP", base=(), wide=()):
    calls = []

    def fake(trade_date, override=None, **kw):
        calls.append(kw)
        wide_run = kw.get("range_offsets") is not None
        return {"tier": tier, "direction": direction, "session_done": True,
                "segments": list(wide if wide_run else base),
                "context": {"range_lower": 22950.0, "range_upper": 23050.0}}

    monkeypatch.setattr(v214d, "replay_v212", fake)
    return calls


def test_gap_up_pc50_takes_calls_from_the_wide_range_and_puts_from_b(monkeypatch):
    calls = _fake_replays(monkeypatch, base=[seg("put", "10:00", "10:30")],
                          wide=[seg("put", "09:30", "09:40"), seg("call", "11:00", "11:20")])
    out = v214d.replay_v214d("2026-09-15")
    assert [(s["pos"], s["signal_range"]) for s in out["segments"]] == [
        ("put", "22950.0-23050.0"), ("call", "pc-50/+100")]
    base_kw, wide_kw = calls
    assert base_kw["suppress_pc50_call_entries"] is True and "range_offsets" not in base_kw
    assert wide_kw["suppress_pc50_call_entries"] is False
    assert wide_kw["range_offsets"] == (-50, 100)
    for kw in calls:
        assert kw["close_confirmed"] is True and kw["exit_buffer"] == 10.0
        assert kw["check_entry_bar"] is True


def test_gap_down_and_non_pc50_days_are_exactly_v214b(monkeypatch):
    for tier, direction in (("PC50", "DOWN"), ("PC400", "UP"), ("PC250", "DOWN")):
        calls = _fake_replays(monkeypatch, tier=tier, direction=direction,
                              base=[seg("put", "10:00", "10:30")])
        out = v214d.replay_v214d("2026-09-15")
        assert len(calls) == 1 and [s["pos"] for s in out["segments"]] == ["put"]


def test_one_position_at_a_time_earliest_chain_wins():
    put_chain = [seg("put", "10:00", "10:10", reason="ENTRY_SPOT_SL", spot=23000.0),
                 seg("put", "10:15", "10:40", spot=23000.0)]          # re-entry at the anchor
    overlapping_call = [seg("call", "10:12", "10:50", spot=23010.0)]
    later_call = [seg("call", "11:00", "11:30", spot=23020.0)]
    merged, skipped = v214d.merge_one_position(put_chain, overlapping_call + later_call)
    assert skipped == 1
    assert [(s["pos"], s["entry_ts"].strftime("%H:%M")) for s in merged] == [
        ("put", "10:00"), ("put", "10:15"), ("call", "11:00")]


def test_range_offsets_anchor_on_round50_prev_close(monkeypatch):
    seen = {}

    class Ctx:
        prev_close, use_trail, sgap, weekday, regime, direction = 23074.9, True, 30.0, "Tue", "TRAIL", "UP"

    monkeypatch.setattr(t212, "_resolve_day", lambda d, o: {
        "lower": 23000.0, "upper": 23100.0, "bucket": "PC50", "direction": "UP"})
    monkeypatch.setattr(t212, "_resolve_replay_context",
                        lambda d, day: (None, Ctx, "regime", True, {"range_lower": 23000.0}))

    def fake_inputs(trade_date, lo, hi, use_abs, range_source):
        seen["range"] = (lo, hi)
        raise RuntimeError("stop here")

    monkeypatch.setattr(t212.champion_inputs, "build_sim_inputs", fake_inputs)
    try:
        t212.replay_v212("2026-09-15", range_offsets=(-50, 100))
    except t212.AlphaV212InputError:
        pass
    assert seen["range"] == (23000.0, 23150.0)          # round50(23074.9) = 23050


def test_tracker_saves_signal_range(monkeypatch):
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    _fake_replays(monkeypatch, wide=[seg("call", "10:00", "10:30", spot=23000.0)])
    key = lambda ts: pd.Timestamp(ts).isoformat()
    quotes = {(key(T("10:00")), 22800, "ce"): {"bid": 199.0, "ask": 200.0, "tradingsymbol": "X"},
              (key(T("10:30")), 22800, "ce"): {"bid": 210.0, "ask": 211.0, "tradingsymbol": "X"}}
    monkeypatch.setattr(v214d, "build_executable_book", lambda *a, **k: ("26SEP", quotes))
    monkeypatch.setattr(v214d.champion_inputs, "ohlc_by_minute", lambda d: {})
    monkeypatch.setattr(v214d.entry_structure, "annotate", lambda *a, **k: None)
    v214d.run_day("2026-09-15", connection=conn)
    row = conn.execute("SELECT side, signal_range, net_rs FROM alpha_v214d_trades").fetchone()
    assert (row["side"], row["signal_range"]) == ("CALL", "pc-50/+100") and row["net_rs"] > 0


def test_registered_as_a_paper_tab_and_runner_but_not_live():
    from labs.services.book_overview import BOOKS
    from labs.ui.live_routes import STRATEGY_PRESETS
    from labs.ui.routes import LIVE_TABS
    assert LIVE_TABS["alpha_v214d"] == "Alpha v2.14 D"
    assert BOOKS["alpha_v214d"]["label"] == "Alpha v2.14 D"
    assert not any("v2.14d" in v[1] or "v214d" in k for k, v in STRATEGY_PRESETS.items())
    template = (ROOT / "templates" / "live_strategy.html").read_text(encoding="utf-8")
    assert "'alpha_v214d'" in template
    for runner in ("pa_paper_tracker.py", "pa_paper_tracker_loop.py"):
        source = (ROOT / runner).read_text(encoding="utf-8")
        assert "alpha_v214d_tracker" in source and '"alpha_v214d"' in source
