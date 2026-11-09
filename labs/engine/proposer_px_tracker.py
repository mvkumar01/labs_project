"""SENSEX Proposer paper books: the price-action exit variant (PX) and v3 variants.

Paper only. This module never calls a broker order API. One replay, several ledgers: a `Book` names
the table prefix, the strategy version it replays and its print clock (PX = `proposer_dt25_px`:
Renko-50 exit and one loss a day; V3 = `proposer_dt25_v3`: the base exits, entries only with the
day). V3_DT35 changes only v3's day target from 2.5% to 3.5%. Three shadows that no live preset
runs sit beside v3 for the comparison with Pramanaa:
DT25 = Pramanaa's own rules (the base `proposer_dt25`) on our clock, DT25_CLOCK = the same on
Pramanaa's print clock, V3_CLOCK = v3 on Pramanaa's print clock.

It replays a session with the SAME code the live bot runs: the engine
(live/engine/proposer_engine.py, variant `proposer_dt25_px`), the ported
Predictor rules (live/engine/proposer_predictor.py: gap-rule regime, 5-class
print every five minutes) and the bar exit (live/engine/proposer_bar_exit.py).

Rules in one place (see the engine docstring for the full set):
  entry     regime-driven first trade on a gap day, afterwards the 5-class
            above 0.45 confidence; nearest weekly, ATM -/+200 ITM, 100 lots
  exits     -30% premium floor, +100 / +40 spot points, opposite gated print,
            2.5% day target, the bar exit of the variant, 15:25
  day stop  no new entry once a trade has closed at a loss that day
  recovery  the 2.5% day halt only arms while the book's lifetime gross is >= 0

Simulation model. The session is walked minute by minute; inside a minute the
SENSEX path visits open -> extreme -> extreme -> close of the collector's 1-min
bar (up bar: low first; down bar: high first) in 1-point steps, because the live
bot polls every second and banks its day target within seconds. The option is
priced along that path as its recorded snapshot at the minute plus a fitted delta
times the spot move, and re-anchored to the next recorded snapshot. Buys fill at
that estimate plus half the recorded bid/ask spread, sells at minus half. So fills
INSIDE a minute are estimates, not recorded quotes; on Pramanaa's own 15-29 Sep
ledger the same model was 20% conservative (alphaIMB research, REBUILD.md sec. 9).
Read the totals as a like-for-like comparison of rule sets, not as a forecast: they
move by lakhs with the assumed entry second (see PRINT_LAG_S).

Each session depends on the sessions before it (the recovery book), so the book is
always built in date order.
"""
from __future__ import annotations

import dataclasses
import io
import math
import sqlite3
import sys
import tarfile
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta, timezone

import pandas as pd

from config.labs_config import ARCHIVE_DIR, DATA_DIR, SHARED_ARCHIVE_DIR, SHARED_LIVE_DIR, UNDERLYINGS
from labs.engine.charges import sensex_round_trip_charges
from live.engine import entry_blocks
from live.engine import proposer_engine as pe
from live.engine import proposer_predictor as pp
from live.engine import proposer_strike as ps
from market_data.expiry import select_expiry_code
from market_data.shared_store import load_options_frame
from storage.db import get_conn

IST = timezone(timedelta(hours=5, minutes=30))
SYMBOL = "SENSEX"
LOT_SIZE = int(UNDERLYINGS[SYMBOL]["lot_size"])
LOTS = 100
QTY = LOT_SIZE * LOTS
STRATEGY_VERSION = pe.STRATEGY_VERSION_PX
PRINT_EVERY_MIN = 5
# Second of the minute at which a print is acted on. The live bot's print second drifts through
# the day; its first real entries (5 Oct 2026) landed 25 s and 28 s into their minutes. The book
# is very sensitive to this: 1 Jun - 5 Oct at 100 lots it nets about -9L at 10-20 s, -0.9L at
# 25 s and -0.5L..+0.2L at 30-45 s, because one-minute bars cannot say where inside the minute
# the extremes fell.
PRINT_LAG_S = 25
PRINT_UNTIL = time(15, 21)            # the live feed stops printing here
ENTRY_CUTOFF, EOD = time(15, 20), time(15, 25)
SESSION_OPEN = time(9, 15)
DEFAULT_START = "2026-06-01"

# Pramanaa's own print clock (services/market-predictor/scheduler.py at 62ef7d4): one loop that does
# its work, sleeps 60 s, and prints the 5-class once 300 s have passed since the last print - so a
# print comes on every fifth pass, 5 x (60 s + the pass's work), and its minute slides through the
# day. The work is how long Pramanaa's server took, so it is measured: 310.5 s between prints on the
# median day of its captured prints, 22 Jun - 29 Sep 2026 (309 - 315 s). Used only by the shadow
# book below; the live bot and the other books print every PRINT_EVERY_MIN minutes.
PRAMANAA_LOOP_SLEEP_S, PRAMANAA_MIN_GAP_S, PRAMANAA_PASS_WORK_S = 60.0, 300.0, 2.1
_PASS_S = PRAMANAA_LOOP_SLEEP_S + PRAMANAA_PASS_WORK_S
PRAMANAA_PRINT_GAP_S = math.ceil(PRAMANAA_MIN_GAP_S / _PASS_S) * _PASS_S        # 310.5
# Pramanaa's x5 counts the forming 1-min candle as the seventh bar, so its first print falls in the 09:21
# minute; its bot entered on it at 09:21:22 - 09:21:58 (median 09:21:49), so a book with `forming` prints
# first at 09:21:45. The forming bar's price here is the last completed close (no look-ahead into the minute).
PRAMANAA_FIRST_PRINT_S = 6 * 60 + 45


class ProposerPxInputError(RuntimeError):
    """Required market data is unavailable for the session."""


@dataclass(frozen=True)
class Book:
    """One paper ledger: its table prefix, the live strategy version it replays and the gap between
    its prints."""
    key: str
    strategy_version: str
    print_gap_s: float = PRINT_EVERY_MIN * 60.0
    forming: bool = False             # Pramanaa's first print: the forming candle counted, 09:21:45


