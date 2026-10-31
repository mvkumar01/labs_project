"""OI Unwind paper books, NIFTY and SENSEX. Paper only: they never call a broker order API.

Vipin's rule (2026-10-31): spot breaking through an OI wall that is unwinding carries on to the next level.
alphaIMB research/experiments/2026-09-09_proposer_v1_reverse_engineering, REBUILD.md sec. 50, cpr_oi_method.py
(setup break_res-_sl30).

  wall      the dashboard's OI wall at each 5-minute mark up to 15:15, on the expiry with the greatest total OI:
            the largest CE OI in spot..spot+band (resistance), the largest PE OI in spot-band..spot (support).
            Unwinding = the strike is active (top 10 OI or top 10 |dOI| of its side) and its 5-minute OI fall is
            meaningful. The gates are copied here from alphaIMB nifty_range_chart_payload (GATES) and frozen, so
            a dashboard recalibration does not change this book. Decisions read the latest mark at least
            60 s old at the bar's start label.
  entry     a 5-minute candle (from 09:15) closes above the resistance wall, the previous candle closed at or below
            it, the wall is unwinding: CALL at that close. Mirrored for PUT: below an unwinding support. Only
            from the 09:20 candle's close, before 15:00. One open trade per side.
  exit      stop 30 NIFTY points against the entry (x 3.24 on SENSEX); target the next CPR level at least 10 points
            beyond the wall; 15:25. On 1-min bars; a bar holding both counts as the stop.
  cpr       the session's standard CPR from the previous session's 1-min high, low and last close.
  option    paper leg: the ATM strike of the nearest expiry after today, 1 lot, ask at the snapshot after the entry
            bar, bid at the snapshot after the exit bar, exchange charges.

Evidence status: 1 Jul - 27 Oct 2026, both books, 83 trades, +Rs 27,100 per lot before charges, +9.9 index points a
trade (NIFTY scale). In-sample; the 30-point stop is an assumption; SENSEX's gates were calibrated on 28 Sep - 27 Oct.
"""
from __future__ import annotations

import bisect
import io
import sqlite3
import tarfile
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta

import pandas as pd

from config.labs_config import ARCHIVE_DIR, DATA_DIR, SHARED_ARCHIVE_DIR, SHARED_LIVE_DIR
from labs.engine import bn_alpha_tracker as core
from labs.engine.charges import round_trip_charges, sensex_round_trip_charges
from labs.engine.expiry_strangle_tracker import expiry_date_of
from market_data.shared_store import load_options_frame
from storage.db import get_conn

IST = core.IST
PAPER_START = "2026-07-01"
FIRST_UNSEEN = "2026-10-28"       # the research data ends 2026-10-27
STOP_PTS, MIN_GAP_PTS = 30.0, 10.0
SESSION_OPEN, FIRST_DECISION, LAST_ENTRY, FLAT = time(9, 15), 5, time(15, 0), time(15, 25)
LAST_WALL_MARK = time(15, 15)
CPR_KEYS = ("S3", "S2", "S1", "BC", "P", "TC", "R1", "R2", "R3")
REFERENCE = {"trades": 83, "days": 50, "rs": 27100, "pts": 9.9}


@dataclass(frozen=True)
class Book:
    key: str
    label: str
    symbol: str
    lot_size: int
    strike_step: int
    scale: float
    oi_pct: float
    oi_raw: float
    band: float
    charges: object


# OI gates: NIFTY's oi_ltp_thresholds_active.json, SENSEX's _SYMBOL_ACTIVE_OI_LTP_THRESHOLDS, as of 2026-10-31.
NIFTY = Book("oi_unwind_nifty", "OI Unwind NIFTY", "NIFTY", 65, 50, 1.0, 4.0, 132000.0, 200.0, round_trip_charges)
SENSEX = Book("oi_unwind_sensex", "OI Unwind SENSEX", "SENSEX", 20, 100, 3.24, 6.2, 24300.0, 600.0,
              sensex_round_trip_charges)
BOOKS = {b.key: b for b in (NIFTY, SENSEX)}
ACTIVE_TOP_N = 10
STRATEGY_VERSION = "oi_unwind_5m_break_stop30_next_cpr_atm_weekly_v1"


