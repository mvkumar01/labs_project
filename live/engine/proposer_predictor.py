"""SENSEX Market Predictor - rule layers the Proposer bot reads. Pure, broker-free.

Port of the Pramanaa market-predictor rules (services/market-predictor: drift.py, intraday10m.py,
intraday5.py, symbols.py at commit 62ef7d4). The database reads of the original are replaced by
plain inputs so the same code serves the live runner, the paper book and replays:

  * spot series   - 1-min SENSEX closes (Kite historical bars, labelled by bar start), today's
                    bars plus the tail of the previous session as a warm-up seed;
  * option chain  - one snapshot of the nearest live expiry (strike, CE OI, PE OI, underlying);
  * regime        - today's daily regime (label + p_bull/p_bear/p_chop).

Layers:
  drift     - SMA50/close micro-trend (dist %, SMA50 slope % over 10 min).
  x5        - 5-class 10-min call: magnitude from realized vol / trend, sign from the micro-trend,
              short momentum, ATM PCR and the regime prior. Prints only once today's momentum is
              live (>= 7 bars today).
  regime    - Pramanaa uses an LLM over overnight news. Measured on 71 sessions its only skill was
              the overnight gap (82% on the gap, 48.6% from 09:21 to close), so this port derives
              the regime from the opening gap instead: deterministic, free, available by 09:16.
"""
from __future__ import annotations

import math
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Optional, Sequence

T1, T2 = 0.00062, 0.00151           # SENSEX 10-min |move| bands (50th / 85th pct)
CLASSES = ("strong_bear", "mild_bear", "chop", "mild_bull", "strong_bull")
DRIFT_SLOPE_MIN = 0.01              # % SMA50 slope over 10 min to call a micro-trend
MA, SLOPE_WIN = 50, 10
X5_SEED, DRIFT_SEED = 60, MA + SLOPE_WIN + 5
GAP_DECISIVE = 0.003                # |open / prev close - 1| that makes the regime decisive


@dataclass(frozen=True)
class Regime:
    label: str                      # bullish | bearish | neutral | risk_off
    p_bull: float
    p_bear: float
    p_chop: float
    confidence: float
    source: str = "gap_rule"


def _clamp(x: float, lo: float, hi: float) -> float:
    return max(lo, min(hi, x))


def seeded(prior: Sequence[tuple[datetime, float]], today: Sequence[tuple[datetime, float]],
           seed_n: int, as_of: Optional[datetime] = None) -> tuple[list[tuple[datetime, float]], int]:
    """Prior-session tail (last seed_n bars) + today's bars up to as_of. Returns (series, boundary)."""
    seed = list(prior)[-seed_n:] if seed_n > 0 else []
    rows = [(t, p) for t, p in today if as_of is None or t <= as_of]
    return seed + rows, len(seed)


# ------------------------------------------------------------------ drift ---
def spot_drift(prior, today, as_of=None, ma: int = MA, slope_win: int = SLOPE_WIN) -> dict:
    series, boundary = seeded(prior, today, ma + slope_win + 5, as_of)
    n_today = max(0, len(series) - boundary)
    if len(series) < ma + 3 or len(series) == boundary:
        return {"dist_pct": None, "slope_pct": None, "sma50": None, "close": None,
                "n": len(series), "n_today": n_today}
    closes = [p for _, p in series]
    end = series[-1][0]
    sma_now = sum(closes[-ma:]) / ma
    prev = [p for t, p in series if t <= end - timedelta(minutes=slope_win)]
    sma_prev = (sum(prev[-ma:]) / ma) if len(prev) >= ma else None
    close = closes[-1]
    return {"dist_pct": round((close - sma_now) / sma_now * 100, 3),
            "slope_pct": round((sma_now - sma_prev) / sma_now * 100, 4) if sma_prev else 0.0,
            "sma50": round(sma_now, 1), "close": close, "n": len(series), "n_today": n_today}


def drift_state(micro: dict) -> Optional[str]:
    dist, slope = micro.get("dist_pct"), micro.get("slope_pct")
    if dist is None or slope is None:
        return None
    if dist > 0 and slope > DRIFT_SLOPE_MIN:
        return "drift_up"
    if dist < 0 and slope < -DRIFT_SLOPE_MIN:
        return "drift_down"
    return "flat"


# --------------------------------------------------------------- features ---
def spot_features(prior, today, as_of=None) -> dict:
    rows, boundary = seeded(prior, today, X5_SEED, as_of)
    closes = [p for _, p in rows]
    n, n_today = len(closes), len(closes) - boundary
    if n < 2 or n_today < 1:
        return {"n": n, "n_today": n_today}

    def ret(k):
        return (closes[-1] / closes[-1 - k] - 1) if n_today > k else None

    rsi = None
    if n >= 15:
        gains = losses = 0.0
        for i in range(n - 14, n):
            d = closes[i] - closes[i - 1]
            gains += max(d, 0)
            losses += max(-d, 0)
        rsi = 100.0 if losses == 0 else 100 - 100 / (1 + (gains / 14) / (losses / 14))
    vol1m = None
    if n >= 6:
        lo = max(1, n - 30)
        rr = [closes[i] / closes[i - 1] - 1 for i in range(lo, n) if i != boundary]
        if len(rr) >= 5:
            m = sum(rr) / len(rr)
            vol1m = (sum((x - m) ** 2 for x in rr) / len(rr)) ** 0.5
    return {"n": n, "n_today": n_today, "last": closes[-1], "r5": ret(5), "r10": ret(10),
            "rsi": rsi, "vol1m": vol1m}