PX = Book("proposer_px", pe.STRATEGY_VERSION_PX)
V3 = Book("proposer_v3", pe.STRATEGY_VERSION_V3)
V3_DT35 = Book("proposer_v3_dt35", pe.STRATEGY_VERSION_V3_DT35)
V3_CLOCK = Book("proposer_v3_clock", pe.STRATEGY_VERSION_V3, PRAMANAA_PRINT_GAP_S)
DT25 = Book("proposer_dt25_shadow", pe.STRATEGY_VERSION)
DT25_CLOCK = Book("proposer_dt25_clock", pe.STRATEGY_VERSION, PRAMANAA_PRINT_GAP_S)
# The live preset proposer_dt35_v3 on Pramanaa's print timing - a paper trial of the copied clock
# before it goes live (Vipin, 2026-10-16).
V3_DT35_CLOCK = Book("proposer_v3_dt35_clock", pe.STRATEGY_VERSION_V3_DT35, PRAMANAA_PRINT_GAP_S)
# Sensex V4: the live preset proposer_dt35_v3 with the live settings of 16 Oct 2026 (Sentinel strike,
# 270 s cooldown) from its first session, so its history shows how they would have traded (Vipin).
V4 = Book("proposer_v4", pe.STRATEGY_VERSION_V3_DT35)
# Sensex V5: V4 with the -20% premium floor replaced by a loss floor in SENSEX points - out once SENSEX
# is SPOT_FLOOR_PTS[book] against the entry (alphaIMB REBUILD sec. 33, "P150"; Vipin, 2026-10-17).
V5 = Book("proposer_v5", pe.STRATEGY_VERSION_V3_DT35)
# Sensex V5 at 2.5%: the live preset proposer_dt35_v5 (V5, the 150-point floor inside the engine) with the
# day target at 2.5% instead of 3.5% (DAY_TARGET_PCT; Vipin, 2026-10-19).
V5_DT25 = Book("proposer_v5_dt25", pe.STRATEGY_VERSION_V5)
# Sensex 2.5% V6: Sensex V5 2.5% with no entry while SENSEX is inside the previous session's CPR (TC..BC), paper only
# (alphaIMB REBUILD sec. 57; Vipin, 2026-11-02).
V6_DT25 = Book("proposer_v6_dt25", pe.STRATEGY_VERSION_V5)
# Sensex 2.5% V6 with the engine's SENSEX loss floor at 125 points instead of 150, paper only - a forward test: 125 was
# picked after looking at October (alphaIMB v6_rescue.py floor125; Vipin, 2026-11-06).
V6_DT25_F125 = Book("proposer_v6_dt25_f125", pe.STRATEGY_VERSION_V5)
# Sensex 2.5% V6 IV-half: V6 at half size (1,000 qty) on a day whose 09:30 SENSEX ATM IV is above 1.2 x the median 09:30
# IV of the earlier sessions from 1 Jul (at least 10 of them), full size (2,000) otherwise; paper only (alphaIMB
# sizing_test.py variant iv_hard1.2, iv_days.py; Vipin, 2026-11-09). See IV_HALF_BOOKS.
V6_DT25_IVH = Book("proposer_v6_dt25_ivh", pe.STRATEGY_VERSION_V5)
NO_INSIDE_CPR_BOOKS = frozenset({V6_DT25.key, V6_DT25_F125.key, V6_DT25_IVH.key})
# Books whose engine spot floor (ProposerParams.spot_floor_pts, SENSEX points) differs from their preset's.
ENGINE_SPOT_FLOOR_PTS = {V6_DT25_F125.key: 125.0}
# NIFTY on Sensex V5 signals: the same SENSEX prints and engine, positions in NIFTY options with NIFTY's
# own exits (labs.engine.proposer_nifty_tracker; Vipin, 2026-10-21).
NIFTY_V5 = Book("proposer_nifty_v5", pe.STRATEGY_VERSION_V5)
# Nifty Scalper V3: the same book with prints every 3 minutes - the live preset proposer_nifty_scalper_v3 - and
# the same on Pramanaa's print clock, paper only (Vipin, 2026-10-28).
NIFTY_V3 = Book("proposer_nifty_v3", pe.STRATEGY_VERSION_V5, 180.0)
NIFTY_V3_PCLOCK = Book("proposer_nifty_v3_pclock", pe.STRATEGY_VERSION_V5, PRAMANAA_PRINT_GAP_S, forming=True)
# Nifty Scalper V4: V3 with the entry blocks of live/engine/entry_blocks.py - the live preset proposer_nifty_scalper_v4
# (Vipin, 2026-11-02).
NIFTY_V4 = Book("proposer_nifty_v4", pe.STRATEGY_VERSION_V5, 180.0)
# Nifty Scalper V4 2.5%: the same book with the day target at 2.5% - the live preset with 2.5% chosen on the
# configure page (alphaIMB v4_tail_risk.py round 4; Vipin, 2026-11-05).
NIFTY_V4_DT25 = Book("proposer_nifty_v4_dt25", pe.STRATEGY_VERSION_V5, 180.0)
NIFTY_BOOKS = frozenset({NIFTY_V5.key, NIFTY_V3.key, NIFTY_V3_PCLOCK.key, NIFTY_V4.key, NIFTY_V4_DT25.key})
ENTRY_BLOCK_BOOKS = frozenset({NIFTY_V4.key, NIFTY_V4_DT25.key})
# Books whose day banks at the day target whatever the book (the live preset's bank_regardless_of_book).
BANK_REGARDLESS_BOOKS = frozenset({NIFTY_V4.key, NIFTY_V4_DT25.key})
# Books that take the live preset's early adverse exit: (first session, (points, seconds)) steps in date order, the
# last step on or before a session applies. 35 pts / 600 s (proposer_engine.V4_EARLY_ADVERSE) on both V4 books,
# replayed from 1 July (Vipin, 2026-11-06; it replaced 25 pts / 180 s on the 2.5% book).
EARLY_ADVERSE_FROM = {NIFTY_V4.key: (("2026-07-01", pe.V4_EARLY_ADVERSE),),
                      NIFTY_V4_DT25.key: (("2026-07-01", pe.V4_EARLY_ADVERSE),)}


@dataclass(frozen=True)
class IvHalf:
    """A day's size from its 09:30 ATM implied vol: `qty` x `factor` on a day whose IV is above `ratio` x the median
    09:30 IV of the earlier sessions from `since` (at least `min_sessions` of them), full size otherwise or when an IV
    is missing. Entries before `at` (IV not known yet) are full size."""
    ratio: float = 1.2
    factor: float = 0.5
    since: str = "2026-07-01"
    min_sessions: int = 10
    at: time = time(9, 30)


