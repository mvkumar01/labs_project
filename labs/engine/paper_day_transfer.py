"""Move replayed paper-book sessions from one labs database to another (Vipin, 2026-11-07).

A tick book's past sessions replay only where the recorded ticks are - locally, after the depth downloader has
moved them off PythonAnywhere - so a rebuild runs there and only its results go to PA:

    local:  payload = export_days(book, results)          # results of proposer_px_tracker.simulate_day(...)
    PA:     python -m labs.engine.paper_day_transfer import <payload.json>

The import replaces each listed session of each book (daily row, trades, prints, tick_replay_days row) in one
transaction, then recomputes book_gross_before from the first imported session on. Paper tables only; it never
touches a live table or an order.
"""
from __future__ import annotations

import json
import sqlite3
import sys

from labs.engine import proposer_px_tracker as tr
from labs.engine import tick_replay

TABLES = ("daily", "trades", "prints")


def _rows(conn: sqlite3.Connection, table: str, day: str) -> list[dict]:
    cur = conn.execute(f"SELECT * FROM {table} WHERE trade_date=?", (day,))
    cols = [c[0] for c in cur.description]
    return [dict(zip(cols, r)) for r in cur.fetchall()]


def export_days(book, results: list[dict]) -> dict:
    """{book, sessions: {date: {daily, trades, prints, replay}}} from simulate_day results, through the same
    _persist the paper loop uses (into a scratch in-memory database)."""
    conn = sqlite3.connect(":memory:")
    tr._ensure_tables(conn, book)
    sessions = {}
    for r in results:
        tr._persist(conn, r, book)
        day = r["trade_date"]
        sessions[day] = {t: _rows(conn, f"{book.key}_{t}", day) for t in TABLES}
        sessions[day]["replay"] = r.get("replay") or tick_replay.ONE_MINUTE
    conn.close()
    return {"book": book.key, "sessions": sessions}


def _insert(conn: sqlite3.Connection, table: str, row: dict) -> None:
    cols = list(row)
    conn.execute(f"INSERT INTO {table} ({','.join(cols)}) VALUES ({','.join('?' * len(cols))})",
                 [row[c] for c in cols])


def import_days(conn: sqlite3.Connection, payloads: list[dict]) -> dict:
    """Replace the payloads' sessions in `conn`; returns {book: [dates]}."""
    for p in payloads:                                   # DDL first: executescript commits on its own
        tr._ensure_tables(conn, _book(p["book"]))
    tick_replay.ensure_table(conn)
    conn.commit()
    out = {}
    try:
        for p in payloads:
            key = p["book"]
            days = sorted(p["sessions"])
            for day in days:
                s = p["sessions"][day]
                for t in TABLES:
                    conn.execute(f"DELETE FROM {key}_{t} WHERE trade_date=?", (day,))
                    for row in s[t]:
                        _insert(conn, f"{key}_{t}", row)
                tick_replay.record(conn, key, day, s["replay"])
            _rebase(conn, key, days[0] if days else None)
            out[key] = days
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    return out


def _book(key: str):
    for value in vars(tr).values():
        if isinstance(value, tr.Book) and value.key == key:
            return value
    raise KeyError(f"unknown paper book {key}")


def _rebase(conn: sqlite3.Connection, key: str, first: str | None) -> None:
    """book_gross_before = the gross of every earlier closed / no-trade session, from `first` on."""
    if not first:
        return
    rows = conn.execute(f"SELECT trade_date, status, gross_rs FROM {key}_daily ORDER BY trade_date").fetchall()
    running = 0.0
    for day, status, gross in rows:
        if day >= first:
            conn.execute(f"UPDATE {key}_daily SET book_gross_before=? WHERE trade_date=?", (round(running, 2), day))
        if status in ("closed", "no_trade"):
            running += float(gross or 0.0)


def main(argv: list[str]) -> int:
    if len(argv) != 2 or argv[0] != "import":
        print("usage: python -m labs.engine.paper_day_transfer import <payload.json>")
        return 2
    with open(argv[1], encoding="utf-8") as fh:
        data = json.load(fh)
    payloads = data if isinstance(data, list) else [data]
    from storage.db import get_conn
    conn = get_conn()
    conn.execute("PRAGMA busy_timeout=60000")
    try:
        done = import_days(conn, payloads)
    finally:
        conn.close()
    print(json.dumps({k: {"sessions": len(v), "first": v[0] if v else None, "last": v[-1] if v else None}
                      for k, v in done.items()}))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
