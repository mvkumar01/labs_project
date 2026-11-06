"""Tick replay for the paper books (Vipin, 2026-11-07): replay a session on the depth recorder's recorded ticks
instead of the synthetic one-minute path, so a paper book stops and fills the way the live runners do.

- NIFTY spot inside each minute = the recorded NIFTY_50 index ticks; the held option's price = its recorded best
  bid / ask (mid for the engine's day-target check); entries fill at the ask and exits at the bid FILL_LATENCY_S
  after the signal (live Angel orders take about 2.3 s to send). A minute without index ticks, or an option without
  a tick in the last 120 s, falls back to the one-minute path / estimate; both are counted per day.
- Ticks: Order Flow Research/data/archive/YYYY-MM-DD/{alias}.parquet in the alphaIMB checkout next to labs (the
  depth recorder compacts the day there at about 15:32 IST; manifest.json marks a complete day). Read-only.
- Which days: books in TICK_BOOKS, from their first tick session. During the session the one-minute path runs as
  before (the day's ticks are not compacted yet); once the day's ticks are complete the replay is on ticks, and
  that post-close run is the recorded result. A past day whose ticks are not here is refused, never silently
  replayed on the one-minute path: rebuild it where the ticks are (locally) and import the result.
- Each replayed day records how it was replayed in tick_replay_days (book, date, replay, fallbacks, latency).
- The paper loop drops storage/tick_replay_done/YYYY-MM-DD.json once every tick book has its day on ticks; the
  depth downloader deletes today's PA files only after that marker exists.

The same rules as alphaIMB research/.../v4_ticks.py (the parity reference). Paper only.
"""
from __future__ import annotations

import bisect
import json
import os
import sqlite3
from datetime import datetime, timedelta
from pathlib import Path

from config.labs_config import BASE_DIR

TICK_ARCHIVE_DIR = Path(os.environ.get(
    "LABS_TICK_ARCHIVE_DIR", BASE_DIR.parent / "alphaIMB" / "Order Flow Research" / "data" / "archive"))
DONE_DIR = BASE_DIR / "storage" / "tick_replay_done"
FILL_LATENCY_S = 2.0
QUOTE_MAX_AGE_S = 120.0
INDEX_ALIAS = "NIFTY_50"
# Book key -> first session replayed on ticks (the option bands were recorded from 27 Oct 2026).
TICK_BOOKS = {"proposer_nifty_v4": "2026-10-27", "proposer_nifty_v4_dt25": "2026-10-27"}
# Sessions on or after a book's first tick session that have no recorded ticks anywhere: one-minute path, labelled.
NO_TICK_DAYS: frozenset = frozenset()
REPLAY_TICKS, REPLAY_1MIN = "ticks", "1min"


class TicksMissing(Exception):
    """A past tick-book session whose ticks are not in this checkout."""


def day_dir(day: str) -> Path:
    return TICK_ARCHIVE_DIR / day


def ready(day: str) -> bool:
    """True once the recorder has compacted the day (manifest written) and the index ticks are there."""
    d = day_dir(day)
    return (d / "manifest.json").is_file() and (d / f"{INDEX_ALIAS}.parquet").is_file()


def wants_ticks(book_key: str, day: str) -> bool:
    start = TICK_BOOKS.get(book_key)
    return bool(start) and day >= start and day not in NO_TICK_DAYS


def ticks_for(book_key: str, day: str, today: str) -> "DayTicks | None":
    """The DayTicks to replay `day` with, or None for the one-minute path. Raises TicksMissing for a past session
    that must be on ticks but whose ticks are not here."""
    if not wants_ticks(book_key, day):
        return None
    if ready(day):
        return DayTicks(day)
    if day >= today:                    # today before the recorder compacts: the one-minute path, intraday
        return None
    raise TicksMissing(f"{day}: {book_key} replays on ticks and they are not in {TICK_ARCHIVE_DIR} "
                       "- rebuild it where the ticks are and import the result")


