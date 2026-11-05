"""Paper book: Nifty Scalper V4 2.5% - the Nifty Scalper V4 book (labs.engine.proposer_nifty_v4_book: V3 with the entry
blocks, banking the day target whatever the book) with the day target at 2.5% instead of 1.5% - the live preset
proposer_nifty_scalper_v4 with 2.5% chosen on the configure page, from 1 Jul 2026. Paper only: it never calls a broker
order API. Owns the proposer_nifty_v4_dt25_* tables.
"""
from __future__ import annotations

from functools import partial

from labs.engine import proposer_px_backfill as _backfill
from labs.engine import proposer_px_tracker as _tracker
from labs.engine import proposer_px_view as _view

BOOK = _tracker.NIFTY_V4_DT25
DEFAULT_START = _tracker.SETTINGS_FROM[BOOK.key]

record_unavailable = partial(_tracker.record_unavailable, book=BOOK)


def run_day(trade_date: str | None = None, **kwargs) -> dict:
    return _tracker.run_day(trade_date, book=BOOK, **kwargs)


def run_backfill(**kwargs) -> dict:
    kwargs.setdefault("start_date", DEFAULT_START)
    return _backfill.run_backfill(book=BOOK, **kwargs)


def tab_data(conn, date_clause: str = "", date_params=()) -> tuple:
    return _view.tab_data(conn, date_clause, date_params, book=BOOK)
