"""Build the inputs champion_sim needs, from the shared store + alphaIMB data.

Shared by the labs paper tracker and the live runner's replay-to-now decider so
both feed the champion engine identical inputs. Pure data assembly — no broker,
no order placement, no labs imports.

  adf      DataFrame[timestamp, alpha, d_pe_sum, d_ce_sum, denom, spot] over the
           cell range, std or abs-denom per use_abs (mirrors build_alpha_regime)
  ce_map   {timestamp -> {strike -> CE OI}}   (v7.7 wall checks)
  pe_map   {timestamp -> {strike -> PE OI}}   (v7.7 DN-PUT filter + v7.9 D2)
  ohlc     champion_sim.OHLC — 1-min open/high/low/close, alphaIMB
           nifty_1min_ohlc.csv where present else shared-store 1-min spot
"""
from __future__ import annotations

import tarfile

import pandas as pd

from config.labs_config import (
    ARCHIVE_DIR, DATA_DIR, SHARED_ARCHIVE_DIR, SHARED_LIVE_DIR)
from live.engine import champion_sim
from live.engine.gemini_range import build_dynamic_range_series
from live.engine.alpha_hybrid import (
    ALPHA_DATA_DIR, _load_baseline, _load_live_data, _prepare_snapshot_frame,
    _previous_trading_days)
from market_data.shared_store import load_options_frame

SYMBOL = "NIFTY"
VIX_TRAIL_CUTOFF = 17.0   # vix_open < cutoff (or missing) -> TRAIL regime

# v2.8 R13 PC400 carve-out thresholds (match v79_v281_isolation.select_alpha_source).
PC400_VIX_BAND_LO = 16.0
PC400_VIX_BAND_HI = 20.0
PC400_SGAP_UP_THRESHOLD = 100.0
PC400_SGAP_DOWN_THRESHOLD = -200.0


def pc400_in_carve_out(vix, sgap) -> bool:
    """v2.8 R13 carve-out: vix-band 16-20, OR sgap_up>100, OR sgap_down<-200.
    In carve-out -> regime+std; else -> gemini_c2+abs_denom (Run F PC400 routing)."""
    try:
        v = float(vix)
        if v == v and PC400_VIX_BAND_LO <= v < PC400_VIX_BAND_HI:
            return True
    except (TypeError, ValueError):
        pass
    return sgap > PC400_SGAP_UP_THRESHOLD or sgap < PC400_SGAP_DOWN_THRESHOLD


def alpha_source(tier, direction, vix, sgap, biggap) -> tuple[str, bool]:
    """Run F alpha routing -> (range_source, use_abs).
      PC50 gap-UP        -> regime, abs_denom
      PC400 biggap (C1)  -> regime (op+-200 range supplied), std
      PC400 non-carve    -> gemini_c2, abs_denom
      else (PC50 DN, PC250 DN, PC400 carve) -> regime, std
    """
    if tier == "PC50" and direction == "UP":
        return "regime", True
    if tier == "PC400":
        if biggap:                              # C1: regime op+-200 + std
            return "regime", False
        if not pc400_in_carve_out(vix, sgap):   # non-carve -> gemini_c2 + abs_denom
            return "gemini_c2", True
        return "regime", False                  # carve-out -> regime + std
    return "regime", False                      # PC50 DN, PC250 DN -> regime + std


def _labs_spot_ohlc(trade_date: str) -> "pd.DataFrame | None":
    """1-min index spot OHLC from the labs collector store — the single source.

    Recent sessions (≤KEEP_DAYS) live as ``data/live/<date>_<SYM>_spot_1min.csv``;
    eod_maintenance tars them into ``data/archive/<date>.tar.gz`` thereafter. The
    live CSV exists intraday, so TODAY resolves to real 1-min high/low (not flat
    spot) — which is what makes intra-bar triggers (trail / TP-SL / v2.12 entry-
    spot recovery) behave the same live as in backfill."""
    name = f"{trade_date}_{SYMBOL}_spot_1min.csv"
    live = DATA_DIR / name
    if live.is_file():
        try:
            return pd.read_csv(live)
        except (OSError, ValueError):
            return None
    tar_path = ARCHIVE_DIR / f"{trade_date}.tar.gz"
    if tar_path.is_file():
        try:
            with tarfile.open(tar_path, "r:gz") as tar:
                member = tar.extractfile(name)
                if member is not None:
                    return pd.read_csv(member)
        except (tarfile.TarError, KeyError, OSError, ValueError):
            return None
    return None


