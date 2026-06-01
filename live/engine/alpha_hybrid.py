"""Hybrid PC alpha reader for the live runner.

This is a local, read-only port of alphaIMB's canonical 5-minute alpha
calculation, scoped to the locked hybrid range file. It does not import the
paper engine or call broker APIs.
"""
from __future__ import annotations

import calendar
import json
import re
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import pandas as pd

from config.labs_config import MARKET_CLOSE, MARKET_OPEN, SHARED_LIVE_DIR

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

_MMM_MAP = {
    "JAN": 1, "FEB": 2, "MAR": 3, "APR": 4, "MAY": 5, "JUN": 6,
    "JUL": 7, "AUG": 8, "SEP": 9, "OCT": 10, "NOV": 11, "DEC": 12,
}


def _last_tuesday_of_month(year: int, month: int) -> date:
    for d in range(calendar.monthrange(year, month)[1], 0, -1):
        if date(year, month, d).weekday() == 1:
            return date(year, month, d)
    raise ValueError(f"no Tuesday in {year}-{month}")


def _expiry_code_to_date(code: Any) -> date | None:
    s = str(code).strip().upper()
    m = re.match(r"^(\d{2})(\d)(\d{2})$", s)
    if m:
        yy, mo, dd = m.groups()
        try:
            return date(2000 + int(yy), int(mo), int(dd))
        except ValueError:
            return None
    m = re.match(r"^(\d{2})([A-Z]{3})$", s)
    if m:
        yy, mmm = m.groups()
        if mmm not in _MMM_MAP:
            return None
        return _last_tuesday_of_month(2000 + int(yy), _MMM_MAP[mmm])
    return None


def _pick_nearest_expiry(expiries, trade_date: str) -> str | None:
    today = pd.Timestamp(trade_date).date()
    decoded = []
    for raw in expiries:
        expiry_date = _expiry_code_to_date(raw)
        if expiry_date is not None and expiry_date >= today:
            decoded.append((expiry_date, str(raw)))
    if not decoded:
        return None
    decoded.sort()
    return decoded[0][1]


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
    df = live_df.set_index("timestamp").sort_index()
    latest = (
        df.groupby([pd.Grouper(freq="5min", closed="left", label="left"), "strike", "type"])
        .last()
        .dropna(subset=["oi"])
        .reset_index()
        .rename(columns={"timestamp": "bucket"})
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
                         "alpha": None, "denom_alg": None})
            continue
        pe_delta = float(in_range.loc[in_range["type"] == "pe", "delta_oi"].sum())
        ce_delta = float(in_range.loc[in_range["type"] == "ce", "delta_oi"].sum())
        denom = pe_delta + ce_delta
        alpha = ((pe_delta - ce_delta) * 100 / denom) if denom else 0.0
        rows.append({
            "timestamp": bucket,
            "spot": float(bucket_df["spot"].iloc[-1]),
            "alpha": round(alpha, 2),
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
    state = json.loads(HYBRID_STATE_FILE.read_text(encoding="utf-8"))
    if state.get("trade_date") != trade_date:
        return None
    if not (state.get("locked") and state.get("verified_open_locked")):
        return None
    if state.get("bucket") == "SKIP" or state.get("lower") is None or state.get("upper") is None:
        return None
    return state


def latest_hybrid_alpha(trade_date: str | None = None) -> dict | None:
    """Return the latest non-null locked hybrid alpha bar, or None for no-trade."""
    trade_date = trade_date or datetime.now(IST).date().isoformat()
    state = _read_locked_hybrid_state(trade_date)
    if state is None:
        return None

    lower = float(state["lower"])
    upper = float(state["upper"])
    live_df = _load_live_data(trade_date)
    baseline_df = _load_baseline(trade_date)
    snapshot_df = _prepare_snapshot_frame(live_df, baseline_df)
    series = _compute_alpha_series(snapshot_df, trade_date, lower, upper)
    series = series.dropna(subset=["alpha"]).copy()
    if series.empty:
        return None

    row = series.iloc[-1]
    alpha = float(row["alpha"])
    return {
        "timestamp": row["timestamp"].isoformat(),
        "trade_date": trade_date,
        "alpha": alpha,
        "alpha_imb": alpha,
        "spot": None if pd.isna(row.get("spot")) else float(row["spot"]),
        "denom_alg": None if pd.isna(row.get("denom_alg")) else float(row["denom_alg"]),
        "bucket": state.get("bucket") or "PC50",
        "tier": state.get("bucket") or "PC50",
        "gap_direction": state.get("direction"),
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
    }
