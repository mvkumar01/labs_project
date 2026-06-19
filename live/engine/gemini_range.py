# Promoted from research/experiments/2026-05-21_gemini_dynamic_range/
# for Alpha v2.7 Gemini dynamic-range pipeline integration
# (2026-05-24). Research source preserved unchanged at original
# path per POLICY §9. If logic evolves here, update the research
# copy too — drift between the two is a documentation defect.
"""
gemini_range_5min.py
--------------------
Per-5-min Gemini dynamic-range + alpha series generator.

Differences from the original Downloads/Gemini_Detector_V2.py:

  Old: one verdict at 09:25 using baseline OI + opening spot.
  New: a (timestamp, lower, upper, alpha, ...) row at EVERY completed
       5-min bar, where the range is recomputed using:

         * current_spot at this bar
         * live OI snapshot at this bar
         * baseline OI = prev day EOD
         * delta_oi = live - baseline
         * range discovery uses POSITIVE delta clusters only
           (fresh OI built today)
         * alpha is computed inside the chosen range using SIGNED delta_oi

  Smoothing variants:
    A. raw       — recompute independently every bar
    B. confirm2  — only adopt a new range if it persists for 2 bars
    C. cap100    — limit centre shift to <=100 pts vs the prior bar's centre

This file is import-safe (no I/O on import). The public API is:

    build_dynamic_range_series(snapshot_df, opening_spot, variant="raw")
        -> DataFrame[timestamp, centre, lower, upper, d_pe, d_ce, alpha]

`snapshot_df` is the broker-5-min snapshot produced by
nifty_range_chart_payload.prepare_snapshot_frame() — columns include
bucket, strike, type, oi, baseline_oi, delta_oi, spot.
"""
from __future__ import annotations

from typing import Literal

import pandas as pd


Variant = Literal["raw", "confirm2", "cap100"]


# ---------------------------------------------------------------- helpers
def _round_to_50(x: float) -> int:
    return int(round(x / 50.0) * 50)


