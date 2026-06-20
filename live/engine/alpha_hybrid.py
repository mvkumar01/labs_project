"""Hybrid PC alpha reader for the live runner.

This is a local, read-only port of alphaIMB's canonical 5-minute alpha
calculation, scoped to the locked hybrid range file. It does not import the
paper engine or call broker APIs.
"""
from __future__ import annotations

import json
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import pandas as pd

from config.labs_config import MARKET_CLOSE, MARKET_OPEN, SHARED_LIVE_DIR
from market_data.expiry import expiry_sort_date, select_expiry_code

IST = timezone(timedelta(hours=5, minutes=30))
SYMBOL = "NIFTY"


def _alpha_base_dir() -> Path:
    pa_dir = Path("/home/mvkumar01/alphaIMB")
    if pa_dir.exists():
        return pa_dir
    local_dir = Path.home() / "alphaIMB"
    if local_dir.exists():
        return local_dir
    return Path(__file__).resolve().parents[3] / "alphaIMB"


ALPHA_BASE_DIR = _alpha_base_dir()
ALPHA_DATA_DIR = ALPHA_BASE_DIR / "data"
ALPHA_CONFIG_DIR = ALPHA_BASE_DIR / "config"
HYBRID_STATE_FILE = ALPHA_CONFIG_DIR / "hybrid_range_state.json"
PREV_DAY_DIR = ALPHA_DATA_DIR / "prev_day"

def _expiry_code_to_date(code: Any) -> date | None:
    return expiry_sort_date(code)


def _pick_nearest_expiry(expiries, trade_date: str) -> str | None:
    return select_expiry_code(expiries, trade_date, "nearest_weekly")


def _market_schedule(trade_date: str) -> pd.DatetimeIndex:
    start = pd.Timestamp(f"{trade_date} {MARKET_OPEN}", tz=IST)
    end = pd.Timestamp(f"{trade_date} {MARKET_CLOSE}", tz=IST) - pd.Timedelta(minutes=5)
    return pd.date_range(start=start, end=end, freq="5min")


def _previous_trading_days(trade_date: str, limit: int = 10) -> list[str]:
    cursor = pd.Timestamp(trade_date, tz=IST).normalize()
    days: list[str] = []
    while len(days) < limit:
        cursor -= pd.Timedelta(days=1)
        if cursor.weekday() < 5:
            days.append(cursor.strftime("%Y-%m-%d"))
    return days


def _load_live_data(trade_date: str) -> pd.DataFrame:
    path = SHARED_LIVE_DIR / trade_date / f"{SYMBOL}_options_1min.csv"
    if not path.exists():
        raise FileNotFoundError(f"Live file not found: {path}")

    df = pd.read_csv(path)
    if "option_type" in df.columns:
        df["type"] = df["option_type"].astype(str).str.lower()
    required = {"timestamp", "spot", "strike", "type", "oi"}
    missing = required.difference(df.columns)
    if missing:
        raise ValueError(f"Live file missing columns: {sorted(missing)}")

    df["timestamp"] = pd.to_datetime(df["timestamp"], errors="coerce")
    df = df.dropna(subset=["timestamp"]).copy()
    if df["timestamp"].dt.tz is None:
        df["timestamp"] = df["timestamp"].dt.tz_localize(IST)
    else:
        df["timestamp"] = df["timestamp"].dt.tz_convert(IST)

    df["type"] = df["type"].astype(str).str.lower()
    df["strike"] = pd.to_numeric(df["strike"], errors="coerce")
    df["oi"] = pd.to_numeric(df["oi"], errors="coerce")
    df["spot"] = pd.to_numeric(df["spot"], errors="coerce")
    df = df.dropna(subset=["strike", "oi", "spot"])

    if "expiry" in df.columns:
        df["expiry"] = df["expiry"].astype(str)
        nearest = _pick_nearest_expiry(df["expiry"].unique(), trade_date)
        if nearest is not None:
            df = df[df["expiry"] == nearest].copy()

    schedule = _market_schedule(trade_date)
    upper_bound = schedule[-1] + pd.Timedelta(minutes=4, seconds=59)
    df = df[(df["timestamp"] >= schedule[0]) & (df["timestamp"] <= upper_bound)]
    if df.empty:
        raise ValueError(f"No intraday rows found for {trade_date}")
    return df.sort_values("timestamp")