# ----------------------------------------------------------------- levels ---
def cpr_levels(high: float, low: float, close: float) -> dict:
    """The dashboard's compute_cpr_levels: TC is the higher central pivot, BC the lower."""
    span, p = high - low, (high + low + close) / 3
    bc_raw, r1, s1 = (high + low) / 2, 2 * p - low, 2 * p - high
    tc_raw = 2 * p - bc_raw
    raw = {"P": p, "TC": max(tc_raw, bc_raw), "BC": min(tc_raw, bc_raw), "R1": r1, "R2": p + span, "R3": r1 + span,
           "S1": s1, "S2": p - span, "S3": s1 - span}
    return {k: round(v, 2) for k, v in raw.items()}


def _wall(rows: pd.DataFrame, active: set, spot: float, book: Book, typ: str):
    """(strike, unwinding) of the side's wall, or None."""
    rows = rows[rows["option_type"] == typ]
    near = (rows[(rows["strike"] >= spot - book.band) & (rows["strike"] <= spot)] if typ == "PE"
            else rows[(rows["strike"] >= spot) & (rows["strike"] <= spot + book.band)])
    if near.empty:
        near = rows[rows["strike"] < spot] if typ == "PE" else rows[rows["strike"] > spot]
    if near.empty:
        return None
    row = near.sort_values(["oi", "strike"], ascending=[False, True]).iloc[0]
    delta, previous = float(row["delta_oi"]), float(row["previous_oi"])
    pct = delta * 100.0 / abs(previous) if previous else 0.0
    meaningful = abs(pct) >= book.oi_pct or abs(delta) >= book.oi_raw
    return float(row["strike"]), bool((int(row["strike"]), typ) in active and meaningful and delta < 0)


def wall_marks(frame: pd.DataFrame, book: Book) -> list[tuple]:
    """[(mark, support, resistance)]: support and resistance are (strike, unwinding) or None."""
    df = frame[["timestamp", "strike", "option_type", "expiry", "oi", "spot"]].copy()
    df["ts"] = pd.to_datetime(df["timestamp"].astype(str).str.slice(0, 19))
    for col in ("strike", "oi", "spot"):
        df[col] = pd.to_numeric(df[col], errors="coerce")
    df["oi"] = df["oi"].fillna(0.0)
    df["option_type"], df["expiry"] = df["option_type"].astype(str).str.upper(), df["expiry"].astype(str)
    df = df.dropna(subset=["ts", "strike", "spot"])
    df = df[df["option_type"].isin(["CE", "PE"]) & (df["ts"].dt.minute % 5 == 0)]
    marks = sorted(df["ts"].unique())
    groups = {m: g for m, g in df.groupby("ts")}
    out = []
    for i, mark in enumerate(marks):
        mark = pd.Timestamp(mark)
        if mark.time() > LAST_WALL_MARK:
            break
        now = groups[marks[i]]
        expiry = now.groupby("expiry")["oi"].sum().sort_values(ascending=False).index[0]
        spot = float(now["spot"].median())
        latest = now[now["expiry"] == expiry].groupby(["strike", "option_type"], as_index=False)["oi"].sum()
        before = groups[marks[max(i - 1, 0)]]
        before = (before[before["expiry"] == expiry].groupby(["strike", "option_type"], as_index=False)["oi"].sum()
                  .rename(columns={"oi": "previous_oi"}))
        merged = latest.merge(before, on=["strike", "option_type"], how="left")
        merged["previous_oi"] = merged["previous_oi"].fillna(merged["oi"])
        merged["delta_oi"] = merged["oi"] - merged["previous_oi"]
        active = set()
        for typ in ("CE", "PE"):
            side = merged[merged["option_type"] == typ]
            top = set(side.nlargest(ACTIVE_TOP_N, "oi").index)
            top |= set(side.assign(abs_delta=side["delta_oi"].abs()).nlargest(ACTIVE_TOP_N, "abs_delta").index)
            active |= {(int(merged.at[k, "strike"]), typ) for k in top}
        out.append((mark.to_pydatetime(), _wall(merged, active, spot, book, "PE"),
                    _wall(merged, active, spot, book, "CE")))
    return out


