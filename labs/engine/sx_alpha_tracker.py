"""SENSEX Alpha paper books SX_PC400 and SX_OPEN1400. Paper only: they never call a broker order API.

Handed over by the alphaIMB research session "New Alpha" (research/experiments/2026-10-24_sensex_alpha,
HANDOFF_PAPER_BOOKS.md, reference_books.py) at Vipin's request, to run forward from Mon 2026-10-26. The
machinery is the BANKNIFTY Alpha book's (bn_alpha_tracker: marks, windows, rule, option leg, ledger), with:

  alpha     ABSOLUTE denominator: (sum dPE - sum dCE) * 100 / (|sum dPE| + |sum dCE|), 0 when both are 0,
            on the nearest SENSEX expiry alive today (the weekly; on expiry day the expiring one); baseline =
            the previous session's settled OI for that expiry (alphaIMB codeB prev_day_oi_SENSEX_<prev>.csv,
            the same numbers as the BSE F&O bhavcopy); OI read at each 5-minute mark 09:15..15:25.
  ranges    SX_PC400     round(prev_close / 100) * 100 +- 400, prev_close = the official previous SENSEX close
                         (Kite daily candle = BSE's UndrlygPric), trail arms at +100, trails 50
            SX_OPEN1400  round(open / 100) * 100 +- 1400, open = the open of the 09:15 SENSEX 1-min bar,
                         trail arms at +150, trails 75
  rule      as bn_alpha: CALL above +30 / PUT below -30, re-entry on a fresh cross, |alpha| > 200 skipped,
            alpha target +-100, alpha stop -30 (+30 for puts), no spot target, EOD at the 15:25 mark.
  option    paper leg (Vipin chose, as bn_alpha): the ATM strike of the same weekly expiry, 1 lot of 20,
            ask in / bid out, BSE charges. Spot points are logged too: the research is in points.

The open is used as the collector recorded it. A 09:15 open equal to the previous close (a stale first
tick, seen in the Proposer on gap days) is NOT replaced; the day's row carries a note saying so.

Evidence status (from the handoff): 221 sessions Oct 2025 - 23 Oct 2026, spot points, no costs: SX_PC400 295
trades +7,944.1 points, 73.3% winning days; SX_OPEN1400 249 trades +11,780.3, 62.0%. Both books were picked
on those sessions; SX_PC400 averages +27 points a trade, thin against spreads and charges.
"""
from __future__ import annotations

import io
import sqlite3
import tarfile
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta
from pathlib import Path

import pandas as pd

from config.labs_config import ARCHIVE_DIR, DATA_DIR, SHARED_ARCHIVE_DIR, SHARED_LIVE_DIR, UNDERLYINGS
from labs.engine import bn_alpha_tracker as core
from labs.engine.charges import sensex_round_trip_charges
from market_data.expiry import expiry_sort_date, is_monthly_expiry
from market_data.shared_store import load_options_frame
from storage.db import get_conn

IST = core.IST
SYMBOL = "SENSEX"
KITE_TOKEN = int(UNDERLYINGS[SYMBOL]["instrument_token"])          # BSE:SENSEX
LOT_SIZE, LOTS, STRIKE_STEP = 20, 1, 100
PAPER_START = "2026-06-01"
FIRST_UNSEEN = "2026-10-26"       # the research data ends 2026-10-23
PREV_DAY_DIR = core.PREV_DAY_DIR


@dataclass(frozen=True)
class Book:
    key: str
    label: str
    anchor: str          # "prev_close" or "open"
    half_width: int
    arm: float
    dist: float
    version: str
    reference: dict


PC400 = Book("sx_pc400", "SENSEX Alpha PC400", "prev_close", 400, 100.0, 50.0,
             "sx_alpha_pc400_abs_t30_tgt100_sl30_trail100_50_atm_weekly_v1",
             {"trades": 295, "sessions": 221, "points": 7944.1, "green_pct": 73.3})
OPEN1400 = Book("sx_open1400", "SENSEX Alpha OPEN1400", "open", 1400, 150.0, 75.0,
                "sx_alpha_open1400_abs_t30_tgt100_sl30_trail150_75_atm_weekly_v1",
                {"trades": 249, "sessions": 221, "points": 11780.3, "green_pct": 62.0})
BOOKS = {b.key: b for b in (PC400, OPEN1400)}


# ----------------------------------------------------------------- sources ---
def options_frame(day: str) -> pd.DataFrame:
    try:
        return load_options_frame(SYMBOL, day, live_root=SHARED_LIVE_DIR, archive_root=SHARED_ARCHIVE_DIR)
    except FileNotFoundError as exc:
        raise core.BnAlphaInputError(f"No {SYMBOL} option quotes for {day}") from exc


