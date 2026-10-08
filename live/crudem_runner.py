"""CRUDEOILM consistent combination: real-time runner.  PHASE 0 - DRY RUN ONLY.

This process decides in real time, the way a live bot must: at each minute boundary it waits for
the candle that has just closed, asks the engine (live/engine/crudem_combo_engine.py) which members
signal on it, and - with no position held - takes the first one at the open of the new minute. It
then watches the price every couple of seconds for the stop, the target and the session close.

PHASE 0: THERE IS NO BROKER CODE PATH IN THIS FILE. Every "order" is a row in live_crudem_orders,
filled at the Kite last traded price read at that moment (dry_run = 1). Its purpose is to measure,
before any real order exists, how a live bot differs from the paper replay
(labs/engine/crudem_combo_tracker.py):
  - decision delay: seconds from the minute boundary to the decision (the candle must arrive first);
  - entry slippage: the price at the decision against the bar's open the back test enters on;
  - exits seen on polled prices rather than on a bar's high and low.
Phase 1 (real orders on the operator's Angel One account, stop resting at the broker, target and
session close by the bot) is a separate, reviewed change after a clean dry run.

Differences from the back test that are deliberate and recorded on each trade:
  - a percentage stop is taken from the actual fill, not from the bar's open;
  - stop and target sit on the Rs 1 tick, moved outward from the entry, which is touched on the
    same bars as the back test's unrounded levels;
  - the session-close exit is at 23:29, on a polled price, not at the last bar's close, and no
    new entry is taken in that last minute.

Data: completed 1-minute Kite candles. History comes from the shared MCX candle cache in labs.db
(written by the paper loop; storage is neutral infrastructure), topped up each minute straight from
Kite. Contracts are joined exactly as the back test joins them (engine ``stitch``).
"""
from __future__ import annotations

import json
import logging
import time
import uuid
from datetime import date, datetime, time as dtime, timedelta, timezone

import pandas as pd

from live.engine import crudem_combo_engine as eng
from live.notify import notify_telegram
from storage.live_db import get_live_conn, init_live_db

log = logging.getLogger("live.crudem_runner")

IST = timezone(timedelta(hours=5, minutes=30))
UNDERLYING, EXCHANGE = "CRUDEOILM", "MCX"
BOOK = "dry"                          # Phase 1 adds one book per armed connection
DRY_RUN = True                        # Phase 0: this module cannot place an order
LOTS = 1
QTY = LOTS * eng.LOT_QTY
START = "2026-10-07"                  # first session the combination was not chosen on
LOOKBACK_DAYS = 60
NEXT_CONTRACT_DAYS = 7
SESSION_OPEN, SESSION_END = dtime(9, 0), dtime(23, 30)
EOD_EXIT = dtime(23, 29)              # the back test exits at the close of the 23:29 bar
POLL_S = 2.0
CANDLE_WAIT_S = 25                    # give up on a minute whose candle has not arrived by then
KNOWN_EXPIRED = ({"tradingsymbol": "CRUDEOILM26SEPFUT", "instrument_token": 144870407, "expiry": "2026-09-21"},)
# MCX futures charges (labs/engine/charges.py, the back test's cost model)
BROKERAGE_PCT, BROKERAGE_CAP, CTT_SELL, EXCH_TXN, STAMP_BUY, SEBI, GST = 0.0003, 20.0, 0.0001, 0.000021, 0.00002, 0.000001, 0.18


def now_ist() -> datetime:
    return datetime.now(IST).replace(tzinfo=None)


def charges(buy_price: float, sell_price: float, qty: int) -> float:
    buy, sell = float(buy_price) * qty, float(sell_price) * qty
    brokerage = min(buy * BROKERAGE_PCT, BROKERAGE_CAP) + min(sell * BROKERAGE_PCT, BROKERAGE_CAP)
    txn, sebi = EXCH_TXN * (buy + sell), SEBI * (buy + sell)
    return brokerage + CTT_SELL * sell + txn + sebi + STAMP_BUY * buy + GST * (brokerage + txn + sebi)