# ------------------------------------------------------------------- rule ---
def _side(s: int, bars: list[tuple], marks: list[tuple], cpr: dict, book: Book, session_done: bool) -> list[dict]:
    """s = +1 reads the session as it is (CALL); s = -1 flips prices and walls, so a PUT reads as a CALL."""
    k, n = book.scale, len(bars)
    t = [b[0] for b in bars]
    o = [s * b[1] for b in bars]
    hi = [max(s * b[2], s * b[3]) for b in bars]
    lo = [min(s * b[2], s * b[3]) for b in bars]
    c = [s * b[4] for b in bars]
    index = {x: i for i, x in enumerate(t)}
    levels = [s * float(cpr[x]) for x in CPR_KEYS]
    times = [m[0] for m in marks]
    start = datetime.combine(t[0].date(), SESSION_OPEN)
    trades, busy = [], -1
    for i in range(FIRST_DECISION, n):
        if t[i].time() >= LAST_ENTRY:
            break
        if int((t[i] - start).total_seconds() // 60) % 5 != 4:
            continue
        j = bisect.bisect_right(times, t[i] - timedelta(seconds=60)) - 1
        wall = (marks[j][2] if s > 0 else marks[j][1]) if j >= 0 else None
        prev = index.get(t[i] - timedelta(minutes=5))
        if wall is None or prev is None or not wall[1]:
            continue
        x = s * wall[0]
        if not c[prev] <= x < c[i] or busy >= i:
            continue
        beyond = [v for v in levels if v > x + MIN_GAP_PTS * k]
        target = min(beyond) if beyond else None
        entry, stop = c[i], c[i] - STOP_PTS * k
        if target is None or target <= entry:
            continue
        trade = {"side": "CALL" if s > 0 else "PUT", "entry_t": t[i], "entry_spot": s * entry, "wall": wall[0],
                 "target": s * target, "stop": s * stop, "exit_t": None, "exit_spot": None, "reason": None}
        for e in range(i + 1, n):
            if t[e].time() >= FLAT:
                exit_px, why = c[e], "EOD"
            elif lo[e] <= stop:
                exit_px, why = min(o[e], stop), "STOP"
            elif hi[e] >= target:
                exit_px, why = target, "TARGET"
            else:
                continue
            trade.update(exit_t=t[e], exit_spot=s * exit_px, reason=why, last=e)
            busy = e
            break
        if trade["reason"] is None:
            if session_done:
                trade.update(exit_t=t[-1], exit_spot=s * c[-1], reason="EOD", last=n - 1)
                busy = n - 1
            else:
                trade["last"], busy = n - 1, n - 1
        trades.append(trade)
    return trades


def simulate(bars: list[tuple], marks: list[tuple], cpr: dict, book: Book, session_done: bool = True) -> list[dict]:
    """Closed and open trades of the session, in entry order. Bars are (minute, o, h, l, c), completed only."""
    if not bars:
        return []
    return sorted(_side(1, bars, marks, cpr, book, session_done) + _side(-1, bars, marks, cpr, book, session_done),
                  key=lambda x: x["entry_t"])


# ----------------------------------------------------------------- option ---
def option_book(frame: pd.DataFrame, day: str, book: Book) -> tuple:
    """(quotes by (type, strike), the expiry code): the nearest expiry after today."""
    f = frame.copy()
    f["expiry"] = f["expiry"].astype(str).str.upper()
    f["ts"] = pd.to_datetime(f["timestamp"].astype(str).str.slice(0, 19)).dt.floor("min")
    f["option_type"] = f["option_type"].astype(str).str.upper()
    for col in ("strike", "ltp", "bid", "ask"):
        f[col] = pd.to_numeric(f[col], errors="coerce")
    d0 = date.fromisoformat(day)
    codes = {c: expiry_date_of(c, book.symbol) for c in f["expiry"].unique()}
    live = {c: d for c, d in codes.items() if d is not None and d > d0}
    if not live:
        return {}, None
    code = min(live, key=live.get)
    chain = f[(f["expiry"] == code) & (f["ltp"] > 0)].dropna(subset=["strike"])
    cols = ["ltp", "bid", "ask"] + (["tradingsymbol"] if "tradingsymbol" in chain.columns else [])
    return ({(typ, int(k)): g.drop_duplicates("ts", keep="last").set_index("ts")[cols]
             for (typ, k), g in chain.groupby(["option_type", "strike"])}, code)


def quote(quotes: dict, key: tuple, minute: datetime, column: str):
    """The bid or ask of the first snapshot within 3 minutes after this bar, last price when that side is empty."""
    q = quotes.get(key)
    if q is None:
        return None
    for m in range(1, 4):
        ts = minute + timedelta(minutes=m)
        if ts in q.index:
            v = q.at[ts, column]
            return float(v if v == v and v > 0 else q.at[ts, "ltp"])
    return None


def price(t: dict, quotes: dict, code, book: Book, step_pts_only: bool = False) -> dict:
    strike = int(round(t["entry_spot"] / book.strike_step) * book.strike_step)
    typ = "CE" if t["side"] == "CALL" else "PE"
    key = (typ, strike)
    closed = t["exit_t"] is not None
    sign = 1 if t["side"] == "CALL" else -1
    qty = book.lot_size
    q = quotes.get(key)
    symbol = str(q["tradingsymbol"].iloc[0]) if q is not None and "tradingsymbol" in q.columns else None
    ask = quote(quotes, key, t["entry_t"], "ask")
    bid = quote(quotes, key, t["exit_t"], "bid") if closed else None
    out = {**t, "strike": strike, "tradingsymbol": symbol, "expiry_code": code, "entry_ask": ask, "exit_bid": bid,
           "qty": qty, "status": "closed" if closed else "open", "option_pts": None, "gross_rs": None,
           "charges_rs": None, "net_rs": None, "quote_status": "open",
           "spot_pts": round(sign * (t["exit_spot"] - t["entry_spot"]), 2) if closed else None}
    if closed and (ask is None or bid is None):
        out["quote_status"] = "no_quote"
    elif closed:
        gross = (bid - ask) * qty
        charges = float(book.charges(ask, bid, qty)["total"])
        out.update(option_pts=round(bid - ask, 2), gross_rs=round(gross, 2), charges_rs=round(charges, 2),
                   net_rs=round(gross - charges, 2), quote_status="priced")
    return out


# ---------------------------------------------------------------- sources ---
def options_frame(day: str, book: Book) -> pd.DataFrame:
    try:
        return load_options_frame(book.symbol, day, live_root=SHARED_LIVE_DIR, archive_root=SHARED_ARCHIVE_DIR)
    except FileNotFoundError as exc:
        raise core.BnAlphaInputError(f"No {book.symbol} option quotes for {day}") from exc


def spot_bars(day: str, book: Book) -> dict:
    live = DATA_DIR / f"{day}_{book.symbol}_spot_1min.csv"
    if live.is_file():
        return core._read_spot_csv(live)
    tar = ARCHIVE_DIR / f"{day}.tar.gz"
    if tar.is_file():
        with tarfile.open(tar) as tf:
            m = next((x for x in tf.getmembers() if x.name.endswith(f"_{book.symbol}_spot_1min.csv")
                      and "BANKNIFTY" not in x.name), None)
            if m is not None:
                return core._read_spot_csv(io.BytesIO(tf.extractfile(m).read()))
    return {}


def _session(bars: dict) -> list[tuple]:
    return [(m, *bars[m]) for m in sorted(bars) if m.time() >= SESSION_OPEN]


def previous_hlc(day: str, book: Book) -> tuple[float, float, float, str]:
    for prev in core._previous_weekdays(day):
        rows = _session(spot_bars(prev, book))
        if rows:
            return max(r[2] for r in rows), min(r[3] for r in rows), rows[-1][4], prev
    raise core.BnAlphaInputError(f"No {book.symbol} session before {day}")


# ------------------------------------------------------------------ ledger ---
def _ensure_tables(conn: sqlite3.Connection, key: str) -> None:
    conn.executescript(
        f"""
        CREATE TABLE IF NOT EXISTS {key}_daily (
            trade_date TEXT PRIMARY KEY, status TEXT NOT NULL, expiry_code TEXT, cpr_date TEXT, bc REAL, p REAL, tc REAL,
            r1 REAL, s1 REAL, latest_mark TEXT, latest_spot REAL, support REAL, support_unwinding INTEGER,
            resistance REAL, resistance_unwinding INTEGER, n_trades INTEGER NOT NULL DEFAULT 0, spot_pnl_pts REAL,
            gross_rs REAL, charges_rs REAL, net_rs REAL, qty INTEGER, strategy_version TEXT NOT NULL, error TEXT,
            updated_at TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS {key}_trades (
            trade_date TEXT NOT NULL, seq INTEGER NOT NULL, status TEXT NOT NULL, side TEXT NOT NULL,
            entry_ts TEXT NOT NULL, exit_ts TEXT, wall REAL, target REAL, stop REAL, entry_spot REAL, exit_spot REAL,
            spot_pnl_pts REAL, exit_reason TEXT, strike INTEGER, tradingsymbol TEXT, entry_ask REAL, exit_bid REAL,
            option_pts REAL, gross_rs REAL, charges_rs REAL, net_rs REAL, qty INTEGER, quote_status TEXT,
            PRIMARY KEY (trade_date, seq)
        );
        """
    )
    conn.commit()


def _save(conn: sqlite3.Connection, day: str, book: Book, r: dict, error: str | None = None) -> None:
    key, stamp = book.key, datetime.now(IST).isoformat(timespec="seconds")
    conn.execute(f"DELETE FROM {key}_trades WHERE trade_date=?", (day,))
    if error:
        conn.execute(f"INSERT OR REPLACE INTO {key}_daily (trade_date,status,n_trades,strategy_version,error,updated_at) "
                     "VALUES (?,?,?,?,?,?)", (day, "unavailable", 0, STRATEGY_VERSION, error[:300], stamp))
        conn.commit()
        return
    fmt = lambda ts: ts.isoformat(timespec="minutes") if ts is not None else None   # noqa: E731
    trades = r["trades"]
    for seq, t in enumerate(trades, 1):
        conn.execute(
            f"INSERT INTO {key}_trades (trade_date,seq,status,side,entry_ts,exit_ts,wall,target,stop,entry_spot,exit_spot,"
            "spot_pnl_pts,exit_reason,strike,tradingsymbol,entry_ask,exit_bid,option_pts,gross_rs,charges_rs,net_rs,qty,"
            "quote_status) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (day, seq, t["status"], t["side"], fmt(t["entry_t"]), fmt(t["exit_t"]), t["wall"], t["target"], t["stop"],
             t["entry_spot"], t["exit_spot"], t["spot_pts"], t["reason"] or "holding", t["strike"], t["tradingsymbol"],
             t["entry_ask"], t["exit_bid"], t["option_pts"], t["gross_rs"], t["charges_rs"], t["net_rs"], t["qty"],
             t["quote_status"]))
    closed = [t for t in trades if t["status"] == "closed"]
    status = "live" if not r["session_done"] else ("traded" if trades else "no_trade")
    if r["session_done"] and any(t["quote_status"] != "priced" for t in closed):
        status = "partial_quotes"
    cpr, m = r["cpr"], r["marks"][-1] if r["marks"] else (None, None, None)
    sup, res = m[1] or (None, None), m[2] or (None, None)
    conn.execute(
        f"INSERT OR REPLACE INTO {key}_daily (trade_date,status,expiry_code,cpr_date,bc,p,tc,r1,s1,latest_mark,latest_spot,"
        "support,support_unwinding,resistance,resistance_unwinding,n_trades,spot_pnl_pts,gross_rs,charges_rs,net_rs,qty,"
        "strategy_version,error,updated_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (day, status, r["expiry"], r["cpr_date"], cpr["BC"], cpr["P"], cpr["TC"], cpr["R1"], cpr["S1"], fmt(m[0]),
         r["latest_spot"], sup[0], sup[1], res[0], res[1], len(trades), round(sum(t["spot_pts"] for t in closed), 2),
         round(sum(t["gross_rs"] or 0 for t in closed), 2), round(sum(t["charges_rs"] or 0 for t in closed), 2),
         round(sum(t["net_rs"] or 0 for t in closed), 2), book.lot_size, STRATEGY_VERSION, None, stamp))
    conn.commit()


