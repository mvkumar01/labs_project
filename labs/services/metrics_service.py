"""
Query helpers for P&L, trade logs, signal logs, and equity curves.
All functions return plain Python dicts/lists (JSON-serialisable).
"""
from datetime import datetime

import pytz

from storage.db import get_conn

IST = pytz.timezone("Asia/Kolkata")


def get_today_pnl(bot_id: str, conn=None) -> float:
    today = datetime.now(IST).strftime("%Y-%m-%d")
    close = conn is None
    if conn is None:
        conn = get_conn()
    try:
        row = conn.execute(
            "SELECT COALESCE(SUM(pnl_rs), 0) as total FROM trades WHERE bot_id=? AND trade_date=?",
            (bot_id, today),
        ).fetchone()
        return float(row["total"])
    finally:
        if close:
            conn.close()


def get_total_pnl(bot_id: str, conn=None) -> float:
    close = conn is None
    if conn is None:
        conn = get_conn()
    try:
        row = conn.execute(
            "SELECT COALESCE(SUM(pnl_rs), 0) as total FROM trades WHERE bot_id=?",
            (bot_id,),
        ).fetchone()
        return float(row["total"])
    finally:
        if close:
            conn.close()


def get_trade_log(bot_id: str, limit: int = 200, conn=None) -> list[dict]:
    close = conn is None
    if conn is None:
        conn = get_conn()
    try:
        rows = conn.execute("""
            SELECT trade_id, trade_date, underlying, side, symbol, strike, expiry,
                   entry_time, exit_time, entry_ltp, exit_ltp, entry_spot, exit_spot,
                   pnl_pts, pnl_rs, exit_reason, holding_mins
            FROM trades WHERE bot_id=?
            ORDER BY exit_time DESC LIMIT ?
        """, (bot_id, limit)).fetchall()
        return [dict(r) for r in rows]
    finally:
        if close:
            conn.close()


def get_signal_log(bot_id: str, limit: int = 200, conn=None) -> list[dict]:
    close = conn is None
    if conn is None:
        conn = get_conn()
    try:
        rows = conn.execute("""
            SELECT signal_id, ts, underlying, bar_close, rsi, signal_type, acted, skip_reason
            FROM signals WHERE bot_id=?
            ORDER BY ts DESC LIMIT ?
        """, (bot_id, limit)).fetchall()
        return [dict(r) for r in rows]
    finally:
        if close:
            conn.close()


def get_equity_curve(bot_id: str, conn=None) -> list[dict]:
    """Returns cumulative P&L series as [{date, cumulative_pnl_rs}]."""
    close = conn is None
    if conn is None:
        conn = get_conn()
    try:
        rows = conn.execute("""
            SELECT trade_date, SUM(pnl_rs) as day_pnl
            FROM trades WHERE bot_id=?
            GROUP BY trade_date ORDER BY trade_date ASC
        """, (bot_id,)).fetchall()
        cumulative = 0.0
        result = []
        for r in rows:
            cumulative += float(r["day_pnl"])
            result.append({"date": r["trade_date"], "cumulative_pnl_rs": round(cumulative, 2)})
        return result
    finally:
        if close:
            conn.close()


def get_open_position(bot_id: str, conn=None) -> dict | None:
    close = conn is None
    if conn is None:
        conn = get_conn()
    try:
        row = conn.execute(
            "SELECT * FROM positions WHERE bot_id=? AND status='open'", (bot_id,)
        ).fetchone()
        return dict(row) if row else None
    finally:
        if close:
            conn.close()


def get_all_bots_summary(conn=None) -> list[dict]:
    """
    Returns one dict per bot with: bot_id, name, underlying, strategy_type,
    status, today_pnl_rs, total_pnl_rs, open_position (symbol or None).
    """
    from labs.services.bot_service import list_bots
    close = conn is None
    if conn is None:
        conn = get_conn()
    try:
        bots = list_bots(conn=conn)
        today = datetime.now(IST).strftime("%Y-%m-%d")
        result = []
        for b in bots:
            bid = b["bot_id"]
            today_row = conn.execute(
                "SELECT COALESCE(SUM(pnl_rs),0) as p FROM trades WHERE bot_id=? AND trade_date=?",
                (bid, today),
            ).fetchone()
            total_row = conn.execute(
                "SELECT COALESCE(SUM(pnl_rs),0) as p FROM trades WHERE bot_id=?",
                (bid,),
            ).fetchone()
            pos_row = conn.execute(
                "SELECT symbol FROM positions WHERE bot_id=? AND status='open'", (bid,)
            ).fetchone()
            result.append({
                "bot_id":        bid,
                "name":          b["name"],
                "underlying":    b["underlying"],
                "strategy_type": b["strategy_type"],
                "status":        b["status"],
                "today_pnl_rs":  round(float(today_row["p"]), 2),
                "total_pnl_rs":  round(float(total_row["p"]), 2),
                "open_position": pos_row["symbol"] if pos_row else None,
            })
        return result
    finally:
        if close:
            conn.close()


def get_performance_stats(bot_id: str, conn=None) -> dict:
    close = conn is None
    if conn is None:
        conn = get_conn()
    try:
        rows = conn.execute(
            "SELECT pnl_rs, holding_mins, exit_reason FROM trades WHERE bot_id=?",
            (bot_id,),
        ).fetchall()
        if not rows:
            return {"total_trades": 0}
        total  = len(rows)
        wins   = sum(1 for r in rows if r["pnl_rs"] > 0)
        losses = sum(1 for r in rows if r["pnl_rs"] <= 0)
        total_pnl = sum(r["pnl_rs"] for r in rows)
        avg_hold  = sum(r["holding_mins"] for r in rows) / total
        exit_reasons = {}
        for r in rows:
            exit_reasons[r["exit_reason"]] = exit_reasons.get(r["exit_reason"], 0) + 1
        return {
            "total_trades": total,
            "wins":         wins,
            "losses":       losses,
            "win_pct":      round(wins / total * 100, 1),
            "total_pnl_rs": round(total_pnl, 2),
            "avg_hold_mins": round(avg_hold, 1),
            "exit_reasons": exit_reasons,
        }
    finally:
        if close:
            conn.close()
