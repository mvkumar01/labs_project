from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent.parent

UNDERLYINGS = {
    "NIFTY": {
        "index_symbol": "NSE:NIFTY 50",
        "lot_size": 65,
        "strike_step": 50,
    },
    "BANKNIFTY": {
        "index_symbol": "NSE:NIFTY BANK",
        "lot_size": 15,
        "strike_step": 100,
    },
    "SENSEX": {
        "index_symbol": "BSE:SENSEX",
        "lot_size": 20,
        "strike_step": 100,
    },
}

MARKET_OPEN  = "09:15"
MARKET_CLOSE = "15:30"
EOD_CUTOFF   = "15:25"

STRIKE_BAND_PCT = 0.10   # ±10% of spot
COLLECTOR_INTERVAL_SECS = 60

DATA_DIR    = BASE_DIR / "data" / "live"
ARCHIVE_DIR = BASE_DIR / "data" / "archive"
DB_PATH     = BASE_DIR / "storage" / "labs.db"
STATE_DIR   = BASE_DIR / "storage" / "state"
LOG_DIR     = BASE_DIR / "logs"
TOKEN_PATH  = BASE_DIR / "config" / "zerodha_token.json"

STRATEGY_TYPES = [
    "rsi_sma",
    "rsi_ema_sma",
    "ema_crossover",
    "trend_pullback",
]

ENTRY_RULE_TOKENS = [
    "rsi_below_oversold",
    "rsi_above_overbought",
    "ema_fast_above_slow",
    "ema_fast_below_slow",
    "ema_fast_crosses_above",
    "ema_fast_crosses_below",
    "spot_above_sma",
    "spot_below_sma",
]

EXIT_RULE_TOKENS = [
    "rsi_above_overbought",
    "rsi_below_oversold",
    "ema_fast_crosses_below",
    "ema_fast_crosses_above",
    "spot_below_sma",
    "spot_above_sma",
]

EXPIRY_MODES  = ["nearest_weekly", "next_weekly", "monthly"]
STRIKE_MODES  = ["atm", "itm_N", "otm_N"]
