"""Basket replay: how alternative option structures would have performed on the
exact v2.11 champion signal timing.

Reads the persisted v2.11 paper ledger (paper_strategy_trades: side, entry_ts,
exit_ts, entry_spot per segment) and re-prices each segment as a multi-leg
BASKET from the nearest-expiry exact-minute bid/ask book (same quote source as
the v2.12 tracker). BUY legs fill at ask and close at bid; SELL legs fill at
bid and close at ask; charges are applied per leg. Missing quotes make the
whole trade explicitly "unavailable" — never a partial basket, never an LTP
fallback. Paper only; this module never places orders.

Rationale (2026-07-07): on a breakeven entry-spot stop a naked long option
still loses (theta/IV/spread); a delta~1 synthetic books ~0 by construction,
and debit spreads hard-cap the loss. This tab measures those trade-offs on the
real signal history instead of arguing about them.
"""
from __future__ import annotations

import json
import sqlite3
from datetime import datetime

from labs.engine.alpha_v212_tracker import (
    QTY,
    build_executable_book,
    _timestamp_key,
)
from labs.engine.charges import round_trip_charges
from labs.engine.paper_strategy_tracker import _r50
from storage.db import get_conn


# Strike offsets are points relative to the segment's entry spot, rounded to
# the nearest 50. ITM for a CE is BELOW spot (-200); ITM for a PE is ABOVE
# (+200). Every leg uses the same contract at entry and exit.
BASKETS = {
    "CALL": {
        "long_synthetic": {
            "label": "Long Synthetic (B ATM CE + S ATM PE)",
            "legs": [("BUY", "ce", 0), ("SELL", "pe", 0)],
        },
        "protected_synthetic_long": {
            "label": "Protected Synthetic Long (B ATM CE + S ATM PE + B OTM200 PE)",
            "legs": [("BUY", "ce", 0), ("SELL", "pe", 0), ("BUY", "pe", -200)],
        },
        "bull_call_spread": {
            "label": "Bull Call Spread (B ITM200 CE + S OTM200 CE)",
            "legs": [("BUY", "ce", -200), ("SELL", "ce", 200)],
        },
        "protected_call": {
            "label": "Protected Call (B ITM200 CE + B OTM200 PE)",
            "legs": [("BUY", "ce", -200), ("BUY", "pe", -200)],
        },
    },
    "PUT": {
        "short_synthetic": {
            "label": "Short Synthetic (B ATM PE + S ATM CE)",
            "legs": [("BUY", "pe", 0), ("SELL", "ce", 0)],
        },
        "protected_synthetic_short": {
            "label": "Protected Synthetic Short (B ATM PE + S ATM CE + B OTM200 CE)",
            "legs": [("BUY", "pe", 0), ("SELL", "ce", 0), ("BUY", "ce", 200)],
        },
        "bear_put_spread": {
            "label": "Bear Put Spread (B ITM200 PE + S OTM200 PE)",
            "legs": [("BUY", "pe", 200), ("SELL", "pe", -200)],
        },
        "protected_put": {
            "label": "Protected Put (B ITM200 PE + B OTM200 CE)",
            "legs": [("BUY", "pe", 200), ("BUY", "ce", 200)],
        },
    },
}


def _ensure_tables(conn: sqlite3.Connection) -> None:
    conn.executescript(
        """
        CREATE TABLE IF NOT EXISTS basket_daily (
            trade_date  TEXT NOT NULL,
            side        TEXT NOT NULL,
            basket      TEXT NOT NULL,
            expiry_code TEXT,
            n_trades    INTEGER NOT NULL,
            priced      INTEGER NOT NULL,
            unavailable INTEGER NOT NULL,
            gross_rs    REAL NOT NULL,
            charges_rs  REAL NOT NULL,
            net_rs      REAL NOT NULL,
            updated_at  TEXT NOT NULL,
            PRIMARY KEY (trade_date, side, basket)
        );
        CREATE TABLE IF NOT EXISTS basket_trades (
            trade_date TEXT NOT NULL,
            side       TEXT NOT NULL,
            basket     TEXT NOT NULL,
            seq        INTEGER NOT NULL,
            status     TEXT NOT NULL,
            legs_json  TEXT,
            gross_rs   REAL,
            charges_rs REAL,
            net_rs     REAL,
            PRIMARY KEY (trade_date, side, basket, seq)
        );
        """
    )
    conn.commit()


