"""Guarded atomic historical rebuild for the Alpha v2.12 paper ledger."""
from __future__ import annotations

import argparse
import json
from pathlib import Path


def _override_from_context(context: dict) -> dict:
    required = (
        "range_lower", "range_upper", "tier", "direction",
        "previous_session_date", "prev_close", "open_spot",
    )
    missing = [key for key in required if context.get(key) is None]
    # A recorded null VIX is valid audited provenance: the champion deliberately
    # falls back to TRAIL.  A context that omitted the field entirely is not.
    if "vix_open" not in context:
        missing.append("vix_open")
    if missing:
        raise ValueError(f"Stored v2.12 context missing {missing}")
    return {
        "lower": context["range_lower"],
        "upper": context["range_upper"],
        "bucket": context["tier"],
        "direction": context["direction"],
        "vix": context["vix_open"],
        "previous_session_date": context["previous_session_date"],
        "prev_close": context["prev_close"],
        "prev_close_source": context.get("prev_close_source"),
        "open_spot": context["open_spot"],
        "open_spot_source": context.get("open_spot_source"),
        "pc400_v210_biggap": bool(context.get("biggap")),
        # These inputs came from the ledger's audited context_json.  Preserve
        # them verbatim during a convention-only rebuild instead of comparing
        # them with a shared archive that may have been revised afterward.
        "_trust_historical_context": True,
        "skip": False,
    }


def _stored_context_ranges(start_date: str | None, end_date: str | None) -> dict:
    """Recover immutable day inputs before replacing historical v2.12 rows."""
    from labs.engine.alpha_v212_tracker import _ensure_tables
    from storage.db import get_conn

    conn = get_conn()
    _ensure_tables(conn)
    try:
        rows = conn.execute(
            "SELECT trade_date,context_json FROM alpha_v212_daily "
            "WHERE context_json IS NOT NULL AND (? IS NULL OR trade_date>=?) "
            "AND (? IS NULL OR trade_date<=?) ORDER BY trade_date",
            (start_date, start_date, end_date, end_date),
        ).fetchall()
    finally:
        conn.close()
    ranges = {}
    for row in rows:
        context = json.loads(row["context_json"])
        ranges[row["trade_date"]] = _override_from_context(context)
    return ranges


def backfill(
    ranges_path: str | None,
    *,
    from_db: bool = False,
    start_date: str | None = None,
    end_date: str | None = None,
    expected_net: float | None = None,
    expected_segments: int | None = None,
    publish: bool = True,
) -> dict:
    from labs.engine.alpha_v212_tracker import _ensure_tables, run_day
    from storage.db import get_conn

    if from_db:
        ranges = _stored_context_ranges(start_date, end_date)
    elif ranges_path:
        ranges = json.loads(Path(ranges_path).read_text(encoding="utf-8"))
    else:
        raise ValueError("Provide ranges_path or from_db=True")
    dates = [
        date for date in sorted(ranges)
        if (start_date is None or date >= start_date)
        and (end_date is None or date <= end_date)
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

    if not publish:
        print(f"Preflight passed (no rows changed): {totals}")
        return totals

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
    parser.add_argument("ranges_path", nargs="?")
    parser.add_argument("--from-db", action="store_true")
    parser.add_argument("--start-date")
    parser.add_argument("--end-date")
    parser.add_argument("--expect-net", type=float)
    parser.add_argument("--expect-segments", type=int)
    parser.add_argument("--preflight-only", action="store_true")
    args = parser.parse_args()
    backfill(
        args.ranges_path,
        from_db=args.from_db,
        start_date=args.start_date,
        end_date=args.end_date,
        expected_net=args.expect_net,
        expected_segments=args.expect_segments,
        publish=not args.preflight_only,
    )
