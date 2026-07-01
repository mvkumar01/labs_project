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
from dataclasses import asdict, dataclass

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
MIN_VALID_VIX = 5.0
MAX_VALID_VIX = 100.0
SPOT_CONTEXT_TOLERANCE = 0.05
VIX_CONTEXT_TOLERANCE = 0.01

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


class ContextInputError(RuntimeError):
    """The session context is missing, stale, contradictory, or implausible."""


@dataclass(frozen=True)
class ResolvedDayContext:
    sgap: float
    weekday: str
    use_trail: bool
    regime: str
    previous_session_date: str
    prev_close: float
    prev_close_source: str
    open_spot: float
    open_spot_source: str
    direction: str
    vix_open: float | None
    vix_source: str

    def to_dict(self) -> dict:
        return asdict(self)


def previous_session_close(trade_date: str) -> tuple[str, float, str]:
    """Return the immediately preceding captured NIFTY session close.

    The shared market store determines both the prior session date and close.
    ``vix_history.csv`` is deliberately not a fallback: PA keeps a sparse
    rolling VIX file, so treating its last older row as yesterday caused June
    replays to use an April close and silently switch Alpha routing.
    """
    for pday in _previous_trading_days(trade_date):
        try:
            prior = load_options_frame(
                SYMBOL,
                pday,
                live_root=SHARED_LIVE_DIR,
                archive_root=SHARED_ARCHIVE_DIR,
                columns=["timestamp", "spot"],
            )
            prior = prior.copy()
            prior["timestamp"] = pd.to_datetime(
                prior["timestamp"], errors="coerce"
            )
            prior["spot"] = pd.to_numeric(prior["spot"], errors="coerce")
            prior = prior.dropna(subset=["timestamp", "spot"])
            spots = prior.groupby("timestamp")["spot"].first().sort_index()
            # Frozen single-print files are not valid market sessions.
            if len(spots) >= 2 and spots.nunique() >= 2:
                source = str(prior.attrs.get("source_path") or "shared_store")
                return pday, float(spots.iloc[-1]), source
        except Exception:
            continue
    raise ContextInputError(
        f"No preceding non-frozen shared-market session before {trade_date}"
    )


def prev_close(trade_date: str) -> float:
    """Compatibility wrapper returning the exact previous-session close."""
    return previous_session_close(trade_date)[1]


def _valid_vix(value) -> float | None:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    if not pd.notna(number) or not MIN_VALID_VIX <= number <= MAX_VALID_VIX:
        return None
    return number


def resolve_vix_open(
    trade_date: str,
    supplied_vix=None,
    *,
    supplied_source: str = "supplied",
) -> tuple[float | None, str]:
    """Resolve VIX without ever borrowing a different date's row."""
    exact_value = None
    exact_source = None
    vix_path = ALPHA_DATA_DIR / "analytics" / "vix_history.csv"
    if vix_path.exists():
        try:
            frame = pd.read_csv(vix_path, dtype={"date": str})
            exact = frame[frame["date"] == trade_date]
            if not exact.empty:
                exact_value = _valid_vix(exact.iloc[-1].get("vix_open"))
                if exact_value is None:
                    raise ContextInputError(
                        f"Invalid exact-date VIX row for {trade_date}"
                    )
                exact_source = f"vix_history:{trade_date}"
        except ContextInputError:
            raise
        except Exception as exc:
            raise ContextInputError(
                f"Unable to read exact-date VIX for {trade_date}: {exc}"
            ) from exc
    if supplied_vix is not None:
        valid = _valid_vix(supplied_vix)
        if valid is None:
            raise ContextInputError(
                f"Implausible VIX for {trade_date}: {supplied_vix!r}"
            )
        if (
            exact_value is not None
            and abs(valid - exact_value) > VIX_CONTEXT_TOLERANCE
        ):
            raise ContextInputError(
                f"VIX mismatch for {trade_date}: supplied={valid:.2f}, "
                f"exact_date={exact_value:.2f} ({exact_source})"
            )
        return valid, supplied_source
    if exact_value is not None:
        return exact_value, exact_source
    return None, "missing_exact_date"


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


