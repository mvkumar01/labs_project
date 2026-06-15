"""R2 consistency book — wall-derived range @09:45 + VIX-scaled TP exit.

R2 is a SECOND, structurally-independent book run alongside the main RECO/Run-F
book (Alpha v2.10, §6C.2 of alphaIMB current_alpha_strategy_spec.md). It exists
for all-regime consistency: it makes money in the reverting regimes where the
main book bleeds (daily corr +0.12). Toggle-gated per user; small fixed size.

This module is PURE LOGIC + data loading — it does NOT place orders or import
the live runner. The runner consumes `r2_alpha_bars()` for entries (reusing the
main AlphaSignalEngine crossover logic on R2's range) and `r2_vix_tp_exit()` for
the R2-specific exit (VIX-scaled spot take-profit, NO stop-loss).

Spec:
  - Range: locked ONCE at the first completed 5-min bar with start >= 09:45.
    lower = argmax(ΔPE OI) strike within 600 below lock-spot,
    upper = argmax(ΔCE OI) strike within 600 above lock-spot.
    Fallback: spot ± 300 if a side has no wall. Min span 100. Rounded to 50.
    Held all day (re-deriving intraday is proven strictly worse — ATM migration).
  - Alpha: std algebraic (same formula as the main book) over the R2 range.
  - Exit: VIX-scaled spot TP, NO SL. TP = 15 pts if vix_open < 13 else 60 pts.
    (Adding a spot SL was tested and hurts in every combo — R2 losses are
    alpha-managed and small; a spot SL fires first and locks bigger losses.)
"""
from __future__ import annotations

from datetime import datetime, timezone, timedelta
from typing import Any

import pandas as pd

# Reuse the canonical data pipeline (DRY — same loaders the main book uses).
from live.engine.alpha_hybrid import (
    IST,
    SYMBOL,
    _load_live_data,
    _load_baseline,
    _prepare_snapshot_frame,
    _compute_alpha_series,
    _market_schedule,
    latest_spot_1min,  # re-exported for the runner's R2 spot-exit polling
)

# R2 parameters (spec §6C.2 / lo15_hi60 config).
R2_LOCK_HHMM = "09:45"      # lock at first completed bucket with start >= this
R2_WALL_SEARCH = 600        # search window each side of lock-spot for walls
R2_FALLBACK_HALF = 300      # spot ± this when a side has no wall
R2_MIN_SPAN = 100           # minimum upper-lower
R2_TP_LOW_VIX = 15.0        # vix_open < 13 -> TP 15
R2_TP_HIGH_VIX = 60.0       # vix_open >= 13 -> TP 60
R2_VIX_TP_SPLIT = 13.0

_R2_BARS_CACHE: dict = {}


def _r50(x: float) -> int:
    return int(round(x / 50.0) * 50)


def compute_r2_range(lock_bucket_df: pd.DataFrame, spot: float) -> tuple[int, int]:
    """Wall-derived range from a single locked bucket's ΔOI landscape.

    lock_bucket_df: rows for ONE bucket with columns [strike, type, delta_oi].
    Returns (lower, upper), both multiples of 50, span >= R2_MIN_SPAN.
    """
    pe = lock_bucket_df[lock_bucket_df["type"] == "pe"]
    ce = lock_bucket_df[lock_bucket_df["type"] == "ce"]

    below = pe[(pe["strike"] < spot) & (pe["strike"] >= spot - R2_WALL_SEARCH)]
    above = ce[(ce["strike"] > spot) & (ce["strike"] <= spot + R2_WALL_SEARCH)]

    if not below.empty and below["delta_oi"].max() > 0:
        lower = float(below.loc[below["delta_oi"].idxmax(), "strike"])
    else:
        lower = spot - R2_FALLBACK_HALF
    if not above.empty and above["delta_oi"].max() > 0:
        upper = float(above.loc[above["delta_oi"].idxmax(), "strike"])
    else:
        upper = spot + R2_FALLBACK_HALF

    lower, upper = _r50(lower), _r50(upper)
    if upper - lower < R2_MIN_SPAN:                 # widen symmetrically
        mid = (upper + lower) / 2.0
        lower = _r50(mid - R2_MIN_SPAN / 2.0)
        upper = _r50(mid + R2_MIN_SPAN / 2.0)
    return lower, upper


def r2_tp_points(vix_open: float | None) -> float:
    """VIX-scaled take-profit distance (points). NaN/None -> high-VIX TP (safe:
    wider TP banks less aggressively; never tighter than 15)."""
    if vix_open is None:
        return R2_TP_HIGH_VIX
    try:
        v = float(vix_open)
    except (TypeError, ValueError):
        return R2_TP_HIGH_VIX
    if v != v:  # NaN
        return R2_TP_HIGH_VIX
    return R2_TP_LOW_VIX if v < R2_VIX_TP_SPLIT else R2_TP_HIGH_VIX


