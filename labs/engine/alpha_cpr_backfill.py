"""Bounded backfill for the Alpha-CPR paper book.

Reuses the SAME per-day champion ranges as v2.12 / v2.13 — the Alpha-CPR book
differs only in its EXITS, so its entries must come off identical ranges for the
tabs to be comparable. Writes ``alpha_cpr_daily`` / ``_trades`` via the tracker.
Idempotent: ``run_day`` REPLACEs a date's rows, so a re-run is safe.

Driven in small batches from a PA web request (see ``/labs/api/alpha_cpr/
backfill``) so a single request never runs long — the caller keeps calling while
``remaining`` > 0, exactly like v2.13 and the Baskets tab.

``DEFAULT_START`` matches the other overlay books and the ``/labs/live`` display
filter. Pass an earlier ``start_date`` (e.g. 2026-04-01) to build the research
parity window, which the UI will not show but which can be queried directly.

Paper only; never places orders.
"""
from __future__ import annotations

from labs.engine.alpha_v213_backfill import _historical_ranges
from labs.engine.alpha_cpr_tracker import _ensure_tables, run_day
from storage.db import get_conn

DEFAULT_START = "2026-06-01"


def _pending(start_date: str, end_date: str | None, conn) -> tuple[list, dict]:
    """(days in range with a stored champion range but no Alpha-CPR row,
    oldest first; the full range->override map)."""
    _ensure_tables(conn)
    ranges = _historical_ranges(start_date, end_date)
    done = {
        row[0] for row in conn.execute("SELECT trade_date FROM alpha_cpr_daily")
    }
    pending = [d for d in sorted(ranges) if d not in done]
    return pending, ranges


def run_backfill(*, start_date: str = DEFAULT_START, end_date: str | None = None,
                 limit: int = 5, rebuild: bool = False) -> dict:
    """Replay up to ``limit`` pending days. A day that fails (unavailable quote,
    missing CPR prev-session) is reported in ``errors`` and left pending; the
    caller stops when a batch makes no progress (``done`` empty)."""
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
            run_day(trade_date, override=ranges[trade_date],
                    require_all_quotes=True, connection=conn, commit=False)
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
    remaining_progressable = [d for d in remaining if d not in errors]
    return {"done": done, "remaining": len(remaining_progressable),
            "errors": errors}