IV_HALF_BOOKS = {V6_DT25_IVH.key: IvHalf()}


def atm_iv(near: pd.DataFrame, expiry, at: datetime, step: int = 100):
    """The ATM implied vol at the `at` snapshot of the traded expiry (`near`: its quotes with ltp > 0): the average of
    the ATM call's and put's IV, each from its bid/ask mid (its LTP without a valid quote), ATM = spot to the nearest
    `step`, T = calendar time from `at` to 15:30 on expiry day over 365 days (alphaIMB iv_days.py). None if missing."""
    s = near[near["timestamp"] == at]
    ed = ps.expiry_date(expiry)
    if s.empty or ed is None:
        return None
    spot = float(s["spot"].iloc[0])
    k = round(spot / step) * step
    years = (datetime.combine(ed, time(15, 30)) - at).total_seconds() / (365 * 86400)
    ivs = []
    for typ in ("CE", "PE"):
        r = s[(s["option_type"] == typ) & (s["strike"] == k)]
        if r.empty:
            continue
        r = r.iloc[-1]
        px = (r["bid"] + r["ask"]) / 2 if r["bid"] > 0 and r["ask"] > r["bid"] else r["ltp"]
        iv = ps.implied_vol(float(px), spot, k, years, typ)
        if iv:
            ivs.append(iv)
    return sum(ivs) / len(ivs) if ivs else None


def iv_size(rule: IvHalf, iv_today, history: list) -> dict:
    """{iv_0930, iv_ref, iv_n, qty}: the day's size under `rule` (history = the earlier sessions' 09:30 IVs)."""
    hist = sorted(float(x) for x in history if x is not None and x == x)
    ref = float(pd.Series(hist).median()) if hist else None
    half = bool(iv_today and ref and len(hist) >= rule.min_sessions and iv_today > rule.ratio * ref)
    qty = max(1, int(round(QTY * rule.factor / LOT_SIZE))) * LOT_SIZE if half else QTY
    return {"iv_0930": round(iv_today, 6) if iv_today else None, "iv_ref": round(ref, 6) if ref else None,
            "iv_n": len(hist), "qty": qty}


def iv_history(conn: sqlite3.Connection, book: Book, day: str) -> list:
    rule = IV_HALF_BOOKS[book.key]
    return [r[0] for r in conn.execute(f"SELECT iv_0930 FROM {book.key}_daily WHERE trade_date>=? AND trade_date<? "
                                       "AND iv_0930 IS NOT NULL ORDER BY trade_date", (rule.since, day))]


def early_adverse_on(day: str, book: Book):
    """(points, seconds) of the book's early adverse exit on `day`, or None when it has none that day."""
    rule = None
    for start, step in EARLY_ADVERSE_FROM.get(book.key, ()):
        if day >= start:
            rule = step
    return rule


# ------------------------------------------------------------------ schema ---
def _ensure_tables(conn: sqlite3.Connection, book: Book = PX) -> None:
    conn.executescript(
        f"""
        CREATE TABLE IF NOT EXISTS {book.key}_daily (
            trade_date        TEXT PRIMARY KEY,
            status            TEXT NOT NULL,
            expiry_code       TEXT,
            regime            TEXT,
            gap_pct           REAL,
            n_trades          INTEGER NOT NULL,
            n_losses          INTEGER NOT NULL DEFAULT 0,
            day_banked        INTEGER NOT NULL DEFAULT 0,
            gross_rs          REAL,
            charges_rs        REAL,
            net_rs            REAL,
            book_gross_before REAL,
            through_ts        TEXT,
            lots              INTEGER NOT NULL,
            qty               INTEGER NOT NULL,
            bar_exit          TEXT,
            strategy_version  TEXT NOT NULL,
            error             TEXT,
            updated_at        TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS {book.key}_trades (
            trade_date     TEXT NOT NULL,
            seq            INTEGER NOT NULL,
            signal         TEXT NOT NULL,
            side           TEXT NOT NULL,
            strike         INTEGER NOT NULL,
            tradingsymbol  TEXT,
            expiry_code    TEXT,
            entry_ts       TEXT NOT NULL,
            exit_ts        TEXT,
            entry_spot     REAL,
            exit_spot      REAL,
            entry_price    REAL,
            exit_price     REAL,
            gross_rs       REAL,
            charges_rs     REAL,
            net_rs         REAL,
            exit_rule      TEXT,
            PRIMARY KEY (trade_date, seq)
        );
        CREATE TABLE IF NOT EXISTS {book.key}_prints (
            trade_date TEXT NOT NULL,
            seq        INTEGER NOT NULL,
            minute     TEXT NOT NULL,
            sec        REAL NOT NULL,
            x5         TEXT,
            x5_conf    REAL,
            PRIMARY KEY (trade_date, seq)
        );
        """
    )
    if book.key in IV_HALF_BOOKS:
        have = {r[1] for r in conn.execute(f"PRAGMA table_info({book.key}_daily)")}
        for col in ("iv_0930", "iv_ref", "iv_n"):
            if col not in have:
                conn.execute(f"ALTER TABLE {book.key}_daily ADD COLUMN {col} REAL")
    conn.commit()


# ------------------------------------------------------------------- data ---
def _read_spot_csv(source) -> list[tuple]:
    f = pd.read_csv(source)
    f["ts"] = pd.to_datetime(f["timestamp"].astype(str).str.slice(0, 19))
    f = f.drop_duplicates("ts").sort_values("ts")
    return [(t.to_pydatetime(), float(o), float(h), float(l), float(c))
            for t, o, h, l, c in zip(f["ts"], f["open"], f["high"], f["low"], f["close"])]


def spot_ohlc(day: str) -> list[tuple]:
    """Collector SENSEX 1-min bars (t, o, h, l, c): live dir first, then the day's archive."""
    live = DATA_DIR / f"{day}_{SYMBOL}_spot_1min.csv"
    if live.is_file():
        return _read_spot_csv(live)
    tar = ARCHIVE_DIR / f"{day}.tar.gz"
    if tar.is_file():
        with tarfile.open(tar) as tf:
            m = next((x for x in tf.getmembers() if x.name.endswith(f"_{SYMBOL}_spot_1min.csv")), None)
            if m is not None:
                return _read_spot_csv(io.BytesIO(tf.extractfile(m).read()))
    return []