def _leg_strike(entry_spot: float, otype: str, offset: int) -> int:
    # Offsets are defined CE-relative in BASKETS (ITM CE below spot); a PE leg
    # entry in BASKETS carries its own sign, so both use plain addition.
    return int(_r50(float(entry_spot) + offset))


def _quote(quotes: dict, ts_key: str, strike: int, otype: str):
    q = quotes.get((ts_key, strike, otype)) or {}
    return q.get("bid"), q.get("ask")


def price_basket_trade(trade: dict, legs: list, quotes: dict) -> dict:
    """Price one v2.11 segment as a basket. All legs must have finite,
    sane (ask >= bid) quotes at BOTH the entry and exit minute; otherwise the
    whole trade is 'unavailable' (no partial baskets, no LTP fallback)."""
    entry_key = _timestamp_key(trade["entry_ts"])
    exit_key = _timestamp_key(trade["exit_ts"])
    spot = float(trade["entry_spot"])
    detail, gross, charges = [], 0.0, 0.0
    for action, otype, offset in legs:
        strike = _leg_strike(spot, otype, offset)
        e_bid, e_ask = _quote(quotes, entry_key, strike, otype)
        x_bid, x_ask = _quote(quotes, exit_key, strike, otype)
        if None in (e_bid, e_ask, x_bid, x_ask) or e_ask < e_bid or x_ask < x_bid:
            return {"status": "unavailable", "legs": [], "gross_rs": None,
                    "charges_rs": None, "net_rs": None}
        if action == "BUY":
            entry_px, exit_px = float(e_ask), float(x_bid)
            pnl_pts = exit_px - entry_px
        else:
            entry_px, exit_px = float(e_bid), float(x_ask)
            pnl_pts = entry_px - exit_px
        leg_charges = float(round_trip_charges(entry_px, exit_px, QTY)["total"])
        gross += pnl_pts * QTY
        charges += leg_charges
        detail.append({
            "action": action, "otype": otype, "strike": strike,
            "entry_px": entry_px, "exit_px": exit_px,
            "pnl_pts": round(pnl_pts, 2), "charges_rs": round(leg_charges, 2),
        })
    return {
        "status": "priced",
        "legs": detail,
        "gross_rs": round(gross, 2),
        "charges_rs": round(charges, 2),
        "net_rs": round(gross - charges, 2),
    }


def _v211_trades(conn: sqlite3.Connection, trade_date: str) -> list:
    cur = conn.execute(
        "SELECT seq, side, entry_ts, exit_ts, entry_spot FROM "
        "paper_strategy_trades WHERE trade_date=? AND entry_ts IS NOT NULL "
        "AND exit_ts IS NOT NULL AND entry_spot IS NOT NULL ORDER BY seq",
        (trade_date,),
    )
    cols = [c[0] for c in cur.description]
    return [dict(zip(cols, r)) for r in cur.fetchall()]


