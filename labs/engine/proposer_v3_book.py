"""SENSEX Proposer v3: paper book.

Paper only. This module never calls a broker order API.

v3 is the live SENSEX Proposer (`proposer_dt25`: gap-rule regime read at the 09:15 close, 5-class
print, +100 / +40 spot points, opposite print, 2.5% day target, 15:25) with one entry filter - a
trade is taken only WITH THE DAY: SENSEX's last completed 1-minute close must be at least 75 points
beyond the 09:15 close in the trade's direction - and the premium floor at -20% (the other presets:
-30%; changed 2026-10-10, the book was rebuilt from 1 June then). No Renko exit, no loss cap.
Live preset `proposer_dt25_v3` runs the same engine parameters.

The companion `proposer_dt35_v3` book and live preset change only the day target from 2.5% to 3.5%.
Unlike the Pramanaa-clock shadows, both v3 books replay the full history from 1 June 2026.

Why: on 1 Jun - 8 Oct 2026 the entries that were not with the day carried most of the trades that
ended worse than Rs 1 lakh at 100 lots, and none of v3's winners dipped below -20% before winning
(alphaIMB research, REBUILD.md sec. 18 and 20). Both levels were chosen on that same data, so this
book is the test of them: the 75 points from 9 Oct 2026 on, the -20% floor from 12 Oct 2026 on.

The replay, fills model and ledger layout are the price-action book's
(labs/engine/proposer_px_tracker.py); this module binds them to the v3 ledger
(tables proposer_v3_daily / proposer_v3_trades).

Shadows (from 2026-10-14), paper only, no live preset runs them: Pramanaa's own rules (the base
`proposer_dt25`, no with-the-day filter) on our five-minute print clock and on Pramanaa's own print
clock (a print every 310.5 s, the gap of its scheduler's loop), and v3 on Pramanaa's clock. They are
compared, day by day, with what Pramanaa's predictor printed and its paper bot traded
(labs/engine/pramanaa_capture.py): the pair on Pramanaa's rules isolates what the clock alone does.
"""
from __future__ import annotations

import logging
import sqlite3
from functools import partial

from labs.engine import pramanaa_capture as _pramanaa
from labs.engine import proposer_px_backfill as _backfill
from labs.engine import proposer_px_tracker as _tracker
from labs.engine import proposer_px_view as _view

log = logging.getLogger("labs.proposer_v3")
BOOK = _tracker.V3
# Full-history live-preset companions. These deliberately do not inherit SHADOW_START.
VARIANTS = ((_tracker.V3_DT35, "v3 at 3.5% (live preset), our clock"),)
# (book, label) - the order the comparison shows them in
SHADOWS = ((_tracker.DT25, "Pramanaa's rules, our clock"),
           (_tracker.DT25_CLOCK, "Pramanaa's rules, Pramanaa's clock"),
           (_tracker.V3_CLOCK, "v3, Pramanaa's clock"))
DEFAULT_START = _tracker.DEFAULT_START
FIRST_UNSEEN_SESSION = "2026-10-09"       # sessions before this are the ones the rule was fitted on
# The shadows start where the saved Pramanaa ledger starts. Their books are separate from 1 June: a
# shadow whose running total goes negative early loses the day-target stop (recovery mode) and
# its replay runs away from anything Pramanaa did, which would swamp the comparison.
SHADOW_START = "2026-09-21"

record_unavailable = partial(_tracker.record_unavailable, book=BOOK)


def run_day(trade_date: str | None = None, **kwargs) -> dict:
    """The paper loop's entry point: v3, its full-history variants, then its shadows."""
    result = _tracker.run_day(trade_date, book=BOOK, **kwargs)
    for book, _ in VARIANTS + SHADOWS:
        try:
            _tracker.run_day(trade_date, book=book, **kwargs)
        except Exception as exc:                           # noqa: BLE001 - logged, v3 unaffected
            log.warning("proposer comparison book %s failed for %s: %s", book.key, trade_date, exc)
    return result


def run_backfill(**kwargs) -> dict:
    """Backfill v3 and 3.5% from the requested start, then shadows from SHADOW_START."""
    result = _backfill.run_backfill(book=BOOK, **kwargs)
    result["variants"] = {}
    for book, _ in VARIANTS:
        try:
            result["variants"][book.key] = _backfill.run_backfill(book=book, **kwargs)
        except Exception as exc:                           # noqa: BLE001
            result["variants"][book.key] = {"error": f"{type(exc).__name__}: {exc}"}
    result["shadows"] = {}
    shadow_kwargs = {**kwargs, "start_date": max(kwargs.get("start_date") or DEFAULT_START, SHADOW_START)}
    for book, _ in SHADOWS:
        try:
            result["shadows"][book.key] = _backfill.run_backfill(book=book, **shadow_kwargs)
        except Exception as exc:                           # noqa: BLE001
            result["shadows"][book.key] = {"error": f"{type(exc).__name__}: {exc}"}
    return result


def _table_rows(conn, sql: str, params=()) -> list[dict]:
    try:
        cur = conn.execute(sql, tuple(params))
    except sqlite3.OperationalError:                       # table not created yet
        return []
    cols = [c[0] for c in cur.description]
    return [dict(zip(cols, r)) for r in cur.fetchall()]


