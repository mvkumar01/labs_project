"""Paper book: NIFTY on Sensex V5 signals - the Sensex V5 2.5% book's SENSEX signals traded on NIFTY options
with NIFTY's own exits (next weekly on NIFTY's expiry day, spot targets 1.5 x the scaled SENSEX ones, the
150-point floor scaled to NIFTY, a 1.5% day target, Rs 12L of premium per entry), from 1 Jul 2026. The
replay is labs.engine.proposer_nifty_tracker. Paper only: it never calls a broker order API. Owns the
proposer_nifty_v5_* tables.
"""
from __future__ import annotations

from functools import partial

from labs.engine import proposer_px_backfill as _backfill
from labs.engine import proposer_px_tracker as _tracker
from labs.engine import proposer_px_view as _view

BOOK = _tracker.NIFTY_V5
DEFAULT_START = _tracker.SETTINGS_FROM[BOOK.key]

record_unavailable = partial(_tracker.record_unavailable, book=BOOK)


def run_day(trade_date: str | None = None, **kwargs) -> dict:
    return _tracker.run_day(trade_date, book=BOOK, **kwargs)


def run_backfill(**kwargs) -> dict:
    kwargs.setdefault("start_date", DEFAULT_START)
    return _backfill.run_backfill(book=BOOK, **kwargs)


def tab_data(conn, date_clause: str = "", date_params=()) -> tuple:
    return _view.tab_data(conn, date_clause, date_params, book=BOOK)
