"""Idempotent Telegram entry/exit alerts for the v2.11 and v2.12 paper books."""
from __future__ import annotations

import hashlib
import sqlite3
from datetime import datetime, timedelta
from typing import Callable

from labs.engine.paper_strategy_tracker import IST
from live.notify import notify_telegram
from storage.db import get_conn


CLAIM_LEASE = timedelta(minutes=2)
TRACKERS = {
    "v2.11": {
        "table": "paper_strategy_trades",
        "query": (
            "SELECT seq, side, strike, entry_ts, exit_ts, entry_spot, exit_spot, "
            "entry_prem AS entry_price, exit_prem AS exit_price, net_rs, "
            "entry_rule, exit_reason, NULL AS status, NULL AS tradingsymbol "
            "FROM paper_strategy_trades WHERE trade_date=? ORDER BY seq"
        ),
    },
    "v2.12": {
        "table": "alpha_v212_trades",
        "query": (
            "SELECT seq, side, strike, entry_ts, exit_ts, entry_spot, exit_spot, "
            "entry_ask AS entry_price, exit_bid AS exit_price, net_rs, "
            "entry_rule, exit_reason, status, tradingsymbol "
            "FROM alpha_v212_trades WHERE trade_date=? ORDER BY seq"
        ),
    },
}


def _ensure_table(conn: sqlite3.Connection) -> None:
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS paper_trade_alerts (
            tracker TEXT NOT NULL,
            trade_date TEXT NOT NULL,
            event_key TEXT NOT NULL,
            event_type TEXT NOT NULL,
            state TEXT NOT NULL,
            claimed_at TEXT NOT NULL,
            sent_at TEXT,
            PRIMARY KEY (tracker, trade_date, event_key)
        )
        """
    )
    conn.commit()


def _rows(conn: sqlite3.Connection, tracker: str, trade_date: str) -> list[dict]:
    cursor = conn.execute(TRACKERS[tracker]["query"], (trade_date,))
    columns = [item[0] for item in cursor.description]
    return [dict(zip(columns, row)) for row in cursor.fetchall()]


def _event_key(tracker: str, trade_date: str, row: dict, event_type: str) -> str:
    parts = [
        tracker,
        trade_date,
        event_type,
        str(row.get("side") or ""),
        str(row.get("strike") or ""),
        str(row.get("entry_ts") or ""),
    ]
    if event_type == "exit":
        parts.extend(
            [str(row.get("exit_ts") or ""), str(row.get("exit_reason") or "")]
        )
    return hashlib.sha256("|".join(parts).encode("utf-8")).hexdigest()


def _is_closed(row: dict) -> bool:
    return (
        str(row.get("status") or "").lower() != "open"
        and str(row.get("exit_reason") or "").lower() != "holding"
    )


def _number(value, digits: int = 2) -> str:
    if value is None:
        return "n/a"
    return f"{float(value):,.{digits}f}"


def _clock(value) -> str:
    if not value:
        return "n/a"
    timestamp = datetime.fromisoformat(str(value))
    if timestamp.tzinfo is None:
        timestamp = timestamp.replace(tzinfo=IST)
    return timestamp.astimezone(IST).strftime("%H:%M")


def _message(tracker: str, row: dict, event_type: str) -> str:
    side = str(row.get("side") or "?")
    contract = row.get("tradingsymbol") or f"NIFTY {row.get('strike')} {side}"
    if event_type == "entry":
        return (
            f"🟢 PAPER {tracker} ENTRY\n"
            f"{contract} @ {_number(row.get('entry_price'))}\n"
            f"Spot {_number(row.get('entry_spot'), 1)} | {_clock(row.get('entry_ts'))} | "
            f"{row.get('entry_rule') or 'rule n/a'}"
        )
    return (
        f"🔴 PAPER {tracker} EXIT\n"
        f"{contract} @ {_number(row.get('exit_price'))}\n"
        f"Net ₹{_number(row.get('net_rs'))} | Spot {_number(row.get('exit_spot'), 1)} | "
        f"{_clock(row.get('exit_ts'))} | {row.get('exit_reason') or 'reason n/a'}"
    )


def _claim(
    conn: sqlite3.Connection,
    tracker: str,
    trade_date: str,
    event_key: str,
    event_type: str,
    now: datetime,
) -> bool:
    claimed_at = now.isoformat()
    stale_before = (now - CLAIM_LEASE).isoformat()
    cursor = conn.execute(
        """
        INSERT INTO paper_trade_alerts
            (tracker, trade_date, event_key, event_type, state, claimed_at)
        VALUES (?, ?, ?, ?, 'pending', ?)
        ON CONFLICT(tracker, trade_date, event_key) DO UPDATE SET
            state='pending', claimed_at=excluded.claimed_at
        WHERE paper_trade_alerts.state='pending'
          AND paper_trade_alerts.claimed_at < ?
        """,
        (tracker, trade_date, event_key, event_type, claimed_at, stale_before),
    )
    conn.commit()
    return cursor.rowcount == 1


def emit_paper_trade_alerts(
    tracker: str,
    trade_date: str,
    *,
    connection: sqlite3.Connection | None = None,
    sender: Callable[[str], bool] = notify_telegram,
    now: datetime | None = None,
) -> int:
    """Send unseen current-day events and return the number sent successfully."""
    if tracker not in TRACKERS:
        raise ValueError(f"Unsupported paper alert tracker: {tracker}")
    now = now or datetime.now(IST)
    if trade_date != now.astimezone(IST).date().isoformat():
        return 0

    owns_connection = connection is None
    conn = connection or get_conn()
    try:
        _ensure_table(conn)
        sent = 0
        for row in _rows(conn, tracker, trade_date):
            event_types = ["entry"]
            if _is_closed(row):
                event_types.append("exit")
            for event_type in event_types:
                key = _event_key(tracker, trade_date, row, event_type)
                if not _claim(conn, tracker, trade_date, key, event_type, now):
                    continue
                if sender(_message(tracker, row, event_type)):
                    conn.execute(
                        "UPDATE paper_trade_alerts SET state='sent', sent_at=? "
                        "WHERE tracker=? AND trade_date=? AND event_key=?",
                        (now.isoformat(), tracker, trade_date, key),
                    )
                    conn.commit()
                    sent += 1
                else:
                    # Release immediately so a transient Telegram/config failure
                    # is retried on the next paper-loop pass.
                    conn.execute(
                        "DELETE FROM paper_trade_alerts "
                        "WHERE tracker=? AND trade_date=? AND event_key=? AND state='pending'",
                        (tracker, trade_date, key),
                    )
                    conn.commit()
        return sent
    finally:
        if owns_connection:
            conn.close()