def replay_day(day: str, book: Book, *, now: datetime | None = None) -> dict:
    """Read the session's data and replay it. Pure: writes nothing."""
    frame = options_frame(day, book)
    bars = _session(spot_bars(day, book))
    if not bars:
        raise core.BnAlphaInputError(f"No {book.symbol} 1-min spot bars for {day}")
    high, low, close, cpr_date = previous_hlc(day, book)
    if now is not None:
        limit = now.astimezone(IST).replace(tzinfo=None) - timedelta(minutes=1)
        bars = [b for b in bars if b[0] <= limit]
    done = now is None or (bars and bars[-1][0].time() >= FLAT)
    marks = wall_marks(frame, book)
    if now is not None:
        limit = now.astimezone(IST).replace(tzinfo=None)
        marks = [m for m in marks if m[0] <= limit]
    cpr = cpr_levels(high, low, close)
    quotes, code = option_book(frame, day, book)
    trades = [price(t, quotes, code, book) for t in simulate(bars, marks, cpr, book, bool(done))]
    return {"trades": trades, "cpr": cpr, "cpr_date": cpr_date, "marks": marks, "expiry": code,
            "session_done": bool(done), "latest_spot": bars[-1][4] if bars else None}


def run_day(trade_date: str, book: Book, *, now: datetime | None = None, persist: bool = True,
            connection: sqlite3.Connection | None = None) -> dict:
    own = connection is None
    conn = connection or get_conn()
    _ensure_tables(conn, book.key)
    try:
        today = (now or datetime.now(IST)).astimezone(IST).date().isoformat()
        try:
            r = replay_day(trade_date, book, now=now if trade_date == today else None)
        except core.BnAlphaInputError as exc:
            if persist:
                _save(conn, trade_date, book, {}, error=str(exc))
            return {"trade_date": trade_date, "status": "unavailable", "error": str(exc)}
        if persist:
            _save(conn, trade_date, book, r)
        closed = [t for t in r["trades"] if t["status"] == "closed"]
        return {"trade_date": trade_date, "trades": len(r["trades"]), "marks": len(r["marks"]),
                "open": sum(t["status"] == "open" for t in r["trades"]),
                "net_rs": round(sum(t["net_rs"] or 0 for t in closed), 2)}
    finally:
        if own:
            conn.close()


