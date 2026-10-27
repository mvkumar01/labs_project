"""Paper book: NIFTY on Sensex V5 signals - the Sensex V5 2.5% book's SENSEX signals traded on NIFTY options
with NIFTY's own exits (next weekly on NIFTY's expiry day, spot targets 1.5 x the scaled SENSEX ones, the
150-point floor scaled to NIFTY, a 1.5% day target, Rs 12L of premium per entry), from 1 Jul 2026. The
replay is labs.engine.proposer_nifty_tracker. Paper only: it never calls a broker order API. Owns the
proposer_nifty_v5_* tables.

Two twins run with it - same run, same backfill - and are shown on the same tab: Nifty Scalper V3 (prints every
3 minutes, the live preset proposer_nifty_scalper_v3; proposer_nifty_v3_* tables) and Nifty Scalper V3 on
Pramanaa's print clock (forming candle counted, first print 09:21:45, then every 310.5 s; paper only;
proposer_nifty_v3_pclock_* tables). A twin failure never stops the main book.
"""
from __future__ import annotations

import logging
from functools import partial

from labs.engine import proposer_px_backfill as _backfill
from labs.engine import proposer_px_tracker as _tracker
from labs.engine import proposer_px_view as _view

BOOK = _tracker.NIFTY_V5
TWINS = ((_tracker.NIFTY_V3, "Nifty Scalper V3 - prints every 3 min (live preset)"),
         (_tracker.NIFTY_V3_PCLOCK, "Nifty Scalper V3 - Pramanaa's timing (paper only)"))
DEFAULT_START = _tracker.SETTINGS_FROM[BOOK.key]
log = logging.getLogger(__name__)

record_unavailable = partial(_tracker.record_unavailable, book=BOOK)


def _twin(fn, book, *args, **kwargs):
    try:
        return fn(*args, book=book, **kwargs)
    except Exception as e:                       # a twin is a comparison; never stop the main book
        log.warning("%s twin failed: %s", book.key, e)
        return {"error": str(e)}


def run_day(trade_date: str | None = None, **kwargs) -> dict:
    out = _tracker.run_day(trade_date, book=BOOK, **kwargs)
    twins = {book.key: (_twin(_tracker.run_day, book, trade_date, **kwargs) or {}).get("net_rs") for book, _ in TWINS}
    return {**out, "twins_net_rs": twins} if isinstance(out, dict) else out


def run_backfill(**kwargs) -> dict:
    kwargs.setdefault("start_date", DEFAULT_START)
    out = _backfill.run_backfill(book=BOOK, **kwargs)
    for book, _ in TWINS:
        twin = _twin(_backfill.run_backfill, book, **kwargs) or {}
        if isinstance(out, dict) and not twin.get("error"):
            # the tab's button repeats while anything remains, so the twins fill from 1 July too
            out = {**out, "remaining": max(int(out.get("remaining") or 0), int(twin.get("remaining") or 0))}
    return out


def tab_data(conn, date_clause: str = "", date_params=()) -> tuple:
    rows, trades, stats = _view.tab_data(conn, date_clause, date_params, book=BOOK)
    if not isinstance(stats, dict):
        return rows, trades, stats
    twins = []
    for book, label in TWINS:
        try:
            t_rows, _, t_stats = _view.tab_data(conn, date_clause, date_params, book=book)
        except Exception as e:
            t_rows, t_stats = [], {"error": str(e)}
        twins.append({"key": book.key, "label": label, "stats": t_stats,
                      "by_day": {r["trade_date"]: r.get("net_rs") for r in t_rows}})
    days = [{"trade_date": r["trade_date"], "v1": r.get("net_rs"),
             "twins": [t["by_day"].get(r["trade_date"]) for t in twins]} for r in rows[:40]]
    return rows, trades, {**stats, "twins": twins, "twin_days": days}
