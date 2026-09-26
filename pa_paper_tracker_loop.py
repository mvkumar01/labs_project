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
# MCX books (CRUDEOIL) trade 09:00-23:30; run a few minutes past the close so the
# session is replayed once more and frozen (crude tracker FINAL_AFTER = 23:40).
MCX_OPEN = time(9, 0)
MCX_CLOSE = time(23, 50)


def _in_session(now: datetime) -> bool:
    return now.weekday() < 5 and OPEN <= now.time() <= CLOSE


def _in_mcx_session(now: datetime) -> bool:
    return now.weekday() < 5 and MCX_OPEN <= now.time() <= MCX_CLOSE


def main() -> None:
    from labs.engine.paper_strategy_tracker import run_day as run_nifty_day
    from labs.engine.alpha_v211b_tracker import run_day as run_v211b_day
    from labs.engine.alpha_v212_tracker import run_day as run_v212_day
    from labs.engine.alpha_v212b10_tracker import run_day as run_v212b10_day
    from labs.engine.alpha_v214_tracker import run_day as run_v214_day
    from labs.engine.alpha_v214c_tracker import run_day as run_v214c_day
    from labs.engine.sensex_alpha_inverted_tracker import run_day as run_sensex_inverted_day
    from labs.engine.alpha_cpr_tracker import run_day as run_cpr_day
    from labs.engine.theta_straddle_tracker import run_day as run_theta_straddle_day
    from labs.engine.theta_iron_fly_tracker import run_day as run_theta_iron_fly_day
    from labs.engine.crude_macd_st_tracker import run_live as run_crude_macd_st_live
    from labs.engine.btc_rsi_roc_tracker import run_live as run_btc_rsi_roc_live
    from labs.services.paper_trade_alerts import emit_paper_trade_alerts
    print(f"[paper-loop] started {datetime.now(IST).isoformat()}", flush=True)
    last_log = {
        "nifty": None,
        "alpha_v211b": None,
        "alpha_v212": None,
        "alpha_v212b10": None,
        "alpha_v214": None,
        "alpha_v214c": None,
        "sensex_alpha_inverted": None,
        "alpha_cpr": None,
        "theta_straddle": None,
        "theta_iron_fly": None,
        "crude_macd_st": None,
        "btc_rsi_roc": None,
    }
    while True:
        now = datetime.now(IST)
        if _in_session(now):
            for name, runner in (
                ("nifty", run_nifty_day),
                ("alpha_v211b", run_v211b_day),
                ("alpha_v212", run_v212_day),
                ("alpha_v212b10", run_v212b10_day),
                ("alpha_v214", run_v214_day),
                ("alpha_v214c", run_v214c_day),
                ("sensex_alpha_inverted", run_sensex_inverted_day),
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
        if _in_mcx_session(now):
            # MCX paper book; isolated so a Kite or data error never touches the NSE books.
            try:
                res = run_crude_macd_st_live(now)
                if res != last_log["crude_macd_st"]:
                    print(f"[paper-loop:crude_macd_st] {now.strftime('%H:%M')} {res}", flush=True)
                    last_log["crude_macd_st"] = res
            except Exception as exc:
                print(f"[paper-loop:crude_macd_st] error: {type(exc).__name__}: {exc}", flush=True)
        # BTCUSDT trades 24/7: its paper book runs every cycle, isolated like the MCX books,
        # so the loop never idles longer than a minute. "through" moves every minute and is
        # left out of the change check to keep the log to real changes.
        try:
            res = run_btc_rsi_roc_live(now)
            shown = {k: v for k, v in res.items() if k != "through"}
            if shown != last_log["btc_rsi_roc"]:
                print(f"[paper-loop:btc_rsi_roc] {now.strftime('%H:%M')} {res}", flush=True)
                last_log["btc_rsi_roc"] = shown
        except Exception as exc:
            print(f"[paper-loop:btc_rsi_roc] error: {type(exc).__name__}: {exc}", flush=True)
        _time.sleep(POLL_MARKET)


if __name__ == "__main__":
    main()