def _load_baseline(trade_date: str) -> pd.DataFrame:
    for prev_day in _previous_trading_days(trade_date):
        candidate = PREV_DAY_DIR / f"prev_day_oi_{SYMBOL}_{prev_day}.csv"
        if candidate.exists():
            baseline = pd.read_csv(candidate)
            baseline["type"] = baseline["type"].astype(str).str.lower()
            baseline["strike"] = pd.to_numeric(baseline["strike"], errors="coerce")
            baseline["oi"] = pd.to_numeric(baseline["oi"], errors="coerce").fillna(0)
            return baseline.dropna(subset=["strike"])[["strike", "type", "oi"]]
    combined = ALPHA_DATA_DIR / "baseline_oi.csv"
    if combined.exists():
        baseline = pd.read_csv(combined)
        if "symbol" in baseline.columns:
            baseline = baseline[baseline["symbol"] == SYMBOL].copy()
        baseline["type"] = baseline["type"].astype(str).str.lower()
        baseline["strike"] = pd.to_numeric(baseline["strike"], errors="coerce")
        baseline["oi"] = pd.to_numeric(baseline["oi"], errors="coerce").fillna(0)
        return baseline.dropna(subset=["strike"])[["strike", "type", "oi"]]
    raise FileNotFoundError(f"No baseline OI file found under {ALPHA_DATA_DIR}")


def _prepare_snapshot_frame(live_df: pd.DataFrame, baseline_df: pd.DataFrame) -> pd.DataFrame:
    # OI is a point-in-time SNAPSHOT, not an aggregate. The 5-min alpha uses the
    # OI reading AT each mark (09:15, 09:20, 09:25, …) — selection by timestamp,
    # NOT a 5-min Grouper.last(). The old .last() took the bucket's final 1-min
    # row (e.g. 09:19) and labelled it 09:15, shifting the whole series ~4 min and
    # diverging materially from the validated research backtest (2026-06-19 fix).
    df = live_df[live_df["timestamp"].dt.minute % 5 == 0].copy()
    df = df.rename(columns={"timestamp": "bucket"})
    latest = (
        df.sort_values("bucket")
        .groupby(["bucket", "strike", "type"], as_index=False)
        .last()
        .dropna(subset=["oi"])
    )
    baseline = baseline_df.rename(columns={"oi": "baseline_oi"})
    merged = latest.merge(baseline, on=["strike", "type"], how="left")
    merged["baseline_oi"] = merged["baseline_oi"].fillna(0)
    merged["delta_oi"] = merged["oi"] - merged["baseline_oi"]
    return merged


def _compute_alpha_series(snapshot_df: pd.DataFrame, trade_date: str,
                          lower: float, upper: float) -> pd.DataFrame:
    rows: list[dict[str, Any]] = []
    for bucket, bucket_df in snapshot_df.groupby("bucket"):
        in_range = bucket_df[(bucket_df["strike"] >= lower) & (bucket_df["strike"] <= upper)]
        if in_range.empty:
            rows.append({"timestamp": bucket, "spot": float(bucket_df["spot"].iloc[-1]),
                         "alpha": None, "alpha_abs": None, "denom_alg": None})
            continue
        pe_delta = float(in_range.loc[in_range["type"] == "pe", "delta_oi"].sum())
        ce_delta = float(in_range.loc[in_range["type"] == "ce", "delta_oi"].sum())
        denom = pe_delta + ce_delta
        alpha = ((pe_delta - ce_delta) * 100 / denom) if denom else 0.0
        # v2.8.1 abs-denom sibling (bounded [-100, +100]) — routed per tier
        # in hybrid_alpha_bars; the std algebraic alpha above is unchanged.
        denom_abs = abs(pe_delta) + abs(ce_delta)
        alpha_abs = ((pe_delta - ce_delta) * 100 / denom_abs) if denom_abs else 0.0
        rows.append({
            "timestamp": bucket,
            "spot": float(bucket_df["spot"].iloc[-1]),
            "alpha": round(alpha, 2),
            "alpha_abs": round(alpha_abs, 2),
            "denom_alg": denom,
        })
    series = pd.DataFrame(rows)
    if series.empty:
        series = pd.DataFrame({"timestamp": _market_schedule(trade_date)})
    series["timestamp"] = pd.to_datetime(series["timestamp"])
    series = series.set_index("timestamp").sort_index()
    series = series.reindex(_market_schedule(trade_date))
    return series.reset_index().rename(columns={"index": "timestamp"})


