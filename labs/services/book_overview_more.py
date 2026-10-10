"""More cards for the /labs/live Overview tab: the simulated books added after the first fifteen, the
dry-run runners and the real-money runners.

Same card fields as labs/services/book_overview.py, plus:
  kind        "Simulated" | "Dry run" | "Live"        (cards without it are simulated books; the
              dashboard never shows the word "paper" - tests/test_labs_overview_theme.py)
  tab         the /labs/live tab a card opens, when that is not its key
  href        a link outside /labs/live (the login-protected Live Trading page)
  trades      closed trades, where the book counts them
  usd         the card is in US dollars and stays out of the rupee totals
  no_figures  the card shows status only (real-money accounts: this page needs no login)
"""
from __future__ import annotations

import sqlite3

SENSEX_PAPER_QTY = 2000          # the Proposer paper books trade 100 lots of 20

MORE_BOOKS = {
    "proposer_px": {"label": "Sensex Proposer + Renko", "instrument": "SENSEX options", "size": "100 lots"},
    "proposer_v3": {"label": "Sensex Proposer v3", "instrument": "SENSEX options", "size": "100 lots"},
    "nifty_expiry_sale_A": {"label": "Expiry straddle sale A", "instrument": "NIFTY options, expiry days",
                            "size": "1 lot per leg", "tab": "nifty_expiry_sale"},
    "nifty_expiry_sale_B": {"label": "Expiry straddle sale B", "instrument": "NIFTY options, expiry days",
                            "size": "1 lot per leg", "tab": "nifty_expiry_sale"},
    "nifty_expiry_sale_B50": {"label": "Expiry straddle sale B50 (pair stop)", "instrument": "NIFTY options, expiry days",
                              "size": "1 lot per leg", "tab": "nifty_expiry_sale"},
    "crudem_combo": {"label": "CRUDEOILM Combo (6 rules)", "instrument": "CRUDEOILM futures", "size": "1 lot · 10 barrels"},
    "gold_cci": {"label": "GOLD CCI short", "instrument": "GOLD futures", "size": "1 lot"},
    "infy_tcs_pair": {"label": "INFY / TCS pair", "instrument": "INFY and TCS shares", "size": "Rs 10 lakh a leg"},
    "crypto_xs": {"label": "Crypto taker imbalance", "instrument": "Binance USDT perpetuals", "size": "$100,000 simulated equity"},
}


def _rows(conn, sql: str, params=()) -> list[dict]:
    cur = conn.execute(sql, params)
    cols = [c[0] for c in cur.description]
    return [dict(zip(cols, r)) for r in cur.fetchall()]


def _f(value) -> float:
    try:
        return float(value or 0)
    except (TypeError, ValueError):
        return 0.0


def _card(key: str, meta: dict, daily: list[dict], today: str, *, capital: float | None, position: str = "Flat",
          open_net: float = 0.0, trades: int | None = None, kind: str = "Simulated", **more) -> dict:
    total = sum(d["net"] for d in daily)
    active = [d for d in daily if d["net"] != 0]
    wins = [d for d in active if d["net"] > 0]
    card = {
        "key": key, **meta, "status": "Running", "kind": kind,
        "today_net": round(sum(d["net"] for d in daily if d["trade_date"] == today), 2),
        "position": position, "open_net": round(open_net, 2), "total_net": round(total, 2),
        "capital": round(capital, 2) if capital else None,
        "return_pct": round(100 * total / capital, 2) if capital else None,
        "win_days": len(wins), "active_days": len(active),
        "win_pct": round(100 * len(wins) / len(active)) if active else None,
        "sessions": len(daily), "last_date": max((d["trade_date"] for d in daily), default=None), **more,
    }
    if trades is not None:
        card["trades"] = trades
    return card


def _proposer(conn, key: str, today: str) -> dict:
    daily = [{"trade_date": r["trade_date"], "net": _f(r["net_rs"])} for r in _rows(
        conn, f"SELECT trade_date, net_rs FROM {key}_daily WHERE status IN ('closed','no_trade','live')")]
    trades = _rows(conn, f"SELECT trade_date, side, entry_price, exit_ts FROM {key}_trades")
    held = [t for t in trades if t["trade_date"] == today and not t["exit_ts"]]
    capital = max((_f(t["entry_price"]) * SENSEX_PAPER_QTY for t in trades), default=0.0)
    return _card(key, MORE_BOOKS[key], daily, today, capital=capital,
                 position=f"Long {held[-1]['side']}" if held else "Flat",
                 trades=sum(1 for t in trades if t["exit_ts"]))