def _prior_closes(day: str) -> list[tuple]:
    probe = date.fromisoformat(day)
    for _ in range(10):
        probe -= timedelta(days=1)
        bars = spot_ohlc(probe.isoformat()) if probe.weekday() < 5 else []
        if bars:
            return [(b[0], b[4]) for b in bars]
    return []


def _session_frame(day: str) -> pd.DataFrame:
    try:
        frame = load_options_frame(SYMBOL, day, live_root=SHARED_LIVE_DIR, archive_root=SHARED_ARCHIVE_DIR)
    except FileNotFoundError as exc:
        raise ProposerPxInputError(f"No SENSEX option quotes for {day}") from exc
    need = {"timestamp", "spot", "strike", "option_type", "expiry", "bid", "ask", "ltp", "oi"}
    missing = need.difference(frame.columns)
    if missing:
        raise ProposerPxInputError(f"SENSEX quotes missing columns: {sorted(missing)}")
    frame = frame.copy()
    frame["timestamp"] = pd.to_datetime(frame["timestamp"], errors="coerce")
    if frame["timestamp"].dt.tz is not None:
        frame["timestamp"] = frame["timestamp"].dt.tz_convert("Asia/Kolkata").dt.tz_localize(None)
    frame["timestamp"] = frame["timestamp"].dt.floor("min")
    frame["option_type"] = frame["option_type"].astype(str).str.upper()
    frame["expiry"] = frame["expiry"].astype(str).str.upper()
    for column in ("spot", "strike", "bid", "ask", "ltp", "oi"):
        frame[column] = pd.to_numeric(frame[column], errors="coerce")
    return frame.dropna(subset=["timestamp", "spot", "strike"])


# The live runner takes Sentinel's |delta| >= 0.60 strike from 2026-10-16 (live/engine/proposer_strike.py);
# the simulated books follow from the next session, so the sessions already booked keep ATM -/+200.
DELTA_STRIKE_FROM = "2026-10-19"
# The live presets wait 270 s after an exit from 2026-10-16; the books that mirror them follow from the
# next session. The shadows copy Pramanaa's simulated bot, which waits 300 s.
COOLDOWN_270_FROM = "2026-10-19"
# Books that take both from an earlier session instead.
SETTINGS_FROM = {"proposer_v4": "2026-07-01", "proposer_v5": "2026-07-01", "proposer_v5_dt25": "2026-07-01", "proposer_v6_dt25": "2026-07-01",
                 "proposer_v6_dt25_f125": "2026-07-01", "proposer_v6_dt25_ivh": "2026-07-01",
                 "proposer_nifty_v5": "2026-07-01", "proposer_nifty_v3": "2026-07-01",
                 "proposer_nifty_v3_pclock": "2026-07-01", "proposer_nifty_v4": "2026-07-01",
                 "proposer_nifty_v4_dt25": "2026-07-01"}
# Books whose day target differs from their live preset's.
DAY_TARGET_PCT = {"proposer_v5_dt25": 0.025, "proposer_v6_dt25": 0.025, "proposer_v6_dt25_f125": 0.025, "proposer_v6_dt25_ivh": 0.025, "proposer_nifty_v5": 0.015, "proposer_nifty_v3": 0.015,
                  "proposer_nifty_v3_pclock": 0.015, "proposer_nifty_v4": 0.015,
                  "proposer_nifty_v4_dt25": 0.025}
# Books whose loss floor is in SENSEX points instead of a share of the premium. Checked on the same
# simulated price path as every other exit, after the engine's own exits; the premium floor is
# switched off (as in the research run: -99.9%).
SPOT_FLOOR_PTS = {"proposer_v5": 150.0}
NO_PREMIUM_FLOOR = -0.999


def params_on(day: str, book) -> "pe.ProposerParams":
    """The parameters a book trades with on `day`: the live preset's, except that sessions before
    COOLDOWN_270_FROM (or the book's SETTINGS_FROM), and the shadow books, keep the 300 s re-entry
    cooldown."""
    params = pe.params_for(pe.STRATEGY_VERSION_NIFTY_SCALPER_V1 if book.key in NIFTY_BOOKS
                           else book.strategy_version)
    if ((day < SETTINGS_FROM.get(book.key, COOLDOWN_270_FROM) or book.key not in DELTA_STRIKE_BOOKS)
            and params.reentry_cooldown_secs != 300.0):
        params = dataclasses.replace(params, reentry_cooldown_secs=300.0)
    if book.key in SPOT_FLOOR_PTS:
        params = dataclasses.replace(params, loss_floor_pct=NO_PREMIUM_FLOOR)
    if book.key in DAY_TARGET_PCT:
        params = dataclasses.replace(params, daily_target_pct=DAY_TARGET_PCT[book.key])
    if book.key in BANK_REGARDLESS_BOOKS:
        params = dataclasses.replace(params, bank_regardless_of_book=True)
    if book.key in ENGINE_SPOT_FLOOR_PTS:
        params = dataclasses.replace(params, spot_floor_pts=ENGINE_SPOT_FLOOR_PTS[book.key])
    early = early_adverse_on(day, book)
    if early:
        params = dataclasses.replace(params, early_adverse_pts=early[0], early_adverse_secs=early[1])
    return params


def spot_floor_exit(eng, sig, pos: dict, spot: float, book) -> "pe.Signal":
    """The book's SENSEX-point loss floor: EXIT once spot is SPOT_FLOOR_PTS against the entry spot,
    unless the engine is already exiting. The exit re-arms the re-entry cooldown like any loss."""
    pts = SPOT_FLOOR_PTS.get(book.key)
    if not pts or sig.action == "EXIT" or spot is None or not pos.get("espot"):
        return sig
    against = (pos["espot"] - spot) if pos["side"] == "CE" else (spot - pos["espot"])
    if against < pts:
        return sig
    eng.skip_cd_arm = False
    return pe.Signal("EXIT", "CALL" if pos["side"] == "CE" else "PUT", "spot_floor")
