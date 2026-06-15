"""Unit tests for the R2 book pure logic (range + VIX-scaled TP exit).

No live data needed — exercises compute_r2_range / r2_tp_points / r2_vix_tp_exit
on synthetic ΔOI landscapes. Run: python tests/test_r2_book.py
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import pandas as pd
from live.engine.r2_book import (
    compute_r2_range, r2_tp_points, r2_vix_tp_exit,
    R2_TP_LOW_VIX, R2_TP_HIGH_VIX,
)

n = 0
def ok(cond, msg):
    global n
    assert cond, f"FAIL: {msg}"
    n += 1
    print(f"  ok: {msg}")


def bucket(rows):
    return pd.DataFrame(rows, columns=["strike", "type", "delta_oi"])


# --- compute_r2_range: clear walls both sides ------------------------------
df = bucket([
    (24700, "pe", 5_000), (24800, "pe", 90_000), (24900, "pe", 10_000),  # PE wall 24800
    (25100, "ce", 8_000), (25200, "ce", 80_000), (25300, "ce", 12_000),  # CE wall 25200
])
lo, hi = compute_r2_range(df, spot=25000.0)
ok((lo, hi) == (24800, 25200), f"walls picked: {lo},{hi} (expect 24800,25200)")

# --- fallback when a side has no positive wall -----------------------------
df2 = bucket([
    (25100, "ce", 70_000), (25200, "ce", 40_000),                        # CE wall 25100
    (24800, "pe", -3_000), (24900, "pe", -1_000),                        # PE all negative -> fallback
])
lo, hi = compute_r2_range(df2, spot=25000.0)
ok(lo == 25000 - 300 and hi == 25100, f"PE fallback to spot-300: {lo},{hi}")

# --- min span enforcement (walls too close) --------------------------------
df3 = bucket([(24990, "pe", 50_000), (25010, "ce", 50_000)])
lo, hi = compute_r2_range(df3, spot=25000.0)
ok(hi - lo >= 100, f"min span enforced: {lo},{hi} span={hi-lo}")

# --- VIX-scaled TP points --------------------------------------------------
ok(r2_tp_points(11.0) == R2_TP_LOW_VIX, "vix<13 -> TP 15")
ok(r2_tp_points(13.0) == R2_TP_HIGH_VIX, "vix==13 -> TP 60")
ok(r2_tp_points(18.0) == R2_TP_HIGH_VIX, "vix>=13 -> TP 60")
ok(r2_tp_points(None) == R2_TP_HIGH_VIX, "None vix -> TP 60 (safe)")
ok(r2_tp_points(float("nan")) == R2_TP_HIGH_VIX, "NaN vix -> TP 60 (safe)")

# --- VIX-scaled TP exit, NO stop-loss --------------------------------------
# low VIX (TP=15)
ok(r2_vix_tp_exit("call", 25000, 25015, 11.0) == "r2_vix_tp", "CALL TP hit at +15 (low vix)")
ok(r2_vix_tp_exit("call", 25000, 25014, 11.0) is None, "CALL no exit below +15")
ok(r2_vix_tp_exit("call", 25000, 24900, 11.0) is None, "CALL adverse move -> NO SL exit")
ok(r2_vix_tp_exit("put", 25000, 24985, 11.0) == "r2_vix_tp", "PUT TP hit at -15 (low vix)")
ok(r2_vix_tp_exit("put", 25000, 25100, 11.0) is None, "PUT adverse move -> NO SL exit")
# high VIX (TP=60)
ok(r2_vix_tp_exit("call", 25000, 25059, 18.0) is None, "CALL no exit below +60 (high vix)")
ok(r2_vix_tp_exit("call", 25000, 25060, 18.0) == "r2_vix_tp", "CALL TP hit at +60 (high vix)")

print(f"\nAll {n} assertions passed — R2 book pure logic verified.")
