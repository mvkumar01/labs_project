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


def _market_open(now: datetime) -> bool:
    t = now.strftime("%H:%M")
    return MARKET_OPEN <= t <= MARKET_CLOSE


def _next_minute_boundary(now: datetime) -> datetime:
    """Return the next whole-minute datetime."""
    return (now + timedelta(minutes=1)).replace(second=0, microsecond=0)


def run():
    log.info(f"Labs collector started. cwd={Path.cwd()} base={BASE_DIR}")
    kite = get_kite()

    while True:
        now        = datetime.now(IST)
        trade_date = now.strftime("%Y-%m-%d")

        if not _market_open(now):
            if now.strftime("%H:%M") > MARKET_CLOSE:
                log.info("Market closed. Collector exiting.")
                break
            wait = (_next_minute_boundary(now) - now).total_seconds()
            time.sleep(max(wait, 5))
            continue

        ts = now.replace(second=0, microsecond=0)

        for underlying in UNDERLYINGS:
            try:
                spot = collect_spot(kite, underlying, trade_date, ts)
                log.info(f"{underlying} spot={spot:.2f}")

                n = collect_options(kite, underlying, spot, trade_date, ts)
                log.info(f"{underlying} options={n} rows written")

            except Exception as exc:
                log.error(f"{underlying} collection error: {exc}", exc_info=False)

        # Sleep until next minute boundary
        elapsed = (datetime.now(IST) - now).total_seconds()
        sleep_for = max(COLLECTOR_INTERVAL_SECS - elapsed, 1)
        time.sleep(sleep_for)


if __name__ == "__main__":
    run()
