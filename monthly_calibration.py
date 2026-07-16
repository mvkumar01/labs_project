"""Monthly OI calibration recommendation job (no automatic production edit).

Schedule daily if convenient; it runs only on day 1 unless --force is passed.
GREEN analytical candidates automatically receive the configured shadow replay,
but promotion remains blocked by calibration_center.json.
"""
from __future__ import annotations

from datetime import datetime
import sys

import pytz

from labs.services.calibration_service import INTERVALS, SYMBOLS, recalculate, shadow_test


def run(force: bool = False) -> list[dict]:
    now = datetime.now(pytz.timezone("Asia/Kolkata"))
    if now.day != 1 and not force:
        print("[calibration] Not the monthly run day; no changes made.")
        return []
    results = []
    for symbol in SYMBOLS:
        for interval in INTERVALS:
            candidate = recalculate(symbol, interval)
            if all(candidate.get("gate_checks", {}).values()):
                candidate = shadow_test(symbol, interval)
            results.append({
                "symbol": symbol, "interval": interval,
                "candidate": candidate["candidate_version"],
                "recommendation": candidate["recommendation"],
                "shadow": candidate["shadow_status"],
                "automatic_promotion": candidate["automatic_promotion_enabled"],
            })
            print(results[-1])
    return results


if __name__ == "__main__":
    run(force="--force" in sys.argv)