def tick_levels(side: int, stop: float, target: float) -> tuple[float, float]:
    """Stop and target on the Rs 1 tick, each moved outward from the entry (a long's stop down and
    target up). Prices only trade on the tick, so these are touched on exactly the bars on which
    the back test's unrounded levels are."""
    import math
    tick, eps = eng.TICK_SIZE, 1e-9
    down = lambda x: math.floor(x / tick + eps) * tick           # noqa: E731
    up = lambda x: math.ceil(x / tick - eps) * tick              # noqa: E731
    return (down(stop), up(target)) if side > 0 else (up(stop), down(target))


# ------------------------------------------------------------------ schema ---
def ensure_schema(conn=None) -> None:
    own = conn is None
    conn = conn or get_live_conn()
    try:
        conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS live_crudem_position (
                book TEXT PRIMARY KEY, state TEXT NOT NULL, updated_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS live_crudem_orders (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                book TEXT NOT NULL, trade_ref TEXT NOT NULL, kind TEXT NOT NULL, side TEXT NOT NULL,
                symbol TEXT NOT NULL, qty INTEGER NOT NULL, reason TEXT,
                decided_at TEXT NOT NULL, delay_s REAL, ref_price REAL, bar_open REAL,
                fill_price REAL, broker_order_id TEXT, status TEXT NOT NULL, dry_run INTEGER NOT NULL
            );
            CREATE TABLE IF NOT EXISTS live_crudem_trades (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                book TEXT NOT NULL, trade_ref TEXT NOT NULL UNIQUE, trade_date TEXT NOT NULL, cid INTEGER NOT NULL,
                direction TEXT NOT NULL, symbol TEXT NOT NULL, qty INTEGER NOT NULL, signal_ts TEXT NOT NULL,
                entry_ts TEXT NOT NULL, exit_ts TEXT, entry_price REAL NOT NULL, exit_price REAL,
                stop_price REAL NOT NULL, target_price REAL NOT NULL, stop_dist REAL NOT NULL,
                exit_reason TEXT, gross_rs REAL, charges_rs REAL, net_rs REAL, dry_run INTEGER NOT NULL
            );
            CREATE TABLE IF NOT EXISTS live_crudem_decisions (
                book TEXT NOT NULL, signal_ts TEXT NOT NULL, cid INTEGER NOT NULL, outcome TEXT NOT NULL,
                detail TEXT, decided_at TEXT NOT NULL, PRIMARY KEY (book, signal_ts, cid)
            );
            """
        )
        conn.commit()
    finally:
        if own:
            conn.close()


# -------------------------------------------------------------------- feed ---
class KiteFeed:
    """Read-only market data from the labs Kite data session (never a broker order session)."""

    def __init__(self):
        self._master: dict = {"date": None, "rows": []}

    def _kite(self):
        from auth.session_manager import get_kite
        return get_kite()

    def contracts(self) -> list[dict]:
        today = now_ist().date()
        if self._master["date"] != today or not self._master["rows"]:
            rows = [{"tradingsymbol": r["tradingsymbol"], "instrument_token": int(r["instrument_token"]),
                     "expiry": str(r["expiry"])[:10]}
                    for r in self._kite().instruments(EXCHANGE)
                    if r.get("name") == UNDERLYING and r.get("instrument_type") == "FUT"]
            self._master.update(date=today, rows=rows)
        return list(self._master["rows"])

    def candles(self, token: int, frm: datetime, to: datetime) -> pd.DataFrame:
        rows = self._kite().historical_data(int(token), frm, to, "minute")
        out = pd.DataFrame([{"ts": _naive(c["date"]), "open": float(c["open"]), "high": float(c["high"]),
                             "low": float(c["low"]), "close": float(c["close"]),
                             "volume": float(c.get("volume") or 0)} for c in rows])
        return out if len(out) else pd.DataFrame(columns=["ts", "open", "high", "low", "close", "volume"])

    def ltp(self, symbol: str) -> float | None:
        key = f"{EXCHANGE}:{symbol}"
        try:
            return float(self._kite().ltp(key)[key]["last_price"])
        except Exception as e:
            log.warning("ltp read failed %s: %s", symbol, type(e).__name__)
            return None


def _naive(value) -> datetime:
    t = pd.Timestamp(value)
    if t.tzinfo is not None:
        t = t.tz_convert("Asia/Kolkata").tz_localize(None)
    return t.floor("min").to_pydatetime()


def cached_history(start: date, end: date) -> tuple[dict[str, pd.DataFrame], dict[str, str]]:
    """Candles and contract expiries the paper loop has already stored in labs.db (read only)."""
    from storage.db import get_conn
    frames: dict[str, pd.DataFrame] = {}
    expiries: dict[str, str] = {c["tradingsymbol"]: c["expiry"] for c in KNOWN_EXPIRED}
    conn = get_conn()
    try:
        try:
            expiries.update(dict(conn.execute("SELECT tradingsymbol, expiry FROM crudem_combo_contracts").fetchall()))
            f = pd.read_sql_query(
                "SELECT tradingsymbol, ts, open, high, low, close, volume FROM crude_minute_bars "
                "WHERE tradingsymbol LIKE ? AND ts >= ? AND ts < ? ORDER BY ts", conn,
                params=(UNDERLYING + "%FUT", start.isoformat(), (end + timedelta(days=1)).isoformat()))
        except Exception as e:                        # tables not created yet
            log.warning("no cached MCX candles in labs.db: %s", type(e).__name__)
            return frames, expiries
    finally:
        conn.close()
    f["ts"] = pd.to_datetime(f["ts"])
    for sym, g in f.groupby("tradingsymbol"):
        frames[sym] = g.drop(columns="tradingsymbol").reset_index(drop=True)
    return frames, expiries


class Series:
    """The rolled 1-minute series, kept current: cached history once a day, Kite every minute."""

    def __init__(self, feed, history=cached_history):
        self.feed, self.history = feed, history
        self.day: date | None = None
        self.frames: dict[str, pd.DataFrame] = {}
        self.contracts: list[dict] = []

    def _load_day(self, now: datetime) -> None:
        start = now.date() - timedelta(days=LOOKBACK_DAYS)
        frames, expiries = self.history(start, now.date())
        by_symbol = {s: {"tradingsymbol": s, "expiry": e, "instrument_token": None} for s, e in expiries.items()}
        for c in self.feed.contracts():
            by_symbol[c["tradingsymbol"]] = c
        self.contracts = sorted((c for c in by_symbol.values() if c["expiry"] >= start.isoformat()),
                                key=lambda c: c["expiry"])
        self.frames = {s: f for s, f in frames.items() if s in by_symbol}
        self.day = now.date()
        for c in self.wanted(now):                    # nothing cached (paper loop not run yet): pull it
            if c["tradingsymbol"] not in self.frames:
                parts = []
                for k in range(0, LOOKBACK_DAYS + 1, 55):
                    frm = datetime.combine(start + timedelta(days=k), dtime(0, 0))
                    to = min(datetime.combine(start + timedelta(days=min(k + 54, LOOKBACK_DAYS)), dtime(23, 59)), now)
                    parts.append(self.feed.candles(c["instrument_token"], frm, to))
                full = pd.concat(parts, ignore_index=True)
                full = full[full["ts"] < now.replace(second=0, microsecond=0)]       # completed minutes only
                self.frames[c["tradingsymbol"]] = full.drop_duplicates("ts", keep="last").sort_values("ts")                     .reset_index(drop=True)

    def wanted(self, now: datetime) -> list[dict]:
        """The front contract, and from a week before its expiry the next one."""
        live = [c for c in self.contracts if c["expiry"] >= now.date().isoformat() and c.get("instrument_token")]
        if len(live) > 1 and (date.fromisoformat(live[0]["expiry"]) - now.date()).days <= NEXT_CONTRACT_DAYS:
            return live[:2]
        return live[:1]

    def front(self, now: datetime) -> dict | None:
        """The contract traded today: the nearest expiry strictly after the session."""
        live = [c for c in self.contracts if c["expiry"] > now.date().isoformat() and c.get("instrument_token")]
        return live[0] if live else None

    def refresh(self, now: datetime) -> None:
        if self.day != now.date():
            self._load_day(now)
        cutoff = now.replace(second=0, microsecond=0)
        for c in self.wanted(now):
            have = self.frames.get(c["tradingsymbol"])
            last = have["ts"].iloc[-1].to_pydatetime() if have is not None and len(have) else None
            if last is not None and last >= cutoff - timedelta(minutes=1):
                continue
            frm = (last - timedelta(minutes=2)) if last is not None else cutoff - timedelta(minutes=30)
            fresh = self.feed.candles(c["instrument_token"], frm, now)
            fresh = fresh[fresh["ts"] < cutoff]                       # completed minutes only
            if len(fresh):
                merged = fresh if have is None else pd.concat([have, fresh], ignore_index=True)
                self.frames[c["tradingsymbol"]] = merged.drop_duplicates("ts", keep="last").sort_values("ts") \
                    .reset_index(drop=True)

    def frame(self, now: datetime) -> pd.DataFrame:
        parts = [(c["tradingsymbol"], date.fromisoformat(c["expiry"]), self.frames[c["tradingsymbol"]])
                 for c in self.contracts if self.frames.get(c["tradingsymbol"]) is not None
                 and len(self.frames[c["tradingsymbol"]])]
        return eng.stitch(parts, now.date())[0] if parts else pd.DataFrame(columns=["ts", "open", "high", "low", "close"])

    def bar_open(self, symbol: str, minute: datetime) -> float | None:
        f = self.frames.get(symbol)
        if f is None or not len(f):
            return None
        hit = f[f["ts"] == pd.Timestamp(minute)]
        return float(hit["open"].iloc[0]) if len(hit) else None


# ------------------------------------------------------------------- state ---
def load_position(conn, book: str = BOOK) -> dict | None:
    row = conn.execute("SELECT state FROM live_crudem_position WHERE book=?", (book,)).fetchone()
    return json.loads(row[0]) if row and row[0] and row[0] != "null" else None


def save_position(conn, pos: dict | None, book: str = BOOK) -> None:
    conn.execute("INSERT INTO live_crudem_position (book, state, updated_at) VALUES (?,?,?) "
                 "ON CONFLICT(book) DO UPDATE SET state=excluded.state, updated_at=excluded.updated_at",
                 (book, json.dumps(pos), now_ist().isoformat(timespec="seconds")))
    conn.commit()


def _record_decision(conn, signal_ts, cid: int, outcome: str, detail: str | None, now: datetime) -> None:
    conn.execute("INSERT OR IGNORE INTO live_crudem_decisions (book, signal_ts, cid, outcome, detail, decided_at) "
                 "VALUES (?,?,?,?,?,?)", (BOOK, str(signal_ts), cid, outcome, detail, now.isoformat(timespec="seconds")))


def _order(conn, pos: dict, kind: str, side: str, reason: str, now: datetime, ref_price, fill: float) -> None:
    conn.execute(
        "INSERT INTO live_crudem_orders (book, trade_ref, kind, side, symbol, qty, reason, decided_at, delay_s, "
        "ref_price, fill_price, status, dry_run) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (BOOK, pos["ref"], kind, side, pos["symbol"], QTY, reason, now.isoformat(timespec="seconds"),
         round(now.second + now.microsecond / 1e6, 2) if kind == "entry" else None, ref_price, fill,
         "DRY_FILLED", int(DRY_RUN)))


# ------------------------------------------------------------------ runner ---
class Runner:
    def __init__(self, feed=None, series: Series | None = None):
        self.feed = feed or KiteFeed()
        self.series = series or Series(self.feed)
        self.decided: datetime | None = None          # the minute boundary already handled
        self.warned: datetime | None = None

    # -- one poll ---------------------------------------------------------
    def step(self, now: datetime, conn) -> dict | None:
        """One poll. Returns the position held after it (None when flat)."""
        pos = load_position(conn)
        in_session = now.weekday() < 5 and SESSION_OPEN <= now.time() < SESSION_END
        if pos is not None and (not in_session or pos["entry_ts"][:10] != now.date().isoformat()):
            # the runner was down at the session close: an intraday position cannot be carried
            return self._manage(pos, now, conn, force="eod_missed")
        if not in_session:
            return pos
        if pos is not None:
            pos = self._manage(pos, now, conn)
        cutoff = now.replace(second=0, microsecond=0)
        if self.decided == cutoff or cutoff.time() <= SESSION_OPEN:
            return pos
        self.series.refresh(now)
        front = self.series.front(now)
        if front is None:
            return pos
        frame = self.series.frame(now)
        if not len(frame) or frame["ts"].iloc[-1] < pd.Timestamp(cutoff) - pd.Timedelta(minutes=1):
            if now.second >= CANDLE_WAIT_S:           # the candle never came: this minute is skipped
                self.decided = cutoff
                if self.warned != cutoff:
                    log.warning("candle for %s not available after %ds - minute skipped",
                                (cutoff - timedelta(minutes=1)).time(), CANDLE_WAIT_S)
                    self.warned = cutoff
            return pos
        self.decided = cutoff
        self._backfill_bar_open(front["tradingsymbol"], conn)
        fires = eng.pending_fires(frame, START, cutoff)
        for k, fire in enumerate(fires):
            if pos is not None:
                _record_decision(conn, fire["signal_ts"], fire["cid"], "position_held", f"held by {pos['cid']}", now)
            elif now.time() >= EOD_EXIT:
                _record_decision(conn, fire["signal_ts"], fire["cid"], "session_closing", None, now)
            else:
                pos = self._enter(fire, front, now, conn)
                _record_decision(conn, fire["signal_ts"], fire["cid"], "taken" if pos else "no_price",
                                 f"delay {now.second}s" if pos else None, now)
        conn.commit()
        return pos

    # -- entry ------------------------------------------------------------
    def _enter(self, fire: dict, front: dict, now: datetime, conn) -> dict | None:
        symbol = front["tradingsymbol"]
        price = self.feed.ltp(symbol)
        if not price:
            log.warning("entry skipped: no price for %s", symbol)
            return None
        member = eng.MEMBER_BY_CID[fire["cid"]]
        dist, stop, target = eng.levels(member, price, fire["atr_stop_dist"])
        stop, target = tick_levels(member.side, stop, target)
        pos = {"ref": uuid.uuid4().hex[:12], "cid": member.cid, "side": member.side, "symbol": symbol,
               "qty": QTY, "signal_ts": str(fire["signal_ts"]), "entry_ts": now.isoformat(timespec="seconds"),
               "entry_minute": now.replace(second=0, microsecond=0).isoformat(), "entry_price": price,
               "stop_dist": dist, "stop": stop, "target": target}
        _order(conn, pos, "entry", "BUY" if member.side > 0 else "SELL", f"rule {member.cid}", now, None, price)
        conn.execute(
            "INSERT INTO live_crudem_trades (book, trade_ref, trade_date, cid, direction, symbol, qty, signal_ts, "
            "entry_ts, entry_price, stop_price, target_price, stop_dist, dry_run) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (BOOK, pos["ref"], now.date().isoformat(), member.cid, "long" if member.side > 0 else "short", symbol,
             QTY, pos["signal_ts"], pos["entry_ts"], price, pos["stop"], pos["target"], dist, int(DRY_RUN)))
        save_position(conn, pos)
        log.info("DRY ENTER %s rule=%s %s @ %.1f stop %.1f target %.1f (decided %ds into the minute)",
                 "LONG" if member.side > 0 else "SHORT", member.cid, symbol, price, pos["stop"], pos["target"], now.second)
        notify_telegram(f"[DRY-RUN] CRUDEOILM combo {'LONG' if member.side > 0 else 'SHORT'} {symbol} @ {price:.0f} "
                        f"| rule {member.cid} | stop {pos['stop']:.0f} target {pos['target']:.0f}")
        return pos

    # -- an open position ---------------------------------------------------
    def _manage(self, pos: dict, now: datetime, conn, force: str | None = None) -> dict | None:
        price = self.feed.ltp(pos["symbol"])
        if not price:
            if not force:
                return pos
            price = pos["entry_price"]                 # no price to mark it at: closed flat, and flagged
        side = pos["side"]
        reason = force
        if reason is not None:
            pass
        elif side * (price - pos["stop"]) <= 0:
            reason = "stop"
        elif side * (price - pos["target"]) >= 0:
            reason = "target"
        elif now.time() >= EOD_EXIT:
            reason = "eod"
        if reason is None:
            return pos
        level = pos["stop"] if reason == "stop" else (pos["target"] if reason == "target" else None)
        _order(conn, pos, "exit", "SELL" if side > 0 else "BUY", reason, now, level, price)
        gross = side * (price - pos["entry_price"]) * pos["qty"]
        buy, sell = (pos["entry_price"], price) if side > 0 else (price, pos["entry_price"])
        cost = charges(buy, sell, pos["qty"])
        conn.execute("UPDATE live_crudem_trades SET exit_ts=?, exit_price=?, exit_reason=?, gross_rs=?, charges_rs=?, "
                     "net_rs=? WHERE trade_ref=?",
                     (now.isoformat(timespec="seconds"), price, reason, round(gross, 2), round(cost, 2),
                      round(gross - cost, 2), pos["ref"]))
        save_position(conn, None)
        log.info("DRY EXIT %s rule=%s @ %.1f gross %.0f net %.0f", reason, pos["cid"], price, gross, gross - cost)
        notify_telegram(f"[DRY-RUN] CRUDEOILM combo exit ({reason}) @ {price:.0f} | rule {pos['cid']} | "
                        f"net Rs {gross - cost:,.0f}")
        return None

    def _backfill_bar_open(self, symbol: str, conn) -> None:
        """Once an entry's minute has closed, store that bar's open: the price the back test enters on."""
        rows = conn.execute(
            "SELECT o.id, t.entry_ts FROM live_crudem_orders o JOIN live_crudem_trades t ON t.trade_ref = o.trade_ref "
            "WHERE o.book=? AND o.kind='entry' AND o.bar_open IS NULL AND o.symbol=?", (BOOK, symbol)).fetchall()
        for oid, entry_ts in rows:
            opened = self.series.bar_open(symbol, datetime.fromisoformat(entry_ts).replace(second=0, microsecond=0))
            if opened is not None:
                conn.execute("UPDATE live_crudem_orders SET bar_open=? WHERE id=?", (opened, oid))


def run(max_cycles: int | None = None, clock=now_ist, runner: Runner | None = None) -> None:
    init_live_db()
    ensure_schema()
    runner = runner or Runner()
    log.info("crudem_runner boot | phase 0 dry run | book=%s lots=%d start=%s", BOOK, LOTS, START)
    cycles = 0
    while True:
        now = clock()
        try:
            conn = get_live_conn()
            try:
                runner.step(now, conn)
            finally:
                conn.close()
        except Exception as e:
            log.error("crudem cycle error: %s: %s", type(e).__name__, str(e)[:200])
        cycles += 1
        if max_cycles is not None and cycles >= max_cycles:
            return
        in_session = now.weekday() < 5 and SESSION_OPEN <= now.time() < SESSION_END
        time.sleep(POLL_S if in_session else 30.0)


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    run()