# The books that mirror a live preset (and the 3.5% trial on Pramanaa's timing); the shadows copy
# Pramanaa's simulated bot (ATM -/+200).
DELTA_STRIKE_BOOKS = frozenset({"proposer_px", "proposer_v3", "proposer_v3_dt35", "proposer_v3_dt35_clock",
                                "proposer_v4", "proposer_v5", "proposer_v5_dt25", "proposer_v6_dt25", "proposer_v6_dt25_f125",
                                "proposer_v6_dt25_ivh", "proposer_nifty_v5",
                                "proposer_nifty_v3", "proposer_nifty_v3_pclock", "proposer_nifty_v4",
                                "proposer_nifty_v4_dt25"})


def _entry_strike(day: str, near: pd.DataFrame, t: datetime, spot: float, side: str,
                  book_key: str = "proposer_v3") -> tuple[int, str]:
    """The strike an entry at `t` buys: before DELTA_STRIKE_FROM ATM -/+200; from it, the live
    runner's rule on the latest snapshot of the chain at or before `t`."""
    if day < SETTINGS_FROM.get(book_key, DELTA_STRIKE_FROM) or book_key not in DELTA_STRIKE_BOOKS:
        return pe.itm_strike(spot, side)
    seen = near[near["timestamp"] <= t]
    if seen.empty:
        return pe.itm_strike(spot, side)
    snap = seen[seen["timestamp"] == seen["timestamp"].max()]
    _, strike, typ, _ = ps.delta_itm_symbol(snap, side, spot, date.fromisoformat(day))
    return strike, typ


class _Contract:
    """Recorded quotes of one option, with the estimate used between snapshots."""

    def __init__(self, quotes: pd.DataFrame, snap_spot: dict):
        self.q, self.snap_spot = quotes, snap_spot

    def quote(self, t: datetime):
        """(ltp, half spread) of the latest snapshot at or before t (at most 2 min old)."""
        s = self.q[self.q.index <= t]
        if s.empty or (t - s.index[-1]) > timedelta(minutes=2):
            return None
        row = s.iloc[-1]
        ltp = float(row["ltp"])
        bid, ask = float(row["bid"] or 0), float(row["ask"] or 0)
        return ltp, ((ask - bid) / 2 if bid > 0 and ask > bid else ltp * 0.0025)

    def delta(self, t: datetime) -> float:
        s = self.q[(self.q.index <= t) & (self.q.index > t - timedelta(minutes=21))]["ltp"]
        sp = pd.Series({k: self.snap_spot.get(k.to_pydatetime()) for k in s.index}, dtype=float)
        d = pd.DataFrame({"o": s.diff(), "s": sp.diff()}).dropna()
        d = d[d["s"].abs() > 0]
        if len(d) >= 5 and (d["s"] ** 2).sum() > 0:
            beta = abs((d["o"] * d["s"]).sum() / (d["s"] ** 2).sum())
            if 0.2 <= beta <= 1.2:
                return min(0.95, max(0.3, beta))
        return 0.65

    def symbol(self, t: datetime):
        if "tradingsymbol" not in self.q.columns:
            return None
        s = self.q[self.q.index <= t]
        return None if s.empty else str(s.iloc[-1]["tradingsymbol"])


# --------------------------------------------------------------- predictor ---
def _prints(day: str, prior: list, today: list, open_ref: float | None, chains: dict,
            through: datetime, gap_s: float = PRINT_EVERY_MIN * 60.0,
            forming: bool = False) -> tuple[list[dict], pp.Regime]:
    """The live feed's prints, replayed: gap-rule regime, the first 5-class call as soon as one is
    valid (PRINT_LAG_S into its minute), then one every `gap_s` seconds; each from completed bars
    only (bars before its minute). `sec` is the second of minute `t` at which the print is acted
    on. At the default gap (exactly five minutes) every print lands PRINT_LAG_S into its minute,
    as the live feed's do; at PRAMANAA_PRINT_GAP_S the second and the minute slide. `forming`: Pramanaa's
    first print - the forming bar counted (at the last completed close), from 09:21:45."""
    regime = pp.regime_from_gap(prior[-1][1] if prior else None, open_ref)
    out = []
    at = datetime.combine(date.fromisoformat(day), SESSION_OPEN) + (
        timedelta(seconds=PRAMANAA_FIRST_PRINT_S) if forming else timedelta(minutes=1, seconds=PRINT_LAG_S))
    while True:
        m = at.replace(second=0, microsecond=0)
        if m > through or m.time() > PRINT_UNTIL:
            break
        done = [b for b in today if b[0] < m]
        chain = None
        if done and m in chains:
            k, ce, pe_ = chains[m]
            idx = abs(k - done[-1][1]).argsort()[:7]
            chain = {"spot": done[-1][1],
                     "atm_pcr": (pe_[idx].sum() / ce[idx].sum()) if ce[idx].sum() > 0 else None}
        rows = done + [(m, done[-1][1])] if forming and done else done
        x = pp.predict_x5(prior, rows, chain, regime) if done else None
        if x:
            out.append({"t": m, "sec": (at - m).total_seconds(), "regime": regime.label, "x5": x["x5"],
                        "x5_conf": x["x5_conf"], "x5_asof": m.isoformat(),
                        "drift_state": pp.drift_state(x["micro"])})
            at += timedelta(seconds=gap_s)
        else:
            at += timedelta(minutes=1)          # not valid yet: the feed asks again a minute later
    return out, regime


# -------------------------------------------------------------------- path ---
def _nodes(bar) -> list[tuple[float, float]]:
    _, o, h, l, c = bar
    e1, e2 = (l, h) if c >= o else (h, l)
    return [(0.0, o), (20.0, e1), (40.0, e2), (60.0, c)]


def _dense(nodes) -> list[tuple[float, float]]:
    pts = []
    for (s0, x0), (s1, x1) in zip(nodes, nodes[1:]):
        n = max(1, int(math.ceil(abs(x1 - x0))))
        for i in range(n):
            pts.append((s0 + (s1 - s0) * i / n, x0 + (x1 - x0) * i / n))
    return pts


