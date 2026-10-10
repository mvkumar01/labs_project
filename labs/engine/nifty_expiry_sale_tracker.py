"""Paper-only NIFTY expiry-day straddle sale.

Paper only. This module never calls a broker order API.

On every NIFTY expiry day (the day the nearest contract in the chain expires) the at-the-money
straddle of that contract is sold in the morning and both legs are bought back at the first
snapshot at or after 15:15. One lot. Three books, as asked by the Strategy Tester session that
found the lead (2026-10-10):

  A    sold at the first snapshot at or after 09:45 that prices both legs, at the strike nearest
       the NIFTY index value at 09:45 (Strategy Tester research/scripts/v2_size_vs_implied.py,
       part E). No stop.
  B    sold at the first snapshot at or after 09:21, at the strike nearest the forward the chain
       implies (strike + call - put, the median over the three strikes where call and put are
       closest) - the registered forward design
       configs/v3/registered/nifty_expiry_sale_forward_20261002.yaml. No stop.
  B50  B's entry with a stop on the pair at 50%: both legs are bought back at the first snapshot
       that shows the two together half as dear again as they were sold for. A shadow book.

Prices. The research priced a contract at a snapshot as the middle of bid and ask when both are
quoted and not wide (ask - bid <= max(2, a quarter of the middle)), otherwise the last traded
price. That "research price" finds the strike, the forward and the stop, and gives
`research_gross_bps` - the number to hold against the research figures. The paper P&L is stricter:
a leg is SOLD at its bid and BOUGHT BACK at its ask, then the short-option charge model; a side
with no quote takes the research price worsened by 0.2 points, the slippage of the registration.

Evidence status: a lead, not a tested edge. It was found on 46 expiry days (2025-07-03 to
2026-08-18), eight of them on or after 1 June 2026 - those rows are tagged "in the research
sample". Days the research never read, and every day from LIVE_FROM on, are the new evidence.
Nothing here is tuned from what the book shows. A short straddle needs margin and its bad days are
large (research: worst day -77 bps on quotes, -321 bps on daily files).

A day without usable quotes at the entry or the exit is recorded as skipped with the reason;
no other time is substituted.
"""
from __future__ import annotations

import calendar
import math
import sqlite3
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta, timezone

import numpy as np
import pandas as pd

from config.labs_config import SHARED_ARCHIVE_DIR, SHARED_LIVE_DIR, UNDERLYINGS
from labs.engine.charges import short_option_round_trip_charges
from market_data.expiry import expiry_sort_date, is_monthly_expiry, select_expiry_code
from market_data.shared_store import load_options_frame
from storage.db import get_conn

IST = timezone(timedelta(hours=5, minutes=30))
SYMBOL = "NIFTY"
LOT_SIZE = int(UNDERLYINGS[SYMBOL]["lot_size"])
LOTS = 1
QTY = LOT_SIZE * LOTS
EXIT_TIME, EXIT_WAIT = time(15, 15), 6
SLIPPAGE_POINTS = 0.2
ESTIMATED_MARGIN_RATE = 0.10            # as the 09:20 straddle book: 10% of index notional
EXPIRY_WEEKDAY = 1                      # Tuesday (NSE moved NIFTY's expiry there in September 2025)
# A monthly code carries no day: it expires on the month's last EXPIRY_WEEKDAY. When that is a
# holiday the exchange moves it; name such a month here (code -> ISO date). Weekly codes carry
# their own date, holiday moves included.
EXPIRY_OVERRIDES: dict[str, str] = {}
DEFAULT_START = "2026-06-01"
LIVE_FROM = "2026-10-10"                # expiry days from here on were traded as they happened
RESEARCH_SAMPLE = frozenset({"2026-06-02", "2026-06-09", "2026-06-23", "2026-07-07", "2026-07-14",
                             "2026-07-21", "2026-08-11", "2026-08-18"})
STRATEGY_VERSION = "nifty_expiry_straddle_sale_v1"


@dataclass(frozen=True)
class Book:
    key: str
    label: str
    entry: time
    wait: int                # minutes after `entry` within which the sale may be made
    strike_from: str         # "index" | "forward"
    stop: float | None       # stop on the pair, as a share of what it was sold for