class DayTicks:
    """One session's recorded ticks: the index path inside each minute and option quotes."""

    def __init__(self, day: str, latency_s: float = FILL_LATENCY_S):
        self.day, self.lat = day, float(latency_s)
        self.cache: dict = {}
        self.fallback = {"min": 0, "opt": 0}
        self.idx = self._series(INDEX_ALIAS)

    def _series(self, alias):
        if alias in self.cache:
            return self.cache[alias]
        import pandas as pd
        path = day_dir(self.day) / f"{alias}.parquet"
        if not path.is_file():
            self.cache[alias] = None
            return None
        f = pd.read_parquet(path, columns=["recv_ts", "ltp", "bid1_px", "ask1_px"])
        ts = f.recv_ts
        ts = ts.dt.tz_convert("Asia/Kolkata").dt.tz_localize(None) if ts.dt.tz is not None else ts
        f = f.assign(t=ts).sort_values("t", kind="stable")
        out = (list(f.t.dt.to_pydatetime()), f.ltp.tolist(), f.bid1_px.tolist(), f.ask1_px.tolist())
        self.cache[alias] = out
        return out

    def path(self, t, nb, secs, trm, base):
        """The minute's (second, spot) path from the index ticks, print seconds inserted; `base` when < 2 ticks."""
        if self.idx is None:
            self.fallback["min"] += 1
            return base
        ts, ltp = self.idx[0], self.idx[1]
        a, b = bisect.bisect_left(ts, t), bisect.bisect_left(ts, t + timedelta(minutes=1))
        pts = [((ts[k] - t).total_seconds(), float(ltp[k])) for k in range(a, b) if ltp[k] == ltp[k]]
        if len(pts) < 2:
            self.fallback["min"] += 1
            return base
        return trm._with_print_seconds(pts, secs, pts[-1][1])

    def quote(self, sym, when):
        """(mid, half spread) of the option's last tick at or before `when`, within QUOTE_MAX_AGE_S; else None."""
        s = self._series(sym) if sym else None
        if s is None:
            return None
        k = bisect.bisect_right(s[0], when) - 1
        if k < 0 or (when - s[0][k]).total_seconds() > QUOTE_MAX_AGE_S:
            return None
        ltp, bid, ask = s[1][k], s[2][k], s[3][k]
        bid = bid if bid == bid and bid > 0 else None
        ask = ask if ask == ask and ask > 0 else None
        if bid and ask:
            return (bid + ask) / 2, (ask - bid) / 2
        return (float(ltp), 0.0) if ltp == ltp and ltp > 0 else None

    def held(self, pos, now, ltp):
        q = self.quote(pos.get("sym"), now)
        if q is None:
            self.fallback["opt"] += 1
            return ltp
        pos["last"], pos["half"] = q
        return q[0]

    def entry(self, sym, now, est, q):
        tq = self.quote(sym, now + timedelta(seconds=self.lat))
        if tq is None:
            self.fallback["opt"] += 1
            return est, q
        return tq[0], (tq[0], tq[1])

    def exit_px(self, pos, now, px):
        q = self.quote(pos.get("sym"), now + timedelta(seconds=self.lat))
        if q is None:
            self.fallback["opt"] += 1
            return px
        return q[0] - q[1]

    def summary(self) -> dict:
        return {"replay": REPLAY_TICKS, "fb_min": self.fallback["min"], "fb_opt": self.fallback["opt"],
                "latency_s": self.lat}


ONE_MINUTE = {"replay": REPLAY_1MIN, "fb_min": None, "fb_opt": None, "latency_s": None}


# ------------------------------------------------------------------ record ---
def ensure_table(conn: sqlite3.Connection) -> None:
    conn.execute("CREATE TABLE IF NOT EXISTS tick_replay_days (book TEXT NOT NULL, trade_date TEXT NOT NULL, "
                 "replay TEXT NOT NULL, fb_min INTEGER, fb_opt INTEGER, latency_s REAL, updated_at TEXT NOT NULL, "
                 "PRIMARY KEY (book, trade_date))")


