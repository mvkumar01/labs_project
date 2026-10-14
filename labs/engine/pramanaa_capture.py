"""Pramanaa's own SENSEX Proposer, as captured from its pages: the 5-class prints and the paper
bot's trades. Read-only reference data for comparing the labs Proposer books with Pramanaa.

Nothing here talks to Pramanaa. Its pages need a login, so the data arrives as files saved from a
logged-in browser and is imported with

    python -m labs.engine.pramanaa_capture import FILE [FILE ...]

  *.tsv    the Market Predictor page's rows, one per print: date, HH:MM, kind ('5' = 5-class),
           label, confidence % (alphaIMB research captures, predictor_full*.tsv);
  *.mhtml  the paper bot's trade ledger page (labs/Proposer-v1d-...): Date, Entry, Exit, Dir,
           Signal, Instrument, Qty, Entry Rs, Exit Rs, ... P&L, Exit Rule.

Each import also records the span of dates the file covers, so a day inside it with no trade
reads as "Pramanaa did not trade", and a day outside it as "not captured".
"""
from __future__ import annotations

import email
import html
import re
import sqlite3
import sys
from datetime import date, datetime
from email import policy
from pathlib import Path

from storage.db import get_conn

MONTHS = {m: i for i, m in enumerate(
    ["jan", "feb", "mar", "apr", "may", "jun", "jul", "aug", "sep", "oct", "nov", "dec"], start=1)}


def _ensure_tables(conn: sqlite3.Connection) -> None:
    conn.executescript(
        """
        CREATE TABLE IF NOT EXISTS pramanaa_prints (
            trade_date TEXT NOT NULL, minute TEXT NOT NULL, x5 TEXT NOT NULL, x5_conf REAL,
            PRIMARY KEY (trade_date, minute)
        );
        CREATE TABLE IF NOT EXISTS pramanaa_trades (
            trade_date TEXT NOT NULL, entry_time TEXT NOT NULL, exit_time TEXT, side TEXT, signal TEXT,
            instrument TEXT NOT NULL, qty INTEGER, entry_price REAL, exit_price REAL, pnl_rs REAL,
            exit_rule TEXT, PRIMARY KEY (trade_date, entry_time, instrument)
        );
        CREATE TABLE IF NOT EXISTS pramanaa_coverage (
            kind TEXT NOT NULL, first_date TEXT NOT NULL, last_date TEXT NOT NULL, source TEXT,
            imported_at TEXT NOT NULL, PRIMARY KEY (kind, first_date, last_date)
        );
        """
    )
    conn.commit()


# ------------------------------------------------------------------- parsing ---
def parse_prints_tsv(text: str) -> list[tuple]:
    """(trade_date, 'HH:MM', x5, conf) for every 5-class row."""
    out = []
    for line in text.splitlines():
        p = line.rstrip("\n").split("\t")
        if len(p) < 5 or p[2] != "5" or not re.fullmatch(r"\d{4}-\d{2}-\d{2}", p[0]):
            continue
        label = p[3].strip().lower().replace(" ", "_")
        try:
            conf = float(p[4]) / 100.0
        except ValueError:
            conf = None
        out.append((p[0], p[1][:5], label, conf))
    return out


def _number(cell: str) -> float | None:
    m = re.search(r"\d[\d,]*(?:\.\d+)?", cell or "")
    if not m:
        return None
    value = float(m.group(0).replace(",", ""))
    return -value if "-" in cell[: m.start()] else value


def _clock(cell: str) -> str | None:
    m = re.match(r"\s*(\d{1,2}):(\d{2}):(\d{2})\s*([ap]m)?", cell or "", re.I)
    if not m:
        return None
    h = int(m.group(1)) % 12 if m.group(4) else int(m.group(1))
    if m.group(4) and m.group(4).lower() == "pm":
        h += 12
    return f"{h:02d}:{m.group(2)}:{m.group(3)}"


def _day(cell: str, today: date) -> str | None:
    m = re.match(r"\s*(\d{1,2})\s+([A-Za-z]{3})", cell or "")
    if not m or m.group(2).lower() not in MONTHS:
        return None
    month, year = MONTHS[m.group(2).lower()], today.year
    if month > today.month:                     # the ledger shows no year; a later month is last year's
        year -= 1
    return date(year, month, int(m.group(1))).isoformat()