BOOKS = {
    "A": Book("A", "A: sold 09:45 at the index strike", time(9, 45), 10, "index", None),
    "B": Book("B", "B: sold 09:21 at the forward strike", time(9, 21), 6, "forward", None),
    "B50": Book("B50", "B50: as B, pair stop at 50%", time(9, 21), 6, "forward", 0.5),
}


class ExpirySaleSkip(RuntimeError):
    """The day cannot be traded by the rules; the reason is recorded, nothing is substituted."""


class ExpirySaleInputError(RuntimeError):
    """The session's quotes cannot be loaded at all."""


# ------------------------------------------------------------------ schema ---
def _ensure_tables(conn: sqlite3.Connection) -> None:
    conn.executescript(
        """
        CREATE TABLE IF NOT EXISTS nifty_expiry_sale_sessions (
            trade_date  TEXT PRIMARY KEY,
            is_expiry   INTEGER NOT NULL,
            expiry_code TEXT,
            final       INTEGER NOT NULL DEFAULT 0,
            updated_at  TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS nifty_expiry_sale_trades (
            trade_date          TEXT NOT NULL,
            book                TEXT NOT NULL,
            status              TEXT NOT NULL,          -- closed | open | skipped
            source              TEXT NOT NULL,          -- backfill | live
            sample              TEXT NOT NULL,
            expiry_code         TEXT,
            strike              INTEGER,
            entry_ts            TEXT,
            exit_ts             TEXT,
            index_at_entry      REAL,
            forward_at_entry    REAL,
            ref_level           REAL,                   -- what basis points are measured on
            call_sold           REAL, call_bought REAL,
            put_sold            REAL, put_bought REAL,
            call_entry_bid      REAL, call_entry_ask REAL, call_exit_bid REAL, call_exit_ask REAL,
            put_entry_bid       REAL, put_entry_ask REAL, put_exit_bid REAL, put_exit_ask REAL,
            straddle_sold_research   REAL,
            straddle_bought_research REAL,
            research_gross_bps  REAL,
            stop_triggered      INTEGER NOT NULL DEFAULT 0,
            stop_ts             TEXT,
            stop_level          REAL,
            exit_reason         TEXT,
            qty                 INTEGER NOT NULL,
            capital_required_rs REAL,
            gross_rs            REAL,
            charges_rs          REAL,
            net_rs              REAL,
            net_bps             REAL,
            strategy_version    TEXT NOT NULL,
            error               TEXT,
            updated_at          TEXT NOT NULL,
            PRIMARY KEY (trade_date, book)
        );
        """
    )
    conn.commit()


# ------------------------------------------------------------------ expiry ---
def expiry_date_of(code: str) -> date | None:
    """The day a NIFTY contract code expires. Weekly codes carry it; a monthly code is the month's
    last EXPIRY_WEEKDAY unless EXPIRY_OVERRIDES names another day."""
    code = str(code).strip().upper()
    if code in EXPIRY_OVERRIDES:
        return date.fromisoformat(EXPIRY_OVERRIDES[code])
    key = expiry_sort_date(code)
    if key is None:
        return None
    if not is_monthly_expiry(code):
        return key
    last = date(key.year, key.month, calendar.monthrange(key.year, key.month)[1])
    return last - timedelta(days=(last.weekday() - EXPIRY_WEEKDAY) % 7)


def sample_tag(trade_date: str) -> str:
    if trade_date in RESEARCH_SAMPLE:
        return "in the research sample"
    return "live" if trade_date >= LIVE_FROM else "not seen in research"