def resolve_day_context(
    trade_date: str,
    ohlc: "champion_sim.OHLC",
    direction,
    vix,
    *,
    vix_source: str = "supplied",
    previous_session_date: str | None = None,
    prev_close: float | None = None,
    prev_close_source: str | None = None,
    open_spot: float | None = None,
    open_spot_source: str | None = None,
) -> ResolvedDayContext:
    """Resolve and validate immutable session context for a replay."""
    has_explicit_previous = previous_session_date is not None or prev_close is not None
    if has_explicit_previous:
        if previous_session_date is None or prev_close is None:
            raise ContextInputError(
                "Explicit previous-session context requires both date and close"
            )
        try:
            previous_ts = pd.Timestamp(previous_session_date)
            trade_ts = pd.Timestamp(trade_date)
            pc = float(prev_close)
        except (TypeError, ValueError) as exc:
            raise ContextInputError("Invalid explicit previous-session context") from exc
        if previous_ts >= trade_ts:
            raise ContextInputError(
                f"Previous session {previous_session_date} is not before {trade_date}"
            )
        if not pd.notna(pc) or pc <= 0:
            raise ContextInputError(f"Invalid explicit previous close: {prev_close!r}")
        previous_date = previous_ts.date().isoformat()
        pc_source = str(prev_close_source or "audited_override").strip()
        if not pc_source:
            raise ContextInputError("Explicit previous close requires provenance")
        observed_date, observed_pc, observed_source = previous_session_close(trade_date)
        if (
            previous_date != observed_date
            or abs(pc - observed_pc) > SPOT_CONTEXT_TOLERANCE
        ):
            raise ContextInputError(
                f"Previous-session mismatch for {trade_date}: "
                f"supplied={previous_date} close={pc:.2f}, "
                f"shared_store={observed_date} close={observed_pc:.2f} "
                f"({observed_source})"
            )
    else:
        previous_date, pc, pc_source = previous_session_close(trade_date)
    has_explicit_open = open_spot is not None or open_spot_source is not None
    if has_explicit_open and (open_spot is None or not open_spot_source):
        raise ContextInputError(
            "Explicit opening context requires both spot and provenance"
        )
    day_open = open_spot if has_explicit_open else ohlc.day_open()
    try:
        day_open = float(day_open)
    except (TypeError, ValueError) as exc:
        raise ContextInputError(
            f"Verified/opening NIFTY spot unavailable for {trade_date}"
        ) from exc
    if not pd.notna(day_open) or day_open <= 0:
        raise ContextInputError(
            f"Invalid opening NIFTY spot for {trade_date}: {day_open!r}"
        )
    observed_open = float(ohlc.day_open())
    if (
        has_explicit_open
        and abs(day_open - observed_open) > SPOT_CONTEXT_TOLERANCE
    ):
        raise ContextInputError(
            f"Opening mismatch for {trade_date}: supplied={day_open:.2f}, "
            f"session_ohlc_09:15={observed_open:.2f}"
        )
    sgap = day_open - pc
    resolved_direction = "UP" if sgap >= 0 else "DOWN"
    supplied_direction = str(direction or "").upper().replace("DN", "DOWN")
    if supplied_direction and supplied_direction != resolved_direction:
        raise ContextInputError(
            f"Gap direction mismatch for {trade_date}: locked={supplied_direction}, "
            f"computed={resolved_direction} from open={day_open:.2f} - "
            f"prev_close={pc:.2f} ({previous_date})"
        )
    resolved_vix, resolved_vix_source = resolve_vix_open(
        trade_date, vix, supplied_source=vix_source
    )
    weekday = pd.Timestamp(trade_date).strftime("%a")  # Mon..Fri
    use_trail = resolved_vix is None or resolved_vix < VIX_TRAIL_CUTOFF
    regime = "TRAIL" if use_trail else "WALL"
    return ResolvedDayContext(
        sgap=sgap,
        weekday=weekday,
        use_trail=use_trail,
        regime=regime,
        previous_session_date=previous_date,
        prev_close=pc,
        prev_close_source=pc_source,
        open_spot=day_open,
        open_spot_source=(
            str(open_spot_source) if has_explicit_open else "session_ohlc_09:15"
        ),
        direction=resolved_direction,
        vix_open=resolved_vix,
        vix_source=resolved_vix_source,
    )


def day_context(trade_date: str, ohlc: "champion_sim.OHLC", direction, vix):
    """Compatibility tuple for callers that do not need provenance."""
    context = resolve_day_context(trade_date, ohlc, direction, vix)
    return (
        context.sgap,
        context.weekday,
        context.use_trail,
        context.regime,
    )
