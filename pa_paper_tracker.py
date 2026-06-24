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
    from labs.engine.sensex_alpha_tracker import run_day as run_sensex_day
    from labs.engine.sensex_alpha_inverted_tracker import run_day as run_sensex_inverted_day
    from labs.engine.sensex_v211_tracker import run_day as run_sensex_v211_day
    from labs.engine.sensex_v211_inverted_tracker import run_day as run_sensex_v211_inverted_day
    arg = sys.argv[1] if len(sys.argv) > 1 else None
    print({
        "nifty": run_nifty_day(arg),
        "sensex_alpha": run_sensex_day(arg),
        "sensex_alpha_inverted": run_sensex_inverted_day(arg),
        "sensex_v211": run_sensex_v211_day(arg),
        "sensex_v211_inverted": run_sensex_v211_inverted_day(arg),
    })
