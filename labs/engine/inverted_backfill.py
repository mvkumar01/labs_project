"""Guarded atomic backfill for both inverted SENSEX paper books."""
from __future__ import annotations

import argparse
import json
from pathlib import Path


def backfill(ranges_path: str) -> dict:
    from labs.engine.sensex_alpha_inverted_tracker import (
        _ensure_tables as ensure_alpha,
        run_day as run_alpha,
    )
    from labs.engine.sensex_v211_inverted_tracker import (
        _ensure_tables as ensure_v211,
        run_day as run_v211,
    )
    from storage.db import get_conn

    ranges = json.loads(Path(ranges_path).read_text(encoding="utf-8"))
    conn = get_conn()
    alpha_dates = [
        row[0] for row in conn.execute(
            "SELECT trade_date FROM sensex_alpha_daily ORDER BY trade_date"
        ).fetchall()
    ]
    v211_dates = [
        row[0] for row in conn.execute(
            "SELECT trade_date FROM sensex_v211_daily ORDER BY trade_date"
        ).fetchall()
        if row[0] in ranges
    ]
    if not alpha_dates or not v211_dates:
        raise RuntimeError(
            "Original SENSEX paper books must be populated before inverted backfill"
        )

    print("Preflight (no inverted rows will be changed)...")
    alpha_preview = [run_alpha(date, persist=False) for date in alpha_dates]
    v211_preview = [
        run_v211(
            date, override=ranges[date], persist=False, require_all_quotes=True
        )
        for date in v211_dates
    ]
    totals = {
        "sensex_alpha": {
            "days": len(alpha_preview),
            "trades": sum(row["n_trades"] for row in alpha_preview),
            "spot_pnl_pts": round(
                sum(row["spot_pnl_pts"] for row in alpha_preview), 2
            ),
            "option_gross_rs": round(
                sum(row["option_gross_rs"] for row in alpha_preview), 2
            ),
            "unavailable": sum(
                row["option_unavailable_trades"] for row in alpha_preview
            ),
        },
        "sensex_v211": {
            "days": len(v211_preview),
            "trades": sum(row["n_trades"] for row in v211_preview),
            "option_gross_rs": round(
                sum(row["option_gross_rs"] for row in v211_preview), 2
            ),
            "unavailable": sum(
                row["option_unavailable_trades"] for row in v211_preview
            ),
        },
    }
    print(f"Preflight passed: {totals}")

    ensure_alpha(conn)
    ensure_v211(conn)
    try:
        conn.execute("BEGIN IMMEDIATE")
        for date in alpha_dates:
            run_alpha(date, connection=conn, commit=False)
        for date in v211_dates:
            run_v211(
                date, override=ranges[date], require_all_quotes=True,
                connection=conn, commit=False,
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
    args = parser.parse_args()
    backfill(args.ranges_path)
