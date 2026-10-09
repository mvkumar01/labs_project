"""MCX gold CCI short: real-time runner.  PHASE 0 - DRY RUN ONLY.

Decides in real time, the way a live bot must. At each minute boundary it waits for the GOLD
candle that has just closed, asks the engine (live/engine/gold_cci_engine.py) whether the rule
signals on it and - with no position held - sells at the open of the new minute. It then watches
the price every couple of seconds for the stop, the target and the session close, and moves the
stop to the entry price once the trade has been one stop distance in profit.

PHASE 0: THERE IS NO BROKER CODE PATH IN THIS FILE. Every "order" is a row in live_gold_orders,
filled at the Kite last traded price read at that moment (dry_run = 1).

Two contracts, on purpose:
  signal  GOLD (1 kg), the front contract - the instrument the rule was found on;
  traded  GOLDM (100 g, a tenth of the money), the nearest contract at least ROLL_DAYS from expiry.
The stop (0.25%) and target (3 stops) are taken from the GOLDM fill and watched on GOLDM's price.

Differences from the paper replay (labs/engine/gold_cci_tracker.py) that are deliberate and
recorded on each trade:
  - the paper book trades GOLD itself, on bar highs and lows; this trades GOLDM on polled prices;
  - stop and target sit on the Rs 1 tick, moved outward from the entry;
  - the stop moves to the entry when the minute rolls over after a polled price one stop distance
    in favour (the back test: after a completed bar that traded there);
  - the session-close exit is at 23:29 on a polled price, and no entry is taken in that minute.

Data: completed 1-minute Kite candles of GOLD. History comes from the shared MCX candle cache in
labs.db (the paper loop writes it, including the seeded October contract), topped up each minute
straight from Kite, and joined exactly as the paper book joins it.
"""
from __future__ import annotations

import json
import logging
import uuid
from datetime import date, datetime, time as dtime, timedelta

import pandas as pd

from live import crudem_runner as cr
from live.engine import gold_cci_engine as eng
from live.notify import notify_telegram

log = logging.getLogger("live.gold_runner")

SIGNAL_UNDERLYING, TRADE_UNDERLYING, EXCHANGE = "GOLD", "GOLDM", "MCX"
BOOK = "dry"
DRY_RUN = True                        # Phase 0: this module cannot place an order
LOTS = 1
QTY = LOTS * 10                       # GOLDM: 100 g, quoted per 10 g -> Rs 10 per rupee of price
REPLAY_START = "2026-06-01"           # the paper book's start, so both judge "already in a trade" alike
START = "2026-10-09"                  # first session this runner trades
ROLL_DAYS = 5                         # leave a GOLDM contract this many days before its expiry
SESSION_OPEN, SESSION_END, EOD_EXIT = cr.SESSION_OPEN, cr.SESSION_END, cr.EOD_EXIT
POLL_S, CANDLE_WAIT_S = cr.POLL_S, cr.CANDLE_WAIT_S
SEEDED_EXPIRY = {"GOLD26OCTFUT": "2026-09-28"}     # as the paper book joins the seeded contract

now_ist, charges, tick_levels = cr.now_ist, cr.charges, cr.tick_levels


