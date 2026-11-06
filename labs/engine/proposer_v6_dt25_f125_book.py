"""Paper book: Sensex 2.5% V6 floor 125 - Sensex 2.5% V6 (labs.engine.proposer_v6_dt25_book: Sensex V5 2.5% with no entry
while SENSEX is inside the previous session's CPR) with the SENSEX loss floor at 125 points against the entry instead of
150 (Vipin, 2026-11-06). Everything else as V6. A forward test: 125 was picked after looking at October. Paper only: it
never calls a broker order API; no live preset. Owns the proposer_v6_dt25_f125_* tables.
"""
from __future__ import annotations

from functools import partial

from labs.engine import proposer_px_backfill as _backfill
from labs.engine import proposer_px_tracker as _tracker
from labs.engine import proposer_px_view as _view

BOOK = _tracker.V6_DT25_F125
DEFAULT_START = _tracker.SETTINGS_FROM[BOOK.key]

record_unavailable = partial(_tracker.record_unavailable, book=BOOK)


def run_day(trade_date: str | None = None, **kwargs) -> dict:
    return _tracker.run_day(trade_date, book=BOOK, **kwargs)


def run_backfill(**kwargs) -> dict:
    kwargs.setdefault("start_date", DEFAULT_START)
    return _backfill.run_backfill(book=BOOK, **kwargs)


def tab_data(conn, date_clause: str = "", date_params=()) -> tuple:
    return _view.tab_data(conn, date_clause, date_params, book=BOOK)
