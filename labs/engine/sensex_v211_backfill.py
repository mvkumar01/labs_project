"""Guarded atomic backfill for NIFTY v2.11 signals on SENSEX ATM options."""
from __future__ import annotations

import argparse
import json
from pathlib import Path


def backfill(
    ranges_path: str,
    *,
    expected_gross: float | None = None,
    expected_trades: int | None = None,
    expected_unavailable: int | None = None,
) -> dict:
    from labs.engine.sensex_v211_tracker import _ensure_tables, run_day
    from storage.db import get_conn

    ranges = json.loads(Path(ranges_path).read_text(encoding="utf-8"))
    if not ranges:
        raise ValueError("SENSEX v2.11 backfill range map is empty")
    print("Preflight (no SENSEX v2.11 rows will be changed)...")
    preview = []
    for trade_date in sorted(ranges):
        result = run_day(
            trade_date,
            override=ranges[trade_date],
            persist=False,
            require_all_quotes=True,
        )
        preview.append(result)
        print(
            f"  {trade_date} {result['status']:>9} trades={result['n_trades']} "
            f"gross=Rs{result['option_gross_rs']:+.2f}"
        )
    totals = {
        "days": len(preview),
        "trades": sum(int(row["n_trades"]) for row in preview),
        "option_gross_rs": round(
            sum(float(row["option_gross_rs"]) for row in preview), 2
        ),
        "option_unavailable_trades": sum(
            int(row["option_unavailable_trades"]) for row in preview
        ),
    }
    checks = (
        (expected_gross, totals["option_gross_rs"], "gross P&L", 0.01),
        (expected_trades, totals["trades"], "trade count", 0),
        (
            expected_unavailable,
            totals["option_unavailable_trades"],
            "unavailable quote count",
            0,
        ),
    )
    for expected, actual, label, tolerance in checks:
        if expected is not None and abs(float(actual) - float(expected)) > tolerance:
            raise RuntimeError(
                f"Preflight {label} mismatch: expected {expected}, got {actual}; "
                "database rows retained"
            )

    print(f"Preflight passed: {totals}\nPublishing atomically...")
    conn = get_conn()
    _ensure_tables(conn)
    try:
        conn.execute("BEGIN IMMEDIATE")
        for trade_date in sorted(ranges):
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
    parser.add_argument("--expect-gross", type=float)
    parser.add_argument("--expect-trades", type=int)
    parser.add_argument("--expect-unavailable", type=int)
    args = parser.parse_args()
    backfill(
        args.ranges_path,
        expected_gross=args.expect_gross,
        expected_trades=args.expect_trades,
        expected_unavailable=args.expect_unavailable,
    )
