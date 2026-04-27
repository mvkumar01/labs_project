"""
Labs market-data collector.
Runs every 60 seconds during 09:15–15:30 IST.
Collects 1-min spot + options snapshots for NIFTY, BANKNIFTY, SENSEX.

PA scheduled task: python collector/run_collector.py
"""
import logging
import sys
import time
from datetime import datetime, timedelta
from pathlib import Path

import pytz

BASE_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BASE_DIR))

from auth.session_manager import get_kite
from collector.spot_collector import collect_spot
from collector.options_collector import collect_options
from config.labs_config import UNDERLYINGS, MARKET_OPEN, MARKET_CLOSE, COLLECTOR_INTERVAL_SECS, LOG_DIR

IST = pytz.timezone("Asia/Kolkata")
MARKET_OPEN_TIME = datetime.strptime(MARKET_OPEN, "%H:%M").time()
MARKET_CLOSE_TIME = datetime.strptime(MARKET_CLOSE, "%H:%M").time()

LOG_DIR.mkdir(parents=True, exist_ok=True)
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [collector] %(message)s",
    handlers=[
        logging.FileHandler(LOG_DIR / f"collector_{datetime.now(IST).strftime('%Y-%m-%d')}.log"),
        logging.StreamHandler(),
    ],
)
log = logging.getLogger(__name__)

# Exit after this many consecutive all-underlying auth failures so PA restarts
# the process and reloads the fresh token written by generate_token.py.
_AUTH_FAIL_LIMIT = 10
_AUTH_PHRASES = ("api_key", "access_token", "invalid token", "token exception")


def _is_auth_error(exc: Exception) -> bool:
    return any(p in str(exc).lower() for p in _AUTH_PHRASES)


def _market_open(now: datetime) -> bool:
    t = now.time()
    return MARKET_OPEN_TIME <= t <= MARKET_CLOSE_TIME


def _next_minute_boundary(now: datetime) -> datetime:
    """Return the next whole-minute datetime."""
    return (now + timedelta(minutes=1)).replace(second=0, microsecond=0)


def run():
    log.info(f"Labs collector started. cwd={Path.cwd()} base={BASE_DIR}")

    consecutive_auth_fails = 0

    while True:
        kite = get_kite()
        now        = datetime.now(IST)
        trade_date = now.strftime("%Y-%m-%d")
        market_open = _market_open(now)
        log.info(
            "loop now=%s tz=%s hms=%s MARKET_OPEN=%s MARKET_CLOSE=%s market_open=%s",
            now.isoformat(),
            now.tzinfo,
            now.strftime("%H:%M:%S"),
            MARKET_OPEN,
            MARKET_CLOSE,
            market_open,
        )

        if not market_open:
            consecutive_auth_fails = 0
            wait = max((_next_minute_boundary(now) - now).total_seconds(), 5)
            log.info(
                "Market closed. Collector sleeping %.1fs before retry.",
                wait,
            )
            time.sleep(wait)
            continue

        ts = now.replace(second=0, microsecond=0)

        auth_fails_this_cycle = 0
        for underlying in UNDERLYINGS:
            try:
                spot = collect_spot(kite, underlying, trade_date, ts)
                log.info(f"{underlying} spot={spot:.2f}")

                n = collect_options(kite, underlying, spot, trade_date, ts)
                log.info(f"{underlying} options={n} rows written")

            except Exception as exc:
                log.error(f"{underlying} collection error: {exc}", exc_info=False)
                if _is_auth_error(exc):
                    auth_fails_this_cycle += 1

        if auth_fails_this_cycle == len(UNDERLYINGS):
            consecutive_auth_fails += 1
            log.warning(
                "All underlyings failed auth (%d/%d). PA will restart on exit.",
                consecutive_auth_fails, _AUTH_FAIL_LIMIT,
            )
            if consecutive_auth_fails >= _AUTH_FAIL_LIMIT:
                log.error("Auth failed for %d consecutive cycles — exiting for PA restart.", _AUTH_FAIL_LIMIT)
                sys.exit(1)
        else:
            consecutive_auth_fails = 0

        # Sleep until next minute boundary
        elapsed = (datetime.now(IST) - now).total_seconds()
        sleep_for = max(COLLECTOR_INTERVAL_SECS - elapsed, 1)
        time.sleep(sleep_for)


if __name__ == "__main__":
    run()