def ohlc_by_minute(trade_date: str) -> dict:
    """{'HH:MM': (open,high,low,close)} for the trade_date.

    SOURCE PRIORITY (labs is the single source of truth for OHLC, 2026-06-29):
      1. labs collector spot OHLC — ``data/live`` (incl. today, live) then the
         day's ``data/archive/<date>.tar.gz``. Real 1-min high/low.
      2. legacy alphaIMB ``nifty_1min_ohlc.csv`` — fills any minute labs lacks
         (pre-archive history before 2026-05-21).
      3. shared-store 1-min spot (open=high=low=close=spot) — last-resort gap
         fill for minutes neither source covers.

    Because the labs live CSV exists intraday, TODAY now resolves to real high/low
    rather than flat spot, so intra-bar triggers fire identically live and in
    backfill (previously today fell back to flat spot and suppressed v2.12
    re-entries / trail / TP-SL)."""
    out: dict = {}
    labs = _labs_spot_ohlc(trade_date)
    if labs is not None and not labs.empty:
        ts = pd.to_datetime(labs["timestamp"], errors="coerce")
        if getattr(ts.dt, "tz", None) is not None:
            ts = ts.dt.tz_localize(None)
        labs = labs.assign(ts=ts).dropna(subset=["ts"])
        labs = labs[labs["ts"].dt.strftime("%Y-%m-%d") == trade_date]
        for _, r in labs.iterrows():
            out[r["ts"].strftime("%H:%M")] = (
                float(r["open"]), float(r["high"]), float(r["low"]), float(r["close"]))
    try:
        oc = pd.read_csv(ALPHA_DATA_DIR / "analytics" / "nifty_1min_ohlc.csv")
        oc["ts"] = pd.to_datetime(oc["timestamp"]).dt.tz_localize(None)
        oc = oc[oc["ts"].dt.strftime("%Y-%m-%d") == trade_date]
        for _, r in oc.iterrows():
            hm = r["ts"].strftime("%H:%M")
            if hm not in out:
                out[hm] = (
                    float(r["open"]), float(r["high"]), float(r["low"]), float(r["close"]))
    except Exception:
        pass
    try:
        sdf = load_options_frame(
            SYMBOL,
            trade_date,
            live_root=SHARED_LIVE_DIR,
            archive_root=SHARED_ARCHIVE_DIR,
            columns=["timestamp", "spot"],
        )
        for ts_s, sp in sdf.groupby("timestamp")["spot"].first().items():
            hm = str(ts_s)[11:16]
            if hm and hm not in out:
                out[hm] = (float(sp), float(sp), float(sp), float(sp))
    except (FileNotFoundError, ValueError):
        pass
    return out


def prev_close(trade_date: str) -> float | None:
    """Prev-day NIFTY close: vix_history.nifty_close (prior row) if present,
    else the previous trading day's last shared-store 1-min spot."""
    try:
        v = pd.read_csv(ALPHA_DATA_DIR / "analytics" / "vix_history.csv", dtype={"date": str})
        prev = v[v["date"] < trade_date].sort_values("date")
        if not prev.empty and pd.notna(prev.iloc[-1].get("nifty_close")):
            return float(prev.iloc[-1]["nifty_close"])
    except Exception:
        pass
    for pday in _previous_trading_days(trade_date):
        try:
            prior = load_options_frame(
                SYMBOL,
                pday,
                live_root=SHARED_LIVE_DIR,
                archive_root=SHARED_ARCHIVE_DIR,
                columns=["timestamp", "spot"],
            )
            s = prior.groupby("timestamp")["spot"].first()
            if len(s):
                return float(s.iloc[-1])
        except Exception:
            continue
    return None


