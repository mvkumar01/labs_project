"""Paper book: Sensex V4 - the live preset proposer_dt35_v3 (SENSEX Proposer v3 rules, 3.5% day target)
with the live settings of 16 Oct 2026 from its first session (1 Jul 2026): Sentinel's |delta| >= 0.60
strike (ATM -/+200..500) and a 270 s wait after an exit. The other books take both only from 19 Oct, so
this one shows how they would have traded (Vipin, 2026-10-16). Same signal, contract, fill and exit
machinery as the Sensex V3 3.5% book. Paper only: it never calls a broker order API. Owns the
proposer_v4_* tables.
"""
from __future__ import annotations

from functools import partial

from labs.engine import proposer_px_backfill as _backfill
from labs.engine import proposer_px_tracker as _tracker
from labs.engine import proposer_px_view as _view

BOOK = _tracker.V4
DEFAULT_START = _tracker.SETTINGS_FROM[BOOK.key]

record_unavailable = partial(_tracker.record_unavailable, book=BOOK)


def run_day(trade_date: str | None = None, **kwargs) -> dict:
    return _tracker.run_day(trade_date, book=BOOK, **kwargs)


def run_backfill(**kwargs) -> dict:
    kwargs.setdefault("start_date", DEFAULT_START)
    return _backfill.run_backfill(book=BOOK, **kwargs)


def tab_data(conn, date_clause: str = "", date_params=()) -> tuple:
    return _view.tab_data(conn, date_clause, date_params, book=BOOK)