def _read_locked_hybrid_state(trade_date: str) -> dict | None:
    if not HYBRID_STATE_FILE.exists():
        return None
    # utf-8-sig: a BOM here would otherwise raise and silently no-trade the
    # whole day (same failure class as the live_env.json incident).
    state = json.loads(HYBRID_STATE_FILE.read_text(encoding="utf-8-sig"))
    if state.get("trade_date") != trade_date:
        return None
    if not (state.get("locked") and state.get("verified_open_locked")):
        return None
    if state.get("bucket") == "SKIP" or state.get("lower") is None or state.get("upper") is None:
        return None
    return state


# Single-entry caches. The runner polls every ~2s per connection; recomputing
# the whole pandas pipeline each time is wasted CPU. Keyed on file mtimes +
# wall-clock minute (the completed-bar cutoff depends on "now").
_BARS_CACHE: dict = {}
_SPOT_CACHE: dict = {}


def hybrid_alpha_bars(trade_date: str | None = None) -> tuple[dict | None, list]:
    """All COMPLETED 5-min hybrid alpha bars for the day, oldest -> newest.

    Returns (state, bars). (None, []) on no-trade (SKIP / not locked / no data).

    COMPLETED-BAR DISCIPLINE (2026-06 audit fix): the in-progress 5-min bucket
    is EXCLUDED. It mutates as 1-min rows land, which previously made the
    runner re-evaluate the same bar on every mutation — entries/exits could
    fire on intra-bar alpha noise that does not exist at bar close, diverging
    from the validated backtest semantics. A bucket is included only once its
    window has fully elapsed (bucket_start + 5min <= now IST).

    ALPHA ROUTING (v2.8.1 champion parity): on PC50 gap-UP days the bar's
    `alpha` is the ABS-DENOM value (regime range, bounded [-100, +100]); all
    other cells keep the std algebraic alpha. `alpha_imb` always carries the
    std value, `denom_alg` the algebraic denominator (v7.8 guard input).
    NOTE: the general PC400 non-carve-out Gemini c2 cell is NOT reproducible
    from the locked static range file — it degrades to regime range + std alpha
    by design. EXCEPTION (v2.10 C1): on PC400 gap-DN |sgap|>=250 days, alphaIMB's
    09:45 wall-lock writer writes the wall-lock lower/upper PLUS a
    `pc400_v210_biggap` tag into the locked state; when present, this reader
    uses that range with abs-denom alpha (full C1). Absent tag -> unchanged.
    """
    trade_date = trade_date or datetime.now(IST).date().isoformat()
    csv_path = SHARED_LIVE_DIR / trade_date / f"{SYMBOL}_options_1min.csv"

    def _mtime(p: Path) -> int:
        try:
            return p.stat().st_mtime_ns
        except OSError:
            return 0

    key = (trade_date, _mtime(csv_path), _mtime(HYBRID_STATE_FILE),
           datetime.now(IST).strftime("%H:%M"))
    cached = _BARS_CACHE.get("entry")
    if cached and cached[0] == key:
        return cached[1]

    state = _read_locked_hybrid_state(trade_date)
    if state is None:
        result = (None, [])
        _BARS_CACHE["entry"] = (key, result)
        return result

    lower = float(state["lower"])
    upper = float(state["upper"])
    live_df = _load_live_data(trade_date)
    baseline_df = _load_baseline(trade_date)
    snapshot_df = _prepare_snapshot_frame(live_df, baseline_df)
    series = _compute_alpha_series(snapshot_df, trade_date, lower, upper)
    series = series.dropna(subset=["alpha"]).copy()

    # Completed windows only: bucket_start + 5min <= now.
    cutoff = pd.Timestamp(datetime.now(IST)) - pd.Timedelta(minutes=5)
    series = series[series["timestamp"] <= cutoff]

    bucket = state.get("bucket") or "PC50"
    direction = state.get("direction")
    # v2.8.1 cell: PC50 gap-UP uses abs-denom. v2.10 C1: PC400 gap-DN big-gap
    # (|sgap|>=250) Gemini wall-lock cell also uses abs-denom. The cell is
    # signalled by `pc400_v210_biggap` in the locked state, written by
    # alphaIMB's 09:45 wall-lock writer together with the wall-lock lower/upper.
    # If that tag is absent (writer not yet deployed) this is False -> behaviour
    # is exactly as before (PC400 degrades to regime+std, per the NOTE above).
    pc400_v210_biggap = bool(state.get("pc400_v210_biggap"))
    use_abs = (bucket == "PC50" and direction == "UP") or pc400_v210_biggap

    bars: list[dict] = []
    for _, row in series.iterrows():
        alpha_std = float(row["alpha"])
        alpha_abs = None if pd.isna(row.get("alpha_abs")) else float(row["alpha_abs"])
        alpha = alpha_abs if (use_abs and alpha_abs is not None) else alpha_std
        bars.append({
            "timestamp": row["timestamp"].isoformat(),
            "trade_date": trade_date,
            "alpha": alpha,
            "alpha_imb": alpha_std,
            "alpha_formula": "abs_denom" if (use_abs and alpha_abs is not None) else "std",
            "spot": None if pd.isna(row.get("spot")) else float(row["spot"]),
            "denom_alg": None if pd.isna(row.get("denom_alg")) else float(row["denom_alg"]),
            "bucket": bucket,
            "tier": bucket,
            "gap_direction": direction,
            # 09:15 locked VIX — v2.9.1 overlay gate + PC400 regime selection.
            "vix_at_open": state.get("vix_at_open"),
            "lower": lower,
            "upper": upper,
            "range_state": {
                "verified_open": state.get("verified_open"),
                "gap_pts": state.get("gap_pts"),
                "base_bucket": state.get("base_bucket"),
                "base_width": state.get("base_width"),
                "overlay_applied": state.get("overlay_applied"),
            },
            "pc50_range_source": state.get("pc50_range_source"),
            "pc250_range_source": state.get("pc250_range_source"),
            "pc400_range_source": state.get("pc400_range_source"),
            "pc400_in_carve_out": state.get("pc400_in_carve_out"),
            "pc400_carve_out_reason": state.get("pc400_carve_out_reason"),
            "pc400_v210_biggap": pc400_v210_biggap,   # v2.10 C1 cell
        })

    result = (state, bars)
    _BARS_CACHE["entry"] = (key, result)
    return result


