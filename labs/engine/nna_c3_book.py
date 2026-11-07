"""Paper book: Nifty New Alpha V1 twin C3 points (labs.engine.nna_twins_tracker, key nna_c3). Paper only: it never
calls a broker order API. Owns the nna_c3_* tables. Here for labs.engine.tick_replay's post-close pass."""
from __future__ import annotations

from labs.engine import nna_twins_tracker as _tracker

KEY = "nna_c3"
BOOK = _tracker.BOOKS[KEY]


def run_day(trade_date: str | None = None, **kwargs) -> dict:
    return _tracker.run_day(trade_date, key=KEY, **kwargs)


def tab_data(conn, date_clause: str = "", date_params=()) -> tuple:
    return _tracker.tab_data(conn, KEY, date_clause, date_params)
