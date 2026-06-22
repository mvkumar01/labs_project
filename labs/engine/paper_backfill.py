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

import argparse
import json
from pathlib import Path


def backfill(
    ranges_path: str,
    *,
    expected_net: float | None = None,
    expected_trades: int | None = None,
) -> None:
    """Preflight every date, validate benchmarks, then publish every date.

    The preflight is deliberately complete and prices all four contract
    variants.  Any missing CSV/Parquet input or quote aborts before existing DB
    rows are changed.
    """
    from labs.engine.paper_strategy_tracker import _ensure_tables, run_day
    from storage.db import get_conn
    data = json.loads(Path(ranges_path).read_text())

    print("Preflight (no DB result rows will be changed)...")
    preview: list[dict] = []
    for date in sorted(data):
        result = run_day(
            date,
            override=data[date],
            persist=False,
            require_all_variants=True,
        )
        preview.append(result)
        print(
            f"  {date}  {result['status']:>8}  trades={result['n_trades']}  "
            f"net=Rs{result.get('net_rs', 0):+.2f}"
        )

    preview_net = round(sum(float(row.get("net_rs") or 0) for row in preview), 2)
    preview_trades = sum(int(row.get("n_trades") or 0) for row in preview)
    if expected_net is not None and abs(preview_net - expected_net) > 0.01:
        raise RuntimeError(
            f"Preflight net mismatch: expected Rs{expected_net:+.2f}, "
            f"got Rs{preview_net:+.2f}; DB rows retained"
        )
    if expected_trades is not None and preview_trades != expected_trades:
        raise RuntimeError(
            f"Preflight trade-count mismatch: expected {expected_trades}, "
            f"got {preview_trades}; DB rows retained"
        )

    print(
        f"Preflight passed: {len(preview)} days, {preview_trades} trades, "
        f"net Rs{preview_net:+.2f}\nPublishing..."
    )
    total = 0.0
    conn = get_conn()
    _ensure_tables(conn)
    try:
        conn.execute("BEGIN IMMEDIATE")
        for date in sorted(data):
            ov = data[date]
            res = run_day(
                date,
                override=ov,
                require_all_variants=True,
                connection=conn,
                commit=False,
            )
            total += float(res.get("net_rs") or 0)
            print(f"  {date}  {res['status']:>8}  trades={res['n_trades']}  "
                  f"net=Rs{res.get('net_rs', 0):+.2f}")
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    print(f"\nBackfilled {len(data)} days | cumulative net Rs{total:+.2f}")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("ranges_path")
    parser.add_argument("--expect-net", type=float)
    parser.add_argument("--expect-trades", type=int)
    args = parser.parse_args()
    backfill(
        args.ranges_path,
        expected_net=args.expect_net,
        expected_trades=args.expect_trades,
    )