def chain_features(chain: Sequence[dict], underlying: float) -> Optional[dict]:
    """ATM+/-3 (7 strikes nearest the underlying) OI and PCR. chain rows: strike, ce_oi, pe_oi."""
    valid = [e for e in chain if e.get("strike") is not None]
    near = sorted(valid, key=lambda e: abs(float(e["strike"]) - underlying))[:7]
    if not near:
        return None
    ce = sum(float(e.get("ce_oi") or 0) for e in near)
    pe = sum(float(e.get("pe_oi") or 0) for e in near)
    return {"spot": underlying, "atm_pcr": (pe / ce if ce > 0 else None)}


# --------------------------------------------------------------- 5-class ---
def _sigmoid(x: float) -> float:
    return 1.0 / (1.0 + math.exp(-_clamp(x, -30, 30)))


def _magnitude_probs(expected_move: float, t1: float = T1, t2: float = T2) -> dict:
    w = t1 * 0.6
    p_strong = _sigmoid((expected_move - t2) / w)
    p_chop = _sigmoid((t1 - expected_move) / w)
    p_mild = max(0.0, 1.0 - p_strong - p_chop)
    tot = p_strong + p_chop + p_mild or 1.0
    return {"chop": p_chop / tot, "mild": p_mild / tot, "strong": p_strong / tot}


def _sign_p_up(spot: dict, chain: Optional[dict], regime: Optional[Regime], micro: Optional[dict]):
    mt = 0.0
    if micro and micro.get("dist_pct") is not None:
        mt = _clamp(micro["dist_pct"] * 2.5 + (micro.get("slope_pct") or 0.0) * 25, -1, 1)
        if micro.get("n_today") is not None:
            mt *= min(1.0, max(0.0, float(micro["n_today"])) / 60.0)
    r5 = (spot.get("r5") or 0.0) if spot else 0.0
    r10 = (spot.get("r10") or 0.0) if spot else 0.0
    mom = _clamp((0.6 * r5 + 0.4 * r10) / 0.001, -1, 1)
    pcr = (chain or {}).get("atm_pcr")
    pcr_tilt = _clamp(pcr - 1.0, -0.6, 0.6) if pcr else 0.0
    reg = _clamp(regime.p_bull - regime.p_bear, -0.5, 0.5) if regime else 0.0
    score = _clamp(0.55 * mt + 0.20 * mom + 0.15 * pcr_tilt + 0.10 * reg, -1, 1)
    return _clamp(0.5 + 0.55 * score, 0.15, 0.85), {"mt": mt, "mom": mom, "pcr_tilt": pcr_tilt, "reg": reg}


def score_x5(spot: dict, chain: Optional[dict], regime: Optional[Regime], micro: Optional[dict]):
    p_up, comp = _sign_p_up(spot, chain, regime, micro)
    p_down = 1.0 - p_up
    vol1m = spot.get("vol1m") if spot else None
    vol_move = (vol1m * math.sqrt(10)) if vol1m else T1 * 0.5
    trend_move = abs(micro["slope_pct"]) / 100.0 * 4.0 if (micro and micro.get("slope_pct")) else 0.0
    if micro and micro.get("n_today") is not None:
        trend_move *= min(1.0, max(0.0, float(micro["n_today"])) / 60.0)
    expected = max(vol_move, trend_move)
    mag = _magnitude_probs(expected)
    probs = {"strong_bear": mag["strong"] * p_down, "mild_bear": mag["mild"] * p_down,
             "chop": mag["chop"], "mild_bull": mag["mild"] * p_up, "strong_bull": mag["strong"] * p_up}
    tot = sum(probs.values()) or 1.0
    probs = {k: round(v / tot, 4) for k, v in probs.items()}
    return probs, round(max(probs.values()), 4), {**comp, "p_up": p_up, "expected_move": expected}


def predict_x5(prior, today, chain: Optional[dict], regime: Optional[Regime], as_of=None) -> Optional[dict]:
    """The 10-min 5-class print, or None during warm-up (< 10 bars, or < 7 bars today)."""
    spot = spot_features(prior, today, as_of)
    if not spot or spot.get("n", 0) < 10 or spot.get("n_today", 0) < 7:
        return None
    micro = spot_drift(prior, today, as_of)
    probs, conf, comp = score_x5(spot, chain, regime, micro)
    decision = max(probs, key=probs.get)
    return {"x5": decision, "x5_conf": conf, "probs5": probs, "components": comp,
            "spot": spot, "micro": micro}


# ---------------------------------------------------------------- regime ---
def regime_from_gap(prev_close: Optional[float], open_px: Optional[float],
                    threshold: float = GAP_DECISIVE) -> Regime:
    """Daily regime from the opening gap. Neutral when either price is missing."""
    if not prev_close or not open_px:
        return Regime("neutral", 1 / 3, 1 / 3, 1 / 3, 0.0)
    gap = open_px / prev_close - 1
    if gap >= threshold:
        return Regime("bullish", 0.50, 0.20, 0.30, round(min(0.8, 0.4 + abs(gap) * 50), 3))
    if gap <= -threshold:
        return Regime("bearish", 0.20, 0.50, 0.30, round(min(0.8, 0.4 + abs(gap) * 50), 3))
    return Regime("neutral", 0.33, 0.33, 0.34, round(1 - abs(gap) / threshold, 3))
