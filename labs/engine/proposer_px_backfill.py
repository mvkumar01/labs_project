"""Backfill for the SENSEX Proposer + price-action exit paper book.

Every session with SENSEX quotes is replayed in date order, because each one starts from the
book the sessions before it left (the recovery mode reads the lifetime gross). A session whose
market data is missing is recorded as unavailable, never traded on invented inputs.
"""
from __future__ import annotations

from datetime import datetime, time, timedelta

from config.labs_config import SHARED_ARCHIVE_DIR, SHARED_LIVE_DIR
from labs.engine.proposer_px_tracker import (
    DEFAULT_START,
    IST,
    ProposerPxInputError,
    SYMBOL,
    _ensure_tables,
    record_unavailable,
    run_day,
)
from market_data.shared_store import resolve_options_source
from storage.db import get_conn


def _default_end_date() -> str:
    now = datetime.now(IST)
    session = now.date()
    if session.weekday() < 5 and now.time() < time(15, 30):
        session -= timedelta(days=1)
    return session.isoformat()


def sessions_with_quotes(start_date: str, end_date: str) -> list[str]:
    candidates = set()
    for root in (SHARED_ARCHIVE_DIR, SHARED_LIVE_DIR):
        if not root.exists():
            continue
        for path in root.iterdir():
            if not path.is_dir():
                continue
            try:
                session = datetime.strptime(path.name, "%Y-%m-%d").date()
            except ValueError:
                continue
            if session.weekday() < 5 and start_date <= session.isoformat() <= end_date:
                candidates.add(session.isoformat())
    available = []
    for session in sorted(candidates):
        try:
            resolve_options_source(SYMBOL, session, live_root=SHARED_LIVE_DIR,
                                   archive_root=SHARED_ARCHIVE_DIR)
            available.append(session)
        except FileNotFoundError:
            pass
    return available


def run_backfill(*, start_date: str = DEFAULT_START, end_date: str | None = None,
                 limit: int = 5, rebuild: bool = False) -> dict:
    """Replay the next `limit` pending sessions, oldest first.

    `rebuild` first clears the book from `start_date` on: call it once, then continue without it.
    Date order is a hard requirement, so the run stops at the first session that errors."""
    end_date = end_date or _default_end_date()
    conn = get_conn()
    _ensure_tables(conn)
    try:
        if rebuild:
            conn.execute("DELETE FROM proposer_px_trades WHERE trade_date>=?", (start_date,))
            conn.execute("DELETE FROM proposer_px_daily WHERE trade_date>=?", (start_date,))
            conn.commit()
        done = {row[0] for row in conn.execute(
            "SELECT trade_date FROM proposer_px_daily WHERE trade_date>=? AND trade_date<=? "
            "AND status IN ('closed','no_trade','unavailable')", (start_date, end_date))}
    finally:
        conn.close()
    pending = [s for s in sessions_with_quotes(start_date, end_date) if s not in done]
    completed, unavailable, errors = [], [], {}
    for session in pending[:max(1, min(int(limit), 20))]:
        try:
            result = run_day(session)
            completed.append({"date": session, "trades": result["n_trades"], "net_rs": result["net_rs"]})
        except ProposerPxInputError as exc:
            record_unavailable(session, str(exc))
            unavailable.append({"date": session, "reason": str(exc)})
        except Exception as exc:                                  # noqa: BLE001
            errors[session] = f"{type(exc).__name__}: {exc}"
            break
    remaining = max(0, len(pending) - len(completed) - len(unavailable))
    return {"done": completed, "unavailable": unavailable, "errors": errors,
            "remaining": 0 if errors else remaining}


if __name__ == "__main__":
    import json
    import sys
    args = [a for a in sys.argv[1:] if not a.startswith("--")]
    out = run_backfill(limit=int(args[0]) if args else 5, rebuild="--rebuild" in sys.argv)
    print(json.dumps(out, indent=2, default=str))
