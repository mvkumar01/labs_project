"""SENSEX Proposer v3: paper book.

Paper only. This module never calls a broker order API.

v3 is the live SENSEX Proposer (`proposer_dt25`: gap-rule regime read at the 09:15 close, 5-class
print, +100 / +40 spot points, opposite print, 2.5% day target, 15:25) with one entry filter - a
trade is taken only WITH THE DAY: SENSEX's last completed 1-minute close must be at least 75 points
beyond the 09:15 close in the trade's direction - and the premium floor at -20% (the other presets:
-30%; changed 2026-10-10, the book was rebuilt from 1 June then). No Renko exit, no loss cap.
Live preset `proposer_dt25_v3` runs the same engine parameters.

Why: on 1 Jun - 8 Oct 2026 the entries that were not with the day carried most of the trades that
ended worse than Rs 1 lakh at 100 lots, and none of v3's winners dipped below -20% before winning
(alphaIMB research, REBUILD.md sec. 18 and 20). Both levels were chosen on that same data, so this
book is the test of them: the 75 points from 9 Oct 2026 on, the -20% floor from 12 Oct 2026 on.

The replay, fills model and ledger layout are the price-action book's
(labs/engine/proposer_px_tracker.py); this module binds them to the v3 ledger
(tables proposer_v3_daily / proposer_v3_trades).
"""
from __future__ import annotations

from functools import partial

from labs.engine import proposer_px_backfill as _backfill
from labs.engine import proposer_px_tracker as _tracker
from labs.engine import proposer_px_view as _view

BOOK = _tracker.V3
DEFAULT_START = _tracker.DEFAULT_START
FIRST_UNSEEN_SESSION = "2026-10-09"       # sessions before this are the ones the rule was fitted on

run_day = partial(_tracker.run_day, book=BOOK)
run_backfill = partial(_backfill.run_backfill, book=BOOK)
record_unavailable = partial(_tracker.record_unavailable, book=BOOK)


def tab_data(conn, date_clause: str = "", date_params=()) -> tuple:
    """(daily rows, trades, stats) for the /labs/live tab, with the fitted / unseen split."""
    rows, trades, stats = _view.tab_data(conn, date_clause, date_params, book=BOOK)
    if stats:
        done = [r for r in rows if r["status"] in ("closed", "no_trade")]
        unseen = [r for r in done if r["trade_date"] >= FIRST_UNSEEN_SESSION]
        stats["first_unseen"] = FIRST_UNSEEN_SESSION
        stats["unseen_days"] = len(unseen)
        stats["unseen_net"] = round(sum(float(r["net_rs"] or 0) for r in unseen), 2)
        stats["fitted_net"] = round(stats["net_total"] - stats["unseen_net"], 2)
    return rows, trades, stats
