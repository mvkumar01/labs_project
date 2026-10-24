"""Nifty New Alpha V1 paper book. Paper only: it never calls a broker order API.

Handed over by the alphaIMB research session "New Alpha" (research/experiments/2026-10-24_nifty_bare_range,
HANDOFF_PAPER_BOOK.md, reference_book.py) at Vipin's request, to run forward from Mon 2026-10-26. It is the
BANKNIFTY Alpha book's machinery (bn_alpha_tracker: marks, windows, rule, option leg, ledger) on NIFTY, and is
independent of the v2.11 / v2.14 champion: no tiers, no-trade days, Renko, 14:00 cutoff or other v2.x layer.

  range     c = round(prev_close / 50) * 50, strikes [c - 350, c + 350] inclusive, locked before the open.
            prev_close = the official previous NIFTY close (Kite daily candle = NSE bhavcopy UndrlygPric),
            never the v2.x hybrid / option-implied resolver.
  alpha     the standard formula (sum dPE - sum dCE) * 100 / (sum dPE + sum dCE), 0 when the sum is 0, on
            the nearest NIFTY expiry alive today (the weekly; on expiry day the expiring one); baseline = the
            previous session's settled OI for that expiry (alphaIMB codeB prev_day_oi_NIFTY_<prev>.csv = NSE
            bhavcopy); OI read at each 5-minute mark 09:15..15:25.
  rule      CALL above +30 / PUT below -30, re-entry on a fresh cross, |alpha| > 200 skipped; exits: spot
            trail that arms at +30 and trails 15 (1-min bars, bn_alpha's window convention), alpha target
            +-100, alpha stop -30 (+30 for puts), no spot target, EOD at the 15:25 mark.
  option    paper leg: the ATM strike (nearest 50) of the same expiry, 1 lot of 65, ask in / bid out, NSE
            charges. Spot points are logged too: the research is in points.

Evidence status (from the handoff): 266 sessions 2025-07-01 -> 2026-10-23, spot points, no costs: 323 trades,
+3,797.8 points, 73.3% winning days, 78.0% winning trades; the width was chosen on those sessions. About +10
points a trade, so spread and charges take a large share: the option leg is the number to watch.
"""
from __future__ import annotations

import io
import sqlite3
import tarfile
from datetime import date, datetime, time, timedelta

import pandas as pd

from config.labs_config import ARCHIVE_DIR, DATA_DIR, SHARED_ARCHIVE_DIR, SHARED_LIVE_DIR, UNDERLYINGS
from labs.engine import bn_alpha_tracker as core
from labs.engine.charges import round_trip_charges
from market_data.expiry import expiry_sort_date, is_monthly_expiry
from market_data.shared_store import load_options_frame
from storage.db import get_conn

IST = core.IST
SYMBOL = "NIFTY"
KEY, LABEL = "nna_v1", "Nifty New Alpha V1"
KITE_TOKEN = int(UNDERLYINGS[SYMBOL]["instrument_token"])          # NSE:NIFTY 50
LOT_SIZE, LOTS, STRIKE_STEP = 65, 1, 50
HALF_WIDTH, TRAIL_ARM, TRAIL_DIST = 350, 30.0, 15.0
STRATEGY_VERSION = "nna_v1_pc350_t30_tgt100_sl30_trail30_15_atm_weekly_v1"
PAPER_START = "2026-06-01"
FIRST_UNSEEN = "2026-10-26"       # the research data ends 2026-10-23
REFERENCE = {"trades": 323, "sessions": 266, "points": 3797.8, "green_pct": 73.3}
PREV_DAY_DIR = core.PREV_DAY_DIR


# ----------------------------------------------------------------- sources ---
def options_frame(day: str) -> pd.DataFrame:
    try:
        return load_options_frame(SYMBOL, day, live_root=SHARED_LIVE_DIR, archive_root=SHARED_ARCHIVE_DIR)
    except FileNotFoundError as exc:
        raise core.BnAlphaInputError(f"No {SYMBOL} option quotes for {day}") from exc


