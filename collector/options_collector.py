"""
Fetches option chain quotes for an underlying and appends rows to the shared
1-min options CSV in SHARED_LIVE_DIR/<date>/<UNDERLYING>_options_1min.csv.

Canonical schema (consumed by Labs and AlphaIMB):
    timestamp, underlying, tradingsymbol, strike, option_type, expiry,
    ltp, bid, ask, oi, volume, spot
"""
import csv
from datetime import datetime
from pathlib import Path

from config.labs_config import SHARED_LIVE_DIR
from collector.instruments import build_option_symbols

_BATCH_SIZE = 500  # Kite quote API limit per call

_HEADER = [
    "timestamp", "underlying", "tradingsymbol", "strike", "option_type",
    "expiry", "ltp", "bid", "ask", "oi", "volume", "spot",
]


def _shared_csv_path(underlying: str, trade_date: str) -> Path:
    day_dir = SHARED_LIVE_DIR / trade_date
    day_dir.mkdir(parents=True, exist_ok=True)
    return day_dir / f"{underlying}_options_1min.csv"


def _ensure_header(path: Path) -> None:
    if not path.exists():
        with path.open("w", newline="") as f:
            csv.writer(f).writerow(_HEADER)


def _parse_symbol(sym: str, underlying: str):
    """
    Extract (strike, option_type, expiry_str) from a Kite tradingsymbol.
    Example: NIFTY26APR22400CE → (22400, 'CE', '26APR')
    """
    suffix   = sym[len(underlying):]        # "26APR22400CE"
    opt_type = suffix[-2:]                  # "CE" or "PE"
    i = 0
    while i < len(suffix) - 2 and not suffix[i].isdigit():
        i += 1
    expiry_s = suffix[i:i + 5]             # "26APR"
    strike_s = suffix[i + 5:-2]
    return int(strike_s) if strike_s.isdigit() else 0, opt_type, expiry_s


def collect_options(kite, underlying: str, spot: float, trade_date: str, ts: datetime) -> int:
    """
    Builds instrument list, fetches quotes in batches, appends rows to shared CSV.
    Returns number of rows written.
    """
    symbols = build_option_symbols(kite, underlying, spot)
    if not symbols:
        return 0

    exchange     = "NFO" if underlying in ("NIFTY", "BANKNIFTY") else "BFO"
    full_symbols = [f"{exchange}:{s}" for s in symbols]

    quotes: dict = {}
    for i in range(0, len(full_symbols), _BATCH_SIZE):
        quotes.update(kite.quote(full_symbols[i:i + _BATCH_SIZE]))

    path = _shared_csv_path(underlying, trade_date)
    _ensure_header(path)

    ts_str = ts.strftime("%Y-%m-%d %H:%M:%S")
    rows_written = 0

    with path.open("a", newline="") as f:
        writer = csv.writer(f)
        for full_sym, q in quotes.items():
            sym              = full_sym.split(":")[1]
            strike, opt_type, expiry_s = _parse_symbol(sym, underlying)
            depth = q.get("depth", {})
            bid   = depth.get("buy",  [{}])[0].get("price", 0)
            ask   = depth.get("sell", [{}])[0].get("price", 0)
            writer.writerow([
                ts_str, underlying, sym, strike, opt_type, expiry_s,
                q.get("last_price", 0), bid, ask,
                q.get("oi", 0), q.get("volume", 0), spot,
            ])
            rows_written += 1

    return rows_written
