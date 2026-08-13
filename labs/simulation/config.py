"""Configurable instrument universe and simulator defaults."""
from pathlib import Path

from config.labs_config import BASE_DIR


SIMULATION_DATA_DIR = BASE_DIR / "data" / "simulation" / "1min"
SIMULATION_STATE_DIR = BASE_DIR / "storage" / "simulation"
SIMULATION_DB_PATH = BASE_DIR / "storage" / "simulation.db"
ALPHAIMB_KITE_TOKEN = Path.home() / "alphaIMB" / "zerodha_access_token.json"

STARTING_CAPITAL = 1_000_000.0
SUPPORTED_TIMEFRAMES = ("1m", "5m", "15m", "1h")
SUPPORTED_SPEEDS = (1, 2, 5, 10, 30, 60)

# Weights are from NSE Indices factsheets dated 2026-07-31. This metadata is
# deliberately configuration, not execution logic, so index rebalances are a
# small auditable update. Kite tokens are resolved dynamically from symbols.
INSTRUMENTS = {
    "NIFTY": {
        "name": "NIFTY 50",
        "exchange": "NSE",
        "kite_symbol": "NIFTY 50",
        "kind": "index",
        "group": "Indices",
        "weight": None,
        "price_step": 0.05,
    },
    "HDFCBANK": {"name": "HDFC Bank", "exchange": "NSE", "kite_symbol": "HDFCBANK", "kind": "equity", "group": "NIFTY 50 Top 5", "weight": 10.27, "price_step": 0.05},
    "ICICIBANK": {"name": "ICICI Bank", "exchange": "NSE", "kite_symbol": "ICICIBANK", "kind": "equity", "group": "NIFTY 50 Top 5", "weight": 9.22, "price_step": 0.05},
    "RELIANCE": {"name": "Reliance Industries", "exchange": "NSE", "kite_symbol": "RELIANCE", "kind": "equity", "group": "NIFTY 50 Top 5", "weight": 7.92, "price_step": 0.05},
    "BHARTIARTL": {"name": "Bharti Airtel", "exchange": "NSE", "kite_symbol": "BHARTIARTL", "kind": "equity", "group": "NIFTY 50 Top 5", "weight": 5.37, "price_step": 0.05},
    "LT": {"name": "Larsen & Toubro", "exchange": "NSE", "kite_symbol": "LT", "kind": "equity", "group": "NIFTY 50 Top 5", "weight": 4.13, "price_step": 0.05},
    "DIVISLAB": {"name": "Divi's Laboratories", "exchange": "NSE", "kite_symbol": "DIVISLAB", "kind": "equity", "group": "NIFTY Next 50 Top 5", "weight": 4.04, "price_step": 0.05},
    "TVSMOTOR": {"name": "TVS Motor", "exchange": "NSE", "kite_symbol": "TVSMOTOR", "kind": "equity", "group": "NIFTY Next 50 Top 5", "weight": 4.00, "price_step": 0.05},
    "TMCV": {"name": "Tata Motors", "exchange": "NSE", "kite_symbol": "TMCV", "kind": "equity", "group": "NIFTY Next 50 Top 5", "weight": 3.60, "price_step": 0.05},
    "HAL": {"name": "Hindustan Aeronautics", "exchange": "NSE", "kite_symbol": "HAL", "kind": "equity", "group": "NIFTY Next 50 Top 5", "weight": 3.48, "price_step": 0.05},
    "ADANIPOWER": {"name": "Adani Power", "exchange": "NSE", "kite_symbol": "ADANIPOWER", "kind": "equity", "group": "NIFTY Next 50 Top 5", "weight": 3.46, "price_step": 0.05},
}


def instrument_list() -> list[dict]:
    return [{"symbol": symbol, **meta} for symbol, meta in INSTRUMENTS.items()]


def ensure_private_dirs() -> None:
    for path in (SIMULATION_DATA_DIR, SIMULATION_STATE_DIR):
        Path(path).mkdir(parents=True, exist_ok=True)