def _with_print_seconds(path: list[tuple[float, float]], secs, close: float | None = None) -> list[tuple[float, float]]:
    """The path with a point at every print's second (spot interpolated between its neighbours). The path is
    walked in 1-point steps, so a quiet minute is visited only at 0/20/40 s: without these points a print due at
    25 s was acted on at 40 s and one due after the last step was never acted on (Pramanaa's 09:21:54 call on
    13 Oct and 1 Oct 2026; alphaIMB REBUILD sec. 46)."""
    out = list(path)
    for s in sorted({float(x) for x in secs if x is not None}):
        if s >= 60.0 or any(abs(p[0] - s) < 1e-9 for p in out):
            continue
        k = next((i for i, p in enumerate(out) if p[0] > s), len(out))
        if k == 0:
            continue
        s0, x0 = out[k - 1]
        s1, x1 = out[k] if k < len(out) else (60.0, x0 if close is None else close)
        x = x0 + (x1 - x0) * (s - s0) / (s1 - s0) if s1 > s0 else x0
        out.insert(k, (s, x))
    return out


def _rest_of_minute(sec: float, spot: float, bar) -> list[tuple[float, float]]:
    _, o, h, l, c = bar
    return [(sec, spot), ((sec + 60) / 2, h if c >= o else l), (60.0, c)]


