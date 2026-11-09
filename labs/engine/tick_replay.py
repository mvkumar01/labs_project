"""Tick replay for the paper books (Vipin, 2026-11-07): replay a session on the depth recorder's recorded ticks
instead of the synthetic one-minute path, so a paper book stops and fills the way the live runners do.

- Books: Nifty Scalper V4 1.5% / 2.5% and Nifty Scalper V3 (NIFTY options) and Sensex 2.5% V6 (SENSEX options), all
  from 27 Oct 2026, the first session with recorded option bands ("Ok send both" for V3 / V6); the three Nifty New
  Alpha V1 twins (nna_c1 / nna_c2 / nna_c3, labs.engine.nna_twins_tracker) from 27 Oct too.
- Spot inside each minute = the book's recorded index ticks (NIFTY_50, or SENSEX for a SENSEX book; TICK_INDEX); the
  held option's price = its recorded best
  bid / ask (mid for the engine's day-target check); entries fill at the ask and exits at the bid FILL_LATENCY_S
  after the signal (live Angel orders take about 2.3 s to send). A minute without index ticks, or an option without
  a tick in the last 120 s, falls back to the one-minute path / estimate; both are counted per day.
- Ticks: Order Flow Research/data/archive/YYYY-MM-DD/{alias}.parquet in the alphaIMB checkout next to labs (the
  depth recorder compacts the day there at about 15:32 IST; manifest.json marks a complete day); during the session
  the recorder's growing data/live/YYYY-MM-DD/{alias}.csv, read incrementally (only complete new lines each run).
  Read-only.
- Which days: books in TICK_BOOKS, from their first tick session. All day on ticks (Vipin, 2026-11-07: option B):
  intraday from the live CSVs ("ticks_live"), after the close from the compacted day ("ticks") - that run is the
  recorded result. Today with no live index file yet: the one-minute path, labelled. A past day whose ticks are
  not here is refused, never silently replayed on the one-minute path: rebuild it where the ticks are (locally)
  and import the result.
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
from datetime import datetime, timedelta, timezone
from pathlib import Path

from config.labs_config import BASE_DIR

TICK_ARCHIVE_DIR = Path(os.environ.get(
    "LABS_TICK_ARCHIVE_DIR", BASE_DIR.parent / "alphaIMB" / "Order Flow Research" / "data" / "archive"))
TICK_LIVE_DIR = Path(os.environ.get("LABS_TICK_LIVE_DIR", TICK_ARCHIVE_DIR.parent / "live"))
DONE_DIR = BASE_DIR / "storage" / "tick_replay_done"
FILL_LATENCY_S = 2.0
QUOTE_MAX_AGE_S = 120.0
INDEX_ALIAS = "NIFTY_50"
_IST = timezone(timedelta(hours=5, minutes=30))
# Book key -> first session replayed on ticks (the option bands were recorded from 27 Oct 2026).
TICK_BOOKS = {"proposer_nifty_v4": "2026-10-27", "proposer_nifty_v4_dt25": "2026-10-27",
              "proposer_nifty_v3": "2026-10-27", "proposer_v6_dt25": "2026-10-27",
              "proposer_v6_dt25_ivh": "2026-10-27",
              "nna_c1": "2026-10-27", "nna_c2": "2026-10-27", "nna_c3": "2026-10-27",
              "proposer_nifty_v4_fc": "2026-10-27", "proposer_nifty_v4_dt25_fc": "2026-10-27"}
# Book key -> its index's recorder alias, when not NIFTY_50.
TICK_INDEX = {"proposer_v6_dt25": "SENSEX", "proposer_v6_dt25_ivh": "SENSEX"}
# Sessions on or after a book's first tick session that have no recorded ticks anywhere: one-minute path, labelled.
NO_TICK_DAYS: frozenset = frozenset()
REPLAY_TICKS, REPLAY_TICKS_LIVE, REPLAY_1MIN = "ticks", "ticks_live", "1min"
# A book's closed session: the proposer books write closed / no_trade, the NNA twins traded / no_trade /
# partial_quotes (a session whose trades lack a quote is still over).
DONE_STATUSES = ("closed", "no_trade", "traded", "partial_quotes")
ON_TICKS = (REPLAY_TICKS, REPLAY_TICKS_LIVE)


class TicksMissing(Exception):
    """A past tick-book session whose ticks are not in this checkout."""


def day_dir(day: str) -> Path:
    return TICK_ARCHIVE_DIR / day


def index_of(book_key: str) -> str:
    """The recorder alias of the book's index ticks."""
    return TICK_INDEX.get(book_key, INDEX_ALIAS)


def ready(day: str, index: str = INDEX_ALIAS) -> bool:
    """True once the recorder has compacted the day (manifest written) and the index ticks are there."""
    d = day_dir(day)
    return (d / "manifest.json").is_file() and (d / f"{index}.parquet").is_file()


def ready_for(book_key: str, day: str) -> bool:
    return ready(day, index_of(book_key))


def live_ready(day: str, index: str = INDEX_ALIAS) -> bool:
    """True while the recorder is writing the day's index ticks to its live CSV."""
    return (TICK_LIVE_DIR / day / f"{index}.csv").is_file()


def wants_ticks(book_key: str, day: str) -> bool:
    start = TICK_BOOKS.get(book_key)
    return bool(start) and day >= start and day not in NO_TICK_DAYS


