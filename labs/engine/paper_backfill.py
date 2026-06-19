"""Backfill the paper tracker for past days using champion ranges from a JSON.

The live hybrid_range_state.json only holds the current day, so historical days
can't self-backfill. This reads a {date: {lower,upper,bucket,direction,vix,
pc400_v210_biggap,skip}} map (produced by alphaIMB
research/experiments/2026-06-18_1min_sampling_fix/june_champion_ranges.py) and
replays each day through run_day(date, override=...). Requires the shared-store
OI + prev-day baselines for those dates to be present.

Usage:
    python3 -m labs.engine.paper_backfill path/to/june_champion_ranges.json
"""
from __future__ import annotations

import json
import sys
from pathlib import Path


def backfill(ranges_path: str) -> None:
    from labs.engine.paper_strategy_tracker import run_day
    data = json.loads(Path(ranges_path).read_text())
    total = 0.0
    for date in sorted(data):
        ov = data[date]
        try:
            res = run_day(date, override=ov)
        except Exception as exc:
            print(f"  {date}  ERROR {type(exc).__name__}: {exc}")
            continue
        total += float(res.get("net_rs") or 0)
        print(f"  {date}  {res['status']:>8}  trades={res['n_trades']}  "
              f"net=Rs{res.get('net_rs', 0):+.0f}")
    print(f"\nBackfilled {len(data)} days | cumulative net Rs{total:+.0f}")


if __name__ == "__main__":
    if len(sys.argv) < 2:
        print("usage: python3 -m labs.engine.paper_backfill <ranges.json>")
        sys.exit(1)
    backfill(sys.argv[1])
