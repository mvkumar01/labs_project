"""Guarded atomic June backfill for the Alpha v2.12 paper ledger."""
from __future__ import annotations

import argparse
import json
from pathlib import Path


def backfill(
    ranges_path: str,
    *,
    end_date: str | None = None,
    expected_net: float | None = None,
    expected_segments: int | None = None,
) -> dict:
    from labs.engine.alpha_v212_tracker import _ensure_tables, run_day
    from storage.db import get_conn

    ranges = json.loads(Path(ranges_path).read_text(encoding="utf-8"))
    dates = [
        date for date in sorted(ranges)
        if end_date is None or date <= end_date
    ]
    if not dates:
        raise ValueError("Alpha v2.12 backfill range map is empty")
    print("Preflight (no Alpha v2.12 rows will be changed)...")
    preview = []
    for trade_date in dates:
        result = run_day(
            trade_date,
            override=ranges[trade_date],
            persist=False,
            require_all_quotes=True,
        )
        preview.append(result)
        print(
            f"  {trade_date} {result['status']:>9} "
            f"segments={result['n_segments']} net=Rs{result['net_rs']:+.2f}"
        )
    totals = {
        "days": len(preview),
        "segments": sum(row["n_segments"] for row in preview),
        "priced_segments": sum(row["priced_segments"] for row in preview),
        "unavailable_segments": sum(
            row["unavailable_segments"] for row in preview
        ),
        "spot_pnl_pts": round(sum(row["spot_pnl_pts"] for row in preview), 2),
        "gross_rs": round(sum(row["gross_rs"] for row in preview), 2),
        "charges_rs": round(sum(row["charges_rs"] for row in preview), 2),
        "net_rs": round(sum(row["net_rs"] for row in preview), 2),
    }
    if expected_net is not None and abs(totals["net_rs"] - expected_net) > 0.01:
        raise RuntimeError(
            f"Preflight net mismatch: expected {expected_net}, "
            f"got {totals['net_rs']}; existing rows retained"
        )
    if expected_segments is not None and totals["segments"] != expected_segments:
        raise RuntimeError(
            f"Preflight segment mismatch: expected {expected_segments}, "
            f"got {totals['segments']}; existing rows retained"
        )
    if totals["unavailable_segments"]:
        raise RuntimeError(
            f"Preflight has {totals['unavailable_segments']} unavailable segments; "
            "existing rows retained"
        )

    print(f"Preflight passed: {totals}\nPublishing atomically...")
    conn = get_conn()
    _ensure_tables(conn)
    try:
        conn.execute("BEGIN IMMEDIATE")
        for trade_date in dates:
            run_day(
                trade_date,
                override=ranges[trade_date],
                require_all_quotes=True,
                connection=conn,
                commit=False,
            )
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    print(f"Published: {totals}")
    return totals


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("ranges_path")
    parser.add_argument("--end-date")
    parser.add_argument("--expect-net", type=float)
    parser.add_argument("--expect-segments", type=int)
    args = parser.parse_args()
    backfill(
        args.ranges_path,
        end_date=args.end_date,
        expected_net=args.expect_net,
        expected_segments=args.expect_segments,
    )