def spot_bars(day: str) -> dict:
    """The collector's SENSEX 1-min bars {minute: (o, h, l, c)}: live dir first, then the archive."""
    live = DATA_DIR / f"{day}_{SYMBOL}_spot_1min.csv"
    if live.is_file():
        return core._read_spot_csv(live)
    tar = ARCHIVE_DIR / f"{day}.tar.gz"
    if tar.is_file():
        with tarfile.open(tar) as tf:
            m = next((x for x in tf.getmembers() if x.name.endswith(f"_{SYMBOL}_spot_1min.csv")), None)
            if m is not None:
                return core._read_spot_csv(io.BytesIO(tf.extractfile(m).read()))
    return {}


def baseline(day: str, expiry_code: str) -> tuple[pd.DataFrame, str]:
    """The previous session's settled OI for exactly this expiry (a weekly by date, a monthly code by month)."""
    exp = expiry_sort_date(expiry_code)
    monthly = is_monthly_expiry(expiry_code)
    for prev in core._previous_weekdays(day):
        names = [f"prev_day_oi_{SYMBOL}_{prev}.csv", f"prev_day_oi_{SYMBOL}_MONTHLY_{prev}.csv"]
        for name in names:
            path = PREV_DAY_DIR / name
            if not path.is_file():
                continue
            f = pd.read_csv(path)
            if "symbol" in f.columns:
                f = f[f["symbol"].astype(str).str.upper() == SYMBOL]
            if "expiry" in f.columns and exp is not None:
                e = pd.to_datetime(f["expiry"].astype(str), errors="coerce")
                if monthly:
                    f = f[(e.dt.year == exp.year) & (e.dt.month == exp.month) & (e.dt.date >= date.fromisoformat(day))]
                else:
                    f = f[e.dt.date == exp]
            f = f.assign(type=f["type"].astype(str).str.lower(),
                         strike=pd.to_numeric(f["strike"], errors="coerce"),
                         oi=pd.to_numeric(f["oi"], errors="coerce")).dropna(subset=["strike", "oi"])
            f = f[f["type"].isin(["ce", "pe"])]
            if not f.empty:
                return f.groupby(["strike", "type"], as_index=False)["oi"].sum(), prev
    raise core.BnAlphaInputError(f"No settled {SYMBOL} baseline for {expiry_code} before {day} under {PREV_DAY_DIR}")


def prev_close(day: str, kite=None) -> tuple[float, str, str]:
    """(close, its session, source): Kite's daily candle (the official close), else the collector's last bar."""
    try:
        if kite is None:
            from auth.session_manager import get_kite
            kite = get_kite()
        d0 = date.fromisoformat(day)
        rows = kite.historical_data(KITE_TOKEN, datetime.combine(d0 - timedelta(days=12), time(0)),
                                    datetime.combine(d0 - timedelta(days=1), time(23, 59)), "day")
        rows = [r for r in rows if pd.Timestamp(r["date"]).date() < d0]
        if rows:
            last = rows[-1]
            return float(last["close"]), pd.Timestamp(last["date"]).date().isoformat(), "kite_daily"
    except Exception:
        pass
    for prev in core._previous_weekdays(day):
        bars = spot_bars(prev)
        if bars:
            return float(bars[max(bars)][3]), prev, "collector_last_1min"
    raise core.BnAlphaInputError(f"No previous {SYMBOL} close before {day}")


# ------------------------------------------------------------------ replay ---
def replay_day(day: str, *, now: datetime | None = None, kite=None, known_close: tuple | None = None) -> dict:
    """Read the session's data once and replay both books. Pure: writes nothing."""
    close, close_date, close_source = known_close or prev_close(day, kite)
    frame = options_frame(day)
    expiry = core.nearest_expiry(frame["expiry"].astype(str).str.upper().unique(), day)
    if expiry is None:
        raise core.BnAlphaInputError(f"No live {SYMBOL} expiry on {day}")
    base, base_date = baseline(day, expiry)
    bars = spot_bars(day)
    if not bars:
        raise core.BnAlphaInputError(f"No {SYMBOL} 1-min spot bars for {day}")
    first = min(bars)
    if first.time() != time(9, 15):
        raise core.BnAlphaInputError(f"No 09:15 {SYMBOL} bar for {day} (first bar {first:%H:%M})")
    day_open = float(bars[first][0])
    note = (f"09:15 open {day_open:.2f} equals the previous close: possibly a stale first tick"
            if abs(day_open - close) < 0.01 else None)
    out = {}
    for book in BOOKS.values():
        centre = close if book.anchor == "prev_close" else day_open
        inp = core.session_inputs(day, close=centre, frame=frame, bars=bars, base=base, now=now,
                                  half_width=book.half_width, abs_den=True)
        closed, open_ = core.simulate(inp, arm=book.arm, dist=book.dist)
        price = dict(lot_size=LOT_SIZE, lots=LOTS, charges_fn=sensex_round_trip_charges, step=STRIKE_STEP)
        trades = [dict(core.price_trade(t, inp, **price), status="closed") for t in closed]
        if open_:
            trades.append(dict(core.price_trade(open_, inp, **price), status="open"))
        out[book.key] = {"inp": inp, "trades": trades, "prev_close": centre if book.anchor == "open" else close,
                         "prev_close_date": day if book.anchor == "open" else close_date,
                         "prev_close_source": "open_0915" if book.anchor == "open" else close_source,
                         "baseline_date": base_date}
    return {"books": out, "close": (close, close_date, close_source), "open": day_open, "note": note}


