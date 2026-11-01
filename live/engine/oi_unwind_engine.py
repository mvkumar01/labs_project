"""OI Unwind on live: the paper books' rule (labs/engine/oi_unwind_tracker.py) for the Proposer runner.

The entry rule itself is the tracker's entry_signal, so paper and live read one rule. What differs from paper
(Vipin, 2026-11-02):
  - one position per connection: a signal is skipped while a trade is open. On the 83 research trades that skips 5
    of 58 NIFTY signals (+Rs 374 of +Rs 18,577) and no SENSEX signal;
  - one index per connection, one strategy version each;
  - stop and target are checked on every tick, not on 1-min highs and lows, and a signal older than
    SIGNAL_WINDOW_S at its candle's close is not taken;
  - entries hold when the latest wall mark is older than MAX_MARK_AGE (paper reads any older mark).

Evidence is in the tracker's header: in-sample, and the 30-point stop is an assumption.
"""
from __future__ import annotations

import json
import logging
import threading
import time
from datetime import date, datetime, timedelta
from typing import Optional

import pandas as pd

from config.labs_config import SHARED_ARCHIVE_DIR, SHARED_LIVE_DIR
from labs.engine import oi_unwind_tracker as U
from labs.engine.expiry_strangle_tracker import expiry_date_of
from live.engine import proposer_engine as pe
from market_data.shared_store import load_options_frame

log = logging.getLogger("live.oi_unwind")

BOOK_BY_VERSION = {pe.STRATEGY_VERSION_OI_UNWIND_NIFTY: U.NIFTY, pe.STRATEGY_VERSION_OI_UNWIND_SENSEX: U.SENSEX}
ENTRY_REASON = "oi_unwind_break"
LEVELS_KEY = "oi_unwind_levels"
SIGNAL_WINDOW_S = 30           # seconds after the candle's close; later than this it is history, not a signal
MAX_MARK_AGE = timedelta(minutes=10)   # at the candle's last 1-min label: one missed mark passes, two hold entries
MARK_DELAY = timedelta(seconds=60)     # the tracker's rule: a decision reads marks at least this old
WALL_COLUMNS = ["timestamp", "strike", "option_type", "expiry", "oi", "spot"]


def book_for(strategy_version) -> Optional[U.Book]:
    return BOOK_BY_VERSION.get(str(strategy_version or ""))


# ------------------------------------------------------------------ walls ---
def latest_marks(frame: pd.DataFrame, book: U.Book, keep: int = 2) -> list[tuple]:
    """wall_marks(frame, book)[-keep:] without working through the day's earlier marks."""
    stamps = pd.to_datetime(frame["timestamp"].astype(str).str.slice(0, 19))
    grid = sorted(stamps[stamps.dt.minute % 5 == 0].dropna().unique())
    return U.wall_marks(frame[stamps.isin(grid[-(keep + 1):])], book)[-keep:]


def read_marks(day: str, book: U.Book) -> list[tuple]:
    frame = load_options_frame(book.symbol, day, live_root=SHARED_LIVE_DIR, archive_root=SHARED_ARCHIVE_DIR,
                               columns=WALL_COLUMNS)
    return latest_marks(frame, book)


def read_cpr(day: str, book: U.Book) -> dict:
    high, low, close, _ = U.previous_hlc(day, book)
    return U.cpr_levels(high, low, close)


class WallFeed:
    """The session's CPR and latest wall marks for one book. Reading the day's option file takes seconds and this
    runner checks other connections' exits on every tick, so step() only starts the read, on its own thread."""
    RETRY_S = 15.0

    def __init__(self, book: U.Book, marks_fn=read_marks, cpr_fn=read_cpr, background: bool = True):
        self.book, self.marks_fn, self.cpr_fn, self.background = book, marks_fn, cpr_fn, background
        self.day: Optional[str] = None
        self.marks: list[tuple] = []
        self.cpr: Optional[dict] = None
        self._busy = False
        self._tried = float("-inf")

    def _current(self, now: datetime) -> bool:
        wanted = (now - MARK_DELAY).replace(second=0, microsecond=0)
        wanted -= timedelta(minutes=wanted.minute % 5)
        return self.cpr is not None and bool(self.marks) and self.marks[-1][0] >= wanted

    def step(self, now: datetime) -> None:
        if not U.SESSION_OPEN <= now.time() < U.LAST_ENTRY:
            return
        day = now.date().isoformat()
        if day != self.day:
            self.day, self.marks, self.cpr, self._tried = day, [], None, float("-inf")
        if self._busy or self._current(now) or time.monotonic() - self._tried < self.RETRY_S:
            return
        self._busy, self._tried = True, time.monotonic()
        if self.background:
            threading.Thread(target=self._read, args=(day,), daemon=True, name=f"oi-walls-{self.book.symbol}").start()
        else:
            self._read(day)

    def _read(self, day: str) -> None:
        started = time.monotonic()
        try:
            cpr = self.cpr if self.cpr is not None else self.cpr_fn(day, self.book)
            marks = self.marks_fn(day, self.book)
            if day == self.day:
                self.cpr, self.marks = cpr, marks
            log.info("%s walls read in %.1fs, latest mark %s", self.book.symbol, time.monotonic() - started,
                     marks[-1][0].time() if marks else None)
        except Exception as e:                  # the collector's first rows may not exist yet; retried
            log.warning("%s wall read failed: %s: %s", self.book.symbol, type(e).__name__, e)
        finally:
            self._busy = False


