"""Independent paper book for SENSEX Proposer v3 with a 3.5% day target.

This uses the same signal, contract, fill and exit machinery as SENSEX Proposer v3. Its only
strategy-parameter difference is the 3.5% day target. It owns the existing
``proposer_v3_dt35_*`` ledger tables and never places broker orders.
"""
from __future__ import annotations

from functools import partial

from labs.engine import proposer_px_backfill as _backfill
from labs.engine import proposer_px_tracker as _tracker
from labs.engine import proposer_px_view as _view

BOOK = _tracker.V3_DT35
DEFAULT_START = _tracker.DEFAULT_START

record_unavailable = partial(_tracker.record_unavailable, book=BOOK)


def run_day(trade_date: str | None = None, **kwargs) -> dict:
    """Run only the independent 3.5% paper book for one session."""
    return _tracker.run_day(trade_date, book=BOOK, **kwargs)


def run_backfill(**kwargs) -> dict:
    """Backfill only the independent 3.5% paper ledger."""
    return _backfill.run_backfill(book=BOOK, **kwargs)


def tab_data(conn, date_clause: str = "", date_params=()) -> tuple:
    """Return dashboard rows, trades and statistics for this book."""
    return _view.tab_data(conn, date_clause, date_params, book=BOOK)
