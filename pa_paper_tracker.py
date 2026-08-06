"""PythonAnywhere launcher for the daily PAPER strategy tracker.

Scheduled ~15:40 IST (after the 15:30 close, once the day's 5-min bars are
complete). Idempotent per date. Optional arg: a YYYY-MM-DD date to (re)run.
"""
from pathlib import Path
import sys

BASE_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(BASE_DIR))

if __name__ == "__main__":
    from labs.engine.paper_strategy_tracker import run_day as run_nifty_day
    from labs.engine.alpha_v211a_tracker import run_day as run_v211a_day
    from labs.engine.alpha_v212_tracker import run_day as run_v212_day
    from labs.engine.alpha_v213_tracker import run_day as run_v213_day
    from labs.engine.sensex_alpha_tracker import run_day as run_sensex_day
    from labs.engine.sensex_alpha_inverted_tracker import run_day as run_sensex_inverted_day
    from labs.engine.sensex_v211_tracker import run_day as run_sensex_v211_day
    from labs.engine.sensex_v211_inverted_tracker import run_day as run_sensex_v211_inverted_day
    from labs.engine.alpha_cpr_tracker import run_day as run_cpr_day
    arg = sys.argv[1] if len(sys.argv) > 1 else None
    results = {
        "nifty": run_nifty_day(arg),
        "alpha_v211a": run_v211a_day(arg),
        "alpha_v212": run_v212_day(arg),
        "alpha_v213": run_v213_day(arg),
        "sensex_alpha": run_sensex_day(arg),
        "sensex_alpha_inverted": run_sensex_inverted_day(arg),
        "sensex_v211": run_sensex_v211_day(arg),
        "sensex_v211_inverted": run_sensex_v211_inverted_day(arg),
    }
    # Alpha-CPR is an unproven paper candidate and runs LAST behind a guard:
    # a missing CPR prev-session or quote must never abort the established
    # books above, which have already persisted by this point.
    try:
        results["alpha_cpr"] = run_cpr_day(arg)
    except Exception as exc:
        results["alpha_cpr"] = f"{type(exc).__name__}: {exc}"
    print(results)
