"""Guarded historical backfill for the SENSEX-own Alpha paper tracker.

Every requested session is first replayed without database writes. Optional
benchmarks can then fail the run before publication. Once preflight succeeds,
all daily and trade rows are published in one SQLite transaction.
"""
from __future__ import annotations

import argparse

import pandas as pd


def trading_dates(start: str, end: str) -> list[str]:
    return [day.strftime("%Y-%m-%d") for day in pd.bdate_range(start, end)]


def backfill(
    dates: list[str],
    *,
    expected_spot: float | None = None,
    expected_option: float | None = None,
    expected_trades: int | None = None,
    expected_unavailable: int | None = None,
) -> dict:
    from labs.engine.sensex_alpha_tracker import _ensure_tables, run_day
    from storage.db import get_conn

    if not dates:
        raise ValueError("At least one SENSEX Alpha backfill date is required")

    print("Preflight (no SENSEX Alpha result rows will be changed)...")
    preview = []
    for date in sorted(set(dates)):
        result = run_day(date, persist=False)
        preview.append(result)
        print(
            f"  {date}  {result['status']:>8}  trades={result['n_trades']}  "
            f"spot={result['spot_pnl_pts']:+.2f}  option=Rs{result['option_gross_rs']:+.2f}"
        )

    totals = {
        "days": len(preview),
        "trades": sum(int(row["n_trades"]) for row in preview),
        "spot_pnl_pts": round(sum(float(row["spot_pnl_pts"]) for row in preview), 2),
        "option_gross_rs": round(sum(float(row["option_gross_rs"]) for row in preview), 2),
        "option_unavailable_trades": sum(
            int(row["option_unavailable_trades"]) for row in preview
        ),
    }
    checks = (
        (expected_spot, totals["spot_pnl_pts"], "spot P&L", 0.01),
        (expected_option, totals["option_gross_rs"], "option gross", 0.01),
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
        for date in sorted(set(dates)):
            run_day(date, connection=conn, commit=False)
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    print(f"Published: {totals}")
    return totals


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("start", help="first session date, YYYY-MM-DD")
    parser.add_argument("end", help="last session date, YYYY-MM-DD")
    parser.add_argument("--expect-spot", type=float)
    parser.add_argument("--expect-option", type=float)
    parser.add_argument("--expect-trades", type=int)
    parser.add_argument("--expect-unavailable", type=int)
    args = parser.parse_args()
    backfill(
        trading_dates(args.start, args.end),
        expected_spot=args.expect_spot,
        expected_option=args.expect_option,
        expected_trades=args.expect_trades,
        expected_unavailable=args.expect_unavailable,
    )