def shadow_comparison(conn) -> dict:
    """Day by day: v3 and its shadows and, where Pramanaa's pages have been imported, Pramanaa's
    prints (minute by minute) and its paper bot's trades."""
    done = "status IN ('closed','no_trade')"
    books = ([(BOOK.key, "v3 at 2.5% (live preset), our clock")]
             + [(b.key, label) for b, label in VARIANTS + SHADOWS])
    nets, minutes, entries = {}, {}, {}
    for key, _ in books:
        nets[key] = {r["trade_date"]: float(r["net_rs"] or 0) for r in _table_rows(
            conn, f"SELECT trade_date, net_rs FROM {key}_daily WHERE {done}")}
        for r in _table_rows(conn, f"SELECT trade_date, minute FROM {key}_prints"):
            minutes.setdefault((key, r["trade_date"]), set()).add(r["minute"])
        for r in _table_rows(conn, f"SELECT trade_date, entry_ts, side FROM {key}_trades ORDER BY entry_ts"):
            entries.setdefault((key, r["trade_date"]), []).append(f"{r['entry_ts'][11:19]} {r['side']}")
    comparison_days = set().union(*(set(nets[b.key]) for b, _ in VARIANTS + SHADOWS))
    if not comparison_days:
        return {}
    pram_prints: dict = {}
    for r in _table_rows(conn, "SELECT trade_date, minute FROM pramanaa_prints"):
        pram_prints.setdefault(r["trade_date"], set()).add(r["minute"])
    pram_trades: dict = {}
    for r in _table_rows(conn, "SELECT trade_date, entry_time, side, pnl_rs FROM pramanaa_trades ORDER BY entry_time"):
        pram_trades.setdefault(r["trade_date"], []).append(r)

    common = sorted(set.intersection(*(set(nets[k]) for k, _ in books)), reverse=True)
    days, match_tot = [], {k: [0, 0] for k, _ in books}
    for day in common:
        row = {"trade_date": day, "books": {}}
        pram = pram_prints.get(day)
        for key, _ in books:
            ours = minutes.get((key, day))
            m = (sum(1 for x in pram if x in ours), len(pram)) if pram and ours is not None else None
            if m and all(minutes.get((k, day)) is not None for k, _ in books):
                match_tot[key][0] += m[0]
                match_tot[key][1] += m[1]
            row["books"][key] = {"net": nets[key][day], "entries": entries.get((key, day), []), "match": m}
        pt = pram_trades.get(day, [])
        known = bool(pt) or _pramanaa.covered(conn, "trades", day)
        row["pramanaa_net"] = round(sum(float(r["pnl_rs"] or 0) for r in pt), 2) if known else None
        row["pramanaa_entries"] = [f"{r['entry_time']} {r['side'] or ''}".strip() for r in pt]
        days.append(row)
    with_pram = [d for d in days if d["pramanaa_net"] is not None]
    summary = []
    for key, label in books:
        t = match_tot[key]
        ordered_nets = [nets[key][day] for day in sorted(nets[key])]
        equity = peak = max_dd = 0.0
        for net in ordered_nets:
            equity += net
            peak = max(peak, equity)
            max_dd = max(max_dd, peak - equity)
        summary.append({
            "key": key, "label": label, "net": round(sum(d["books"][key]["net"] for d in days), 2),
            "total_net": round(sum(ordered_nets), 2),
            "worst_day": round(min(ordered_nets, default=0.0), 2),
            "max_dd": round(max_dd, 2),
            "trades": sum(len(entries.get((key, day), [])) for day in nets[key]),
            "sessions": len(ordered_nets),
            "net_on_pramanaa_days": round(sum(d["books"][key]["net"] for d in with_pram), 2),
            "minute_match_pct": round(100 * t[0] / t[1], 1) if t[1] else None,
            "same_as_pramanaa_side_days": sum(
                1 for d in with_pram if d["pramanaa_entries"] and d["books"][key]["entries"]
                and d["books"][key]["entries"][0].split()[-1] == d["pramanaa_entries"][0].split()[-1]),
        })
    return {
        "days": days[:120], "sessions": len(days), "books": summary,
        "pramanaa_days": len(with_pram), "pramanaa_net": round(sum(d["pramanaa_net"] for d in with_pram), 2),
        "pramanaa_traded_days": sum(1 for d in with_pram if d["pramanaa_entries"]),
        "pramanaa_print_count": match_tot[BOOK.key][1], "gap_s": _tracker.PRAMANAA_PRINT_GAP_S,
    }


def tab_data(conn, date_clause: str = "", date_params=()) -> tuple:
    """(daily rows, trades, stats) for the /labs/live tab, with the fitted / unseen split and the
    shadow comparison."""
    rows, trades, stats = _view.tab_data(conn, date_clause, date_params, book=BOOK)
    if stats:
        done = [r for r in rows if r["status"] in ("closed", "no_trade")]
        unseen = [r for r in done if r["trade_date"] >= FIRST_UNSEEN_SESSION]
        stats["first_unseen"] = FIRST_UNSEEN_SESSION
        stats["unseen_days"] = len(unseen)
        stats["unseen_net"] = round(sum(float(r["net_rs"] or 0) for r in unseen), 2)
        stats["fitted_net"] = round(stats["net_total"] - stats["unseen_net"], 2)
        try:
            stats["shadow"] = shadow_comparison(conn)
        except Exception as exc:                           # noqa: BLE001 - the v3 tab still renders
            stats["shadow"] = {"error": f"{type(exc).__name__}: {exc}"}
    return rows, trades, stats