# ------------------------------------------------------------------- data ---
def _session_frame(trade_date: str) -> pd.DataFrame:
    try:
        frame = load_options_frame(SYMBOL, trade_date, live_root=SHARED_LIVE_DIR, archive_root=SHARED_ARCHIVE_DIR)
    except Exception as exc:
        raise ExpirySaleInputError(f"Unable to load NIFTY quotes for {trade_date}: {type(exc).__name__}: {exc}") from exc
    need = {"timestamp", "spot", "strike", "option_type", "expiry", "ltp", "bid", "ask"}
    missing = need.difference(frame.columns)
    if missing:
        raise ExpirySaleInputError(f"NIFTY quote data missing columns: {sorted(missing)}")
    f = frame.copy()
    ts = pd.to_datetime(f["timestamp"], errors="coerce")
    if ts.dt.tz is not None:
        ts = ts.dt.tz_convert("Asia/Kolkata").dt.tz_localize(None)
    f["minute"] = ts.dt.floor("min")
    f["expiry"] = f["expiry"].astype(str).str.upper()
    f["cp"] = f["option_type"].astype(str).str.upper().str[0]
    for c in ("spot", "strike", "ltp", "bid", "ask"):
        f[c] = pd.to_numeric(f[c], errors="coerce")
    return f.dropna(subset=["minute", "strike"])


@dataclass
class Panel:
    """The expiring contract's quotes on (minute x strike) grids."""
    minutes: pd.DatetimeIndex
    strikes: np.ndarray
    price: dict            # "C" / "P" -> research price [T, K]
    bid: dict
    ask: dict
    spot: np.ndarray       # the chain's index value at each snapshot

    def at(self, minute) -> int:
        i = self.minutes.searchsorted(pd.Timestamp(minute))
        return int(i) if i < len(self.minutes) and self.minutes[i] == pd.Timestamp(minute) else -1

    def straddle(self, k: int) -> np.ndarray:
        return self.price["C"][:, k] + self.price["P"][:, k]

    def forward(self, t: int, nearest: int = 3) -> float:
        c, p = self.price["C"][t], self.price["P"][t]
        ok = np.flatnonzero(np.isfinite(c) & np.isfinite(p))
        if ok.size == 0:
            return float("nan")
        best = ok[np.argsort(np.abs(c[ok] - p[ok]), kind="stable")[:nearest]]
        return float(np.median(self.strikes[best] + c[best] - p[best]))


def build_panel(near: pd.DataFrame) -> Panel:
    last = near["ltp"].where(near["ltp"] > 0)
    mid = (near["bid"] + near["ask"]) / 2.0
    quoted = (near["bid"] > 0) & (near["ask"] >= near["bid"]) & ((near["ask"] - near["bid"]) <= np.maximum(2.0, 0.25 * mid))
    near = near.assign(price=mid.where(quoted, last),
                       bid=near["bid"].where(near["bid"] > 0), ask=near["ask"].where(near["ask"] > 0))
    minutes = pd.DatetimeIndex(sorted(near["minute"].unique()))
    seen = np.unique(near["strike"].to_numpy(dtype=np.int64))
    step = int(np.gcd.reduce(np.diff(seen))) if seen.size > 1 else 0
    if step <= 0:
        raise ExpirySaleSkip("the expiring contract has fewer than two strikes quoted")
    strikes = np.arange(seen[0], seen[-1] + step, step, dtype=np.int64)
    out = {name: {} for name in ("price", "bid", "ask")}
    for cp in ("C", "P"):
        g = near[near["cp"] == cp].drop_duplicates(["minute", "strike"], keep="last")
        for name in out:
            out[name][cp] = (g.pivot(index="minute", columns="strike", values=name)
                             .reindex(index=minutes, columns=strikes).to_numpy(dtype=np.float64))
    spot = near.groupby("minute")["spot"].median().reindex(minutes).to_numpy(dtype=np.float64)
    return Panel(minutes, strikes.astype(np.float64), out["price"], out["bid"], out["ask"], spot)


# ------------------------------------------------------------------ replay ---
def _first(mask: np.ndarray, lo: int, hi: int) -> int:
    lo, hi = max(lo, 0), min(hi, mask.size - 1)
    if lo > hi:
        return -1
    hit = np.flatnonzero(mask[lo:hi + 1])
    return lo + int(hit[0]) if hit.size else -1


def _window(panel: Panel, day: str, start: time, wait: int) -> tuple[int, int]:
    """Indices of the first snapshot at or after `start` and of the last one within `wait` minutes."""
    t0 = pd.Timestamp(datetime.combine(date.fromisoformat(day), start))
    lo = int(panel.minutes.searchsorted(t0))
    hi = int(panel.minutes.searchsorted(t0 + pd.Timedelta(minutes=wait), side="right")) - 1
    return lo, hi