def _expiry_sale(conn, book: str, today: str) -> dict:
    key = f"nifty_expiry_sale_{book}"
    rows = _rows(conn, "SELECT trade_date, status, strike, net_rs, capital_required_rs FROM nifty_expiry_sale_trades "
                       "WHERE book=?", (book,))
    daily = [{"trade_date": r["trade_date"], "net": _f(r["net_rs"])} for r in rows if r["status"] == "closed"]
    held = [r for r in rows if r["status"] == "open"]
    return _card(key, MORE_BOOKS[key], daily, today,
                 capital=max((_f(r["capital_required_rs"]) for r in rows), default=0.0),
                 position=f"Short {held[-1]['strike']} straddle" if held else "Flat",
                 open_net=sum(_f(r["net_rs"]) for r in held), trades=len(daily),
                 note=f"{sum(1 for r in rows if r['status'] == 'skipped')} expiry days skipped for lack of quotes")


def _mcx(conn, key: str, today: str) -> dict:
    rows = _rows(conn, f"SELECT trade_date, net_rs, n_trades, open_trades FROM {key}_daily ORDER BY trade_date")
    daily = [{"trade_date": r["trade_date"], "net": _f(r["net_rs"])} for r in rows]
    is_open = bool(rows and int(rows[-1]["open_trades"] or 0))
    return _card(key, MORE_BOOKS[key], daily, today, capital=None, position="Open 1 lot" if is_open else "Flat",
                 trades=sum(int(r["n_trades"] or 0) for r in rows) - int(is_open))


def _pair(conn, today: str) -> dict:
    trades = _rows(conn, "SELECT entry_date, exit_date, long_leg, short_leg, net_rs, status, notional FROM infy_tcs_pair_trades")
    days = _rows(conn, "SELECT trade_date, mtm_rs FROM infy_tcs_pair_days ORDER BY trade_date")
    closed = [t for t in trades if t["status"] != "open" and t["exit_date"]]
    by_day = {d["trade_date"]: 0.0 for d in days}
    for t in closed:
        by_day[t["exit_date"]] = by_day.get(t["exit_date"], 0.0) + _f(t["net_rs"])
    daily = [{"trade_date": d, "net": n} for d, n in sorted(by_day.items())]
    held = [t for t in trades if t["status"] == "open"]
    return _card("infy_tcs_pair", MORE_BOOKS["infy_tcs_pair"], daily, today,
                 capital=2 * max((_f(t["notional"]) for t in trades), default=0.0) or None,
                 position=f"Long {held[-1]['long_leg']} / short {held[-1]['short_leg']}" if held else "Flat",
                 open_net=_f(days[-1]["mtm_rs"]) if held and days else 0.0, trades=len(closed))


def _crypto(conn, today: str) -> dict:
    rows = _rows(conn, "SELECT decision_date, net, equity FROM crypto_xs_daily WHERE complete=1 ORDER BY decision_date")
    daily, prev = [], None
    for r in rows:
        equity = _f(r["equity"])
        start = prev if prev is not None else (equity / (1 + _f(r["net"])) if (1 + _f(r["net"])) else equity)
        daily.append({"trade_date": r["decision_date"], "net": equity - start})
        prev = equity
    first = (_f(rows[0]["equity"]) / (1 + _f(rows[0]["net"]))) if rows and (1 + _f(rows[0]["net"])) else None
    return _card("crypto_xs", MORE_BOOKS["crypto_xs"], daily, today, capital=first, position="Rebalanced daily", usd=True)