def run_live(now: datetime | None = None) -> dict | None:
    """The paper loop's entry: replay today at each 1-min bar, both books. Outside the session it does nothing."""
    now = (now or datetime.now(IST)).astimezone(IST)
    if now.weekday() >= 5 or not (time(9, 16) <= now.time() <= time(15, 45)):
        return None
    day, out = now.date().isoformat(), {}
    for book in BOOKS.values():
        conn = get_conn()
        try:
            _ensure_tables(conn, book.key)
            row = conn.execute(f"SELECT status, updated_at FROM {book.key}_daily WHERE trade_date=?", (day,)).fetchone()
        finally:
            conn.close()
        minute = now.replace(second=0, microsecond=0).isoformat(timespec="seconds")
        if row and row[0] in ("traded", "no_trade", "partial_quotes") or (row and row[1] and row[1] >= minute):
            continue
        out[book.key] = run_day(day, book, now=now)
    return out or None


def run_paper_day(trade_date: str | None = None) -> dict | None:
    """The paper loop's in-session entry (it passes today's date): see run_live."""
    return run_live(datetime.now(IST))


def sessions_with_quotes(start: str, end: str, book: Book) -> list[str]:
    days = set()
    for root in (SHARED_ARCHIVE_DIR, SHARED_LIVE_DIR):
        if root.exists():
            for p in root.iterdir():
                if (p.is_dir() and start <= p.name <= end and date.fromisoformat(p.name).weekday() < 5
                        and any(p.glob(f"{book.symbol}_options_1min.*"))):
                    days.add(p.name)
    return sorted(days)