def _sell(bid: float, research: float) -> float:
    return float(bid) if np.isfinite(bid) and bid > 0 else float(research) - SLIPPAGE_POINTS


def _buy(ask: float, research: float) -> float:
    return float(ask) if np.isfinite(ask) and ask > 0 else float(research) + SLIPPAGE_POINTS


def simulate(panel: Panel, day: str, book: Book, *, final: bool) -> dict:
    """One book for one expiry day. `final` False = the session is still running: a sale without
    its 15:15 snapshot is reported open, marked at the latest snapshot."""
    hhmm = book.entry.strftime("%H:%M")
    lo, hi = _window(panel, day, book.entry, book.wait)
    if lo >= len(panel.minutes) or lo > hi:
        if not final:
            return {"status": "waiting"}
        raise ExpirySaleSkip(f"no snapshot from {hhmm} to {book.wait} minutes later")
    forward = float("nan")
    if book.strike_from == "index":
        index = panel.spot[lo]
        if not (np.isfinite(index) and index > 0):
            raise ExpirySaleSkip(f"no index value at the {panel.minutes[lo].strftime('%H:%M')} snapshot")
        k = int(np.argmin(np.abs(panel.strikes - index)))
        e = _first(np.isfinite(panel.straddle(k)), lo, hi)
        if e < 0:
            raise ExpirySaleSkip(f"no quote for both legs of the {int(panel.strikes[k])} straddle from {hhmm} to {book.wait} minutes later")
        ref = float(index)
    else:
        e = lo
        forward = panel.forward(e)
        if not np.isfinite(forward):
            raise ExpirySaleSkip(f"the chain implies no forward at the {panel.minutes[e].strftime('%H:%M')} snapshot")
        k = int(np.rint((forward - panel.strikes[0]) / (panel.strikes[1] - panel.strikes[0])))
        if not 0 <= k < panel.strikes.size:
            raise ExpirySaleSkip("the forward strike is outside the quoted strikes")
        if not np.isfinite(panel.straddle(k)[e]):
            raise ExpirySaleSkip(f"no quote for both legs of the {int(panel.strikes[k])} straddle at the {panel.minutes[e].strftime('%H:%M')} snapshot")
        ref = float(forward)
    V = panel.straddle(k)
    xlo, xhi = _window(panel, day, EXIT_TIME, EXIT_WAIT)
    x = _first(np.isfinite(V), xlo, xhi)
    if x < 0 and final:
        raise ExpirySaleSkip("no quote for both legs from 15:15 to 15:21")
    closed, reason, out = x > e, "15:15", x
    stop_hit, stop_level = False, None
    last = x if x > e else len(panel.minutes)
    if book.stop is not None:
        stop_level = float(V[e] * (1.0 + book.stop))
        seg = V[e + 1:last]
        hit = np.flatnonzero(np.isfinite(seg) & (seg >= stop_level))
        if hit.size:
            out, stop_hit, closed, reason = e + 1 + int(hit[0]), True, True, "pair stop"
    if not closed:                                   # still running: mark at the latest priced snapshot
        priced = np.flatnonzero(np.isfinite(V[e:]))
        out = e + int(priced[-1])
    legs, gross, charges = {}, 0.0, 0.0
    for cp in ("C", "P"):
        sold = _sell(panel.bid[cp][e, k], panel.price[cp][e, k])
        bought = _buy(panel.ask[cp][out, k], panel.price[cp][out, k])
        legs[cp] = {"sold": sold, "bought": bought,
                    "entry_bid": panel.bid[cp][e, k], "entry_ask": panel.ask[cp][e, k],
                    "exit_bid": panel.bid[cp][out, k], "exit_ask": panel.ask[cp][out, k]}
        gross += (sold - bought) * QTY
        charges += float(short_option_round_trip_charges(sold, bought, QTY)["raw_total"])
    net = gross - charges
    return {
        "status": "closed" if closed else "open", "strike": int(panel.strikes[k]),
        "entry_ts": panel.minutes[e].isoformat(timespec="minutes"),
        "exit_ts": panel.minutes[out].isoformat(timespec="minutes") if closed else None,
        "mark_ts": panel.minutes[out].isoformat(timespec="minutes"),
        "index_at_entry": float(panel.spot[e]) if np.isfinite(panel.spot[e]) else None,
        "forward_at_entry": forward if np.isfinite(forward) else None, "ref_level": ref, "legs": legs,
        "straddle_sold_research": float(V[e]), "straddle_bought_research": float(V[out]),
        "research_gross_bps": float((V[e] - V[out]) / ref * 1e4),
        "stop_triggered": bool(stop_hit), "stop_ts": panel.minutes[out].isoformat(timespec="minutes") if stop_hit else None,
        "stop_level": stop_level, "exit_reason": reason if closed else None,
        "capital_required_rs": round(ref * QTY * ESTIMATED_MARGIN_RATE, 2),
        "gross_rs": round(gross, 2), "charges_rs": round(charges, 2), "net_rs": round(net, 2),
        "net_bps": round(net / (QTY * ref) * 1e4, 3),
    }


