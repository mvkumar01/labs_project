"""Bounded historical backfill for the Alpha v2.12 B10 paper book.

Uses the same per-day champion ranges as every sibling alpha book (the audited
June manifest plus later v2.11 paper contexts), so B10 covers exactly the days
the other books do. Each batch is one transaction: a day with any unpriced
segment aborts the batch and leaves existing rows untouched.

    python3 -m labs.engine.alpha_v212b10_backfill            # resume to done
    python3 -m labs.engine.alpha_v212b10_backfill --rebuild  # recompute all
"""
from __future__ import annotations

from labs.engine.alpha_v212b10_tracker import _ensure_tables, run_day
from labs.engine.alpha_v213_backfill import _historical_ranges
from labs.engine.paper_strategy_tracker import _ensure_tables as _ensure_v211_tables
from storage.db import get_conn


DEFAULT_START = "2026-06-01"


def _pending(start_date: str, end_date: str | None, conn) -> tuple[list, dict]:
    _ensure_tables(conn)
    _ensure_v211_tables(conn)
    ranges = _historical_ranges(start_date, end_date)
    done = {
        row[0] for row in conn.execute("SELECT trade_date FROM alpha_v212b10_daily")
    }
    return [date for date in sorted(ranges) if date not in done], ranges


def run_backfill(
    *,
    start_date: str = DEFAULT_START,
    end_date: str | None = None,
    limit: int = 5,
    rebuild: bool = False,
) -> dict:
    conn = get_conn()
    try:
        pending, ranges = _pending(start_date, end_date, conn)
    finally:
        conn.close()
    if rebuild:
        pending = sorted(ranges)

    done: list[str] = []
    errors: dict[str, str] = {}
    batch = pending[: max(1, limit)]
    conn = get_conn()
    _ensure_tables(conn)
    try:
        conn.execute("BEGIN IMMEDIATE")
        for trade_date in batch:
            run_day(
                trade_date,
                override=ranges[trade_date],
                require_all_quotes=True,
                connection=conn,
                commit=False,
            )
            done.append(trade_date)
        conn.commit()
    except Exception as exc:
        conn.rollback()
        done.clear()
        errors["batch"] = f"{type(exc).__name__}: {exc}"
    finally:
        conn.close()

    conn = get_conn()
    try:
        remaining, _ = _pending(start_date, end_date, conn)
    finally:
        conn.close()
    return {
        "done": done,
        "remaining": len(remaining),
        "errors": errors,
    }


def _run_to_completion(start: str, end: str | None, rebuild: bool) -> int:
    """Drive bounded batches until done; fall back to single days on failure.

    A failing batch rolls back entirely, so a single bad day would otherwise
    block every later one. On a batch error each pending day is retried alone,
    so good days land and the bad one is reported precisely.
    """
    if rebuild:
        conn = get_conn()
        try:
            _ensure_tables(conn)
            conn.execute("DELETE FROM alpha_v212b10_trades WHERE trade_date >= ?",
                         (start,))
            conn.execute("DELETE FROM alpha_v212b10_daily WHERE trade_date >= ?",
                         (start,))
            conn.commit()
        finally:
            conn.close()
    failed: dict[str, str] = {}
    while True:
        conn = get_conn()
        try:
            pending, _ = _pending(start, end, conn)
        finally:
            conn.close()
        pending = [d for d in pending if d not in failed]
        if not pending:
            break
        result = run_backfill(start_date=pending[0], end_date=end, limit=5)
        if result["errors"]:
            for day in pending[:5]:
                one = run_backfill(start_date=day, end_date=day, limit=1)
                if one["errors"]:
                    failed[day] = one["errors"]["batch"]
                    print(f"FAILED {day}: {failed[day]}", flush=True)
                else:
                    print(f"done   {day}", flush=True)
        else:
            for day in result["done"]:
                print(f"done   {day}", flush=True)
    print(f"\nbackfill complete; failed days: {len(failed)}", flush=True)
    for day, err in failed.items():
        print(f"  {day}: {err}", flush=True)
    return 1 if failed else 0


if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser()
    parser.add_argument("--start", default=DEFAULT_START)
    parser.add_argument("--end", default=None)
    parser.add_argument("--rebuild", action="store_true")
    args = parser.parse_args()
    raise SystemExit(_run_to_completion(args.start, args.end, args.rebuild))


__all__ = ["DEFAULT_START", "run_backfill"]