# ------------------------------------------------------------------ schema ---
def ensure_schema(conn=None) -> None:
    own = conn is None
    conn = conn or cr.get_live_conn()
    try:
        conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS live_gold_position (
                book TEXT PRIMARY KEY, state TEXT NOT NULL, updated_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS live_gold_orders (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                book TEXT NOT NULL, trade_ref TEXT NOT NULL, kind TEXT NOT NULL, side TEXT NOT NULL,
                symbol TEXT NOT NULL, qty INTEGER NOT NULL, reason TEXT,
                decided_at TEXT NOT NULL, delay_s REAL, ref_price REAL, signal_bar_close REAL,
                fill_price REAL, broker_order_id TEXT, status TEXT NOT NULL, dry_run INTEGER NOT NULL
            );
            CREATE TABLE IF NOT EXISTS live_gold_trades (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                book TEXT NOT NULL, trade_ref TEXT NOT NULL UNIQUE, trade_date TEXT NOT NULL,
                direction TEXT NOT NULL, symbol TEXT NOT NULL, signal_symbol TEXT NOT NULL, qty INTEGER NOT NULL,
                signal_ts TEXT NOT NULL, entry_ts TEXT NOT NULL, exit_ts TEXT, entry_price REAL NOT NULL,
                exit_price REAL, stop_price REAL NOT NULL, target_price REAL NOT NULL, stop_dist REAL NOT NULL,
                stop_moved_at TEXT, exit_reason TEXT, gross_rs REAL, charges_rs REAL, net_rs REAL,
                dry_run INTEGER NOT NULL
            );
            CREATE TABLE IF NOT EXISTS live_gold_decisions (
                book TEXT NOT NULL, signal_ts TEXT NOT NULL, outcome TEXT NOT NULL, detail TEXT,
                decided_at TEXT NOT NULL, PRIMARY KEY (book, signal_ts)
            );
            """
        )
        conn.commit()
    finally:
        if own:
            conn.close()


# -------------------------------------------------------------------- feed ---
class KiteFeed(cr.KiteFeed):
    """Read-only market data from the labs Kite data session (never a broker order session)."""

    def _futures(self) -> list[dict]:
        today = now_ist().date()
        if self._master["date"] != today or not self._master["rows"]:
            rows = [{"name": r.get("name"), "tradingsymbol": r["tradingsymbol"],
                     "instrument_token": int(r["instrument_token"]), "expiry": str(r["expiry"])[:10]}
                    for r in self._kite().instruments(EXCHANGE)
                    if r.get("name") in (SIGNAL_UNDERLYING, TRADE_UNDERLYING) and r.get("instrument_type") == "FUT"]
            self._master.update(date=today, rows=rows)
        return list(self._master["rows"])

    def contracts(self) -> list[dict]:
        """The signal instrument's futures (what the candle series is built from)."""
        return [c for c in self._futures() if c["name"] == SIGNAL_UNDERLYING]

    def traded(self, today: date) -> dict | None:
        """The GOLDM contract to trade: the nearest one still ROLL_DAYS or more from its expiry."""
        ok = sorted((c for c in self._futures() if c["name"] == TRADE_UNDERLYING
                     and (date.fromisoformat(c["expiry"]) - today).days >= ROLL_DAYS), key=lambda c: c["expiry"])
        return ok[0] if ok else None


def cached_history(start: date, end: date) -> tuple[dict[str, pd.DataFrame], dict[str, str]]:
    """GOLD candles and contract expiries the paper loop has stored in labs.db (read only)."""
    from storage.db import get_conn
    frames: dict[str, pd.DataFrame] = {}
    expiries: dict[str, str] = dict(SEEDED_EXPIRY)
    conn = get_conn()
    try:
        try:
            expiries.update(dict(conn.execute("SELECT tradingsymbol, expiry FROM gold_cci_contracts").fetchall()))
            marks = ",".join("?" * len(expiries))
            f = pd.read_sql_query(
                "SELECT tradingsymbol, ts, open, high, low, close, volume FROM crude_minute_bars "
                f"WHERE tradingsymbol IN ({marks}) AND ts >= ? AND ts < ? ORDER BY ts", conn,
                params=(*expiries, start.isoformat(), (end + timedelta(days=1)).isoformat()))
        except Exception as e:                        # tables not created yet
            log.warning("no cached GOLD candles in labs.db: %s", type(e).__name__)
            return frames, expiries
    finally:
        conn.close()
    f["ts"] = pd.to_datetime(f["ts"])
    for sym, g in f.groupby("tradingsymbol"):
        frames[sym] = g.drop(columns="tradingsymbol").reset_index(drop=True)
    return frames, expiries


# ------------------------------------------------------------------- state ---
def load_position(conn, book: str = BOOK) -> dict | None:
    row = conn.execute("SELECT state FROM live_gold_position WHERE book=?", (book,)).fetchone()
    return json.loads(row[0]) if row and row[0] and row[0] != "null" else None


def save_position(conn, pos: dict | None, book: str = BOOK) -> None:
    conn.execute("INSERT INTO live_gold_position (book, state, updated_at) VALUES (?,?,?) "
                 "ON CONFLICT(book) DO UPDATE SET state=excluded.state, updated_at=excluded.updated_at",
                 (book, json.dumps(pos), now_ist().isoformat(timespec="seconds")))
    conn.commit()


def _record_decision(conn, signal_ts, outcome: str, detail: str | None, now: datetime) -> None:
    conn.execute("INSERT OR IGNORE INTO live_gold_decisions (book, signal_ts, outcome, detail, decided_at) "
                 "VALUES (?,?,?,?,?)", (BOOK, str(signal_ts), outcome, detail, now.isoformat(timespec="seconds")))


def _order(conn, pos: dict, kind: str, side: str, reason: str, now: datetime, ref_price, fill: float,
           signal_close: float | None = None) -> None:
    conn.execute(
        "INSERT INTO live_gold_orders (book, trade_ref, kind, side, symbol, qty, reason, decided_at, delay_s, "
        "ref_price, signal_bar_close, fill_price, status, dry_run) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (BOOK, pos["ref"], kind, side, pos["symbol"], pos["qty"], reason, now.isoformat(timespec="seconds"),
         round(now.second + now.microsecond / 1e6, 2) if kind == "entry" else None, ref_price, signal_close, fill,
         "DRY_FILLED", int(DRY_RUN)))


