"""Bounded backfill for the NIFTY 09:20 ATM short-straddle paper book."""
from __future__ import annotations

from datetime import date, datetime, time, timedelta

from config.labs_config import SHARED_ARCHIVE_DIR, SHARED_LIVE_DIR
from labs.engine.theta_straddle_tracker import (
    IST,
    ThetaStraddleInputError,
    _ensure_tables,
    record_unavailable,
    run_day,
)
from market_data.shared_store import resolve_options_source
from storage.db import get_conn


DEFAULT_START = "2026-06-01"


def _default_end_date() -> str:
    now = datetime.now(IST)
    session = now.date()
    if session.weekday() < 5 and now.time() < time(15, 20):
        session -= timedelta(days=1)
    return session.isoformat()


def _available_dates(start_date: str, end_date: str) -> list[str]:
    candidates = set()
    for root in (SHARED_ARCHIVE_DIR, SHARED_LIVE_DIR):
        if not root.exists():
            continue
        for path in root.iterdir():
            if path.is_dir():
                try:
                    session_date = datetime.strptime(
                        path.name, "%Y-%m-%d"
                    ).date().isoformat()
                except ValueError:
                    continue
                if date.fromisoformat(session_date).weekday() >= 5:
                    continue
                if start_date <= session_date <= end_date:
                    candidates.add(session_date)
    available = []
    for session_date in sorted(candidates):
        try:
            resolve_options_source(
                "NIFTY",
                session_date,
                live_root=SHARED_LIVE_DIR,
                archive_root=SHARED_ARCHIVE_DIR,
            )
            available.append(session_date)
        except FileNotFoundError:
            pass
    return available


def run_backfill(
    *, start_date: str = DEFAULT_START, end_date: str | None = None,
    limit: int = 5, rebuild: bool = False,
) -> dict:
    end_date = end_date or _default_end_date()
    conn = get_conn()
    _ensure_tables(conn)
    try:
        done_dates = {
            row[0] for row in conn.execute(
                "SELECT trade_date FROM theta_straddle_daily WHERE trade_date>=? AND trade_date<=?",
                (start_date, end_date),
            )
        }
    finally:
        conn.close()
    dates = _available_dates(start_date, end_date)
    pending = dates if rebuild else [date for date in dates if date not in done_dates]
    completed, unavailable, errors = [], [], {}
    for trade_date in pending[:max(1, min(int(limit), 20))]:
        try:
            result = run_day(trade_date, require_close=True)
            completed.append({
                "date": trade_date,
                "net_rs": round(float(result["net_rs"]), 2),
                "capital_required_rs": result["capital_required_rs"],
            })
        except ThetaStraddleInputError as exc:
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