def replay_day(trade_date: str, conn: sqlite3.Connection | None = None) -> dict:
    """Price EVERY basket for one recorded v2.11 day and persist the rows.
    Idempotent: rows are REPLACEd. Returns a small per-basket summary."""
    own = conn is None
    if own:
        conn = get_conn()
    try:
        _ensure_tables(conn)
        trades = _v211_trades(conn, trade_date)
        expiry_code, quotes = (None, {})
        if trades:
            expiry_code, quotes = build_executable_book(trade_date)
        now = datetime.now().isoformat(timespec="seconds")
        summary = {}
        for side, baskets in BASKETS.items():
            side_trades = [t for t in trades if (t["side"] or "").upper() == side]
            for bkey, bdef in baskets.items():
                priced = unavail = 0
                gross = charges = net = 0.0
                for t in side_trades:
                    res = price_basket_trade(t, bdef["legs"], quotes)
                    if res["status"] == "priced":
                        priced += 1
                        gross += res["gross_rs"]
                        charges += res["charges_rs"]
                        net += res["net_rs"]
                    else:
                        unavail += 1
                    conn.execute(
                        "INSERT OR REPLACE INTO basket_trades (trade_date,side,"
                        "basket,seq,status,legs_json,gross_rs,charges_rs,net_rs) "
                        "VALUES (?,?,?,?,?,?,?,?,?)",
                        (trade_date, side, bkey, t["seq"], res["status"],
                         json.dumps(res["legs"]), res["gross_rs"],
                         res["charges_rs"], res["net_rs"]),
                    )
                conn.execute(
                    "INSERT OR REPLACE INTO basket_daily (trade_date,side,basket,"
                    "expiry_code,n_trades,priced,unavailable,gross_rs,charges_rs,"
                    "net_rs,updated_at) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                    (trade_date, side, bkey, expiry_code, len(side_trades),
                     priced, unavail, round(gross, 2), round(charges, 2),
                     round(net, 2), now),
                )
                summary[f"{side}:{bkey}"] = round(net, 2)
        conn.commit()
        return summary
    finally:
        if own:
            conn.close()


def pending_dates(conn: sqlite3.Connection | None = None) -> list:
    """Recorded v2.11 days missing rows for ANY currently-defined basket
    (oldest first). A day replayed under an older basket set therefore becomes
    pending again when new baskets are added; replay_day REPLACEs idempotently.
    Rows for baskets no longer defined are ignored (and hidden by the view)."""
    own = conn is None
    if own:
        conn = get_conn()
    try:
        _ensure_tables(conn)
        combos = {f"{s}:{b}" for s in BASKETS for b in BASKETS[s]}
        have: dict = {}
        for d, key in conn.execute(
            "SELECT trade_date, side || ':' || basket FROM basket_daily"
        ):
            have.setdefault(d, set()).add(key)
        cur = conn.execute(
            "SELECT DISTINCT trade_date FROM paper_strategy_trades ORDER BY trade_date"
        )
        return [d for (d,) in cur.fetchall()
                if not combos.issubset(have.get(d, set()))]
    finally:
        if own:
            conn.close()


def run_backfill(limit: int | None = None) -> dict:
    """Replay pending days (bounded by `limit` to stay web-request safe).
    Days whose quote data is missing are recorded as fully 'unavailable' so
    they do not stay pending forever."""
    done, errors = [], {}
    dates = pending_dates()
    if limit:
        dates = dates[:limit]
    for d in dates:
        try:
            replay_day(d)
            done.append(d)
        except Exception as exc:  # quote data absent — mark day unavailable
            errors[d] = f"{type(exc).__name__}: {exc}"
            _mark_day_unavailable(d)
    return {"done": done, "errors": errors,
            "remaining": len(pending_dates())}


def _mark_day_unavailable(trade_date: str) -> None:
    conn = get_conn()
    try:
        _ensure_tables(conn)
        now = datetime.now().isoformat(timespec="seconds")
        trades = _v211_trades(conn, trade_date)
        for side, baskets in BASKETS.items():
            n = len([t for t in trades if (t["side"] or "").upper() == side])
            for bkey in baskets:
                conn.execute(
                    "INSERT OR REPLACE INTO basket_daily (trade_date,side,basket,"
                    "expiry_code,n_trades,priced,unavailable,gross_rs,charges_rs,"
                    "net_rs,updated_at) VALUES (?,?,?,NULL,?,0,?,0,0,0,?)",
                    (trade_date, side, bkey, n, n, now),
                )
        conn.commit()
    finally:
        conn.close()


if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser(description="Backfill basket replay days")
    parser.add_argument("--limit", type=int, default=None)
    args = parser.parse_args()
    result = run_backfill(limit=args.limit)
    print(f"done={len(result['done'])} remaining={result['remaining']}")
    for d, e in result["errors"].items():
        print(f"  {d}: {e}")
