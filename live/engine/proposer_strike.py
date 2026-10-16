"""SENSEX Proposer strike choice: Pramanaa Sentinel's |delta| >= 0.60 rule. Pure, broker-free.

Shared by the live runner (live/proposer_runner.py) and the simulated books
(labs/engine/proposer_px_tracker.py) so both pick the same contract from the same chain.

Strike choice = Pramanaa Sentinel's rule (services/sentinel-bot/order_manager.py
_resolve_itm_option_by_delta, 2026-07-04; Vipin's rule, live here from 2026-10-16): the SHALLOWEST
liquid ITM strike with |delta| >= 0.60, walking ATM -/+200 to ATM -/+500 in 100-pt steps; none
reaching it -> the deepest liquid (highest known delta); delta never computable -> the shallowest
liquid; nothing liquid -> no entry. Black-Scholes on the index spot at 7%, time to expiry in whole
days to the expiry date (at least half a day), from the chain's own last traded prices.
"""
from __future__ import annotations

import math
from datetime import date, timedelta

import pandas as pd

from market_data.expiry import expiry_sort_date, is_monthly_expiry

MIN_ENTRY_DELTA = 0.60
ITM_DISTANCE, MAX_STRIKE_DEPTH = 200, 500
RISK_FREE_RATE = 0.07


def _ncdf(x: float) -> float:
    return 0.5 * (1.0 + math.erf(x / math.sqrt(2.0)))


def _bs_price(spot, strike, T, sigma, typ, r=RISK_FREE_RATE):
    d1 = (math.log(spot / strike) + (r + 0.5 * sigma * sigma) * T) / (sigma * math.sqrt(T))
    d2 = d1 - sigma * math.sqrt(T)
    if typ == "CE":
        return spot * _ncdf(d1) - strike * math.exp(-r * T) * _ncdf(d2)
    return strike * math.exp(-r * T) * _ncdf(-d2) - spot * _ncdf(-d1)


def implied_vol(price, spot, strike, T, typ, r=RISK_FREE_RATE):
    """Sentinel's greeks.implied_volatility: 1%..500%, None below intrinsic (bisection, no scipy)."""
    if price <= 0 or spot <= 0 or strike <= 0 or T <= 0:
        return None
    intrinsic = max(0.0, spot - strike) if typ == "CE" else max(0.0, strike - spot)
    if price < intrinsic:
        return None
    lo, hi = 0.01, 5.0
    f_lo = _bs_price(spot, strike, T, lo, typ, r) - price
    f_hi = _bs_price(spot, strike, T, hi, typ, r) - price
    if f_lo * f_hi > 0:
        return None
    for _ in range(100):
        mid = 0.5 * (lo + hi)
        f_mid = _bs_price(spot, strike, T, mid, typ, r) - price
        if (f_mid > 0) == (f_lo > 0):
            lo, f_lo = mid, f_mid
        else:
            hi = mid
        if hi - lo < 1e-6:
            break
    return round(0.5 * (lo + hi), 4)


def bs_delta(spot, strike, T, sigma, typ, r=RISK_FREE_RATE):
    if not sigma or sigma <= 0 or T <= 0 or spot <= 0 or strike <= 0:
        return None
    d1 = (math.log(spot / strike) + (r + 0.5 * sigma * sigma) * T) / (sigma * math.sqrt(T))
    return round(_ncdf(d1) if typ == "CE" else _ncdf(d1) - 1, 4)


def expiry_date(code) -> date | None:
    """The day a SENSEX contract expires: weekly codes carry it; a monthly one is the month's last Thursday."""
    code = str(code).strip().upper()
    d = expiry_sort_date(code)
    if d is None or not is_monthly_expiry(code):
        return d
    return d - timedelta(days=(d.weekday() - 3) % 7)


def years_to_expiry(expiry: date | None, today: date) -> float:
    """Sentinel's _compute_tte: whole days to the expiry date, at least half a day; ~3 days if unknown."""
    if expiry is None:
        return 3 / 365.0
    days = (expiry - today).days
    return 0.0 if days < 0 else max(days / 365.0, 0.5 / 365.0)


def delta_itm_symbol(chain: pd.DataFrame, side: str, spot: float, today: date) -> tuple[str | None, int, str, dict]:
    """(tradingsymbol or None, strike, CE/PE, what was seen) under Sentinel's |delta| >= 0.60 rule."""
    typ = "CE" if side == "CALL" else "PE"
    atm = round(spot / 100.0) * 100
    sign = -1 if side == "CALL" else 1
    depths = [int(atm + sign * d) for d in range(ITM_DISTANCE, MAX_STRIKE_DEPTH + 1, 100)]
    info = {"atm": int(atm), "candidates": []}
    if chain.empty or "ltp" not in chain:
        return None, depths[0], typ, {**info, "reason": "no chain prices"}
    rows = chain[chain["option_type"] == typ]
    by_strike = {int(float(r.strike)): r for r in rows.itertuples()}
    T = years_to_expiry(expiry_date(chain["expiry"].iloc[0]), today)
    liquid, pick = [], None
    for k in depths:
        r = by_strike.get(k)
        if r is None:
            continue
        ltp = float(r.ltp) if pd.notna(r.ltp) else 0.0
        oi = getattr(r, "oi", None)
        if ltp <= 0 or (oi is not None and pd.notna(oi) and int(oi) <= 0):
            continue
        sigma = implied_vol(ltp, float(spot), k, T, typ)
        d = bs_delta(float(spot), k, T, sigma, typ) if sigma else None
        cand = (k, ltp, abs(d) if d is not None else None, str(r.tradingsymbol))
        liquid.append(cand)
        info["candidates"].append({"strike": k, "ltp": ltp, "delta": cand[2]})
        if cand[2] is not None and cand[2] >= MIN_ENTRY_DELTA:
            pick = cand
            break
    if pick is None:
        if not liquid:
            return None, depths[0], typ, {**info, "reason": "no liquid strike in ATM -/+200..500"}
        known = [c for c in liquid if c[2] is not None]
        pick = max(known, key=lambda c: c[2]) if known else liquid[0]
    return pick[3], pick[0], typ, {**info, "delta": pick[2], "ltp": pick[1], "depth": abs(pick[0] - int(atm))}
