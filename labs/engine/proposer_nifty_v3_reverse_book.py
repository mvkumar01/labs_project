"""Paper book: Nifty Scalper V3 Reverse - the live preset proposer_nifty_scalper_v3_reverse, from 1 Jul 2026. The V3 trade
runs as an unseen shadow (V3's prints every 3 minutes, entry gate, exits, 1.5% day target and recovery book) and the
book holds the opposite option, bought when the shadow enters and sold when it exits. The replay is
labs.engine.proposer_nifty_tracker (Book.reverse). Paper only: it never calls a broker order API. Owns the
proposer_nifty_v3_reverse_* tables (Vipin, 2026-11-13).
"""
from __future__ import annotations

from functools import partial

from labs.engine import proposer_px_backfill as _backfill
from labs.engine import proposer_px_tracker as _tracker
from labs.engine import proposer_px_view as _view

BOOK = _tracker.NIFTY_V3_REVERSE
DEFAULT_START = _tracker.SETTINGS_FROM[BOOK.key]

record_unavailable = partial(_tracker.record_unavailable, book=BOOK)


def run_day(trade_date: str | None = None, **kwargs) -> dict:
    return _tracker.run_day(trade_date, book=BOOK, **kwargs)


def run_backfill(**kwargs) -> dict:
    kwargs.setdefault("start_date", DEFAULT_START)
    return _backfill.run_backfill(book=BOOK, **kwargs)


def tab_data(conn, date_clause: str = "", date_params=()) -> tuple:
    return _view.tab_data(conn, date_clause, date_params, book=BOOK)
