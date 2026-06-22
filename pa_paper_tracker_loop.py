"""Always-on launcher for the LIVE paper strategy tracker.

Re-runs the daily paper replay every ~60s during market hours so /labs/live
updates intraday (run_day replays only COMPLETED 5-min bars and is idempotent,
so re-running mid-session just refreshes the day's row + trades). Outside
market hours it idles. Designed for a PythonAnywhere always-on task.
"""
from datetime import datetime, time, timedelta, timezone
from pathlib import Path
import sys
import time as _time

BASE_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(BASE_DIR))

IST = timezone(timedelta(hours=5, minutes=30))
OPEN = time(9, 15)
CLOSE = time(15, 40)          # a few min past 15:30 close to capture the last bar
POLL_MARKET = 60             # seconds between refreshes during the session
POLL_IDLE = 300              # seconds between checks when closed


def _in_session(now: datetime) -> bool:
    return now.weekday() < 5 and OPEN <= now.time() <= CLOSE


def main() -> None:
    from labs.engine.paper_strategy_tracker import run_day as run_nifty_day
    from labs.engine.sensex_alpha_tracker import run_day as run_sensex_day
    print(f"[paper-loop] started {datetime.now(IST).isoformat()}", flush=True)
    last_log = {"nifty": None, "sensex_alpha": None}
    while True:
        now = datetime.now(IST)
        if _in_session(now):
            for name, runner in (
                ("nifty", run_nifty_day),
                ("sensex_alpha", run_sensex_day),
            ):
                try:
                    res = runner(now.date().isoformat())
                    if res != last_log[name]:
                        print(
                            f"[paper-loop:{name}] {now.strftime('%H:%M')} {res}",
                            flush=True,
                        )
                        last_log[name] = res
                except Exception as exc:  # one tracker must never block the other
                    print(
                        f"[paper-loop:{name}] error: {type(exc).__name__}: {exc}",
                        flush=True,
                    )
            _time.sleep(POLL_MARKET)
        else:
            _time.sleep(POLL_IDLE)


if __name__ == "__main__":
    main()
