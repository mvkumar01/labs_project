"""Paper book: NIFTY on Sensex V5 signals - the Sensex V5 2.5% book's SENSEX signals traded on NIFTY options
with NIFTY's own exits (next weekly on NIFTY's expiry day, spot targets 1.5 x the scaled SENSEX ones, the
150-point floor scaled to NIFTY, a 1.5% day target, Rs 12L of premium per entry), from 1 Jul 2026. The
replay is labs.engine.proposer_nifty_tracker. Paper only: it never calls a broker order API. Owns the
proposer_nifty_v5_* tables.

Its twin on Pramanaa's print clock (NIFTY_V5_PCLOCK: forming candle counted, first print 09:21:45, then every
310.5 s; proposer_nifty_v5_pclock_* tables) runs with it - same run, same backfill - and is shown on the same tab.
A twin failure never stops the main book.
"""
from __future__ import annotations

import logging
from functools import partial

from labs.engine import proposer_px_backfill as _backfill
from labs.engine import proposer_px_tracker as _tracker
from labs.engine import proposer_px_view as _view

BOOK = _tracker.NIFTY_V5
TWIN = _tracker.NIFTY_V5_PCLOCK
DEFAULT_START = _tracker.SETTINGS_FROM[BOOK.key]
log = logging.getLogger(__name__)

record_unavailable = partial(_tracker.record_unavailable, book=BOOK)


def _twin(fn, *args, **kwargs):
    try:
        return fn(*args, book=TWIN, **kwargs)
    except Exception as e:                       # the twin is a comparison; never stop the main book
        log.warning("Pramanaa-clock twin failed: %s", e)
        return {"error": str(e)}


def run_day(trade_date: str | None = None, **kwargs) -> dict:
    out = _tracker.run_day(trade_date, book=BOOK, **kwargs)
    twin = _twin(_tracker.run_day, trade_date, **kwargs)
    if isinstance(out, dict):
        out = {**out, "pramanaa_clock_net_rs": (twin or {}).get("net_rs")}
    return out


def run_backfill(**kwargs) -> dict:
    kwargs.setdefault("start_date", DEFAULT_START)
    out = _backfill.run_backfill(book=BOOK, **kwargs)
    twin = _twin(_backfill.run_backfill, **kwargs) or {}
    if isinstance(out, dict) and not twin.get("error"):
        # the tab's button repeats while anything remains, so the twin fills from 1 July too
        out = {**out, "remaining": max(int(out.get("remaining") or 0), int(twin.get("remaining") or 0)),
               "pramanaa_clock_done": twin.get("done", [])}
    return out


def tab_data(conn, date_clause: str = "", date_params=()) -> tuple:
    rows, trades, stats = _view.tab_data(conn, date_clause, date_params, book=BOOK)
    try:
        t_rows, t_trades, t_stats = _view.tab_data(conn, date_clause, date_params, book=TWIN)
    except Exception as e:
        t_rows, t_trades, t_stats = [], [], {"error": str(e)}
    if isinstance(stats, dict):
        by_day = {r["trade_date"]: r for r in t_rows}
        stats = {**stats, "pclock": t_stats, "pclock_trades": t_trades[:60],
                 "pclock_days": [{"trade_date": r["trade_date"], "ours": r.get("net_rs"),
                                  "pclock": (by_day.get(r["trade_date"]) or {}).get("net_rs")}
                                 for r in rows[:40]]}
    return rows, trades, stats
