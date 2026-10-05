"""Dashboard data for the SENSEX Proposer + price-action exit paper book (/labs/live tab)."""
from __future__ import annotations

import sqlite3

from labs.engine.proposer_px_tracker import LOTS
from live.engine import proposer_engine as pe


def tab_data(conn: sqlite3.Connection, date_clause: str = "", date_params=()) -> tuple:
    """(daily rows, trades, stats), newest first."""
    cur = conn.execute(
        "SELECT trade_date,status,expiry_code,regime,gap_pct,n_trades,n_losses,day_banked,gross_rs,"
        "charges_rs,net_rs,book_gross_before,through_ts,lots,qty,bar_exit,strategy_version,error,updated_at "
        f"FROM proposer_px_daily WHERE 1=1 {date_clause} ORDER BY trade_date DESC LIMIT 400",
        tuple(date_params))
    cols = [c[0] for c in cur.description]
    rows = [dict(zip(cols, r)) for r in cur.fetchall()]
    cur = conn.execute(
        "SELECT trade_date,seq,signal,side,strike,tradingsymbol,expiry_code,entry_ts,exit_ts,entry_spot,"
        "exit_spot,entry_price,exit_price,gross_rs,charges_rs,net_rs,exit_rule "
        f"FROM proposer_px_trades WHERE 1=1 {date_clause} ORDER BY trade_date DESC, seq DESC LIMIT 600",
        tuple(date_params))
    cols = [c[0] for c in cur.description]
    trades = [dict(zip(cols, r)) for r in cur.fetchall()]
    if not rows:
        return rows, trades, {}
    done = [r for r in rows if r["status"] in ("closed", "no_trade")]
    traded = [r for r in done if r["n_trades"]]
    green = [r for r in traded if float(r["gross_rs"] or 0) > 0]
    closed = [t for t in trades if t["exit_ts"]]
    equity = peak = max_dd = 0.0
    for r in reversed(done):
        equity += float(r["net_rs"] or 0)
        peak = max(peak, equity)
        max_dd = max(max_dd, peak - equity)
    months: dict[str, dict] = {}
    for r in done:
        m = months.setdefault(r["trade_date"][:7], {"month": r["trade_date"][:7], "days": 0, "green": 0,
                                                    "red": 0, "trades": 0, "net_rs": 0.0, "worst_day": 0.0})
        m["days"] += 1
        m["trades"] += int(r["n_trades"] or 0)
        m["net_rs"] += float(r["net_rs"] or 0)
        m["green"] += float(r["gross_rs"] or 0) > 0
        m["red"] += float(r["gross_rs"] or 0) < 0
        m["worst_day"] = min(m["worst_day"], float(r["net_rs"] or 0))
    by_rule: dict[str, dict] = {}
    for t in closed:
        key = t["exit_rule"] or "?"
        b = by_rule.setdefault(key, {"rule": key, "n": 0, "net_rs": 0.0})
        b["n"] += 1
        b["net_rs"] += float(t["net_rs"] or 0)
    net_total = sum(float(r["net_rs"] or 0) for r in done)
    stats = {
        "days": len(done), "traded_days": len(traded), "green_days": len(green),
        "red_days": sum(1 for r in traded if float(r["gross_rs"] or 0) < 0),
        "green_pct": round(100 * len(green) / max(len(traded), 1), 1),
        "trades": len(closed),
        "gross_total": round(sum(float(r["gross_rs"] or 0) for r in done), 2),
        "charges_total": round(sum(float(r["charges_rs"] or 0) for r in done), 2),
        "net_total": round(net_total, 2), "net_per_lot": round(net_total / LOTS, 2),
        "worst_day": round(min((float(r["net_rs"] or 0) for r in done), default=0.0), 2),
        "worst_trade": round(min((float(t["net_rs"] or 0) for t in closed), default=0.0), 2),
        "max_dd": round(max_dd, 2),
        "months": [months[k] for k in sorted(months, reverse=True)],
        "by_rule": sorted(by_rule.values(), key=lambda b: -b["n"]),
        "open_trades": [t for t in trades if not t["exit_ts"]],
        "lots": LOTS, "bar_exit": pe.PX_BAR_EXIT,
        "first_date": rows[-1]["trade_date"], "last_date": rows[0]["trade_date"], "latest": rows[0],
    }
    return rows, trades, stats
