"""Card summaries for the /labs/live Overview tab.

One card per running book: today's net P&L, the current position, win days, the capital
deployed and the total net P&L with its return on that capital.

Capital deployed is taken from each book's own trades: the premium paid for option buys
(entry ask x quantity), the estimated or defined-risk capital the short-premium books
record per session, and the margin stored per CRUDEOIL trade. A book holds one position at
a time, so its capital is the largest single commitment it has made (peak deployed), which
is also the base for the return percentage.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
import sqlite3

from labs.engine.charges import sensex_round_trip_charges

IST = timezone(timedelta(hours=5, minutes=30))
NIFTY_QTY = 65
SENSEX_QTY = 20

# key -> card metadata. Order is the display order.
BOOKS = {
    "nifty": {"label": "Alpha v2.11", "instrument": "NIFTY options", "size": "1 lot"},
    "alpha_v211b": {"label": "Alpha 2.11 replay (B)", "instrument": "NIFTY options", "size": "1 lot"},
    "alpha_v212": {"label": "Alpha v2.12", "instrument": "NIFTY options", "size": "1 lot"},
    "alpha_v212b10": {"label": "Alpha v2.14 A", "instrument": "NIFTY options", "size": "1 lot"},
    "alpha_v214": {"label": "Alpha v2.14 B", "instrument": "NIFTY options", "size": "1 lot"},
    "alpha_v214c": {"label": "Alpha v2.14 C", "instrument": "NIFTY options", "size": "1 lot"},
    "alpha_cpr": {"label": "Alpha CPR", "instrument": "NIFTY options", "size": "1-15 lots"},
    "theta_straddle": {"label": "09:20 Theta Straddle", "instrument": "NIFTY options", "size": "1 lot per leg"},
    "theta_iron_fly": {"label": "09:20 Iron Fly", "instrument": "NIFTY options", "size": "1 lot per leg"},
    "crude_macd_st": {"label": "Crude MACD/ST", "instrument": "CRUDEOIL futures", "size": "1 lot"},
    "btc_rsi_roc": {"label": "BTC RSI/ROC short", "instrument": "BTCUSDT spot (Binance)", "size": "0.01 BTC"},
    "sensex_alpha": {"label": "Sensex_alpha", "instrument": "SENSEX options", "size": "1 lot"},
}


def _rows(conn: sqlite3.Connection, sql: str, params=()) -> list[dict]:
    cur = conn.execute(sql, params)
    cols = [c[0] for c in cur.description]
    return [dict(zip(cols, r)) for r in cur.fetchall()]


def _f(value) -> float:
    try:
        return float(value or 0)
    except (TypeError, ValueError):
        return 0.0


def _card(key: str, daily: list[dict], today: str, capital: float, position: str,
          open_net: float = 0.0) -> dict:
    """daily: [{trade_date, net}] for completed and running sessions."""
    total = sum(d["net"] for d in daily)
    active = [d for d in daily if d["net"] != 0]
    wins = [d for d in active if d["net"] > 0]
    today_net = sum(d["net"] for d in daily if d["trade_date"] == today)
    last = max((d["trade_date"] for d in daily), default=None)
    return {
        "key": key, **BOOKS[key], "status": "Running",
        "today_net": round(today_net, 2), "position": position,
        "open_net": round(open_net, 2),
        "total_net": round(total, 2),
        "capital": round(capital, 2) if capital else None,
        "return_pct": round(100 * total / capital, 2) if capital else None,
        "win_days": len(wins), "active_days": len(active),
        "win_pct": round(100 * len(wins) / len(active)) if active else None,
        "sessions": len(daily), "last_date": last,
    }


def _option_buy_book(conn, key: str, daily_table: str, trades_table: str, today: str,
                     lots_col: str | None = None) -> dict:
    daily = [{"trade_date": r["trade_date"], "net": _f(r["net_rs"])} for r in _rows(
        conn, f"SELECT trade_date, net_rs FROM {daily_table} WHERE trade_date >= '2026-06-01'")]
    lots = f", {lots_col} AS lots" if lots_col else ""
    trades = _rows(conn, f"SELECT trade_date, status, side, entry_ask{lots} FROM {trades_table} "
                         "WHERE trade_date >= '2026-06-01'")
    capital = max((_f(t["entry_ask"]) * NIFTY_QTY * (int(t.get("lots") or 1))
                   for t in trades), default=0.0)
    held = [t for t in trades if t["trade_date"] == today and t["status"] == "open"]
    position = f"Long {held[-1]['side']}" if held else "Flat"
    return _card(key, daily, today, capital, position)


def _nifty(conn, today: str) -> dict:
    daily = [{"trade_date": r["trade_date"], "net": _f(r["net_rs"])}
             for r in _rows(conn, "SELECT trade_date, net_rs FROM paper_strategy_daily")]
    trades = _rows(conn, "SELECT trade_date, side, entry_prem, exit_reason FROM paper_strategy_trades")
    capital = max((_f(t["entry_prem"]) * NIFTY_QTY for t in trades), default=0.0)
    held = [t for t in trades if t["trade_date"] == today and t["exit_reason"] == "holding"]
    return _card("nifty", daily, today, capital, f"Long {held[-1]['side']}" if held else "Flat")


def _short_premium(conn, key: str, table: str, today: str) -> dict:
    rows = _rows(conn, f"SELECT trade_date, status, net_rs, capital_required_rs FROM {table}")
    daily = [{"trade_date": r["trade_date"], "net": _f(r["net_rs"])} for r in rows]
    capital = max((_f(r["capital_required_rs"]) for r in rows), default=0.0)
    is_open = any(r["trade_date"] == today and r["status"] == "open" for r in rows)
    return _card(key, daily, today, capital, "Open" if is_open else "Flat")


def _crude(conn, today: str) -> dict:
    daily = [{"trade_date": r["trade_date"], "net": _f(r["net_rs"])} for r in _rows(
        conn, "SELECT trade_date, net_rs FROM crude_macd_st_daily")]
    trades = _rows(conn, "SELECT trade_date, status, net_rs, margin_rs FROM crude_macd_st_trades")
    capital = max((_f(t["margin_rs"]) for t in trades), default=0.0)
    held = [t for t in trades if t["status"] == "open"]
    return _card("crude_macd_st", daily, today, capital, "Long 1 lot" if held else "Flat",
                 open_net=sum(_f(t["net_rs"]) for t in held))


def _btc(conn, today: str) -> dict:
    daily = [{"trade_date": r["trade_date"], "net": _f(r["net_rs"])} for r in _rows(
        conn, "SELECT trade_date, net_rs FROM btc_rsi_roc_daily")]
    trades = _rows(conn, "SELECT status, net_rs, margin_rs FROM btc_rsi_roc_trades")
    capital = max((_f(t["margin_rs"]) for t in trades), default=0.0)
    held = [t for t in trades if t["status"] == "open"]
    return _card("btc_rsi_roc", daily, today, capital, "Short 0.01 BTC" if held else "Flat",
                 open_net=sum(_f(t["net_rs"]) for t in held))


def _sensex(conn, today: str) -> dict:
    # The Sensex_alpha tab shows the inverted-execution tables; this book stores gross
    # option P&L only, so charges are applied here per priced trade.
    trades = _rows(conn, "SELECT trade_date, status, side, entry_ask, exit_bid, option_gross_rs, "
                         "quote_status FROM sensex_alpha_inverted_trades")
    by_day: dict[str, float] = {}
    for t in trades:
        net = 0.0
        if t["quote_status"] == "priced" and t["option_gross_rs"] is not None:
            net = _f(t["option_gross_rs"]) - sensex_round_trip_charges(
                t["entry_ask"], t["exit_bid"], SENSEX_QTY)["total"]
        by_day[t["trade_date"]] = by_day.get(t["trade_date"], 0.0) + net
    days = {r["trade_date"] for r in _rows(conn, "SELECT trade_date FROM sensex_alpha_inverted_daily")}
    daily = [{"trade_date": d, "net": by_day.get(d, 0.0)} for d in sorted(days | set(by_day))]
    capital = max((_f(t["entry_ask"]) * SENSEX_QTY for t in trades), default=0.0)
    held = [t for t in trades if t["trade_date"] == today and t["status"] == "open"]
    return _card("sensex_alpha", daily, today, capital,
                 f"Long {held[-1]['side']}" if held else "Flat")


def build_overview(conn: sqlite3.Connection, today: str | None = None) -> list[dict]:
    today = today or datetime.now(IST).date().isoformat()
    builders = {
        "nifty": lambda: _nifty(conn, today),
        "alpha_v211b": lambda: _option_buy_book(conn, "alpha_v211b", "alpha_v211b_daily",
                                                "alpha_v211b_trades", today),
        "alpha_v212": lambda: _option_buy_book(conn, "alpha_v212", "alpha_v212_daily",
                                               "alpha_v212_trades", today),
        "alpha_v212b10": lambda: _option_buy_book(
            conn, "alpha_v212b10", "alpha_v212b10_daily",
            "alpha_v212b10_trades", today),
        "alpha_v214": lambda: _option_buy_book(
            conn, "alpha_v214", "alpha_v214_daily", "alpha_v214_trades", today),
        "alpha_v214c": lambda: _option_buy_book(
            conn, "alpha_v214c", "alpha_v214c_daily", "alpha_v214c_trades", today),
        "alpha_cpr": lambda: _option_buy_book(conn, "alpha_cpr", "alpha_cpr_daily",
                                              "alpha_cpr_trades", today, lots_col="lots"),
        "theta_straddle": lambda: _short_premium(conn, "theta_straddle", "theta_straddle_daily", today),
        "theta_iron_fly": lambda: _short_premium(conn, "theta_iron_fly", "theta_iron_fly_daily", today),
        "crude_macd_st": lambda: _crude(conn, today),
        "btc_rsi_roc": lambda: _btc(conn, today),
        "sensex_alpha": lambda: _sensex(conn, today),
    }
    cards = []
    for key, build in builders.items():
        try:
            cards.append(build())
        except sqlite3.OperationalError as exc:            # table not created yet
            cards.append({"key": key, **BOOKS[key], "status": "Waiting", "error": str(exc)})
    return cards
