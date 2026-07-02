"""
Labs market-data collector.
Runs every 60 seconds during 09:15–15:30 IST.
Collects 1-min spot + futures + options snapshots for NIFTY, BANKNIFTY, SENSEX.

PA always-on task: python pa_run_collector.py
Auth recovery: after AUTH_FAIL_LIMIT consecutive all-underlying auth failures,
exits with code 1 so PythonAnywhere restarts the process and reloads the
fresh token written by the 08:55 IST scheduled task.
"""
import json
import logging
import sys
import time
from datetime import datetime, timedelta
from pathlib import Path

import pytz

BASE_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BASE_DIR))

import auth.session_manager as session_manager
from collector.spot_collector import MarketSessionClosed, collect_spot
from collector.options_collector import collect_options
from collector.futures_collector import collect_futures
from config.labs_config import (
    UNDERLYINGS, MARKET_OPEN, MARKET_CLOSE,
    COLLECTOR_INTERVAL_SECS, LOG_DIR,
)
IST = pytz.timezone("Asia/Kolkata")
MARKET_OPEN_TIME  = datetime.strptime(MARKET_OPEN,  "%H:%M").time()
MARKET_CLOSE_TIME = datetime.strptime(MARKET_CLOSE, "%H:%M").time()

# Exit after this many consecutive cycles where every underlying fails auth.
# PA always-on restarts automatically; the fresh process reloads the token.
AUTH_FAIL_LIMIT = 3

# Zerodha error strings that identify an invalid/expired token.
_AUTH_PHRASES = ("api_key", "access_token", "invalid token", "token exception")

LOG_DIR.mkdir(parents=True, exist_ok=True)
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [collector] %(message)s",
    handlers=[
        logging.FileHandler(
            LOG_DIR / f"collector_{datetime.now(IST).strftime('%Y-%m-%d')}.log"
        ),
        logging.StreamHandler(),
    ],
)
log = logging.getLogger(__name__)


# ── helpers ──────────────────────────────────────────────────────────────────

def _is_auth_error(exc: Exception) -> bool:
    return any(p in str(exc).lower() for p in _AUTH_PHRASES)


def _market_open(now: datetime) -> bool:
    t = now.replace(tzinfo=None).time()
    return now.weekday() < 5 and MARKET_OPEN_TIME <= t <= MARKET_CLOSE_TIME


def _next_minute_boundary(now: datetime) -> datetime:
    return (now + timedelta(minutes=1)).replace(second=0, microsecond=0)


def _log_token_info() -> None:
    """Log the date embedded in the current token file for audit."""
    try:
        token_path = BASE_DIR / "config" / "zerodha_token.json"
        data = json.loads(token_path.read_text())
        generated_at = data.get("generated_at", "unknown")
        log.info("Token file date: %s", generated_at)
    except Exception as exc:
        log.warning("Could not read token file for audit: %s", exc)


# ── main loop ─────────────────────────────────────────────────────────────────

def run() -> None:
    log.info("Labs collector started. cwd=%s base=%s", Path.cwd(), BASE_DIR)

    # Always start with a fresh token read — never inherit a cached/stale kite.
    session_manager.reset()
    _log_token_info()

    consecutive_auth_fails = 0

    while True:
        now        = datetime.now(IST)
        trade_date = now.strftime("%Y-%m-%d")
        is_open    = _market_open(now)

        log.info(
            "loop hms=%s market_open=%s auth_fail_streak=%d/%d",
            now.strftime("%H:%M:%S"), is_open,
            consecutive_auth_fails, AUTH_FAIL_LIMIT,
        )

        if not is_open:
            consecutive_auth_fails = 0
            wait = max((_next_minute_boundary(now) - now).total_seconds(), 5)
            log.info("Market closed — sleeping %.0fs.", wait)
            time.sleep(wait)
            continue

        # get_kite() auto-reloads if the token file has changed on disk.
        try:
            kite = session_manager.get_kite()
        except Exception as exc:
            log.error("Failed to load kite session: %s", exc)
            time.sleep(60)
            continue

        ts = now.replace(second=0, microsecond=0)
        auth_fails_this_cycle = 0
        successes_this_cycle  = 0
        session_closed = False

        for underlying in UNDERLYINGS:
            try:
                spot = collect_spot(kite, underlying, trade_date, ts)
                n    = collect_options(kite, underlying, spot, trade_date, ts)

                # Isolated: a futures-side failure (e.g. no listed contract)
                # must not block the spot/options success already captured above.
                try:
                    fut_written = collect_futures(kite, underlying, trade_date, ts)
                except Exception as fut_exc:
                    fut_written = False
                    log.error("%s futures collection error: %s", underlying, fut_exc)

                log.info(
                    "%s ok — spot=%.2f options=%d rows futures=%s",
                    underlying, spot, n, fut_written,
                )
                successes_this_cycle += 1

            except MarketSessionClosed as exc:
                # A weekday holiday has no fresh historical candle/quote. Stop
                # the cycle before stale option snapshots can be persisted.
                log.warning("Market session unavailable: %s", exc)
                session_closed = True
                break
            except Exception as exc:
                if _is_auth_error(exc):
                    log.error(
                        "%s auth error (streak=%d/%d): %s",
                        underlying, consecutive_auth_fails + 1, AUTH_FAIL_LIMIT, exc,
                    )
                    auth_fails_this_cycle += 1
                else:
                    log.error("%s collection error: %s", underlying, exc)

        # ── auth failure accounting ───────────────────────────────────────────
        all_auth_failed = (
            not session_closed
            and auth_fails_this_cycle == len(UNDERLYINGS)
        )

        if all_auth_failed:
            consecutive_auth_fails += 1
            # Force a fresh token read on the next cycle — picks up the
            # 08:55 IST token if it has been refreshed since last failure.
            session_manager.reset()
            _log_token_info()

            if consecutive_auth_fails >= AUTH_FAIL_LIMIT:
                log.critical(
                    "Auth failed for %d consecutive cycles. "
                    "Exiting with code 1 — PA will restart and reload token.",
                    AUTH_FAIL_LIMIT,
                )
                sys.exit(1)

            log.warning(
                "All underlyings failed auth. streak=%d/%d. "
                "Token reset — will retry next cycle.",
                consecutive_auth_fails, AUTH_FAIL_LIMIT,
            )
        else:
            if consecutive_auth_fails:
                log.info(
                    "Auth recovered after %d failed cycle(s). streak reset.",
                    consecutive_auth_fails,
                )
            consecutive_auth_fails = 0

        # ── sleep to next minute boundary ─────────────────────────────────────
        elapsed   = (datetime.now(IST) - now).total_seconds()
        sleep_for = max(COLLECTOR_INTERVAL_SECS - elapsed, 1)
        time.sleep(sleep_for)


if __name__ == "__main__":
    run()