def simulate_day(trade_date: str, *, now: datetime | None = None) -> dict:
    """{'is_expiry', 'expiry_code', 'final', 'books': {key: result | {'status': 'skipped', 'error'}}}.
    Pure: reads market data, writes nothing."""
    if date.fromisoformat(trade_date).weekday() >= 5:
        raise ExpirySaleInputError(f"{trade_date} is not a trading weekday")
    frame = _session_frame(trade_date)
    code = select_expiry_code(frame["expiry"].unique(), trade_date, "nearest_weekly")
    expires = expiry_date_of(code) if code else None
    today = now is not None and now.date().isoformat() == trade_date
    final = not today or now.replace(tzinfo=None).time() >= time(15, 30)
    if code is None or expires is None or expires.isoformat() != trade_date:
        return {"trade_date": trade_date, "is_expiry": False, "expiry_code": code, "final": True, "books": {}}
    out = {"trade_date": trade_date, "is_expiry": True, "expiry_code": str(code), "final": final, "books": {}}
    try:
        panel = build_panel(frame[frame["expiry"] == str(code)])
    except ExpirySaleSkip as exc:
        out["books"] = {key: {"status": "skipped", "error": str(exc)} for key in BOOKS}
        return out
    for key, book in BOOKS.items():
        try:
            out["books"][key] = simulate(panel, trade_date, book, final=final)
        except ExpirySaleSkip as exc:
            out["books"][key] = {"status": "skipped", "error": str(exc)}
    return out


# ----------------------------------------------------------------- persist ---
def _persist(conn: sqlite3.Connection, r: dict) -> None:
    stamp = datetime.now(IST).isoformat(timespec="seconds")
    day = r["trade_date"]
    conn.execute(
        "INSERT INTO nifty_expiry_sale_sessions (trade_date,is_expiry,expiry_code,final,updated_at) VALUES (?,?,?,?,?) "
        "ON CONFLICT(trade_date) DO UPDATE SET is_expiry=excluded.is_expiry,expiry_code=excluded.expiry_code,"
        "final=excluded.final,updated_at=excluded.updated_at",
        (day, int(r["is_expiry"]), r.get("expiry_code"), int(r["final"]), stamp))
    for key, b in r["books"].items():
        if b["status"] == "waiting":
            continue
        legs = b.get("legs") or {"C": {}, "P": {}}

        def v(cp, name):
            x = legs[cp].get(name)
            return None if x is None or not math.isfinite(float(x)) else round(float(x), 2)

        conn.execute("DELETE FROM nifty_expiry_sale_trades WHERE trade_date=? AND book=?", (day, key))
        conn.execute(
            "INSERT INTO nifty_expiry_sale_trades (trade_date,book,status,source,sample,expiry_code,strike,entry_ts,"
            "exit_ts,index_at_entry,forward_at_entry,ref_level,call_sold,call_bought,put_sold,put_bought,"
            "call_entry_bid,call_entry_ask,call_exit_bid,call_exit_ask,put_entry_bid,put_entry_ask,put_exit_bid,"
            "put_exit_ask,straddle_sold_research,straddle_bought_research,research_gross_bps,stop_triggered,stop_ts,"
            "stop_level,exit_reason,qty,capital_required_rs,gross_rs,charges_rs,net_rs,net_bps,strategy_version,"
            "error,updated_at) VALUES (" + ",".join("?" * 40) + ")",
            (day, key, b["status"], "live" if day >= LIVE_FROM else "backfill", sample_tag(day), r["expiry_code"],
             b.get("strike"), b.get("entry_ts"), b.get("exit_ts"), b.get("index_at_entry"), b.get("forward_at_entry"),
             b.get("ref_level"), v("C", "sold"), v("C", "bought"), v("P", "sold"), v("P", "bought"),
             v("C", "entry_bid"), v("C", "entry_ask"), v("C", "exit_bid"), v("C", "exit_ask"),
             v("P", "entry_bid"), v("P", "entry_ask"), v("P", "exit_bid"), v("P", "exit_ask"),
             b.get("straddle_sold_research"), b.get("straddle_bought_research"), b.get("research_gross_bps"),
             int(b.get("stop_triggered") or 0), b.get("stop_ts"), b.get("stop_level"), b.get("exit_reason"), QTY,
             b.get("capital_required_rs"), b.get("gross_rs"), b.get("charges_rs"), b.get("net_rs"), b.get("net_bps"),
             STRATEGY_VERSION, b.get("error"), stamp))
    conn.commit()