def close_bars(book: U.Book):
    """The collector's 1-min closes, in the shape proposer_runner.SessionBars reads."""
    def read(day: date) -> list[tuple[datetime, float]]:
        return [(m, ohlc[3]) for m, ohlc in sorted(U.spot_bars(day.isoformat(), book).items())]
    return read


# --------------------------------------------------------------- decisions ---
def candle_label(now: datetime) -> Optional[datetime]:
    """The last 1-min label of the 5-minute candle that has just closed, inside the signal window."""
    if now.minute % 5 or now.second >= SIGNAL_WINDOW_S:
        return None
    return now.replace(second=0, microsecond=0) - timedelta(minutes=1)


def decide(label: datetime, closes: list[tuple[datetime, float]], walls: WallFeed) -> tuple[Optional[dict], str]:
    """(signal or None, why): "ok" is a finished decision, anything else is data still missing for this candle."""
    if walls.cpr is None:
        return None, "cpr_unknown"
    bars = [(m, c, c, c, c) for m, c in closes if U.SESSION_OPEN <= m.time()]
    if not bars or bars[-1][0] != label:
        return None, "bar_missing"
    marks = [m for m in walls.marks if m[0] <= label - MARK_DELAY]
    if not marks or label - marks[-1][0] > MAX_MARK_AGE:
        return None, "walls_stale"
    return U.entry_signal(bars, marks, walls.cpr, walls.book), "ok"


def exit_reason(side: str, spot: float, levels: dict) -> Optional[str]:
    """Stop first, as paper counts a bar holding both."""
    s = 1 if side == "CALL" else -1
    if s * spot <= s * float(levels["stop"]):
        return "oi_stop"
    if s * spot >= s * float(levels["target"]):
        return "oi_target"
    return None


def dump_levels(signal: dict, symbol: str) -> str:
    """Saved before the order goes out: a position must never exist without its stop."""
    return json.dumps({"symbol": symbol, "stop": signal["stop"], "target": signal["target"], "wall": signal["wall"]})


def load_levels(raw, symbol: str) -> Optional[dict]:
    try:
        levels = json.loads(raw or "")
        return levels if levels["symbol"] == symbol and float(levels["stop"]) and float(levels["target"]) else None
    except (ValueError, KeyError, TypeError):
        return None


# ------------------------------------------------------------------ option ---
def next_expiry_code(codes, today: date, symbol: str) -> Optional[str]:
    """Paper's expiry: the nearest listed one strictly after today. None when it is missing (fail closed)."""
    dated = [(expiry_date_of(str(c), symbol), str(c)) for c in codes]
    valid = [(d, c) for d, c in dated if d is not None and d > today]
    return min(valid)[1] if valid else None


def atm_symbol(chain: pd.DataFrame, side: str, spot: float, book: U.Book) -> tuple[Optional[str], int, str]:
    """Paper's option: the strike nearest the signal's close, with a traded price in the latest snapshot."""
    strike, typ = int(round(spot / book.strike_step) * book.strike_step), "CE" if side == "CALL" else "PE"
    if chain.empty:
        return None, strike, typ
    rows = chain[(chain["strike"] == strike) & (chain["option_type"].astype(str).str.upper() == typ)
                 & (pd.to_numeric(chain["ltp"], errors="coerce") > 0)]
    return (str(rows["tradingsymbol"].iloc[0]) if not rows.empty else None), strike, typ