def run_backfill(start: str = PAPER_START, end: str | None = None) -> dict:
    end = end or (datetime.now(IST).date() - timedelta(days=1)).isoformat()
    report = {}
    for book in BOOKS.values():
        done, failed = 0, {}
        for day in sessions_with_quotes(start, end, book):
            out = run_day(day, book)
            if out.get("error"):
                failed[day] = out["error"]
            else:
                done += 1
        report[book.key] = {"done": done, "failed": failed}
    return report


def tab_data(conn: sqlite3.Connection, key: str, date_clause: str = "", date_params=()) -> tuple:
    book = BOOKS[key]
    rows, trades, stats = core.tab_data(conn, date_clause, date_params, prefix=key, first_unseen=FIRST_UNSEEN,
                                        extra={"label": book.label, "symbol": book.symbol, "key": key,
                                               "lot_size": book.lot_size, "lots": 1, "reference": REFERENCE,
                                               "paper_start": PAPER_START, "stop_pts": STOP_PTS * book.scale})
    if stats:
        closed = [t for t in trades if t["status"] == "closed"]
        stats["exit_reasons"] = {k: sum(1 for t in closed if t["exit_reason"] == k) for k in ("TARGET", "STOP", "EOD")}
    return rows, trades, stats


if __name__ == "__main__":
    import json
    import sys
    print(json.dumps(run_backfill(*(sys.argv[1:3])), indent=1, default=str))
