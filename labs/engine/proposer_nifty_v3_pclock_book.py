"""Paper book: Nifty Scalper V3 (Pramanaa timing) - Nifty Scalper V3 with Pramanaa's print timing: the forming candle counted, first print 09:21:45, then
one every 310.5 s (Pramanaa's scheduler). Paper only - not a live preset, from 1 Jul 2026. The replay is
labs.engine.proposer_nifty_tracker. Paper only: it never calls a broker order API. Owns the proposer_nifty_v3_pclock_* tables.
"""
from __future__ import annotations

from functools import partial

from labs.engine import proposer_px_backfill as _backfill
from labs.engine import proposer_px_tracker as _tracker
from labs.engine import proposer_px_view as _view

BOOK = _tracker.NIFTY_V3_PCLOCK
DEFAULT_START = _tracker.SETTINGS_FROM[BOOK.key]

record_unavailable = partial(_tracker.record_unavailable, book=BOOK)


def run_day(trade_date: str | None = None, **kwargs) -> dict:
    return _tracker.run_day(trade_date, book=BOOK, **kwargs)


def run_backfill(**kwargs) -> dict:
    kwargs.setdefault("start_date", DEFAULT_START)
    return _backfill.run_backfill(book=BOOK, **kwargs)


def tab_data(conn, date_clause: str = "", date_params=()) -> tuple:
    return _view.tab_data(conn, date_clause, date_params, book=BOOK)