def _dry_runner(key: str, label: str, instrument: str, table: str, tab: str, today: str) -> dict:
    from storage.live_db import get_live_conn
    conn = get_live_conn()
    try:
        trades = _rows(conn, f"SELECT trade_date, exit_ts, net_rs FROM {table}_trades WHERE book='dry'")
        held = _rows(conn, f"SELECT 1 FROM {table}_position LIMIT 1")
    finally:
        conn.close()
    by_day: dict[str, float] = {}
    for t in trades:
        if t["exit_ts"]:
            by_day[t["trade_date"]] = by_day.get(t["trade_date"], 0.0) + _f(t["net_rs"])
    daily = [{"trade_date": d, "net": n} for d, n in sorted(by_day.items())]
    return _card(key, {"label": label, "instrument": instrument, "size": "1 lot", "tab": tab}, daily, today,
                 capital=None, position="Open 1 lot" if held else "Flat",
                 trades=sum(1 for t in trades if t["exit_ts"]), kind="Dry run",
                 note="Real-time runner on live prices; it places no orders")


def _real_money() -> list[dict]:
    """Status only: which real-money runners have an account armed. No amounts and no account
    names - this page is open without a login; the figures are on the Live Trading page."""
    from storage.live_db import get_live_conn
    conn = get_live_conn()
    try:
        rows = _rows(conn, "SELECT conn_id, key, value FROM live_config WHERE key IN ('mode','strategy_version')")
    finally:
        conn.close()
    conns: dict[str, dict] = {}
    for r in rows:
        conns.setdefault(r["conn_id"], {})[r["key"]] = r["value"]
    groups = {"live_proposer": ("SENSEX Proposer (real money)", "SENSEX options", []),
              "live_nifty": ("NIFTY Alpha (real money)", "NIFTY options", [])}
    for c in conns.values():
        which = "live_proposer" if str(c.get("strategy_version") or "").startswith("proposer") else "live_nifty"
        groups[which][2].append(str(c.get("mode") or "DISARMED"))
    cards = []
    for key, (label, instrument, modes) in groups.items():
        armed, dry = modes.count("LIVE_ARMED"), modes.count("DRY_RUN")
        status = "Armed" if armed else ("Dry run" if dry else "Stopped")
        cards.append({"key": key, "label": label, "instrument": instrument,
                      "size": f"{len(modes)} account{'s' if len(modes) != 1 else ''}", "status": status, "kind": "Live",
                      "no_figures": True, "href": "/live",
                      "note": f"{armed} armed, {dry} on dry run, {len(modes) - armed - dry} stopped. "
                              "Trades and P&L are on the Live Trading page (login)."})
    return cards


def more_cards(conn: sqlite3.Connection, today: str) -> list[dict]:
    builders = {
        "proposer_px": lambda: _proposer(conn, "proposer_px", today),
        "proposer_v3": lambda: _proposer(conn, "proposer_v3", today),
        "nifty_expiry_sale_A": lambda: _expiry_sale(conn, "A", today),
        "nifty_expiry_sale_B": lambda: _expiry_sale(conn, "B", today),
        "nifty_expiry_sale_B50": lambda: _expiry_sale(conn, "B50", today),
        "crudem_combo": lambda: _mcx(conn, "crudem_combo", today),
        "gold_cci": lambda: _mcx(conn, "gold_cci", today),
        "infy_tcs_pair": lambda: _pair(conn, today),
        "crypto_xs": lambda: _crypto(conn, today),
    }
    cards = []
    for key, build in builders.items():
        try:
            cards.append(build())
        except sqlite3.OperationalError as exc:                 # table not created yet
            cards.append({"key": key, **MORE_BOOKS[key], "status": "Waiting", "kind": "Simulated", "error": str(exc)})
    for args in (("dry_crudem", "CRUDEOILM Combo (dry run)", "CRUDEOILM futures", "live_crudem", "crudem_combo"),
                 ("dry_gold", "GOLD CCI short (dry run)", "GOLDM futures", "live_gold", "gold_cci")):
        try:
            cards.append(_dry_runner(*args, today))
        except Exception as exc:                                # noqa: BLE001 - live.db absent or no tables yet
            cards.append({"key": args[0], "label": args[1], "instrument": args[2], "size": "1 lot", "tab": args[4],
                          "status": "Waiting", "kind": "Dry run", "error": str(exc)})
    try:
        cards.extend(_real_money())
    except Exception:                                           # noqa: BLE001 - no live.db on this machine
        pass
    return cards
