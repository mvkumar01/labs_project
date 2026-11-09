"""Paper book: Sensex 2.5% V6 IV-half - Sensex 2.5% V6 (labs.engine.proposer_v6_dt25_book: Sensex V5 2.5% with no entry
while SENSEX is inside the previous session's CPR) at half size (1,000 qty instead of 2,000) for the whole day when the
09:20 SENSEX ATM implied vol is above 1.2 x the median 09:20 IV of the earlier sessions from 1 Jul (at least 10 of
them), from 09:20 on (entries before 09:20 are full size); the 2.5% day target is a share of the open position's premium, so it halves with the size (Vipin, 2026-11-09;
proposer_px_tracker.IV_HALF_BOOKS). Everything else as V6, replayed on the recorded SENSEX ticks from 27 Oct. Paper
only: it never calls a broker order API; no live preset. Owns the proposer_v6_dt25_ivh_* tables.
"""
from __future__ import annotations

from functools import partial

from labs.engine import proposer_px_backfill as _backfill
from labs.engine import proposer_px_tracker as _tracker
from labs.engine import proposer_px_view as _view

BOOK = _tracker.V6_DT25_IVH
DEFAULT_START = _tracker.SETTINGS_FROM[BOOK.key]

record_unavailable = partial(_tracker.record_unavailable, book=BOOK)


def run_day(trade_date: str | None = None, **kwargs) -> dict:
    return _tracker.run_day(trade_date, book=BOOK, **kwargs)


def run_backfill(**kwargs) -> dict:
    kwargs.setdefault("start_date", DEFAULT_START)
    return _backfill.run_backfill(book=BOOK, **kwargs)


def tab_data(conn, date_clause: str = "", date_params=()) -> tuple:
    return _view.tab_data(conn, date_clause, date_params, book=BOOK)