# --------------------------------------------------------------------- day ---
def simulate_day(day: str, *, book_before: float = 0.0, now: datetime | None = None,
                 book: Book = PX, ticks=None, iv_hist: list | None = None) -> dict:
    """Replay one session to its last recorded minute. Pure: reads market data, writes nothing. `ticks` (a
    labs.engine.tick_replay.DayTicks on the SENSEX index, for a SENSEX book) replays inside each minute on the
    recorded index and option ticks, as the NIFTY books do (proposer_nifty_tracker). `iv_hist`: for an IV_HALF_BOOKS book,
    the earlier sessions' 09:30 IVs (iv_history), which set the day's size."""
    if book.key in NIFTY_BOOKS:                        # positions in NIFTY options, signals as here
        from labs.engine import proposer_nifty_tracker
        return proposer_nifty_tracker.simulate_day(day, book_before=book_before, now=now, book=book, ticks=ticks)
    if date.fromisoformat(day).weekday() >= 5:
        raise ProposerPxInputError(f"{day} is not a trading weekday")
    frame = _session_frame(day)
    expiry = select_expiry_code(frame["expiry"].unique(), day, "nearest_weekly")
    if expiry is None:
        raise ProposerPxInputError(f"No nearest SENSEX expiry for {day}")
    snap_spot = {t.to_pydatetime(): float(v) for t, v in frame.groupby("timestamp")["spot"].first().items()}
    near = frame[(frame["expiry"] == str(expiry)) & (frame["ltp"] > 0)]
    cols = ["ltp", "bid", "ask"] + (["tradingsymbol"] if "tradingsymbol" in near.columns else [])
    quote_book = {(typ, int(k)): g.drop_duplicates("timestamp", keep="last").set_index("timestamp")[cols]
            for (typ, k), g in near.groupby(["option_type", "strike"])}
    piv = near.pivot_table(index=["timestamp", "strike"], columns="option_type", values="oi",
                           aggfunc="last").fillna(0)
    chains = {}
    for ts, g in piv.groupby(level=0):
        g = g.droplevel(0)
        zero = 0 * g.iloc[:, 0]
        chains[ts.to_pydatetime()] = (g.index.to_numpy(dtype=float), g.get("CE", zero).to_numpy(),
                                      g.get("PE", zero).to_numpy())

    bars = spot_ohlc(day)
    if not bars:                      # no collector bars: straight lines between quote snapshots
        ts = sorted(snap_spot)
        bars = [(a, snap_spot[a], max(snap_spot[a], snap_spot[b]), min(snap_spot[a], snap_spot[b]), snap_spot[b])
                for a, b in zip(ts, ts[1:]) if b - a == timedelta(minutes=1)]
    bars = [b for b in bars if SESSION_OPEN <= b[0].time()]
    if not bars:
        raise ProposerPxInputError(f"No SENSEX spot bars for {day}")
    bar_at = {b[0]: b for b in bars}
    # replay only minutes that are complete: both the bar and the next quote snapshot exist
    through = min(bars[-1][0], max(snap_spot))
    if now is not None and now.date().isoformat() == day:
        through = min(through, now.replace(second=0, microsecond=0, tzinfo=None) - timedelta(minutes=1))
    session_end = datetime.combine(date.fromisoformat(day), EOD)
    final = through >= session_end - timedelta(minutes=1) or (now is None or now.date().isoformat() != day)

    prior = _prior_closes(day)
    closes_today = [(b[0], b[4]) for b in bars]
    # the gap is read at the close of the 09:15 bar, as the live feed reads the broker's candle
    open_ref = bars[0][4] if bars[0][0].time() == SESSION_OPEN else None
    prints, regime = _prints(day, prior, closes_today, open_ref, chains, min(through, session_end),
                             gap_s=book.print_gap_s, forming=book.forming)
    by_min = {}
    for p in prints:
        by_min.setdefault(p["t"], []).append(p)

    cpr = entry_blocks.previous_cpr(date.fromisoformat(day), spot_ohlc) if book.key in NO_INSIDE_CPR_BOOKS else None
    sizing, size_from = {"qty": QTY}, None
    if book.key in IV_HALF_BOOKS:
        rule = IV_HALF_BOOKS[book.key]
        size_from = datetime.combine(date.fromisoformat(day), rule.at)
        sizing = iv_size(rule, atm_iv(near, expiry, size_from) if size_from <= through else None, iv_hist or [])
    eng = pe.ProposerEngine(params_on(day, book))
    pos, trades, realized, snap, hist = None, [], 0.0, {}, []

    def book_state():
        eng.set_book(day_realized=realized, book_net=book_before + realized,
                     day_losses=sum(1 for x in trades if (x.get("gross_rs") or 0) < 0 and x.get("exit_ts")))

    def close(now_, px, spot_, rule):
        nonlocal realized, pos
        px = pos.pop("tick_exit", px)
        tr = trades[-1]
        gross = (px - pos["ein"]) * pos["qty"]
        charges = sensex_round_trip_charges(pos["ein"], px, pos["qty"])["total"]
        tr.update({"exit_ts": now_.isoformat(timespec="seconds"), "exit_spot": round(spot_, 2),
                   "exit_price": round(px, 2), "gross_rs": round(gross, 2), "charges_rs": round(charges, 2),
                   "net_rs": round(gross - charges, 2), "exit_rule": rule})
        realized += gross
        pos = None

    t = datetime.combine(date.fromisoformat(day), SESSION_OPEN)
    while t <= min(through, session_end):
        bar = bar_at.get(t)
        if bar is None:
            t += timedelta(minutes=1)
            continue
        if t >= session_end:
            if pos:
                q = pos["c"].quote(t)
                close(t, (q[0] - q[1]) if q else pos["last"] - pos["half"], bar[1], "eod")
            break
        pending = by_min.get(t, [])
        anchor_spot, anchor = bar[1], None
        if pos:
            q = pos["c"].quote(t)
            anchor = (q[0], q[1], pos["c"].delta(t)) if q else None
        path, i = _with_print_seconds(_dense(_nodes(bar)), [p["sec"] for p in pending], bar[4]), 0
        if ticks is not None:                                    # the minute on the recorded index ticks
            path = ticks.path(t, bar, [p["sec"] for p in pending], sys.modules[__name__], path)
        closes = [b[4] for b in hist]
        # the with-the-day filter's input: last completed close against the 09:15 close
        move = (hist[-1][4] - open_ref) if hist and open_ref else None
        while i < len(path):
            sec, spot = path[i]
            now_ = t + timedelta(seconds=sec)
            fresh = False
            if pending and sec >= pending[-1]["sec"]:
                snap, pending, fresh = pending[-1], [], True
            book_state()
            if pos:
                ltp = None
                if anchor:
                    sgn = 1 if pos["side"] == "CE" else -1
                    ltp = max(0.05, anchor[0] + sgn * anchor[2] * (spot - anchor_spot))
                    pos["last"], pos["half"] = ltp, anchor[1]
                if ticks is not None:                                # the held option at its recorded bid / ask
                    ltp = ticks.held(pos, now_, ltp)
                if snap:
                    sig = eng.evaluate(now_, snap, pe.Position(pos["side"], pos["ein"], pos["espot"], pos["qty"]),
                                       option_ltp=ltp, spot=spot, closes=closes, entry_idx=pos["idx"])
                    sig = spot_floor_exit(eng, sig, pos, spot, book)
                    if sig.action == "EXIT":
                        mark = ltp if ltp else pos["last"]
                        if sig.reason == "daily_target":
                            # the bank fires at its threshold; a jump past it at a re-anchor happened
                            # somewhere inside the previous minute, so fill at the threshold
                            need = eng.params.daily_target_pct * pos["ein"] * pos["qty"] - realized
                            if not eng.params.bank_regardless_of_book:     # recovery mode: the fill must also clear the book
                                need = max(need, -(book_before + realized))
                            mark = min(mark, pos["ein"] + need / pos["qty"])
                        if ticks is not None:                        # out at the bid, the fill delay later
                            pos["tick_exit"] = ticks.exit_px(pos, now_, mark - pos["half"])
                        close(now_, mark - pos["half"], spot, sig.reason)
                        book_state()
                        eng.evaluate(now_, snap, pe.Position(), option_ltp=None, spot=spot)
            elif fresh and snap:
                sig = eng.evaluate(now_, snap, pe.Position(), option_ltp=None, spot=spot, day_move=move)
                if cpr and sig.action == "ENTER" and entry_blocks.inside_cpr(spot, cpr):
                    sig = pe.Signal("HOLD")
                if sig.action == "ENTER" and now_.time() < ENTRY_CUTOFF:
                    strike, typ = _entry_strike(day, near, now_, spot, sig.side, book.key)
                    quotes = quote_book.get((typ, strike))
                    c = _Contract(quotes, snap_spot) if quotes is not None else None
                    q = c.quote(t) if c else None
                    if q:
                        d = c.delta(t)
                        est = max(0.05, q[0] + (1 if typ == "CE" else -1) * d * (spot - anchor_spot))
                        if ticks is not None:                        # in at the ask, the fill delay later
                            est, q = ticks.entry(c.symbol(t), now_, est, q)
                        pos = {"c": c, "side": typ, "ein": est + q[1], "espot": spot, "last": est,
                               "half": q[1], "idx": len(hist),
                               "qty": sizing["qty"] if size_from is None or now_ >= size_from else QTY}
                        if ticks is not None:
                            pos["sym"] = c.symbol(t)
                        anchor = (q[0], q[1], d)
                        eng.mark_entry(snap)
                        trades.append({"signal": sig.reason, "side": typ, "strike": strike,
                                       "tradingsymbol": c.symbol(t), "expiry_code": str(expiry),
                                       "entry_ts": now_.isoformat(timespec="seconds"),
                                       "entry_spot": round(spot, 2), "entry_price": round(est + q[1], 2),
                                       "qty": pos["qty"], "exit_ts": None})
                        if ticks is None:                            # on ticks the minute's path stands
                            path = path[:i + 1] + _dense(_rest_of_minute(sec, spot, bar))[1:]
            i += 1
        hist.append(bar)
        t += timedelta(minutes=1)

    if pos:                                             # still open at the last replayed minute
        q = pos["c"].quote(through + timedelta(minutes=1)) or pos["c"].quote(through)
        mark = (q[0] - q[1]) if q else pos["last"] - pos["half"]
        if final:
            close(through, mark, bars[-1][4], "eod")
        else:
            trades[-1].update({"exit_price": round(mark, 2), "exit_rule": "open",
                               "gross_rs": round((mark - pos["ein"]) * pos["qty"], 2)})
    closed = [x for x in trades if x.get("exit_ts")]
    prev_close = prior[-1][1] if prior else None
    return {
        "trade_date": day, "status": ("closed" if closed else "no_trade") if final else "live",
        "expiry_code": str(expiry), "regime": regime.label,
        "gap_pct": round((open_ref / prev_close - 1) * 100, 3) if prev_close and open_ref else None,
        "n_trades": len(trades), "n_losses": sum(1 for x in closed if x["gross_rs"] < 0),
        "day_banked": bool(eng.day_done),
        "gross_rs": round(sum(x["gross_rs"] for x in closed), 2),
        "charges_rs": round(sum(x["charges_rs"] for x in closed), 2),
        "net_rs": round(sum(x["net_rs"] for x in closed), 2),
        "book_gross_before": round(book_before, 2),
        "through_ts": through.isoformat(timespec="minutes"), "trades": trades, **sizing,
        "replay": ticks.summary() if ticks is not None else None,
        "prints": [{"minute": p["t"].strftime("%H:%M"), "sec": p["sec"], "x5": p["x5"],
                    "x5_conf": round(float(p["x5_conf"]), 4)} for p in prints],
    }