def latest_hybrid_alpha(trade_date: str | None = None) -> dict | None:
    """Return the latest COMPLETED locked hybrid alpha bar, or None for no-trade."""
    _, bars = hybrid_alpha_bars(trade_date)
    return bars[-1] if bars else None


def latest_spot_1min(trade_date: str | None = None) -> float | None:
    """Latest 1-min NIFTY spot from the shared live CSV (collector cadence).

    Used by the runner's spot-reference exits (trail / v7.11 / PC250 spot
    TP-SL) which need fresher spot than the 5-min completed bar. Cached by
    file mtime so the per-2s poll loop costs one parse per collector write.
    """
    trade_date = trade_date or datetime.now(IST).date().isoformat()
    path = SHARED_LIVE_DIR / trade_date / f"{SYMBOL}_options_1min.csv"
    if not path.exists():
        return None
    try:
        st = path.stat()
    except OSError:
        return None
    key = (str(path), st.st_mtime_ns, st.st_size)
    cached = _SPOT_CACHE.get("entry")
    if cached and cached[0] == key:
        return cached[1]
    try:
        df = pd.read_csv(path, usecols=["spot"])
        spots = pd.to_numeric(df["spot"], errors="coerce").dropna()
        value = float(spots.iloc[-1]) if not spots.empty else None
    except Exception:
        value = None
    _SPOT_CACHE["entry"] = (key, value)
    return value