def _round_to_100_floor(x: float) -> int:
    return int(x // 100 * 100)


def _round_to_100_ceil(x: float) -> int:
    return int((x // 100 + 1) * 100)


# ---------------------------------------------------------------- single-bar detector
def determine_dynamic_range_from_delta(
    bucket_df: pd.DataFrame,
    current_spot: float,
    near_window: int = 500,
    min_width: int = 300,
) -> dict:
    """One bar's worth of range detection.

    bucket_df: rows for one 5-min bucket from a prepared snapshot frame.
               Required columns: strike, type ('ce'/'pe'), delta_oi.
    current_spot: float — the bar's spot.

    Range discovery uses POSITIVE delta_oi clusters only (where fresh
    OI has been built today). PE positive cluster -> support / lower
    bound; CE positive cluster -> resistance / upper bound. Falls back
    to spot +/- min_width if nothing positive found near spot.

    Returns dict with: centre, weighted_centre, lower, upper,
    pe_reference_strikes (list[int]), ce_reference_strikes (list[int]).
    """
    df = bucket_df.copy()
    df["type"] = df["type"].astype(str).str.lower()
    atm = _round_to_50(current_spot)

    near = df[(df["strike"] >= current_spot - near_window) &
              (df["strike"] <= current_spot + near_window)].copy()

    # Positive delta only for wall/centre discovery
    pos = near[near["delta_oi"] > 0].copy()

    if pos.empty:
        # Fallback: ATM-anchored band, 100-pt-clean
        lower = _round_to_100_floor(atm - min_width)
        upper = _round_to_100_ceil(atm + min_width)
        return {
            "centre": atm,
            "weighted_centre": atm,
            "lower": lower,
            "upper": upper,
            "pe_reference_strikes": [],
            "ce_reference_strikes": [],
        }

    # Weighted centre on positive delta OI
    strike_pos = pos.groupby("strike", as_index=False)["delta_oi"].sum()
    total = strike_pos["delta_oi"].sum()
    if total > 0:
        weighted_raw = (strike_pos["strike"] * strike_pos["delta_oi"]).sum() / total
        weighted = _round_to_50(weighted_raw)
    else:
        weighted = atm

    # Hybrid centre: keep ATM if positive-delta centre is close; else split halfway
    if abs(weighted - atm) >= 100:
        centre = _round_to_50((atm + weighted) / 2.0)
    else:
        centre = atm

    # Top 3 PE / CE positive-delta strikes
    pe_pos = pos[pos["type"] == "pe"].sort_values("delta_oi", ascending=False).head(3)
    ce_pos = pos[pos["type"] == "ce"].sort_values("delta_oi", ascending=False).head(3)
    pe_strikes = sorted(int(s) for s in pe_pos["strike"].tolist())
    ce_strikes = sorted(int(s) for s in ce_pos["strike"].tolist())

    # Minimum width around centre
    min_lower = centre - min_width
    min_upper = centre + min_width

    # Expand outward using positive-delta clusters, but never inward of ATM
    pe_ref = min(pe_strikes) if pe_strikes else min_lower
    ce_ref = max(ce_strikes) if ce_strikes else min_upper

    lower_raw = min(pe_ref, min_lower)
    upper_raw = max(ce_ref, min_upper)

    lower = _round_to_100_floor(lower_raw)
    upper = _round_to_100_ceil(upper_raw)

    return {
        "centre": int(centre),
        "weighted_centre": int(weighted),
        "lower": int(lower),
        "upper": int(upper),
        "pe_reference_strikes": pe_strikes,
        "ce_reference_strikes": ce_strikes,
    }


# ---------------------------------------------------------------- alpha inside bar-local range
def _alpha_in_range(bucket_df: pd.DataFrame, lower: int, upper: int) -> tuple[float, float, float]:
    """Signed-delta alpha across [lower, upper]. Returns (d_pe, d_ce, alpha)."""
    in_range = bucket_df[(bucket_df["strike"] >= lower) & (bucket_df["strike"] <= upper)]
    if in_range.empty:
        return 0.0, 0.0, 0.0
    d_pe = float(in_range.loc[in_range["type"] == "pe", "delta_oi"].sum())
    d_ce = float(in_range.loc[in_range["type"] == "ce", "delta_oi"].sum())
    denom = d_pe + d_ce
    alpha = ((d_pe - d_ce) * 100.0 / denom) if denom else 0.0
    return d_pe, d_ce, alpha


# ---------------------------------------------------------------- smoothing
def _smooth_raw(raw_ranges: list[dict]) -> list[dict]:
    return raw_ranges


def _smooth_confirm2(raw_ranges: list[dict]) -> list[dict]:
    """A new (lower, upper) is only adopted when the SAME pair is seen
    for 2 consecutive bars. Until confirmation, hold the prior active range.
    The very first bar establishes the active range as-is."""
    out: list[dict] = []
    active = None
    pending = None
    pending_count = 0
    for r in raw_ranges:
        cand = (r["lower"], r["upper"])
        if active is None:
            active = cand
        else:
            if cand == active:
                pending = None
                pending_count = 0
            elif cand == pending:
                pending_count += 1
                if pending_count >= 1:  # second consecutive bar (1 prior + this)
                    active = cand
                    pending = None
                    pending_count = 0
            else:
                pending = cand
                pending_count = 0
        merged = dict(r)
        merged["lower"], merged["upper"] = active
        out.append(merged)
    return out


def _smooth_cap100(raw_ranges: list[dict]) -> list[dict]:
    """Limit the centre shift between consecutive bars to <=100 strike pts.
    Width is preserved from the new detection; only centre is throttled."""
    out: list[dict] = []
    prev_centre = None
    for r in raw_ranges:
        new_centre = (r["lower"] + r["upper"]) / 2.0
        width_half = (r["upper"] - r["lower"]) / 2.0
        if prev_centre is None:
            eff_centre = new_centre
        else:
            shift = new_centre - prev_centre
            if shift > 100:
                eff_centre = prev_centre + 100
            elif shift < -100:
                eff_centre = prev_centre - 100
            else:
                eff_centre = new_centre
        eff_centre = _round_to_50(eff_centre)
        lower = _round_to_100_floor(eff_centre - width_half)
        upper = _round_to_100_ceil(eff_centre + width_half)
        merged = dict(r)
        merged["lower"] = lower
        merged["upper"] = upper
        merged["centre"] = int(eff_centre)
        prev_centre = eff_centre
        out.append(merged)
    return out


_SMOOTHERS = {
    "raw": _smooth_raw,
    "confirm2": _smooth_confirm2,
    "cap100": _smooth_cap100,
}


# ---------------------------------------------------------------- public API
def build_dynamic_range_series(
    snapshot_df: pd.DataFrame,
    opening_spot: float | None = None,
    variant: Variant = "raw",
    near_window: int = 500,
    min_width: int = 300,
) -> pd.DataFrame:
    """For every 5-min bucket in snapshot_df, detect a dynamic Gemini
    range from positive ΔOI and compute alpha inside it using signed ΔOI.

    Apply the requested smoothing variant to the (lower, upper) sequence,
    then RE-COMPUTE alpha inside the smoothed range so each bar's alpha
    is consistent with its reported range.

    Returns one row per bucket with columns:
        timestamp, spot, centre, lower, upper, d_pe, d_ce, alpha,
        raw_lower, raw_upper, raw_alpha
    """
    if variant not in _SMOOTHERS:
        raise ValueError(f"variant must be one of {list(_SMOOTHERS)}; got {variant!r}")

    df = snapshot_df.copy()
    if df.empty:
        return pd.DataFrame(columns=[
            "timestamp", "spot", "centre", "lower", "upper",
            "d_pe", "d_ce", "alpha", "raw_lower", "raw_upper", "raw_alpha",
        ])

    buckets_sorted = sorted(df["bucket"].unique())
    raw_rows: list[dict] = []
    bucket_frames: dict = {}

    for bucket in buckets_sorted:
        bucket_df = df[df["bucket"] == bucket]
        if bucket_df.empty:
            continue
        bucket_frames[bucket] = bucket_df
        # current spot = bar's spot (use last as bucket label is left-edge)
        current_spot = float(bucket_df["spot"].iloc[-1])
        range_info = determine_dynamic_range_from_delta(
            bucket_df, current_spot,
            near_window=near_window, min_width=min_width,
        )
        d_pe, d_ce, alpha = _alpha_in_range(
            bucket_df, range_info["lower"], range_info["upper"]
        )
        raw_rows.append({
            "timestamp": bucket,
            "spot": current_spot,
            "centre": range_info["centre"],
            "lower": range_info["lower"],
            "upper": range_info["upper"],
            "d_pe": d_pe,
            "d_ce": d_ce,
            "alpha": round(alpha, 2),
        })

    if not raw_rows:
        return pd.DataFrame(columns=[
            "timestamp", "spot", "centre", "lower", "upper",
            "d_pe", "d_ce", "alpha", "raw_lower", "raw_upper", "raw_alpha",
        ])

    smoothed = _SMOOTHERS[variant](raw_rows)

    # Recompute alpha inside the smoothed range so reported alpha matches range
    out_rows: list[dict] = []
    for sr, rr in zip(smoothed, raw_rows):
        bucket = sr["timestamp"]
        b_df = bucket_frames[bucket]
        d_pe, d_ce, alpha = _alpha_in_range(b_df, sr["lower"], sr["upper"])
        out_rows.append({
            "timestamp": bucket,
            "spot": sr["spot"],
            "centre": sr.get("centre", (sr["lower"] + sr["upper"]) // 2),
            "lower": sr["lower"],
            "upper": sr["upper"],
            "d_pe": d_pe,
            "d_ce": d_ce,
            "alpha": round(alpha, 2),
            "raw_lower": rr["lower"],
            "raw_upper": rr["upper"],
            "raw_alpha": rr["alpha"],
        })

    out = pd.DataFrame(out_rows)
    out["timestamp"] = pd.to_datetime(out["timestamp"])
    return out.sort_values("timestamp").reset_index(drop=True)