def _book_before(conn: sqlite3.Connection, day: str, book: Book = PX) -> float:
    row = conn.execute(f"SELECT COALESCE(SUM(gross_rs), 0) FROM {book.key}_daily "
                       "WHERE trade_date < ? AND status IN ('closed', 'no_trade')", (day,)).fetchone()
    return float(row[0] or 0.0)


def run_day(trade_date: str | None = None, *, persist: bool = True,
            connection: sqlite3.Connection | None = None, now: datetime | None = None,
            book: Book = PX) -> dict:
    """Replay a session (to now, for today) and store it. Idempotent. A tick book (labs.engine.tick_replay)
    replays on the recorded ticks: the recorder's live files intraday, the compacted day after the close."""
    from labs.engine import tick_replay
    now = now or datetime.now(IST)
    trade_date = trade_date or now.date().isoformat()
    own = connection is None
    conn = connection or get_conn()
    _ensure_tables(conn, book)
    try:
        # TicksMissing is deliberately not an input error: the backfill must stop, not record the day unavailable
        ticks = tick_replay.ticks_for(book.key, trade_date, now.date().isoformat())
        result = simulate_day(trade_date, book_before=_book_before(conn, trade_date, book), now=now, book=book,
                              ticks=ticks, iv_hist=iv_history(conn, book, trade_date) if book.key in IV_HALF_BOOKS else None)
        if persist:
            _persist(conn, result, book)
        return {k: result[k] for k in ("trade_date", "status", "regime", "n_trades", "gross_rs", "net_rs")}
    finally:
        if own:
            conn.close()


def _persist(conn: sqlite3.Connection, r: dict, book: Book = PX) -> None:
    conn.execute(f"DELETE FROM {book.key}_trades WHERE trade_date=?", (r["trade_date"],))
    for seq, t in enumerate(r["trades"], start=1):
        conn.execute(
            f"INSERT INTO {book.key}_trades (trade_date,seq,signal,side,strike,tradingsymbol,expiry_code,"
            "entry_ts,exit_ts,entry_spot,exit_spot,entry_price,exit_price,gross_rs,charges_rs,net_rs,exit_rule) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (r["trade_date"], seq, t["signal"], t["side"], t["strike"], t.get("tradingsymbol"),
             t.get("expiry_code"), t["entry_ts"], t.get("exit_ts"), t.get("entry_spot"), t.get("exit_spot"),
             t.get("entry_price"), t.get("exit_price"), t.get("gross_rs"), t.get("charges_rs"),
             t.get("net_rs"), t.get("exit_rule")))
    conn.execute(f"DELETE FROM {book.key}_prints WHERE trade_date=?", (r["trade_date"],))
    conn.executemany(
        f"INSERT INTO {book.key}_prints (trade_date,seq,minute,sec,x5,x5_conf) VALUES (?,?,?,?,?,?)",
        [(r["trade_date"], seq, p["minute"], p["sec"], p["x5"], p["x5_conf"])
         for seq, p in enumerate(r.get("prints") or [], start=1)])
    params = pe.params_for(book.strategy_version)
    conn.execute(
        f"INSERT INTO {book.key}_daily (trade_date,status,expiry_code,regime,gap_pct,n_trades,n_losses,"
        "day_banked,gross_rs,charges_rs,net_rs,book_gross_before,through_ts,lots,qty,bar_exit,"
        "strategy_version,error,updated_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,NULL,?) "
        "ON CONFLICT(trade_date) DO UPDATE SET status=excluded.status,expiry_code=excluded.expiry_code,"
        "regime=excluded.regime,gap_pct=excluded.gap_pct,n_trades=excluded.n_trades,"
        "n_losses=excluded.n_losses,day_banked=excluded.day_banked,gross_rs=excluded.gross_rs,"
        "charges_rs=excluded.charges_rs,net_rs=excluded.net_rs,"
        "book_gross_before=excluded.book_gross_before,through_ts=excluded.through_ts,lots=excluded.lots,"
        "qty=excluded.qty,bar_exit=excluded.bar_exit,strategy_version=excluded.strategy_version,"
        "error=NULL,updated_at=excluded.updated_at",
        (r["trade_date"], r["status"], r["expiry_code"], r["regime"], r["gap_pct"], r["n_trades"],
         r["n_losses"], int(r["day_banked"]), r["gross_rs"], r["charges_rs"], r["net_rs"],
         r["book_gross_before"], r["through_ts"], r.get("qty", QTY) // LOT_SIZE, r.get("qty", QTY), params.bar_exit,
         book.strategy_version, datetime.now(IST).isoformat(timespec="seconds")))
    if book.key in IV_HALF_BOOKS:                      # the day's size and the IV that set it
        conn.execute(f"UPDATE {book.key}_daily SET iv_0930=?, iv_ref=?, iv_n=? WHERE trade_date=?",
                     (r.get("iv_0930"), r.get("iv_ref"), r.get("iv_n"), r["trade_date"]))
    from labs.engine import tick_replay
    if book.key in tick_replay.TICK_BOOKS:             # how the day was replayed, for the tab's Replay column
        tick_replay.record(conn, book.key, r["trade_date"], r.get("replay") or tick_replay.ONE_MINUTE)
    conn.commit()


def record_unavailable(trade_date: str, error: str, *, connection: sqlite3.Connection | None = None,
                       book: Book = PX) -> None:
    """An auditable no-result day; never an invented trade."""
    own = connection is None
    conn = connection or get_conn()
    _ensure_tables(conn, book)
    try:
        conn.execute(f"DELETE FROM {book.key}_trades WHERE trade_date=?", (trade_date,))
        conn.execute(
            f"INSERT INTO {book.key}_daily (trade_date,status,n_trades,gross_rs,charges_rs,net_rs,lots,qty,"
            "strategy_version,error,updated_at) VALUES (?,?,0,0,0,0,?,?,?,?,?) "
            "ON CONFLICT(trade_date) DO UPDATE SET status=excluded.status,n_trades=0,n_losses=0,gross_rs=0,"
            "charges_rs=0,net_rs=0,error=excluded.error,updated_at=excluded.updated_at",
            (trade_date, "unavailable", LOTS, QTY, book.strategy_version, str(error)[:500],
             datetime.now(IST).isoformat(timespec="seconds")))
        conn.commit()
    finally:
        if own:
            conn.close()