def spot_bars(day: str) -> dict:
    """The collector's NIFTY 1-min bars {minute: (o, h, l, c)}: live dir first, then the archive."""
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
        path = PREV_DAY_DIR / f"prev_day_oi_{SYMBOL}_{prev}.csv"
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
    """Read the session's data and replay it. Pure: writes nothing."""
    close, close_date, close_source = known_close or prev_close(day, kite)
    frame = options_frame(day)
    expiry = core.nearest_expiry(frame["expiry"].astype(str).str.upper().unique(), day)
    if expiry is None:
        raise core.BnAlphaInputError(f"No live {SYMBOL} expiry on {day}")
    base, base_date = baseline(day, expiry)
    bars = spot_bars(day)
    if not bars:
        raise core.BnAlphaInputError(f"No {SYMBOL} 1-min spot bars for {day}")
    inp = core.session_inputs(day, close=close, frame=frame, bars=bars, base=base, now=now,
                              half_width=HALF_WIDTH, step=STRIKE_STEP)
    closed, open_ = core.simulate(inp, arm=TRAIL_ARM, dist=TRAIL_DIST)
    price = dict(lot_size=LOT_SIZE, lots=LOTS, charges_fn=round_trip_charges, step=STRIKE_STEP)
    trades = [dict(core.price_trade(t, inp, **price), status="closed") for t in closed]
    if open_:
        trades.append(dict(core.price_trade(open_, inp, **price), status="open"))
    return {"inp": inp, "trades": trades, "prev_close": close, "prev_close_date": close_date,
            "prev_close_source": close_source, "baseline_date": base_date}


def run_day(trade_date: str | None = None, *, now: datetime | None = None, kite=None, persist: bool = True,
            connection: sqlite3.Connection | None = None) -> dict:
    now = now or datetime.now(IST)
    day = trade_date or now.astimezone(IST).date().isoformat()
    own = connection is None
    conn = connection or get_conn()
    core._ensure_tables(conn, KEY)
    try:
        row = conn.execute(f"SELECT prev_close, prev_close_date, prev_close_source FROM {KEY}_daily "
                           "WHERE trade_date=? AND prev_close_source='kite_daily'", (day,)).fetchone()
        known = tuple(row) if row else None              # the range is locked once known
        try:
            r = replay_day(day, now=now if day == now.astimezone(IST).date().isoformat() else None,
                           kite=kite, known_close=known)
        except core.BnAlphaInputError as exc:
            if persist:
                core._save(conn, day, {}, error=str(exc), prefix=KEY, version=STRATEGY_VERSION)
            return {"trade_date": day, "status": "unavailable", "error": str(exc)}
        if persist:
            core._save(conn, day, r, prefix=KEY, version=STRATEGY_VERSION, qty=LOT_SIZE * LOTS)
        closed = [t for t in r["trades"] if t["status"] == "closed"]
        return {"trade_date": day, "range": f"{r['inp']['range_lo']}-{r['inp']['range_hi']}",
                "marks": len(r["inp"]["marks"]), "trades": len(r["trades"]),
                "open": sum(t["status"] == "open" for t in r["trades"]),
                "spot_pts": round(sum(t["spot_pts"] for t in closed), 2),
                "net_rs": round(sum(t["net_rs"] or 0 for t in closed), 2)}
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
        core._ensure_tables(conn, KEY)
        row = conn.execute(f"SELECT latest_mark, status FROM {KEY}_daily WHERE trade_date=?", (day,)).fetchone()
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


def tab_data(conn: sqlite3.Connection, date_clause: str = "", date_params=()) -> tuple:
    return core.tab_data(conn, date_clause, date_params, prefix=KEY, first_unseen=FIRST_UNSEEN,
                         extra={"label": LABEL, "half_width": HALF_WIDTH, "arm": TRAIL_ARM, "dist": TRAIL_DIST,
                                "lot_size": LOT_SIZE, "lots": LOTS, "reference": REFERENCE,
                                "paper_start": PAPER_START})


if __name__ == "__main__":
    import json
    import sys
    print(json.dumps(run_backfill(*(sys.argv[1:3])), indent=1, default=str))