def ticks_for(book_key: str, day: str, today: str) -> "DayTicks | None":
    """The DayTicks to replay `day` with, or None for the one-minute path. Raises TicksMissing for a past session
    that must be on ticks but whose ticks are not here."""
    if not wants_ticks(book_key, day):
        return None
    index = index_of(book_key)
    if ready(day, index):
        return DayTicks(day, index=index)
    if day >= today:                    # today: the recorder's live files, else (none yet) the one-minute path
        if not live_ready(day, index):
            return None
        forget_live(day)                                       # keep only today's files cached
        return DayTicks(day, source="live", index=index)
    raise TicksMissing(f"{day}: {book_key} replays on ticks and they are not in {TICK_ARCHIVE_DIR} "
                       "- rebuild it where the ticks are and import the result")


class DayTicks:
    """One session's recorded ticks: the index path inside each minute and option quotes."""

    def __init__(self, day: str, latency_s: float = FILL_LATENCY_S, source: str = "archive",
                 index: str = INDEX_ALIAS):
        self.day, self.lat, self.source, self.index = day, float(latency_s), source, index
        self.cache: dict = {}
        self.fallback = {"min": 0, "opt": 0}
        self.idx = self._series(index)

    def _series(self, alias):
        if alias in self.cache:
            return self.cache[alias]
        if self.source == "live":
            out = _live_series(TICK_LIVE_DIR / self.day / f"{alias}.csv")
            self.cache[alias] = out
            return out
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
        return {"replay": REPLAY_TICKS_LIVE if self.source == "live" else REPLAY_TICKS,
                "fb_min": self.fallback["min"], "fb_opt": self.fallback["opt"], "latency_s": self.lat}


# ------------------------------------------------------------- live files ---
# path -> {"offset", "size", "cols", "t", "ltp", "bid", "ask"}; kept for the paper loop's process so each run
# parses only the lines the recorder appended since the last one.
_LIVE: dict = {}


def _num(text: str) -> float:
    try:
        return float(text) if text != "" else float("nan")
    except ValueError:
        return float("nan")


def _live_series(path: Path):
    """(times, ltp, bid1, ask1) of a live CSV as read_depth would give them after compaction (rows in stable time
    order, a torn last line left for the next read); None when the file is not there."""
    import csv
    import io

    try:
        size = path.stat().st_size
    except OSError:
        _LIVE.pop(str(path), None)
        return None
    key = str(path)
    st = _LIVE.get(key)
    if st is None or size < st["size"]:                       # new file, or rewritten: start over
        st = _LIVE[key] = {"offset": 0, "size": 0, "cols": None, "t": [], "ltp": [], "bid": [], "ask": []}
    if size > st["offset"]:
        with path.open("rb") as fh:
            fh.seek(st["offset"])
            chunk = fh.read(size - st["offset"])
        end = chunk.rfind(b"\n")
        if end >= 0:
            st["offset"] += end + 1
            rows = list(csv.reader(io.StringIO(chunk[:end + 1].decode("utf-8", "replace"))))
            if st["cols"] is None and rows:
                head = rows.pop(0)
                st["cols"] = [head.index(c) for c in ("recv_ts", "ltp", "bid1_px", "ask1_px")]
            it, il, ib, ia = st["cols"] or (0, 0, 0, 0)
            last = st["t"][-1] if st["t"] else None
            disorder = False
            for r in rows:
                try:
                    ts = datetime.fromisoformat(r[it])
                except (ValueError, IndexError):
                    continue
                if ts.tzinfo is not None:
                    ts = ts.astimezone(_IST).replace(tzinfo=None)
                if last is not None and ts < last:
                    disorder = True
                last = ts
                st["t"].append(ts)
                st["ltp"].append(_num(r[il]) if len(r) > il else float("nan"))
                st["bid"].append(_num(r[ib]) if len(r) > ib else float("nan"))
                st["ask"].append(_num(r[ia]) if len(r) > ia else float("nan"))
            if disorder:                                       # the archive's stable sort on time
                order = sorted(range(len(st["t"])), key=st["t"].__getitem__)
                for f in ("t", "ltp", "bid", "ask"):
                    st[f] = [st[f][i] for i in order]
        st["size"] = size
    if not st["t"]:
        return None
    return st["t"], st["ltp"], st["bid"], st["ask"]


def forget_live(day: str | None = None) -> None:
    """Drop cached live files (all, or another day's) - the paper loop keeps only today's."""
    for key in [k for k in _LIVE if day is None or f"{os.sep}{day}{os.sep}" not in k]:
        _LIVE.pop(key, None)


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
        try:
            row = conn.execute(f"SELECT d.status, r.replay FROM {k}_daily d LEFT JOIN tick_replay_days r "
                               "ON r.book=? AND r.trade_date=d.trade_date WHERE d.trade_date=?", (k, day)).fetchone()
        except sqlite3.OperationalError:              # the book has never stored a session here
            return False
        if not row or row[0] not in DONE_STATUSES or row[1] != REPLAY_TICKS:
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
    keys = [k for k in TICK_BOOKS if wants_ticks(k, day)]
    out = {"ready": bool(keys) and all(ready_for(k, day) for k in keys), "ran": [], "marker": None}
    if not any(ready_for(k, day) for k in keys):
        return out
    forget_live()                                             # the live files are gone once compacted
    done = replays_for_day(conn, day)
    for key in keys:                    # date order within a book is kept: only today runs here
        if ready_for(key, day) and done.get(key) != REPLAY_TICKS:
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
