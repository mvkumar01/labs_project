"""Bounded backfill for Alpha v2.13 (v2.11 risk authority + entry-spot recovery
overlay). Reuses the SAME per-day champion ranges as v2.12 — sourced from
``alpha_v212_daily.context_json`` — and writes ``alpha_v213_daily``/``_trades``
via the v2.13 tracker. Idempotent: ``run_day`` REPLACEs a date's rows, so a
re-run is safe. Paper only; never places orders.

Driven in small batches from a PA web request (see ``/labs/api/alpha_v213/
backfill``) so a single request never runs long — the caller keeps calling
while ``remaining`` > 0, exactly like the Baskets tab.
"""
from __future__ import annotations

from labs.engine.alpha_v212_backfill import _stored_context_ranges
from labs.engine.alpha_v213_tracker import _ensure_tables, run_day
from storage.db import get_conn

DEFAULT_START = "2026-06-01"

# The stored v2.12 context carries the prev-session date/close as they were
# when v2.12 was published. The shared store's June baselines were later
# corrected, so those stored closes now differ by a few points and fail
# resolve_day_context's strict prev-close match. Dropping them makes v2.13
# SELF-RESOLVE prev_close from the current (authoritative) shared store —
# exactly what the live daily loop does (it passes no override at all). The
# historical champion RANGE (lower/upper/bucket/direction/vix/open) is kept.
_DROP_FOR_SELF_RESOLVE = ("previous_session_date", "prev_close", "prev_close_source")


def _self_resolve_override(override: dict) -> dict:
    return {k: v for k, v in override.items() if k not in _DROP_FOR_SELF_RESOLVE}


def _pending(start_date: str, end_date: str | None, conn) -> tuple[list, dict]:
    """(v2.11-dated days in range with a stored v2.12 context but no v2.13 row,
    oldest first; the full range->override map)."""
    _ensure_tables(conn)
    ranges = _stored_context_ranges(start_date, end_date)
    done = {
        row[0] for row in conn.execute("SELECT trade_date FROM alpha_v213_daily")
    }
    pending = [d for d in sorted(ranges) if d not in done]
    return pending, ranges


def run_backfill(
    *, start_date: str = DEFAULT_START, end_date: str | None = None,
    limit: int = 5,
) -> dict:
    """Replay up to ``limit`` pending days. A day that fails (e.g. an
    unavailable quote) is reported in ``errors`` and left pending; the caller
    should stop when a batch makes no progress (``done`` empty)."""
    conn = get_conn()
    try:
        pending, ranges = _pending(start_date, end_date, conn)
    finally:
        conn.close()

    done: list[str] = []
    errors: dict[str, str] = {}
    for trade_date in pending[: max(1, limit)]:
        try:
            run_day(trade_date,
                    override=_self_resolve_override(ranges[trade_date]),
                    require_all_quotes=True)
            done.append(trade_date)
        except Exception as exc:  # per-day isolation; surface, keep going
            errors[trade_date] = f"{type(exc).__name__}: {exc}"

    conn = get_conn()
    try:
        remaining, _ = _pending(start_date, end_date, conn)
    finally:
        conn.close()
    # Days that errored this batch are still "pending" but not progressable;
    # exclude them from the count so the caller's loop can terminate.
    remaining_progressable = [d for d in remaining if d not in errors]
    return {
        "done": done,
        "remaining": len(remaining_progressable),
        "errors": errors,
    }
