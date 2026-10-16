"""Paper book: SENSEX Proposer v3 3.5% (the live preset proposer_dt35_v3) on Pramanaa's print timing.

The same signal, contract, fill and exit machinery as the Sensex V3 3.5% book; the only difference
is the clock its 5-class prints come on: every 310.5 s counted from when each was due - Pramanaa's
scheduler (one loop of 60 s + its work, printing once 300 s have passed) - instead of every exact
five minutes. A trial of aligning the predictor with Pramanaa before it goes live (Vipin,
2026-10-16). Strike: Sentinel's |delta| >= 0.60 rule from 2026-10-19, ATM -/+200 before, like the
other books. Paper only: it never calls a broker order API. Owns the proposer_v3_dt35_clock_* tables.
"""
from __future__ import annotations

from functools import partial

from labs.engine import proposer_px_backfill as _backfill
from labs.engine import proposer_px_tracker as _tracker
from labs.engine import proposer_px_view as _view

BOOK = _tracker.V3_DT35_CLOCK
DEFAULT_START = _tracker.DEFAULT_START

record_unavailable = partial(_tracker.record_unavailable, book=BOOK)


def run_day(trade_date: str | None = None, **kwargs) -> dict:
    return _tracker.run_day(trade_date, book=BOOK, **kwargs)


def run_backfill(**kwargs) -> dict:
    return _backfill.run_backfill(book=BOOK, **kwargs)


def tab_data(conn, date_clause: str = "", date_params=()) -> tuple:
    return _view.tab_data(conn, date_clause, date_params, book=BOOK)