def record(conn: sqlite3.Connection, book_key: str, day: str, info: dict) -> None:
    ensure_table(conn)
    conn.execute("INSERT INTO tick_replay_days (book,trade_date,replay,fb_min,fb_opt,latency_s,updated_at) "
                 "VALUES (?,?,?,?,?,?,?) ON CONFLICT(book,trade_date) DO UPDATE SET replay=excluded.replay,"
                 "fb_min=excluded.fb_min,fb_opt=excluded.fb_opt,latency_s=excluded.latency_s,"
                 "updated_at=excluded.updated_at",
                 (book_key, day, info["replay"], info.get("fb_min"), info.get("fb_opt"), info.get("latency_s"),
                  datetime.now().isoformat(timespec="seconds")))


def replays(conn: sqlite3.Connection, book_key: str) -> dict:
    """trade_date -> {replay, fb_min, fb_opt, latency_s} for one book."""
    ensure_table(conn)
    return {r[0]: {"replay": r[1], "fb_min": r[2], "fb_opt": r[3], "latency_s": r[4]} for r in conn.execute(
        "SELECT trade_date,replay,fb_min,fb_opt,latency_s FROM tick_replay_days WHERE book=?", (book_key,))}


def day_done(conn: sqlite3.Connection, day: str) -> bool:
    """Every tick book that wants ticks on `day` has the day closed on ticks."""
    keys = [k for k in TICK_BOOKS if wants_ticks(k, day)]
    if not keys:
        return False
    ensure_table(conn)
    for k in keys:
        row = conn.execute(f"SELECT d.status, r.replay FROM {k}_daily d LEFT JOIN tick_replay_days r "
                           "ON r.book=? AND r.trade_date=d.trade_date WHERE d.trade_date=?", (k, day)).fetchone()
        if not row or row[0] not in ("closed", "no_trade") or row[1] != REPLAY_TICKS:
            return False
    return True


def write_marker(day: str, books: list[str]) -> Path:
    DONE_DIR.mkdir(parents=True, exist_ok=True)
    path = DONE_DIR / f"{day}.json"
    path.write_text(json.dumps({"trade_date": day, "books": books,
                                "written_at": datetime.now().isoformat(timespec="seconds")}), encoding="utf-8")
    return path


def post_close(day: str, conn: sqlite3.Connection, run_book_day) -> dict:
    """After the close: replay any tick book whose day is not yet on ticks (once the ticks are complete), then drop
    the done-marker when every tick book has the day on ticks. `run_book_day(book_key, day)` stores one book-day."""
    out = {"ready": ready(day), "ran": [], "marker": None}
    if not out["ready"]:
        return out
    done = replays_for_day(conn, day)
    for key in TICK_BOOKS:
        if wants_ticks(key, day) and done.get(key) != REPLAY_TICKS:
            run_book_day(key, day)
            out["ran"].append(key)
    if day_done(conn, day) and not (DONE_DIR / f"{day}.json").is_file():
        out["marker"] = str(write_marker(day, [k for k in TICK_BOOKS if wants_ticks(k, day)]))
    return out


def replays_for_day(conn: sqlite3.Connection, day: str) -> dict:
    ensure_table(conn)
    return {r[0]: r[1] for r in conn.execute(
        "SELECT book, replay FROM tick_replay_days WHERE trade_date=?", (day,))}


POST_CLOSE_END = (17, 30)              # the paper loop's post-close tick pass runs from its 15:40 close to 17:30 IST


def run_post_close(day: str) -> dict:
    """The paper loop's post-close pass for `day` (see post_close), on the labs database."""
    import importlib

    from storage.db import get_conn

    def run_book_day(key: str, d: str) -> None:
        importlib.import_module(f"labs.engine.{key}_book").run_day(d)

    conn = get_conn()
    try:
        return post_close(day, conn, run_book_day)
    finally:
        conn.close()
