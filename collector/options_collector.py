"""
Fetches option chain quotes for an underlying and appends rows to the options 1-min CSV.
"""
import csv
from datetime import datetime
from pathlib import Path

from config.labs_config import DATA_DIR
from collector.instruments import build_option_symbols

_BATCH_SIZE = 500  # Kite quote API limit per call


def _options_csv_path(underlying: str, trade_date: str) -> Path:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    return DATA_DIR / f"{trade_date}_{underlying}_options_1min.csv"


def _ensure_header(path: Path) -> None:
    if not path.exists():
        with path.open("w", newline="") as f:
            csv.writer(f).writerow([
                "timestamp", "symbol", "strike", "option_type", "expiry",
                "ltp", "bid", "ask", "oi", "volume", "underlying_ltp",
            ])


def _parse_symbol(sym: str, underlying: str):
    """Extract strike and option_type from a Kite tradingsymbol. Returns (strike, option_type, expiry_str)."""
    # Kite format: NIFTY26APR22400CE  →  underlying + YYMON + strike + CE/PE
    # Parse by stripping underlying prefix, last 2 chars = option_type, middle = strike, rest = expiry
    suffix    = sym[len(underlying):]          # "26APR22400CE"
    opt_type  = suffix[-2:]                    # "CE"
    strike_s  = ""
    expiry_s  = ""
    # split at first digit run after alpha block
    i = 0
    while i < len(suffix) - 2 and not suffix[i].isdigit():
        i += 1
    # i points to first digit (start of year "26")
    # year (2 digits) + month (3 alpha) = 5 chars
    expiry_s = suffix[i:i + 5]
    strike_s = suffix[i + 5:-2]
    return int(strike_s) if strike_s.isdigit() else 0, opt_type, expiry_s


def collect_options(kite, underlying: str, spot: float, trade_date: str, ts: datetime) -> int:
    """
    Builds instrument list, fetches quotes in batches, appends rows to options CSV.
    Returns number of rows written.
    """
    symbols = build_option_symbols(kite, underlying, spot)
    if not symbols:
        return 0

    exchange = "NFO" if underlying in ("NIFTY", "BANKNIFTY") else "BFO"
    full_symbols = [f"{exchange}:{s}" for s in symbols]

    quotes = {}
    for i in range(0, len(full_symbols), _BATCH_SIZE):
        batch = full_symbols[i:i + _BATCH_SIZE]
        quotes.update(kite.quote(batch))

    path = _options_csv_path(underlying, trade_date)
    _ensure_header(path)

    ts_str = ts.strftime("%Y-%m-%d %H:%M:%S")
    rows_written = 0

    with path.open("a", newline="") as f:
        writer = csv.writer(f)
        for full_sym, q in quotes.items():
            sym = full_sym.split(":")[1]
            strike, opt_type, expiry_s = _parse_symbol(sym, underlying)
            depth = q.get("depth", {})
            bid   = depth.get("buy",  [{}])[0].get("price", 0)
            ask   = depth.get("sell", [{}])[0].get("price", 0)
            writer.writerow([
                ts_str, sym, strike, opt_type, expiry_s,
                q.get("last_price", 0),
                bid, ask,
                q.get("oi", 0),
                q.get("volume", 0),
                spot,
            ])
            rows_written += 1

    return rows_written
