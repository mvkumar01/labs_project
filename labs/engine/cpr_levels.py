"""Prev-day CPR band + classic floor pivots for the Alpha-CPR paper book.

Level stack (9 levels), all from the PREVIOUS trading day's spot high/low/close:

    P  = (H + L + C) / 3          pivot
    BC = (H + L) / 2              bottom central
    TC = 2P - BC                  top central
    S1 = 2P - H       R1 = 2P - L
    S2 = P - (H - L)  R2 = P + (H - L)
    S3 = L - 2(H - P) R3 = H + 2(P - L)

TC/BC are returned unordered — the consumer picks nearest-above / nearest-below
numerically, so which of the pair sits higher does not matter.

Research reference: alphaIMB `research/experiments/2026-08-06_cpr_sl_lot_sizing`.
"""
from __future__ import annotations

from pathlib import Path


class CprInputError(RuntimeError):
    """Prev-day OHLC unavailable or unusable — the caller must not guess."""


def compute_levels(high: float, low: float, close: float) -> list[float]:
    """Return the 9 CPR/pivot levels, ascending. Pure — no I/O."""
    h, l, c = float(high), float(low), float(close)
    if not (h > 0 and l > 0 and c > 0):
        raise CprInputError(f"non-positive prev-day OHLC: h={h} l={l} c={c}")
    if h < l:
        raise CprInputError(f"prev-day high {h} below low {l}")
    p = (h + l + c) / 3.0
    bc = (h + l) / 2.0
    tc = 2.0 * p - bc
    rng = h - l
    return sorted([
        p, tc, bc,
        2.0 * p - h, p - rng, l - 2.0 * (h - p),      # S1 S2 S3
        2.0 * p - l, p + rng, h + 2.0 * (p - l),      # R1 R2 R3
    ])


def resolve_stop_target(levels, entry_spot: float, side: str,
                        min_dist: float = 0.0):
    """(stop, target) for one entry. CALL: stop below / target above; PUT mirrors.

    Levels closer than `min_dist` are skipped and the next one out is used — a
    13-point spot stop is ~8 premium points on a 350 option, inside spread and
    noise. Returns None on a side with no qualifying level; that exit then
    simply cannot fire and the position runs to the other level or EOD.
    """
    if not levels:
        return None, None
    esp = float(entry_spot)
    below = [v for v in levels if v < esp - min_dist]
    above = [v for v in levels if v > esp + min_dist]
    if str(side).lower() in ("call", "ce"):
        return (max(below) if below else None), (min(above) if above else None)
    return (min(above) if above else None), (max(below) if below else None)


def prev_session_hlc(trade_date: str, underlying: str = "NIFTY",
                     *, spot_dir: Path | None = None):
    """Prev *trading* day (high, low, close, date) from the labs spot store.

    Reads `data/live/<date>_<SYM>_spot_1min.csv`, newest session strictly
    before `trade_date`. Raises CprInputError when nothing usable is found —
    a missing prior session must fail loudly, never fall back to an older one
    (cf. the 2026-06-23 prev_close incident, which inverted a whole regime).
    """
    import pandas as pd

    if spot_dir is None:
        from config.labs_config import BASE_DIR
        spot_dir = Path(BASE_DIR) / "data" / "live"
    spot_dir = Path(spot_dir)
    suffix = f"_{underlying}_spot_1min.csv"
    dates = sorted(
        p.name[: -len(suffix)] for p in spot_dir.glob(f"*{suffix}")
        if p.name.endswith(suffix) and p.name[: -len(suffix)] < trade_date
    )
    if not dates:
        raise CprInputError(
            f"no prior {underlying} spot session before {trade_date} in {spot_dir}")
    prev = dates[-1]
    df = pd.read_csv(spot_dir / f"{prev}{suffix}")
    cols = {c.lower(): c for c in df.columns}
    try:
        hi = float(df[cols["high"]].max())
        lo = float(df[cols["low"]].min())
        cl = float(df.sort_values(cols.get("timestamp", df.columns[0]))
                   [cols["close"]].iloc[-1])
    except (KeyError, IndexError, ValueError) as exc:
        raise CprInputError(f"unusable spot file for {prev}: {exc}") from exc
    return hi, lo, cl, prev


def levels_for(trade_date: str, underlying: str = "NIFTY",
               *, spot_dir: Path | None = None):
    """(levels, prev_date) for `trade_date`. Raises CprInputError if unavailable."""
    hi, lo, cl, prev = prev_session_hlc(trade_date, underlying, spot_dir=spot_dir)
    return compute_levels(hi, lo, cl), prev
