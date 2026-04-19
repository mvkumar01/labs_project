"""
Builds the list of option instrument tokens to fetch for a given underlying + spot price.
Strike band: spot ± STRIKE_BAND_PCT, rounded to nearest strike_step.
Covers the nearest two expiries (CE + PE).
"""
import math
from datetime import date
from typing import Optional

from config.labs_config import UNDERLYINGS, STRIKE_BAND_PCT


def _round_strike(price: float, step: int) -> int:
    return round(price / step) * step


def get_strike_range(underlying: str, spot: float) -> tuple[int, int]:
    step = UNDERLYINGS[underlying]["strike_step"]
    low  = _round_strike(spot * (1 - STRIKE_BAND_PCT), step)
    high = _round_strike(spot * (1 + STRIKE_BAND_PCT), step)
    return low, high


def build_option_symbols(
    kite,
    underlying: str,
    spot: float,
    max_expiries: int = 2,
) -> list[str]:
    """
    Return a list of Kite instrument symbols (strings) for all CE+PE within the
    ±10% strike band for the nearest `max_expiries` expiries.
    Uses kite.instruments() for the exchange, cached in-process per call.
    """
    cfg      = UNDERLYINGS[underlying]
    step     = cfg["strike_step"]
    low, high = get_strike_range(underlying, spot)

    exchange = "NFO" if underlying in ("NIFTY", "BANKNIFTY") else "BFO"
    instruments = kite.instruments(exchange)

    today = date.today()
    expiries: list[date] = sorted({
        i["expiry"]
        for i in instruments
        if i["name"] == underlying and i["instrument_type"] in ("CE", "PE")
        and i["expiry"] and i["expiry"] >= today
    })
    target_expiries = set(expiries[:max_expiries])

    symbols = [
        i["tradingsymbol"]
        for i in instruments
        if i["name"] == underlying
        and i["instrument_type"] in ("CE", "PE")
        and i["expiry"] in target_expiries
        and low <= i["strike"] <= high
    ]
    return symbols