def parse_ledger_mhtml(data: bytes, today: date | None = None) -> list[tuple]:
    """Rows of the paper bot's trade table: (trade_date, entry, exit, side, signal, instrument, qty,
    entry_price, exit_price, pnl_rs, exit_rule)."""
    today = today or date.today()
    msg = email.message_from_bytes(data, policy=policy.default)
    doc = next((part.get_content() for part in msg.walk() if part.get_content_type() == "text/html"), "")
    out, header = [], None
    for row in re.findall(r"<tr[^>]*>(.*?)</tr>", doc, re.S):
        cells = [re.sub(r"\s+", " ", html.unescape(re.sub(r"<[^>]+>", " ", c))).strip()
                 for c in re.findall(r"<t[dh][^>]*>(.*?)</t[dh]>", row, re.S)]
        if "<th" in row and "Entry" in cells and "Instrument" in cells:
            # 'Entry' / 'Exit' are clock times; 'Entry <currency sign>' / 'Exit <currency sign>' are premiums
            header = [f"{c.split(' ')[0]} Rs" if c.split(" ")[0] in ("Entry", "Exit") and " " in c
                      and not re.search(r"[A-Za-z]", c.split(" ", 1)[1]) else c for c in cells]
            continue
        if header is None or len(cells) != len(header):
            continue
        r = dict(zip(header, cells))
        day = _day(r.get("Date", ""), today)
        entry = _clock(r.get("Entry", ""))
        if not day or not entry or not r.get("Instrument"):
            continue
        out.append((day, entry, _clock(r.get("Exit", "")), r.get("Dir"), r.get("Signal"), r.get("Instrument"),
                    int(_number(r.get("Qty", "")) or 0) or None, _number(r.get("Entry Rs", "")),
                    _number(r.get("Exit Rs", "")), _number(r.get("P&L", "")), r.get("Exit Rule")))
    return out


# -------------------------------------------------------------------- import ---
def import_file(path: Path, conn: sqlite3.Connection, today: date | None = None) -> dict:
    _ensure_tables(conn)
    stamp = datetime.now().isoformat(timespec="seconds")
    if path.suffix.lower() == ".tsv":
        rows = parse_prints_tsv(path.read_text(encoding="utf-8", errors="replace"))
        conn.executemany("INSERT OR REPLACE INTO pramanaa_prints (trade_date,minute,x5,x5_conf) VALUES (?,?,?,?)", rows)
        kind = "prints"
    elif path.suffix.lower() in (".mhtml", ".mht"):
        rows = parse_ledger_mhtml(path.read_bytes(), today)
        conn.executemany(
            "INSERT OR REPLACE INTO pramanaa_trades (trade_date,entry_time,exit_time,side,signal,instrument,qty,"
            "entry_price,exit_price,pnl_rs,exit_rule) VALUES (?,?,?,?,?,?,?,?,?,?,?)", rows)
        kind = "trades"
    else:
        raise ValueError(f"{path.name}: expected a .tsv of prints or an .mhtml of the trade ledger")
    if rows:
        days = sorted({r[0] for r in rows})
        conn.execute("INSERT OR REPLACE INTO pramanaa_coverage (kind,first_date,last_date,source,imported_at) "
                     "VALUES (?,?,?,?,?)", (kind, days[0], days[-1], path.name, stamp))
    conn.commit()
    return {"file": path.name, "kind": kind, "rows": len(rows),
            "first": min((r[0] for r in rows), default=None), "last": max((r[0] for r in rows), default=None)}


def covered(conn: sqlite3.Connection, kind: str, day: str) -> bool:
    try:
        return conn.execute("SELECT 1 FROM pramanaa_coverage WHERE kind=? AND ? BETWEEN first_date AND last_date",
                            (kind, day)).fetchone() is not None
    except sqlite3.OperationalError:
        return False


def main(argv: list[str]) -> int:
    if len(argv) < 2 or argv[0] != "import":
        print(__doc__)
        return 2
    conn = get_conn()
    try:
        for name in argv[1:]:
            print(import_file(Path(name), conn))
    finally:
        conn.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
