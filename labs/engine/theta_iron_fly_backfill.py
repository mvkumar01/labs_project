"""Bounded backfill for the NIFTY 09:20 defined-risk iron fly."""
from __future__ import annotations

from labs.engine.theta_straddle_backfill import (
    DEFAULT_START,
    _available_dates,
    _default_end_date,
)
from labs.engine.theta_iron_fly_tracker import (
    ThetaIronFlyInputError,
    _ensure_tables,
    record_unavailable,
    run_day,
)
from storage.db import get_conn


def run_backfill(
    *,
    start_date: str = DEFAULT_START,
    end_date: str | None = None,
    limit: int = 5,
    rebuild: bool = False,
) -> dict:
    end_date = end_date or _default_end_date()
    conn = get_conn()
    _ensure_tables(conn)
    try:
        done_dates = {
            row[0]
            for row in conn.execute(
                "SELECT trade_date FROM theta_iron_fly_daily "
                "WHERE trade_date>=? AND trade_date<=?",
                (start_date, end_date),
            )
        }
    finally:
        conn.close()
    dates = _available_dates(start_date, end_date)
    pending = dates if rebuild else [day for day in dates if day not in done_dates]
    completed, unavailable, errors = [], [], {}
    for trade_date in pending[: max(1, min(int(limit), 20))]:
        try:
            result = run_day(trade_date, require_close=True)
            completed.append({
                "date": trade_date,
                "net_rs": round(float(result["net_rs"]), 2),
                "capital_required_rs": round(float(result["capital_required_rs"]), 2),
                "exit_reason": result["exit_reason"],
            })
        except ThetaIronFlyInputError as exc:
            record_unavailable(trade_date, str(exc))
            unavailable.append({"date": trade_date, "reason": str(exc)})
        except Exception as exc:
            errors[trade_date] = f"{type(exc).__name__}: {exc}"
    remaining = max(
        0, len(pending) - len(completed) - len(unavailable) - len(errors)
    )
    return {
        "done": completed,
        "unavailable": unavailable,
        "errors": errors,
        "remaining": remaining,
    }