def _gemini_adf(snap: pd.DataFrame, use_abs: bool) -> pd.DataFrame:
    """Gemini-c2 (confirm2) dynamic-range alpha frame — faithful port of
    v79_v281_isolation.build_alpha_gemini_c2. Range is detected per-bar from the
    full OI snapshot; alpha re-computed inside it (std or abs_denom)."""
    # Keep `bucket` as the original tz-aware IST Series — .values/.to_numpy()
    # would strip the timezone and shift every bar by 5:30 (UTC), breaking the
    # ohlc HH:MM lookups and pe_map keys downstream.
    g = pd.DataFrame({
        "bucket": snap["bucket"],
        "strike": snap["strike"].astype(int),
        "type": snap["type"].astype(str),
        "delta_oi": snap["delta_oi"].astype(float),
        "spot": snap["spot"].astype(float),
    }).reset_index(drop=True)
    series = build_dynamic_range_series(g, variant="confirm2")
    if series.empty:
        return pd.DataFrame(columns=["timestamp", "alpha", "d_pe_sum", "d_ce_sum", "denom", "spot"])
    d_pe = series["d_pe"].astype(float)
    d_ce = series["d_ce"].astype(float)
    if use_abs:
        denom = (d_pe.abs() + d_ce.abs())
        alpha = ((d_pe - d_ce) * 100.0 / denom.replace(0, pd.NA)).fillna(0.0)
    else:
        denom = (d_pe + d_ce)
        alpha = series["alpha"].astype(float)
    return pd.DataFrame({
        "timestamp": series["timestamp"], "alpha": alpha.round(2),
        "d_pe_sum": d_pe, "d_ce_sum": d_ce, "denom": denom, "spot": series["spot"],
    }).sort_values("timestamp").reset_index(drop=True)


def build_sim_inputs(trade_date: str, lo: float, hi: float, use_abs: bool,
                     range_source: str = "regime"):
    """(snapshot_df, adf, ce_map, pe_map) for champion_sim from the alpha_hybrid
    pipeline the bot uses. ce_map/pe_map carry per-strike OI for the v7.7 filter
    and v7.9 D2 wall checks (keyed off the locked [lo,hi] range passed to sim()).

    range_source="regime" -> adf is build_alpha_regime over [lo,hi] (std/abs per
    use_abs). range_source="gemini_c2" -> adf is the Gemini-c2 dynamic-range alpha
    (Run F PC400 non-carve-out cell); falls back to regime if gemini is empty."""
    snap = _prepare_snapshot_frame(_load_live_data(trade_date), _load_baseline(trade_date))
    ce_map: dict = {}
    pe_map: dict = {}
    for ts, g in snap[snap["type"] == "ce"].groupby("bucket"):
        ce_map[ts] = dict(zip(g["strike"].astype(int), g["oi"].astype(float)))
    for ts, g in snap[snap["type"] == "pe"].groupby("bucket"):
        pe_map[ts] = dict(zip(g["strike"].astype(int), g["oi"].astype(float)))

    adf = None
    if range_source == "gemini_c2":
        adf = _gemini_adf(snap, use_abs)
        if adf is None or len(adf) < 2:
            adf = None  # defensive fallback to regime (matches research select_adf)
    if adf is None:
        sub = snap[(snap["strike"] >= lo) & (snap["strike"] <= hi)]
        rows = []
        for ts, g in sub.groupby("bucket"):
            d_pe = float(g.loc[g["type"] == "pe", "delta_oi"].sum())
            d_ce = float(g.loc[g["type"] == "ce", "delta_oi"].sum())
            denom = (abs(d_pe) + abs(d_ce)) if use_abs else (d_pe + d_ce)
            alpha = ((d_pe - d_ce) * 100.0 / denom) if denom else 0.0
            rows.append(dict(timestamp=ts, alpha=round(alpha, 2), d_pe_sum=d_pe,
                             d_ce_sum=d_ce, denom=denom, spot=float(g["spot"].iloc[-1])))
        adf = pd.DataFrame(rows).sort_values("timestamp").reset_index(drop=True)
    return snap, adf, ce_map, pe_map


def day_context(trade_date: str, ohlc: "champion_sim.OHLC", direction, vix):
    """Resolve (sgap, weekday, use_trail, regime) for the cell."""
    pc = prev_close(trade_date)
    day_open = ohlc.day_open()
    if day_open is not None and pc is not None:
        sgap = day_open - pc
    else:
        sgap = -1.0 if (direction or "").upper() in ("DOWN", "DN") else 1.0
    weekday = pd.Timestamp(trade_date).strftime("%a")  # Mon..Fri
    try:
        use_trail = vix is None or float(vix) != float(vix) or float(vix) < VIX_TRAIL_CUTOFF
    except (TypeError, ValueError):
        use_trail = True
    return sgap, weekday, use_trail, ("TRAIL" if use_trail else "WALL")
