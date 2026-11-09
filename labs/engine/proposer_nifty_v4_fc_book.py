"""Paper book: Nifty Scalper V4 1.5% · fixed clock - the Nifty Scalper V4 1.5% book (labs.engine.proposer_nifty_tracker) with every print acted
on exactly 30 s into its minute (Book.print_lag_s) and no new entry after the day's 2nd losing trade
(proposer_px_tracker.MAX_LOSSES_PER_DAY), from 1 Jul 2026; recorded ticks from 27 Oct (tick_replay.TICK_BOOKS).
Paper only: it never calls a broker order API. Owns the proposer_nifty_v4_fc_* tables.
"""
from __future__ import annotations

from functools import partial

from labs.engine import proposer_px_backfill as _backfill
from labs.engine import proposer_px_tracker as _tracker
from labs.engine import proposer_px_view as _view

BOOK = _tracker.NIFTY_V4_FC
DEFAULT_START = _tracker.SETTINGS_FROM[BOOK.key]

record_unavailable = partial(_tracker.record_unavailable, book=BOOK)


def run_day(trade_date: str | None = None, **kwargs) -> dict:
    return _tracker.run_day(trade_date, book=BOOK, **kwargs)


def run_backfill(**kwargs) -> dict:
    kwargs.setdefault("start_date", DEFAULT_START)
    return _backfill.run_backfill(book=BOOK, **kwargs)


def tab_data(conn, date_clause: str = "", date_params=()) -> tuple:
    return _view.tab_data(conn, date_clause, date_params, book=BOOK)