# ------------------------------------------------------------------ runner ---
class Runner:
    def __init__(self, feed=None, series: cr.Series | None = None):
        self.feed = feed or KiteFeed()
        self.series = series or cr.Series(self.feed, history=cached_history)
        self.decided: datetime | None = None          # the minute boundary already handled
        self.warned: datetime | None = None

    def step(self, now: datetime, conn) -> dict | None:
        """One poll. Returns the position held after it (None when flat)."""
        pos = load_position(conn)
        in_session = now.weekday() < 5 and SESSION_OPEN <= now.time() < SESSION_END
        if pos is not None and (not in_session or pos["entry_ts"][:10] != now.date().isoformat()):
            # the runner was down at the session close: an intraday position cannot be carried
            return self._manage(pos, now, conn, force="eod_missed")
        if not in_session or now.date().isoformat() < START:
            return pos
        if pos is not None:
            pos = self._manage(pos, now, conn)
        cutoff = now.replace(second=0, microsecond=0)
        if self.decided == cutoff or cutoff.time() <= SESSION_OPEN:
            return pos
        self.series.refresh(now)
        if self.series.front(now) is None:
            return pos
        frame = self.series.frame(now)
        if not len(frame) or frame["ts"].iloc[-1] < pd.Timestamp(cutoff) - pd.Timedelta(minutes=1):
            if now.second >= CANDLE_WAIT_S:           # the candle never came: this minute is skipped
                self.decided = cutoff
                if self.warned != cutoff:
                    log.warning("GOLD candle for %s not available after %ds - minute skipped",
                                (cutoff - timedelta(minutes=1)).time(), CANDLE_WAIT_S)
                    self.warned = cutoff
            return pos
        self.decided = cutoff
        fire = eng.pending_fire(frame, REPLAY_START, cutoff)
        if fire is not None:
            detail = f"cci {fire['cci']:.0f} rsi {fire['rsi']:.0f} di- {fire['minus_di']:.1f} di+ {fire['plus_di']:.1f}"
            if pos is not None:
                _record_decision(conn, fire["signal_ts"], "position_held", detail, now)
            elif now.time() >= EOD_EXIT:
                _record_decision(conn, fire["signal_ts"], "session_closing", detail, now)
            else:
                pos = self._enter(fire, float(frame["close"].iloc[-1]), now, conn)
                _record_decision(conn, fire["signal_ts"], "taken" if pos else "no_price",
                                 f"{detail}; delay {now.second}s" if pos else detail, now)
        conn.commit()
        return pos

    def _enter(self, fire: dict, signal_close: float, now: datetime, conn) -> dict | None:
        contract = self.feed.traded(now.date())
        price = self.feed.ltp(contract["tradingsymbol"]) if contract else None
        if not price:
            log.warning("gold entry skipped: no %s contract or price", TRADE_UNDERLYING)
            return None
        symbol = contract["tradingsymbol"]
        dist, stop, target = eng.levels(price)
        stop, target = tick_levels(eng.SIDE, stop, target)
        front = self.series.front(now)
        pos = {"ref": uuid.uuid4().hex[:12], "side": eng.SIDE, "symbol": symbol,
               "signal_symbol": front["tradingsymbol"] if front else SIGNAL_UNDERLYING, "qty": QTY,
               "signal_ts": str(fire["signal_ts"]), "entry_ts": now.isoformat(timespec="seconds"),
               "minute": now.replace(second=0, microsecond=0).isoformat(), "entry_price": price,
               "stop_dist": dist, "stop": stop, "stop0": stop, "target": target, "best": price, "moved": False}
        _order(conn, pos, "entry", "SELL", "cci short", now, None, price, signal_close)
        conn.execute(
            "INSERT INTO live_gold_trades (book, trade_ref, trade_date, direction, symbol, signal_symbol, qty, signal_ts, "
            "entry_ts, entry_price, stop_price, target_price, stop_dist, dry_run) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (BOOK, pos["ref"], now.date().isoformat(), "short", symbol, pos["signal_symbol"], QTY, pos["signal_ts"],
             pos["entry_ts"], price, stop, target, dist, int(DRY_RUN)))
        save_position(conn, pos)
        log.info("DRY ENTER SHORT %s @ %.0f stop %.0f target %.0f (decided %ds into the minute)",
                 symbol, price, stop, target, now.second)
        notify_telegram(f"[DRY-RUN] Gold CCI SHORT {symbol} @ {price:.0f} | stop {stop:.0f} target {target:.0f}")
        return pos

    def _manage(self, pos: dict, now: datetime, conn, force: str | None = None) -> dict | None:
        price = self.feed.ltp(pos["symbol"])
        if not price:
            if not force:
                return pos
            price = pos["entry_price"]                 # no price to mark it at: closed flat, and flagged
        side = pos["side"]
        minute = now.replace(second=0, microsecond=0).isoformat()
        changed = False
        if force is None and minute != pos.get("minute"):
            # a minute has closed: one stop distance in favour inside it moves the stop to the entry
            if not pos["moved"] and side * (pos["best"] - pos["entry_price"]) >= eng.ACTIVATE_R * pos["stop_dist"]:
                pos["stop"], pos["moved"] = pos["entry_price"], True
                conn.execute("UPDATE live_gold_trades SET stop_moved_at=? WHERE trade_ref=?",
                             (now.isoformat(timespec="seconds"), pos["ref"]))
                log.info("DRY STOP TO ENTRY %s @ %.0f (best %.0f)", pos["symbol"], pos["stop"], pos["best"])
            pos["minute"], changed = minute, True
        reason = force
        if reason is not None:
            pass
        elif side * (price - pos["stop"]) <= 0:
            reason = "stop at entry" if pos["moved"] else "stop"
        elif side * (price - pos["target"]) >= 0:
            reason = "target"
        elif now.time() >= EOD_EXIT:
            reason = "eod"
        if reason is None:
            if side * (price - pos["best"]) > 0:
                pos["best"], changed = price, True
            if changed:
                save_position(conn, pos)
            return pos
        level = pos["stop"] if reason.startswith("stop") else (pos["target"] if reason == "target" else None)
        _order(conn, pos, "exit", "BUY" if side < 0 else "SELL", reason, now, level, price)
        gross = side * (price - pos["entry_price"]) * pos["qty"]
        buy, sell = (pos["entry_price"], price) if side > 0 else (price, pos["entry_price"])
        cost = charges(buy, sell, pos["qty"])
        conn.execute("UPDATE live_gold_trades SET exit_ts=?, exit_price=?, exit_reason=?, gross_rs=?, charges_rs=?, "
                     "net_rs=? WHERE trade_ref=?",
                     (now.isoformat(timespec="seconds"), price, reason, round(gross, 2), round(cost, 2),
                      round(gross - cost, 2), pos["ref"]))
        save_position(conn, None)
        log.info("DRY EXIT %s %s @ %.0f gross %.0f net %.0f", reason, pos["symbol"], price, gross, gross - cost)
        notify_telegram(f"[DRY-RUN] Gold CCI exit ({reason}) @ {price:.0f} | net Rs {gross - cost:,.0f}")
        return None
