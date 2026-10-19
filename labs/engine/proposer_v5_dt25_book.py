"""Paper book: Sensex V5 at 2.5% - Sensex V5 (the live preset proposer_dt35_v5: SENSEX Proposer v3 rules,
Sentinel's |delta| >= 0.60 strike, a 270 s wait after an exit, no premium floor and a trade closed once SENSEX
is 150 points against its entry, from 1 Jul 2026) with the day target at 2.5% instead of 3.5% (Vipin,
2026-10-19). Everything else as V5. Paper only: it never calls a broker order API. Owns the
proposer_v5_dt25_* tables.
"""
from __future__ import annotations

from functools import partial

from labs.engine import proposer_px_backfill as _backfill
from labs.engine import proposer_px_tracker as _tracker
from labs.engine import proposer_px_view as _view

BOOK = _tracker.V5_DT25
DEFAULT_START = _tracker.SETTINGS_FROM[BOOK.key]

record_unavailable = partial(_tracker.record_unavailable, book=BOOK)


def run_day(trade_date: str | None = None, **kwargs) -> dict:
    return _tracker.run_day(trade_date, book=BOOK, **kwargs)


def run_backfill(**kwargs) -> dict:
    kwargs.setdefault("start_date", DEFAULT_START)
    return _backfill.run_backfill(book=BOOK, **kwargs)


def tab_data(conn, date_clause: str = "", date_params=()) -> tuple:
    return _view.tab_data(conn, date_clause, date_params, book=BOOK)
