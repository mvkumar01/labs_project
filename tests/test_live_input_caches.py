"""Live runner input caches (2026-09-29 lag fix): parse once per file version.

Equivalence with the previous implementation was checked on 59 mirror days
(alpha series, ohlc_by_minute, champion_target); these pin the caching rules.
"""

from __future__ import annotations

import os

import pandas as pd

from live.engine import alpha_hybrid as AH
from live.engine import champion_inputs as CI


def test_legacy_ohlc_is_parsed_once_per_file_version(tmp_path, monkeypatch):
    analytics = tmp_path / "analytics"
    analytics.mkdir()
    path = analytics / "nifty_1min_ohlc.csv"
    path.write_text(
        "timestamp,open,high,low,close,volume\n"
        "2026-06-01T09:15:00+0530,1,2,0.5,1.5,0\n"
        "2026-06-01T09:15:00+0530,9,9,9,9,0\n"          # duplicate minute: first row wins
        "2026-06-01T09:16:00+0530,2,3,1.5,2.5,0\n"
        "2026-06-02T09:15:00+0530,5,6,4,5.5,0\n", encoding="utf-8")
    monkeypatch.setattr(CI, "ALPHA_DATA_DIR", tmp_path)
    monkeypatch.setattr(CI, "_LEGACY_OHLC_CACHE", {})
    reads = []
    real_read_csv = pd.read_csv
    monkeypatch.setattr(CI.pd, "read_csv", lambda *a, **k: reads.append(1) or real_read_csv(*a, **k))

    day = CI._legacy_ohlc_minutes("2026-06-01")
    assert day == {"09:15": (1.0, 2.0, 0.5, 1.5), "09:16": (2.0, 3.0, 1.5, 2.5)}
    assert CI._legacy_ohlc_minutes("2026-06-02") == {"09:15": (5.0, 6.0, 4.0, 5.5)}
    assert CI._legacy_ohlc_minutes("2026-09-29") == {}
    assert len(reads) == 1

    path.write_text(path.read_text(encoding="utf-8")
                    + "2026-06-02T09:16:00+0530,6,7,5,6.5,0\n", encoding="utf-8")
    os.utime(path, ns=(1, 2_000_000_000_000_000_000))  # force a new signature
    assert "09:16" in CI._legacy_ohlc_minutes("2026-06-02") and len(reads) == 2


def test_live_frame_is_parsed_once_per_write_and_callers_get_copies(monkeypatch):
    frame = pd.DataFrame({"timestamp": [1], "strike": [23000.0]})
    calls = []
    sig = {"v": ("f", 1, 10)}
    monkeypatch.setattr(AH, "_LIVE_DF_CACHE", {})
    monkeypatch.setattr(AH, "_options_source_sig", lambda d: sig["v"])
    monkeypatch.setattr(AH, "_load_live_data_uncached",
                        lambda d: calls.append(d) or frame.copy())
    a = AH._load_live_data("2026-09-29")
    a.loc[0, "strike"] = -1                              # a caller mutating its copy
    b = AH._load_live_data("2026-09-29")
    assert len(calls) == 1 and b.loc[0, "strike"] == 23000.0
    sig["v"] = ("f", 2, 20)                             # collector appended a minute
    AH._load_live_data("2026-09-29")
    assert len(calls) == 2


def test_alpha_series_matches_a_hand_computation():
    b1, b2 = pd.Timestamp("2026-09-29 09:15", tz="Asia/Kolkata"), \
        pd.Timestamp("2026-09-29 09:20", tz="Asia/Kolkata")
    snap = pd.DataFrame({
        "bucket": [b1, b1, b1, b2, b2],
        "strike": [23000, 23000, 23500, 23000, 23500],
        "type": ["pe", "ce", "pe", "ce", "pe"],
        "delta_oi": [300.0, 100.0, 999.0, 50.0, 7.0],
        "spot": [23010.0, 23011.0, 23012.0, 23020.0, 23021.0],
    })
    out = AH._compute_alpha_series(snap, "2026-09-29", 22950, 23050).set_index("timestamp")
    row1, row2 = out.loc[b1], out.loc[b2]
    assert (row1["alpha"], row1["alpha_abs"], row1["denom_alg"], row1["spot"]) == \
        (50.0, 50.0, 400.0, 23012.0)                    # (300-100)/400; last spot of the mark
    assert (row2["alpha"], row2["alpha_abs"]) == (-100.0, -100.0)
    empty = AH._compute_alpha_series(snap, "2026-09-29", 30000, 30100).set_index("timestamp")
    assert pd.isna(empty.loc[b1, "alpha"]) and empty.loc[b1, "spot"] == 23012.0
