"""Paper book: Sensex V5 - Sensex V4 (the live preset proposer_dt35_v3 with Sentinel's |delta| >= 0.60
strike and a 270 s wait after an exit, from 1 Jul 2026) with one change: no premium floor; instead the
trade is closed once SENSEX is 150 points against its entry spot ("the 150 rule", alphaIMB REBUILD
sec. 33, P150; Vipin, 2026-10-17). Everything else as V4. There is no live preset for it.

Evidence status: 150 was picked in-sample (one of four values tried on 1 Jul - 16 Oct); it has not been
combined with the expiry-day skip; there is no SENSEX tick data, so the 150-point trigger runs on the
same simulated in-minute path (1-min bars, recorded snapshots) as every other exit.

Paper only: it never calls a broker order API. Owns the proposer_v5_* tables.
"""
from __future__ import annotations

from functools import partial

from labs.engine import proposer_px_backfill as _backfill
from labs.engine import proposer_px_tracker as _tracker
from labs.engine import proposer_px_view as _view

BOOK = _tracker.V5
DEFAULT_START = _tracker.SETTINGS_FROM[BOOK.key]

record_unavailable = partial(_tracker.record_unavailable, book=BOOK)


def run_day(trade_date: str | None = None, **kwargs) -> dict:
    return _tracker.run_day(trade_date, book=BOOK, **kwargs)


def run_backfill(**kwargs) -> dict:
    kwargs.setdefault("start_date", DEFAULT_START)
    return _backfill.run_backfill(book=BOOK, **kwargs)


def tab_data(conn, date_clause: str = "", date_params=()) -> tuple:
    return _view.tab_data(conn, date_clause, date_params, book=BOOK)