def r2_vix_tp_exit(side: str, entry_spot: float, spot: float,
                   vix_open: float | None) -> str | None:
    """R2 exit decision: VIX-scaled spot take-profit, NO stop-loss.

    side: "call" | "put". Returns "r2_vix_tp" if the TP level is hit, else None.
    CALL profits when spot rises; PUT profits when spot falls.
    """
    tp = r2_tp_points(vix_open)
    if side == "call":
        return "r2_vix_tp" if spot >= entry_spot + tp else None
    if side == "put":
        return "r2_vix_tp" if spot <= entry_spot - tp else None
    return None


def r2_alpha_bars(trade_date: str | None = None) -> tuple[dict | None, list]:
    """All COMPLETED R2 5-min alpha bars from the lock bar onward.

    Returns (r2_state, bars). (None, []) when not yet locked / no data.
    r2_state carries the locked range + lock bar. Range is locked ONCE at the
    first completed bucket with start >= R2_LOCK_HHMM and held all day.

    COMPLETED-BAR DISCIPLINE: the in-progress bucket is excluded (same rule as
    the main book's hybrid_alpha_bars) so entries/exits fire on bar-close alpha,
    not intra-bar noise.
    """
    trade_date = trade_date or datetime.now(IST).date().isoformat()
    from config.labs_config import SHARED_LIVE_DIR
    csv_path = SHARED_LIVE_DIR / trade_date / f"{SYMBOL}_options_1min.csv"

    def _mtime(p) -> int:
        try:
            return p.stat().st_mtime_ns
        except OSError:
            return 0

    key = (trade_date, _mtime(csv_path), datetime.now(IST).strftime("%H:%M"))
    cached = _R2_BARS_CACHE.get("entry")
    if cached and cached[0] == key:
        return cached[1]

    try:
        live_df = _load_live_data(trade_date)
        baseline_df = _load_baseline(trade_date)
    except (FileNotFoundError, ValueError):
        result = (None, [])
        _R2_BARS_CACHE["entry"] = (key, result)
        return result

    snapshot_df = _prepare_snapshot_frame(live_df, baseline_df)

    # Lock bar = first bucket whose start >= 09:45.
    buckets = sorted(snapshot_df["bucket"].unique())
    lock_bucket = None
    for b in buckets:
        if pd.Timestamp(b).strftime("%H:%M") >= R2_LOCK_HHMM:
            lock_bucket = b
            break
    if lock_bucket is None:
        result = (None, [])
        _R2_BARS_CACHE["entry"] = (key, result)
        return result

    lb = snapshot_df[snapshot_df["bucket"] == lock_bucket]
    lock_spot = float(lb["spot"].iloc[-1])
    lower, upper = compute_r2_range(lb[["strike", "type", "delta_oi"]], lock_spot)

    series = _compute_alpha_series(snapshot_df, trade_date, lower, upper)
    series = series.dropna(subset=["alpha"]).copy()
    series = series[series["timestamp"] >= pd.Timestamp(lock_bucket)]
    cutoff = pd.Timestamp(datetime.now(IST)) - pd.Timedelta(minutes=5)
    series = series[series["timestamp"] <= cutoff]

    state = {
        "trade_date": trade_date, "book": "R2",
        "lock_bar": pd.Timestamp(lock_bucket).isoformat(),
        "lock_spot": lock_spot, "lower": lower, "upper": upper,
    }
    bars: list[dict] = []
    for _, row in series.iterrows():
        bars.append({
            "timestamp": row["timestamp"].isoformat(),
            "trade_date": trade_date,
            "alpha": float(row["alpha"]),        # std algebraic alpha over R2 range
            "alpha_imb": float(row["alpha"]),
            "alpha_formula": "std",
            "spot": None if pd.isna(row.get("spot")) else float(row["spot"]),
            "denom_alg": None if pd.isna(row.get("denom_alg")) else float(row["denom_alg"]),
            "book": "R2",
            "bucket": "R2",
            "tier": "R2",
            "lower": lower, "upper": upper,
            "lock_spot": lock_spot,
        })

    result = (state, bars)
    _R2_BARS_CACHE["entry"] = (key, result)
    return result


def latest_r2_alpha(trade_date: str | None = None) -> dict | None:
    """Latest COMPLETED R2 bar, or None for no-trade / not-yet-locked."""
    _, bars = r2_alpha_bars(trade_date)
    return bars[-1] if bars else None
