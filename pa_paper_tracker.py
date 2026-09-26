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
    from labs.engine.alpha_v211b_tracker import run_day as run_v211b_day
    from labs.engine.alpha_v212_tracker import run_day as run_v212_day
    from labs.engine.alpha_v212b10_tracker import run_day as run_v212b10_day
    from labs.engine.alpha_v214_tracker import run_day as run_v214_day
    from labs.engine.alpha_v214c_tracker import run_day as run_v214c_day
    from labs.engine.sensex_alpha_inverted_tracker import run_day as run_sensex_inverted_day
    from labs.engine.alpha_cpr_tracker import run_day as run_cpr_day
    from labs.engine.theta_straddle_tracker import run_day as run_theta_straddle_day
    from labs.engine.theta_iron_fly_tracker import run_day as run_theta_iron_fly_day
    from labs.services.paper_trade_alerts import emit_paper_trade_alerts
    arg = sys.argv[1] if len(sys.argv) > 1 else None
    results = {
        "nifty": run_nifty_day(arg),
        "alpha_v211b": run_v211b_day(arg),
        "alpha_v212": run_v212_day(arg),
        "sensex_alpha_inverted": run_sensex_inverted_day(arg),
    }
    # Alpha-CPR is an unproven paper candidate and runs LAST behind a guard:
    # a missing CPR prev-session or quote must never abort the established
    # books above, which have already persisted by this point.
    # Alpha v2.12 B10 is new and guarded the same way: it must never abort
    # the established books, which have already persisted by this point.
    try:
        results["alpha_v212b10"] = run_v212b10_day(arg)
    except Exception as exc:
        results["alpha_v212b10"] = f"{type(exc).__name__}: {exc}"
    # Alpha v2.14 (v2.11 replay (B) + B10) is new and guarded the same way.
    try:
        results["alpha_v214"] = run_v214_day(arg)
    except Exception as exc:
        results["alpha_v214"] = f"{type(exc).__name__}: {exc}"
    # Alpha v2.14 C (2.14 B with a Renko 30 overlay) is new and guarded the same way.
    try:
        results["alpha_v214c"] = run_v214c_day(arg)
    except Exception as exc:
        results["alpha_v214c"] = f"{type(exc).__name__}: {exc}"
    try:
        results["alpha_cpr"] = run_cpr_day(arg)
    except Exception as exc:
        results["alpha_cpr"] = f"{type(exc).__name__}: {exc}"
    try:
        results["theta_straddle"] = run_theta_straddle_day(
            arg, require_close=True
        )
    except Exception as exc:
        results["theta_straddle"] = f"{type(exc).__name__}: {exc}"
    try:
        results["theta_iron_fly"] = run_theta_iron_fly_day(
            arg, require_close=True
        )
    except Exception as exc:
        results["theta_iron_fly"] = f"{type(exc).__name__}: {exc}"
    for tracker, result_key in (("v2.11", "nifty"), ("v2.12", "alpha_v212")):
        try:
            emit_paper_trade_alerts(tracker, results[result_key]["trade_date"])
        except Exception as exc:
            print(
                f"[paper-tracker:{tracker}:telegram] error: "
                f"{type(exc).__name__}: {exc}",
                flush=True,
            )
    print(results)
