"""Bounded historical backfill for Alpha v2.11 champion replay B."""
from __future__ import annotations

from labs.engine.alpha_v211b_tracker import _ensure_tables, run_day
from labs.engine.alpha_v213_backfill import _historical_ranges
from labs.engine.paper_strategy_tracker import _ensure_tables as _ensure_v211_tables
from storage.db import get_conn


DEFAULT_START = "2026-06-01"


def _pending(start_date: str, end_date: str | None, conn) -> tuple[list, dict]:
    _ensure_tables(conn)
    _ensure_v211_tables(conn)
    ranges = _historical_ranges(start_date, end_date)
    done = {
        row[0] for row in conn.execute("SELECT trade_date FROM alpha_v211b_daily")
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


__all__ = ["DEFAULT_START", "run_backfill"]