def run_day(trade_date: str | None = None, *, persist: bool = True,
            connection: sqlite3.Connection | None = None, now: datetime | None = None) -> dict:
    """Replay a session (to now, for today) and store it. Idempotent. A day already known not to
    be an expiry day is answered from the sessions table without reading its quotes again."""
    now = now or datetime.now(IST)
    trade_date = trade_date or now.date().isoformat()
    own = connection is None
    conn = connection or get_conn()
    _ensure_tables(conn)
    try:
        known = conn.execute("SELECT is_expiry FROM nifty_expiry_sale_sessions WHERE trade_date=?", (trade_date,)).fetchone()
        if known is not None and not known[0]:
            return {"trade_date": trade_date, "status": "not_expiry"}
        r = simulate_day(trade_date, now=now)
        if persist:
            _persist(conn, r)
        if not r["is_expiry"]:
            return {"trade_date": trade_date, "status": "not_expiry"}
        return {"trade_date": trade_date, "status": "final" if r["final"] else "live", "expiry_code": r["expiry_code"],
                **{key: (b.get("net_rs") if b["status"] in ("closed", "open") else b["status"]) for key, b in r["books"].items()}}
    finally:
        if own:
            conn.close()


# ---------------------------------------------------------------- backfill ---
def _default_end_date() -> str:
    now = datetime.now(IST)
    session = now.date()
    if session.weekday() < 5 and now.time() < time(15, 30):
        session -= timedelta(days=1)
    return session.isoformat()


def sessions_with_quotes(start_date: str, end_date: str) -> list[str]:
    from market_data.shared_store import resolve_options_source
    found = set()
    for root in (SHARED_ARCHIVE_DIR, SHARED_LIVE_DIR):
        if not root.exists():
            continue
        for path in root.iterdir():
            if path.is_dir() and len(path.name) == 10 and start_date <= path.name <= end_date:
                try:
                    if date.fromisoformat(path.name).weekday() < 5:
                        found.add(path.name)
                except ValueError:
                    pass
    out = []
    for session in sorted(found):
        try:
            resolve_options_source(SYMBOL, session, live_root=SHARED_LIVE_DIR, archive_root=SHARED_ARCHIVE_DIR)
            out.append(session)
        except FileNotFoundError:
            pass
    return out


