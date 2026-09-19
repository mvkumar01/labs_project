"""Bounded backfill for the CRUDEOIL MACD/Supertrend paper book.

Each weekday session from ``start_date`` is replayed with ``run_day``; 1-minute candles are
pulled from Kite once per session and kept in ``crude_minute_bars``, so re-runs are cheap.
Frozen sessions are skipped unless ``rebuild`` is set.
"""
from __future__ import annotations

from datetime import date, datetime, timedelta

from labs.engine.crude_macd_st_tracker import FINAL_AFTER, IST, _ensure_tables, run_day
from storage.db import get_conn

DEFAULT_START = "2026-06-01"


def _default_end_date() -> str:
    now = datetime.now(IST)
    session = now.date()
    if now.time() < FINAL_AFTER:
        session -= timedelta(days=1)
    return session.isoformat()


def run_backfill(*, start_date: str = DEFAULT_START, end_date: str | None = None,
                 limit: int = 20, rebuild: bool = False, kite=None) -> dict:
    end_date = end_date or _default_end_date()
    start, end = date.fromisoformat(start_date), date.fromisoformat(end_date)
    sessions = [start + timedelta(days=k) for k in range((end - start).days + 1)]
    sessions = [d.isoformat() for d in sessions if d.weekday() < 5]
    conn = get_conn()
    _ensure_tables(conn)
    try:
        frozen = {row[0] for row in conn.execute(
            "SELECT trade_date FROM crude_macd_st_daily WHERE status IN ('final','no_session') "
            "AND trade_date>=? AND trade_date<=?", (start_date, end_date))}
    finally:
        conn.close()
    pending = sessions if rebuild else [s for s in sessions if s not in frozen]
    if kite is None and pending:
        from auth.session_manager import get_kite
        kite = get_kite()
    done, errors = [], {}
    batch = pending[:max(1, min(int(limit), 60))]
    for session in batch:
        try:
            result = run_day(session, kite=kite, rebuild=rebuild)
            done.append({"date": session, "status": result.get("status"),
                         "trades": result.get("n_trades", 0), "net_rs": result.get("net_rs", 0)})
        except Exception as exc:                                  # noqa: BLE001
            errors[session] = f"{type(exc).__name__}: {exc}"
    remaining = max(0, len(pending) - len(batch))
    return {"done": done, "errors": errors, "remaining": remaining}


if __name__ == "__main__":
    import json
    import sys
    start = sys.argv[1] if len(sys.argv) > 1 else DEFAULT_START
    rebuild = "--rebuild" in sys.argv
    total = {"done": [], "errors": {}}
    while True:
        res = run_backfill(start_date=start, limit=60, rebuild=rebuild)
        total["done"] += res["done"]
        total["errors"].update(res["errors"])
        if res["errors"] or not res["remaining"]:
            break
        rebuild = False
    print(json.dumps(total, indent=2, default=str))
