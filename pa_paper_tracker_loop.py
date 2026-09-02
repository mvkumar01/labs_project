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
    from labs.engine.alpha_v211a_tracker import run_day as run_v211a_day
    from labs.engine.alpha_v211b_tracker import run_day as run_v211b_day
    from labs.engine.alpha_v212_tracker import run_day as run_v212_day
    from labs.engine.alpha_v213_tracker import run_day as run_v213_day
    from labs.engine.sensex_alpha_tracker import run_day as run_sensex_day
    from labs.engine.sensex_alpha_inverted_tracker import run_day as run_sensex_inverted_day
    from labs.engine.sensex_v211_tracker import run_day as run_sensex_v211_day
    from labs.engine.sensex_v211_inverted_tracker import run_day as run_sensex_v211_inverted_day
    from labs.engine.alpha_cpr_tracker import run_day as run_cpr_day
    from labs.engine.theta_straddle_tracker import run_day as run_theta_straddle_day
    from labs.engine.theta_iron_fly_tracker import run_day as run_theta_iron_fly_day
    from labs.services.paper_trade_alerts import emit_paper_trade_alerts
    print(f"[paper-loop] started {datetime.now(IST).isoformat()}", flush=True)
    last_log = {
        "nifty": None,
        "alpha_v211a": None,
        "alpha_v211b": None,
        "alpha_v212": None,
        "alpha_v213": None,
        "sensex_alpha": None,
        "sensex_alpha_inverted": None,
        "sensex_v211": None,
        "sensex_v211_inverted": None,
        "alpha_cpr": None,
        "theta_straddle": None,
        "theta_iron_fly": None,
    }
    while True:
        now = datetime.now(IST)
        if _in_session(now):
            for name, runner in (
                ("nifty", run_nifty_day),
                ("alpha_v211a", run_v211a_day),
                ("alpha_v211b", run_v211b_day),
                ("alpha_v212", run_v212_day),
                ("alpha_v213", run_v213_day),
                ("sensex_alpha", run_sensex_day),
                ("sensex_alpha_inverted", run_sensex_inverted_day),
                ("sensex_v211", run_sensex_v211_day),
                ("sensex_v211_inverted", run_sensex_v211_inverted_day),
                ("alpha_cpr", run_cpr_day),
                ("theta_straddle", run_theta_straddle_day),
                ("theta_iron_fly", run_theta_iron_fly_day),
            ):
                try:
                    res = runner(now.date().isoformat())
                    alert_tracker = {"nifty": "v2.11", "alpha_v212": "v2.12"}.get(name)
                    if alert_tracker:
                        try:
                            emit_paper_trade_alerts(
                                alert_tracker, now.date().isoformat(), now=now
                            )
                        except Exception as alert_exc:
                            print(
                                f"[paper-loop:{name}:telegram] error: "
                                f"{type(alert_exc).__name__}: {alert_exc}",
                                flush=True,
                            )
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