def run_day(trade_date: str | None = None, *, now: datetime | None = None, kite=None, persist: bool = True,
            connection: sqlite3.Connection | None = None) -> dict:
    now = now or datetime.now(IST)
    day = trade_date or now.astimezone(IST).date().isoformat()
    own = connection is None
    conn = connection or get_conn()
    for book in BOOKS.values():
        core._ensure_tables(conn, book.key)
    try:
        row = conn.execute("SELECT prev_close, prev_close_date, prev_close_source FROM sx_pc400_daily "
                           "WHERE trade_date=? AND prev_close_source='kite_daily'", (day,)).fetchone()
        known = tuple(row) if row else None              # the PC400 range is locked once known
        try:
            r = replay_day(day, now=now if day == now.astimezone(IST).date().isoformat() else None,
                           kite=kite, known_close=known)
        except core.BnAlphaInputError as exc:
            if persist:
                for book in BOOKS.values():
                    core._save(conn, day, {}, error=str(exc), prefix=book.key, version=book.version)
            return {"trade_date": day, "status": "unavailable", "error": str(exc)}
        summary = {"trade_date": day}
        for key, b in r["books"].items():
            if persist:
                core._save(conn, day, b, prefix=key, version=BOOKS[key].version, qty=LOT_SIZE * LOTS, note=r["note"])
            closed = [t for t in b["trades"] if t["status"] == "closed"]
            summary[key] = {"range": f"{b['inp']['range_lo']}-{b['inp']['range_hi']}", "trades": len(b["trades"]),
                            "spot_pts": round(sum(t["spot_pts"] for t in closed), 2),
                            "net_rs": round(sum(t["net_rs"] or 0 for t in closed), 2)}
        if r["note"]:
            summary["note"] = r["note"]
        return summary
    finally:
        if own:
            conn.close()


def run_live(now: datetime | None = None) -> dict | None:
    """The paper loop's entry: replay today whenever a new 5-minute mark has completed."""
    now = (now or datetime.now(IST)).astimezone(IST)
    if now.weekday() >= 5 or not (time(9, 16) <= now.time() <= time(15, 45)):
        return None
    day = now.date().isoformat()
    conn = get_conn()
    try:
        core._ensure_tables(conn, PC400.key)
        row = conn.execute("SELECT latest_mark, status FROM sx_pc400_daily WHERE trade_date=?", (day,)).fetchone()
    finally:
        conn.close()
    newest = (now - timedelta(seconds=70)).replace(second=0, microsecond=0, tzinfo=None)
    newest -= timedelta(minutes=newest.minute % 5)
    newest = min(newest, datetime.combine(now.date(), core.LAST_MARK))
    if row and row[1] != "unavailable" and row[0] and row[0] >= newest.isoformat(timespec="minutes"):
        return None
    return run_day(day, now=now)


def run_paper_day(trade_date: str | None = None) -> dict | None:
    """The paper loop's in-session entry (it passes today's date): see run_live."""
    return run_live(datetime.now(IST))


def sessions_with_quotes(start: str, end: str) -> list[str]:
    days = set()
    for root in (SHARED_ARCHIVE_DIR, SHARED_LIVE_DIR):
        if root.exists():
            for p in root.iterdir():
                if (p.is_dir() and start <= p.name <= end and date.fromisoformat(p.name).weekday() < 5
                        and any(p.glob(f"{SYMBOL}_options_1min.*"))):
                    days.add(p.name)
    return sorted(days)


def run_backfill(start: str = PAPER_START, end: str | None = None, kite=None) -> dict:
    end = end or (datetime.now(IST).date() - timedelta(days=1)).isoformat()
    done, failed = [], {}
    for day in sessions_with_quotes(start, end):
        out = run_day(day, kite=kite)
        (failed.__setitem__(day, out["error"]) if out.get("error") else done.append(day))
    return {"done": len(done), "failed": failed}


def tab_data(conn: sqlite3.Connection, key: str, date_clause: str = "", date_params=()) -> tuple:
    book = BOOKS[key]
    return core.tab_data(conn, date_clause, date_params, prefix=book.key, first_unseen=FIRST_UNSEEN,
                         extra={"book_key": book.key, "label": book.label, "anchor": book.anchor,
                                "half_width": book.half_width, "arm": book.arm, "dist": book.dist,
                                "lot_size": LOT_SIZE, "lots": LOTS, "reference": book.reference,
                                "paper_start": PAPER_START})


if __name__ == "__main__":
    import json
    import sys
    print(json.dumps(run_backfill(*(sys.argv[1:3])), indent=1, default=str))