def run_backfill(*, start_date: str = DEFAULT_START, end_date: str | None = None, limit: int = 10,
                 rebuild: bool = False) -> dict:
    """Examine the next `limit` sessions not yet examined, oldest first; expiry days are traded,
    the others only noted. `rebuild` first clears the book from `start_date` on."""
    end_date = end_date or _default_end_date()
    conn = get_conn()
    _ensure_tables(conn)
    try:
        if rebuild:
            conn.execute("DELETE FROM nifty_expiry_sale_trades WHERE trade_date>=?", (start_date,))
            conn.execute("DELETE FROM nifty_expiry_sale_sessions WHERE trade_date>=?", (start_date,))
            conn.commit()
        seen = {row[0] for row in conn.execute(
            "SELECT trade_date FROM nifty_expiry_sale_sessions WHERE trade_date>=? AND trade_date<=? AND final=1",
            (start_date, end_date))}
    finally:
        conn.close()
    pending = [s for s in sessions_with_quotes(start_date, end_date) if s not in seen]
    done, errors = [], {}
    for session in pending[:max(1, min(int(limit), 40))]:
        try:
            done.append(run_day(session))
        except Exception as exc:                                   # noqa: BLE001
            errors[session] = f"{type(exc).__name__}: {exc}"
            break
    remaining = 0 if errors else max(0, len(pending) - len(done))
    missing = _record_missing_expiries(start_date, end_date) if not errors and not remaining else []
    return {"done": done, "expiry_days": [d["trade_date"] for d in done if d["status"] != "not_expiry"],
            "missing_expiry_days": missing, "errors": errors, "remaining": remaining}


def _record_missing_expiries(start_date: str, end_date: str) -> list[str]:
    """An expiry day with no stored session at all: the day before it names its contract as the
    nearest one. Record it as skipped, so it does not silently drop out of the book."""
    conn = get_conn()
    try:
        rows = conn.execute("SELECT trade_date, expiry_code FROM nifty_expiry_sale_sessions").fetchall()
        have = {r[0] for r in rows}
        out = []
        for code in sorted({r[1] for r in rows if r[1]}):
            day = expiry_date_of(code)
            if day is None or not (start_date <= day.isoformat() <= end_date) or day.isoformat() in have:
                continue
            _persist(conn, {"trade_date": day.isoformat(), "is_expiry": True, "expiry_code": code, "final": True,
                            "books": {key: {"status": "skipped", "error": "no NIFTY quotes stored for this session"}
                                      for key in BOOKS}})
            out.append(day.isoformat())
        return out
    finally:
        conn.close()


# -------------------------------------------------------------------- view ---
def tab_data(conn: sqlite3.Connection, date_clause: str = "", date_params=()) -> tuple:
    """(trade rows newest first, per-book stats, page stats) for the /labs/live tab."""
    cur = conn.execute(f"SELECT * FROM nifty_expiry_sale_trades WHERE 1=1 {date_clause} ORDER BY trade_date DESC, book",
                       tuple(date_params))
    cols = [c[0] for c in cur.description]
    rows = [dict(zip(cols, r)) for r in cur.fetchall()]
    if not rows:
        return rows, [], {}

    def summary(items):
        closed = [r for r in items if r["status"] == "closed"]
        nets = [float(r["net_rs"] or 0) for r in closed]
        return {"days": len(closed), "wins": sum(1 for n in nets if n > 0),
                "win_pct": round(100 * sum(1 for n in nets if n > 0) / len(nets)) if nets else None,
                "net_rs": round(sum(nets), 2),
                "avg_net_bps": round(sum(float(r["net_bps"] or 0) for r in closed) / len(closed), 2) if closed else None,
                "avg_research_gross_bps": round(sum(float(r["research_gross_bps"] or 0) for r in closed) / len(closed), 2) if closed else None,
                "worst_rs": round(min(nets), 2) if nets else None, "best_rs": round(max(nets), 2) if nets else None,
                "stops": sum(1 for r in closed if r["stop_triggered"])}

    books = []
    for key, book in BOOKS.items():
        mine = [r for r in rows if r["book"] == key]
        books.append({"key": key, "label": book.label, **summary(mine),
                      "skipped": sum(1 for r in mine if r["status"] == "skipped"),
                      "open": next((r for r in mine if r["status"] == "open"), None),
                      "by_sample": [{"sample": tag, **summary([r for r in mine if r["sample"] == tag])}
                                    for tag in ("in the research sample", "not seen in research", "live")]})
    days = sorted({r["trade_date"] for r in rows})
    stats = {"expiry_days": len(days), "first_date": days[0], "last_date": days[-1], "lots": LOTS, "qty": QTY,
             "live_from": LIVE_FROM, "skipped": [r for r in rows if r["status"] == "skipped"]}
    return rows, books, stats
