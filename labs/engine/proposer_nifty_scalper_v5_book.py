"""Nifty Scalper V5 paper ledger: V4 entries, 2.5% day target, and spot floor."""
from __future__ import annotations

from datetime import date, datetime, timedelta
from functools import lru_cache
import json
from pathlib import Path

from labs.engine import proposer_px_backfill as backfill
from labs.engine import proposer_px_tracker as tracker
from labs.engine import proposer_px_view as view
from storage.db import get_conn


BOOK = tracker.NIFTY_SCALPER_V5
DEFAULT_START = tracker.SETTINGS_FROM[BOOK.key]
HISTORY = Path(__file__).with_name('proposer_nifty_scalper_v5_seed.json')


@lru_cache(maxsize=1)
def _history():
    rows = json.loads(HISTORY.read_text(encoding='utf-8'))
    return {row['trade_date']: row for row in rows}


def _seed(start: str, end: str, limit: int) -> tuple[list[dict], int]:
    historical = [row for day, row in _history().items() if start <= day <= end]
    if not historical:
        return [], 0
    conn = get_conn()
    try:
        tracker._ensure_tables(conn, BOOK)
        done = {row[0] for row in conn.execute(
            f"SELECT trade_date FROM {BOOK.key}_daily WHERE status IN ('closed','no_trade')")}
        pending = [row for row in historical if row['trade_date'] not in done]
        for row in pending[:limit]:
            tracker._persist(conn, row, BOOK)
        completed = [{'date': row['trade_date'], 'trades': row['n_trades'], 'net_rs': row['net_rs']}
                     for row in pending[:limit]]
        return completed, len(pending) - len(completed)
    finally:
        conn.close()


def run_day(trade_date: str | None = None, **kwargs) -> dict:
    now = kwargs.get('now') or datetime.now(tracker.IST)
    day = trade_date or now.date().isoformat()
    historical = _history().get(day)
    if historical is not None:
        if kwargs.get('persist', True):
            connection = kwargs.get('connection')
            if connection is not None:
                tracker._ensure_tables(connection, BOOK)
                tracker._persist(connection, historical, BOOK)
            else:
                _seed(day, day, 1)
        return {k: historical[k] for k in ('trade_date', 'status', 'regime', 'n_trades', 'gross_rs', 'net_rs')}
    if day < DEFAULT_START:
        raise ValueError(f'{BOOK.key} starts on {DEFAULT_START}')
    return tracker.run_day(day, book=BOOK, **kwargs)


def run_backfill(*, start_date: str = DEFAULT_START, end_date: str | None = None, limit: int = 5) -> dict:
    start = max(DEFAULT_START, date.fromisoformat(start_date).isoformat())
    end = date.fromisoformat(end_date).isoformat() if end_date else backfill._default_end_date()
    limit = max(1, min(int(limit), 20))
    seeded, remaining = _seed(start, end, limit)
    live_start = max(start, (date.fromisoformat(max(_history())) + timedelta(days=1)).isoformat())
    if remaining or live_start > end or len(seeded) == limit:
        if not remaining and live_start <= end:
            remaining = len(backfill.sessions_with_quotes(live_start, end))
        return {'done': seeded, 'unavailable': [], 'errors': {}, 'remaining': remaining}
    result = backfill.run_backfill(book=BOOK, start_date=live_start, end_date=end, limit=limit-len(seeded))
    result['done'] = seeded + result['done']
    return result


def tab_data(conn, date_clause: str = '', date_params=()) -> tuple:
    return view.tab_data(conn, date_clause, date_params, book=BOOK)
