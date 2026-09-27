"""Market structure at each fresh Alpha entry, logged on the 2.14 A/B/C paper trades."""

from __future__ import annotations

import sqlite3

import pandas as pd
import pytest

from labs.engine import entry_structure as es
from labs.engine import alpha_v214_tracker as v214


def _frame(levels):
    return {"bars": pd.DataFrame(columns=["open", "high", "low", "close"]), "levels": levels}


def test_room_and_cushion_use_the_nearest_level_or_oi_wall():
    f = _frame({"pdh": 23200.0, "pdl": 22900.0, "pdc": 23050.0, "pivot": 23040.0,
                "bc": 23030.0, "tc": 23060.0})
    ce = {23100: 9e6, 23150: 1e6}              # biggest CE OI above spot -> 23100
    pe = {22950: 2e6, 23000: 8e6}              # biggest PE OI below spot -> 23000
    call = es.at_entry(f, "2026-09-25 10:00", 23045.0, "call", ce, pe)
    assert (call["entry_room_level"], call["entry_room_pts"]) == ("pdc", 5.0)
    assert (call["entry_cushion_level"], call["entry_cushion_pts"]) == ("pivot", 5.0)
    put = es.at_entry(f, "2026-09-25 10:00", 23045.0, "put", ce, pe)
    assert put["entry_room_level"] == "pivot" and put["entry_cushion_level"] == "pdc"


def test_walls_count_when_nearer_than_the_levels():
    f = _frame({"pdh": 23400.0, "pdl": 22700.0})
    out = es.at_entry(f, "2026-09-25 10:00", 23045.0, "call", {23070: 5e6}, {23020: 5e6})
    assert (out["entry_room_level"], out["entry_room_pts"]) == ("oi_wall_ce", 25.0)
    assert (out["entry_cushion_level"], out["entry_cushion_pts"]) == ("oi_wall_pe", 25.0)


def test_indicators_come_from_bins_closed_before_the_entry(monkeypatch):
    minutes = {}
    for k, ts in enumerate(pd.date_range("2026-09-25 09:15", "2026-09-25 11:59", freq="min")):
        p = 23000.0 + (k % 7) * 3.0 - k * 0.2
        minutes[ts.strftime("%H:%M")] = (p, p + 2.0, p - 2.0, p + 1.0)
    monkeypatch.setattr(es, "_previous_session", lambda d, max_back=10: (None, {}))
    frame = es.day_frame("2026-09-25", today_minutes=minutes)
    early = es.at_entry(frame, pd.Timestamp("2026-09-25 09:30", tz="Asia/Kolkata"), 23000.0, "put")
    late = es.at_entry(frame, pd.Timestamp("2026-09-25 11:30", tz="Asia/Kolkata"), 23000.0, "put")
    assert early["entry_chop"] is None                  # 14 bins not yet available
    assert late["entry_chop"] is not None and 0 < late["entry_chop"] < 100
    assert late["entry_atr"] > 0


def test_only_fresh_entries_are_annotated_and_errors_never_block_the_save(monkeypatch):
    monkeypatch.setattr(es, "day_frame", lambda d, today_minutes=None: _frame({"pdh": 23100.0}))
    trades = [{}, {}]
    segs = [{"entry_ts": "2026-09-25 10:00", "entry_spot": 23000.0, "pos": "call"},
            {"entry_ts": "2026-09-25 10:20", "entry_spot": 23000.0, "pos": "call"}]
    es.annotate(trades, segs, [True, False], "2026-09-25")
    assert trades[0]["entry_room_level"] == "pdh" and trades[1]["entry_room_level"] is None

    def boom(*a, **k):
        raise RuntimeError("no data")
    monkeypatch.setattr(es, "day_frame", boom)
    trades = [{}]
    es.annotate(trades, segs[:1], [True], "2026-09-25")
    assert trades[0]["entry_room_pts"] is None


def test_columns_are_added_once():
    conn = sqlite3.connect(":memory:")
    v214._ensure_tables(conn)
    v214._ensure_tables(conn)
    cols = {r[1] for r in conn.execute("PRAGMA table_info(alpha_v214_trades)")}
    assert set(es.FIELDS) <= cols


def test_tracker_saves_the_structure_of_a_fresh_entry(monkeypatch):
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    T = pd.Timestamp("2026-09-25 10:00", tz="Asia/Kolkata")
    replay = {"tier": "PC50", "direction": "DOWN", "session_done": True, "context": {},
              "oi_maps": ({}, {T: {22900: 5e6}}),
              "segments": [dict(pos="put", entry_ts=T, exit_ts=T + pd.Timedelta(minutes=30),
                                reason="TGT_ALPHA", entry_spot=23000.0, exit_spot=22950.0,
                                pnl=50.0, entry_rule="RULE1")]}
    key = lambda ts: pd.Timestamp(ts).isoformat()
    quotes = {(key(T), 23200, "pe"): {"bid": 199.0, "ask": 200.0, "tradingsymbol": "X"},
              (key(T + pd.Timedelta(minutes=30)), 23200, "pe"): {"bid": 230.0, "ask": 231.0,
                                                                  "tradingsymbol": "X"}}
    monkeypatch.setattr(v214, "replay_v214", lambda *a, **k: replay)
    monkeypatch.setattr(v214, "build_executable_book", lambda *a, **k: ("26SEP", quotes))
    monkeypatch.setattr(v214.champion_inputs, "ohlc_by_minute", lambda d: {})
    monkeypatch.setattr(es, "day_frame",
                        lambda d, today_minutes=None: _frame({"pdl": 22960.0}))
    v214.run_day("2026-09-25", connection=conn)
    row = conn.execute("SELECT entry_room_pts, entry_room_level, entry_cushion_pts "
                       "FROM alpha_v214_trades").fetchone()
    assert (row["entry_room_pts"], row["entry_room_level"]) == (40.0, "pdl")
    assert row["entry_cushion_pts"] is None
