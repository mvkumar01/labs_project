"""
Builds the list of option instrument tokens to fetch for a given underlying + spot price.
Strike band: spot ± STRIKE_BAND_PCT, rounded to nearest strike_step.
Covers the nearest two expiries plus the nearest monthly expiry (CE + PE).
"""
import math
from datetime import date
from config.labs_config import UNDERLYINGS, STRIKE_BAND_PCT
from market_data.expiry import expiry_code_from_symbol, is_monthly_expiry

# kite.instruments(exchange) returns the full exchange instrument dump (tens of
# thousands of rows) and doesn't change intraday. Cached per exchange per day so
# adding a second caller (futures) doesn't double the API load of the 60s loop.
_instrument_cache: dict[str, tuple[date, list]] = {}


def _get_instruments(kite, exchange: str) -> list:
    today = date.today()
    cached = _instrument_cache.get(exchange)
    if cached and cached[0] == today:
        return cached[1]
    data = kite.instruments(exchange)
    _instrument_cache[exchange] = (today, data)
    return data


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
    instruments = _get_instruments(kite, exchange)

    today = date.today()
    expiries: list[date] = sorted({
        i["expiry"]
        for i in instruments
        if i["name"] == underlying and i["instrument_type"] in ("CE", "PE")
        and i["expiry"] and i["expiry"] >= today
    })
    target_expiries = set(expiries[:max_expiries])

    # Capture enough depth for every supported bot mode. The nearest two dates
    # support nearest/next; monthly may be a third date early in the month.
    monthly_expiries = sorted({
        i["expiry"]
        for i in instruments
        if i["name"] == underlying
        and i["instrument_type"] in ("CE", "PE")
        and i["expiry"] and i["expiry"] >= today
        and is_monthly_expiry(
            expiry_code_from_symbol(i["tradingsymbol"], underlying) or ""
        )
    })
    if monthly_expiries:
        target_expiries.add(monthly_expiries[0])

    symbols = [
        i["tradingsymbol"]
        for i in instruments
        if i["name"] == underlying
        and i["instrument_type"] in ("CE", "PE")
        and i["expiry"] in target_expiries
        and low <= i["strike"] <= high
    ]
    return symbols


def get_current_future(kite, underlying: str) -> dict:
    """
    Return the instrument dict (tradingsymbol, instrument_token, expiry, ...) for
    the nearest-expiry future contract of `underlying`.
    Raises ValueError if no unexpired future contract is listed.
    """
    exchange = "NFO" if underlying in ("NIFTY", "BANKNIFTY") else "BFO"
    instruments = _get_instruments(kite, exchange)

    today = date.today()
    futures = sorted(
        (
            i for i in instruments
            if i["name"] == underlying
            and i["instrument_type"] == "FUT"
            and i["expiry"] and i["expiry"] >= today
        ),
        key=lambda i: i["expiry"],
    )
    if not futures:
        raise ValueError(f"No unexpired future contract found for {underlying}")
    return futures[0]
