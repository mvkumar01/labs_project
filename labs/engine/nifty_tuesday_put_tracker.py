"""Paper-only NIFTY Tuesday put.

Paper only. This module never calls a broker order API.

The lead (V2 Strategy Tester session, 2026-10-16): an edge-birth scan of NIFTY found "short NIFTY on
Tuesday" as the one edge that appeared only recently - the index fell from 09:20 to 15:25 on Tuesdays
by -22.5 bps in 2024, -13.2 in 2025 and -13.1 in 2026, flat to positive in 2020-22. Priced on option
quotes, buying a put on the 42 Tuesdays from 2025-09-02 to 2026-09-01 made about +Rs 1,478 a lot a day
after Rs 95 of costs (t +1.4). A weak, recent lead found by a scan; this book is its forward test.

Rules (fixed by the research; not to be tuned from what this book shows):
  day         every Tuesday that is a trading day; a holiday is skipped, never moved to another day
  instrument  1 lot of the NIFTY put of the nearest expiry (on an expiry Tuesday, the contract
              expiring that day), strike round((spot at 09:20 + 89) / 50) * 50 - about 89 points in
              the money; spot = the index's 09:20 minute open
  entry/exit  bought at 09:20, sold at 15:25; no stop, no target

Prices. The "research price" of a contract at a snapshot is the middle of bid and ask when both are
quoted and not wide (ask - bid <= max(2, a quarter of the middle)), otherwise the last traded price;
the research took the 09:20 minute, or up to two minutes later when it has no price, and the same at
15:25. `research_net_rs` = (exit - entry) x 65 - Rs 95, exactly the research's own figure, for the
parity check. The headline is stricter: bought at the ask, sold at the bid, then the long-option
charge model; a side with no quote takes the research price worsened by 0.2 points. Sessions before
the shared store (2025-09-02 to 2026-04-21) have five-minute snapshots with the last traded price
only, so their headline is that price worsened by 0.2 points.

Data. The research-sample Tuesdays come from a frozen seed built on the research PC
(data/nifty_tuesday_put_{spot,quote}_seed.csv, scripts/build_nifty_tuesday_put_seed.py): the 09:20
spot from the research's own minute bars, and the put quotes from the files the research read -
alphaIMB five-minute snapshots before the shared store, the shared store's 1-minute quotes after
(PythonAnywhere has no five-minute files and empty shared-store folders for 2026-04-28 to 05-26).
Forward Tuesdays read the shared store and the labs NIFTY 1-minute spot file.

Tags. Tuesdays up to SAMPLE_END are the "research sample"; later ones are "forward". Retirement,
stated in advance: if the first 26 forward Tuesdays net to zero or less after charges, the book is
marked "retire". Tuesdays inside RESERVED_WINDOW belong to a window the Strategy Tester keeps
reserved: reading them here is allowed, and the tab lists them so the inspection can be recorded.
"""
from __future__ import annotations

import io
import math
import sqlite3
import tarfile
from datetime import date, datetime, time, timedelta, timezone
from pathlib import Path

import numpy as np
import pandas as pd

from config.labs_config import ARCHIVE_DIR, DATA_DIR, SHARED_ARCHIVE_DIR, SHARED_LIVE_DIR, UNDERLYINGS
from labs.engine.charges import round_trip_charges
from market_data.expiry import select_expiry_code
from market_data.shared_store import load_options_frame
from storage.db import get_conn

IST = timezone(timedelta(hours=5, minutes=30))
SYMBOL = "NIFTY"
LOT_SIZE = int(UNDERLYINGS[SYMBOL]["lot_size"])
LOTS = 1
QTY = LOT_SIZE * LOTS
TUESDAY = 1
ITM_POINTS = 89
STRIKE_STEP = 50
ENTRY, EXIT = time(9, 20), time(15, 25)
PRICE_WAIT = 2                          # the minute, or up to two minutes later if it has no price
SLIPPAGE_POINTS = 0.2
RESEARCH_COST_RS = 95.0                 # the research's flat round trip a lot
DEFAULT_START = "2025-09-02"
SAMPLE_END = "2026-09-01"               # Tuesdays up to here are the research sample
LIVE_FROM = "2026-10-16"                # Tuesdays from here on were traded as they happened
RETIRE_AFTER = 26
RESERVED_WINDOW = ("2026-09-29", "2026-10-27")
STRATEGY_VERSION = "nifty_tuesday_put_v1"
BASE_DIR = Path(__file__).resolve().parents[2]
SPOT_SEED = BASE_DIR / "data" / "nifty_tuesday_put_spot_seed.csv"
QUOTE_SEED = BASE_DIR / "data" / "nifty_tuesday_put_quote_seed.csv"
SEED_SOURCE = "alphaIMB 5-minute snapshots (seed)"
SEED_1MIN_SOURCE = "shared store, 1-minute quotes (seed)"
STORE_SOURCE = "shared store, 1-minute quotes"


class TuesdayPutSkip(RuntimeError):
    """The Tuesday cannot be traded by the rules; the reason is recorded, nothing is substituted."""


# ------------------------------------------------------------------ schema ---
def _ensure_tables(conn: sqlite3.Connection) -> None:
    conn.executescript(
        """
        CREATE TABLE IF NOT EXISTS nifty_tuesday_put_trades (
            trade_date        TEXT PRIMARY KEY,
            status            TEXT NOT NULL,            -- closed | open | skipped
            source            TEXT NOT NULL,            -- backfill | live
            sample            TEXT NOT NULL,            -- research sample | forward
            expiry_code       TEXT,
            strike            INTEGER,
            spot_0920         REAL,
            spot_source       TEXT,
            quote_source      TEXT,
            entry_ts          TEXT,
            exit_ts           TEXT,
            entry_research    REAL,
            exit_research     REAL,
            entry_bid         REAL, entry_ask REAL, exit_bid REAL, exit_ask REAL,
            bought            REAL,
            sold              REAL,
            fills             TEXT,
            qty               INTEGER NOT NULL,
            gross_rs          REAL,
            charges_rs        REAL,
            net_rs            REAL,
            research_net_rs   REAL,
            index_move_bps    REAL,
            strategy_version  TEXT NOT NULL,
            error             TEXT,
            updated_at        TEXT NOT NULL
        );
        """
    )
    conn.commit()


def sample_tag(trade_date: str) -> str:
    return "research sample" if trade_date <= SAMPLE_END else "forward"


def strike_for(spot: float) -> int:
    """The research's strike: about 89 points in the money, on the 50-point grid."""
    return int(round((float(spot) + ITM_POINTS) / STRIKE_STEP) * STRIKE_STEP)


# ------------------------------------------------------------------- seeds ---
_SEED_CACHE: dict = {}


def _seed(path: Path) -> pd.DataFrame:
    if not path.is_file():
        return pd.DataFrame()
    stamp = path.stat().st_mtime
    hit = _SEED_CACHE.get(path)
    if hit is None or hit[0] != stamp:
        hit = _SEED_CACHE[path] = (stamp, pd.read_csv(path, dtype={"trade_date": str, "expiry": str}))
    return hit[1]


def seed_spot(trade_date: str) -> float | None:
    s = _seed(SPOT_SEED)
    if s.empty:
        return None
    hit = s[s["trade_date"] == trade_date]
    return float(hit["spot_0920_open"].iloc[0]) if len(hit) else None


def seed_quote_days() -> set[str]:
    q = _seed(QUOTE_SEED)
    return set(q["trade_date"]) if not q.empty else set()


# -------------------------------------------------------------------- data ---
def _minute_bars(path_or_buf) -> pd.DataFrame:
    f = pd.read_csv(path_or_buf)
    f["minute"] = pd.to_datetime(f["timestamp"].astype(str).str.slice(0, 19), errors="coerce").dt.floor("min")
    return f.dropna(subset=["minute"]).drop_duplicates("minute", keep="last").set_index("minute")


def labs_spot_0920(trade_date: str) -> float | None:
    """The 09:20 minute open of the labs NIFTY 1-minute spot file (live dir, then the day's archive)."""
    name = f"{trade_date}_{SYMBOL}_spot_1min.csv"
    bars = None
    if (DATA_DIR / name).is_file():
        bars = _minute_bars(DATA_DIR / name)
    elif (ARCHIVE_DIR / f"{trade_date}.tar.gz").is_file():
        with tarfile.open(ARCHIVE_DIR / f"{trade_date}.tar.gz") as tf:
            m = next((x for x in tf.getmembers() if x.name.endswith(f"_{SYMBOL}_spot_1min.csv")), None)
            if m is not None:
                bars = _minute_bars(io.BytesIO(tf.extractfile(m).read()))
    if bars is None or "open" not in bars:
        return None
    at = pd.Timestamp(datetime.combine(date.fromisoformat(trade_date), ENTRY))
    if at not in bars.index:
        return None
    v = float(bars.loc[at, "open"])
    return v if v > 0 else None


def spot_0920(trade_date: str) -> tuple[float | None, str | None]:
    s = seed_spot(trade_date)
    if s is not None:
        return s, "research minute bars (seed)"
    s = labs_spot_0920(trade_date)
    return (s, "labs NIFTY 1-minute spot") if s is not None else (None, None)


def _research_price(f: pd.DataFrame) -> pd.Series:
    last = f["ltp"].where(f["ltp"] > 0)
    mid = (f["bid"] + f["ask"]) / 2.0
    quoted = (f["bid"] > 0) & (f["ask"] >= f["bid"]) & ((f["ask"] - f["bid"]) <= np.maximum(2.0, 0.25 * mid))
    return mid.where(quoted, last)


def nearest_puts(trade_date: str) -> tuple[pd.DataFrame, str, str]:
    """(the nearest expiry's put rows: minute, strike, price, bid, ask; its code; where they came from)."""
    if trade_date in seed_quote_days():
        q = _seed(QUOTE_SEED)
        f = q[q["trade_date"] == trade_date].copy()
        f["minute"] = pd.to_datetime(f["timestamp"]).dt.floor("min")
        for c in ("ltp", "bid", "ask"):
            f[c] = pd.to_numeric(f[c], errors="coerce") if c in f else np.nan
        f = f.assign(price=_research_price(f), bid=f["bid"].where(f["bid"] > 0), ask=f["ask"].where(f["ask"] > 0))
        kind = str(f["kind"].iloc[0]) if "kind" in f else "5min"
        return (f[["minute", "strike", "price", "bid", "ask"]], str(f["expiry"].iloc[0]),
                SEED_1MIN_SOURCE if kind == "1min" else SEED_SOURCE)
    try:
        frame = load_options_frame(SYMBOL, trade_date, live_root=SHARED_LIVE_DIR, archive_root=SHARED_ARCHIVE_DIR)
    except FileNotFoundError as exc:
        raise TuesdayPutSkip("no NIFTY quotes stored for this Tuesday") from exc
    f = frame.copy()
    ts = pd.to_datetime(f["timestamp"], errors="coerce")
    if ts.dt.tz is not None:
        ts = ts.dt.tz_convert("Asia/Kolkata").dt.tz_localize(None)
    f["minute"] = ts.dt.floor("min")
    f["expiry"] = f["expiry"].astype(str).str.upper()
    for c in ("strike", "ltp", "bid", "ask"):
        f[c] = pd.to_numeric(f[c], errors="coerce")
    code = select_expiry_code(f["expiry"].unique(), trade_date, "nearest_weekly")
    if not code:
        raise TuesdayPutSkip("no nearest expiry in the stored chain")
    f = f[(f["expiry"] == str(code)) & (f["option_type"].astype(str).str.upper().str[0] == "P")].dropna(subset=["minute", "strike"])
    f = f.assign(price=_research_price(f), bid=f["bid"].where(f["bid"] > 0), ask=f["ask"].where(f["ask"] > 0))
    return f[["minute", "strike", "price", "bid", "ask"]], str(code), STORE_SOURCE


def _quote_at(puts: pd.DataFrame, day: str, at: time, strike: int) -> dict | None:
    """The first minute from `at` to PRICE_WAIT minutes later with a research price for `strike`."""
    rows = puts[puts["strike"] == strike]
    t0 = pd.Timestamp(datetime.combine(date.fromisoformat(day), at))
    for dt in range(PRICE_WAIT + 1):
        hit = rows[rows["minute"] == t0 + pd.Timedelta(minutes=dt)]
        if len(hit):
            r = hit.iloc[-1]                                       # a later row of the minute wins
            if np.isfinite(r["price"]):
                return {"minute": t0 + pd.Timedelta(minutes=dt), "price": float(r["price"]),
                        "bid": float(r["bid"]) if np.isfinite(r["bid"]) else None,
                        "ask": float(r["ask"]) if np.isfinite(r["ask"]) else None}
    return None


def _latest_quote(puts: pd.DataFrame, strike: int) -> dict | None:
    rows = puts[(puts["strike"] == strike) & np.isfinite(puts["price"])].sort_values("minute")
    if rows.empty:
        return None
    r = rows.iloc[-1]
    return {"minute": r["minute"], "price": float(r["price"]),
            "bid": float(r["bid"]) if np.isfinite(r["bid"]) else None,
            "ask": float(r["ask"]) if np.isfinite(r["ask"]) else None}


# ------------------------------------------------------------------ replay ---
def simulate_day(trade_date: str, *, now: datetime | None = None) -> dict:
    """One Tuesday. Pure: reads market data, writes nothing. A session still running reports an
    open position marked at the latest priced snapshot."""
    day = date.fromisoformat(trade_date)
    if day.weekday() != TUESDAY:
        raise ValueError(f"{trade_date} is not a Tuesday")
    today = now is not None and now.date() == day
    final = not today or now.replace(tzinfo=None).time() >= time(15, 30)
    base = {"trade_date": trade_date, "final": final}
    if today and now.replace(tzinfo=None).time() < time(9, 20 + PRICE_WAIT, 30):
        return {**base, "status": "waiting"}
    spot, spot_source = spot_0920(trade_date)
    if spot is None:
        raise TuesdayPutSkip("no NIFTY 09:20 minute open stored for this Tuesday")
    strike = strike_for(spot)
    puts, code, source = nearest_puts(trade_date)
    base.update(expiry_code=code, strike=strike, spot_0920=spot, spot_source=spot_source, quote_source=source)
    entry = _quote_at(puts, trade_date, ENTRY, strike)
    if entry is None:
        if not final:
            return {**base, "status": "waiting"}
        raise TuesdayPutSkip(f"no price for the {strike} put from 09:20 to 09:22")
    exit_ = _quote_at(puts, trade_date, EXIT, strike)
    closed = exit_ is not None
    if not closed:
        if final:
            raise TuesdayPutSkip(f"no price for the {strike} put from 15:25 to 15:27")
        exit_ = _latest_quote(puts, strike)                      # still running: mark at the latest price
    no_quotes = entry["ask"] is None and exit_["bid"] is None
    bought = entry["ask"] if entry["ask"] is not None else entry["price"] + SLIPPAGE_POINTS
    sold = exit_["bid"] if exit_["bid"] is not None else exit_["price"] - SLIPPAGE_POINTS
    gross = (sold - bought) * QTY
    charges = float(round_trip_charges(bought, sold, QTY)["total"])
    return {
        **base, "status": "closed" if closed else "open",
        "entry_ts": entry["minute"].isoformat(timespec="minutes"),
        "exit_ts": exit_["minute"].isoformat(timespec="minutes") if closed else None,
        "mark_ts": exit_["minute"].isoformat(timespec="minutes"),
        "entry_research": entry["price"], "exit_research": exit_["price"],
        "entry_bid": entry["bid"], "entry_ask": entry["ask"], "exit_bid": exit_["bid"], "exit_ask": exit_["ask"],
        "bought": round(bought, 2), "sold": round(sold, 2),
        "fills": "last traded price +/- 0.2 (no quotes on file)" if no_quotes else "ask in, bid out",
        "gross_rs": round(gross, 2), "charges_rs": round(charges, 2), "net_rs": round(gross - charges, 2),
        "research_net_rs": round((exit_["price"] - entry["price"]) * LOT_SIZE - RESEARCH_COST_RS, 2),
    }


# ----------------------------------------------------------------- persist ---
def _persist(conn: sqlite3.Connection, r: dict) -> None:
    def v(name):
        x = r.get(name)
        if isinstance(x, float) and not math.isfinite(x):
            return None
        return x

    conn.execute("DELETE FROM nifty_tuesday_put_trades WHERE trade_date=?", (r["trade_date"],))
    conn.execute(
        "INSERT INTO nifty_tuesday_put_trades (trade_date,status,source,sample,expiry_code,strike,spot_0920,spot_source,"
        "quote_source,entry_ts,exit_ts,entry_research,exit_research,entry_bid,entry_ask,exit_bid,exit_ask,bought,sold,"
        "fills,qty,gross_rs,charges_rs,net_rs,research_net_rs,index_move_bps,strategy_version,error,updated_at) "
        "VALUES (" + ",".join("?" * 29) + ")",
        (r["trade_date"], r["status"], "live" if r["trade_date"] >= LIVE_FROM else "backfill", sample_tag(r["trade_date"]),
         v("expiry_code"), v("strike"), v("spot_0920"), v("spot_source"), v("quote_source"), v("entry_ts"),
         v("exit_ts") or (v("mark_ts") if r["status"] == "open" else None), v("entry_research"), v("exit_research"),
         v("entry_bid"), v("entry_ask"), v("exit_bid"), v("exit_ask"), v("bought"), v("sold"), v("fills"), QTY,
         v("gross_rs"), v("charges_rs"), v("net_rs"), v("research_net_rs"), v("index_move_bps"), STRATEGY_VERSION,
         v("error"), datetime.now(IST).isoformat(timespec="seconds")))
    conn.commit()


def run_day(trade_date: str | None = None, *, persist: bool = True,
            connection: sqlite3.Connection | None = None, now: datetime | None = None) -> dict:
    """Replay a Tuesday (to now, for today) and store it. Idempotent; other weekdays are a no-op."""
    now = now or datetime.now(IST)
    trade_date = trade_date or now.date().isoformat()
    if date.fromisoformat(trade_date).weekday() != TUESDAY:
        return {"trade_date": trade_date, "status": "not_tuesday"}
    own = connection is None
    conn = connection or get_conn()
    _ensure_tables(conn)
    try:
        try:
            r = simulate_day(trade_date, now=now)
        except TuesdayPutSkip as exc:
            r = {"trade_date": trade_date, "status": "skipped", "final": True, "error": str(exc)}
        if r["status"] == "waiting":
            return {"trade_date": trade_date, "status": "waiting"}
        if persist:
            _persist(conn, r)
        return {"trade_date": trade_date, "status": r["status"], "net_rs": r.get("net_rs"),
                "research_net_rs": r.get("research_net_rs"), "error": r.get("error")}
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


def tuesdays_with_data(start_date: str, end_date: str) -> list[str]:
    """Tuesdays with stored NIFTY quotes (shared store or seed). A Tuesday with none is a holiday
    or a day the collector missed; tab_data lists those separately."""
    from market_data.shared_store import resolve_options_source
    found = {d for d in seed_quote_days() if start_date <= d <= end_date}
    for root in (SHARED_ARCHIVE_DIR, SHARED_LIVE_DIR):
        if not root.exists():
            continue
        for path in root.iterdir():
            if path.is_dir() and len(path.name) == 10 and start_date <= path.name <= end_date:
                try:
                    if date.fromisoformat(path.name).weekday() != TUESDAY:
                        continue
                    resolve_options_source(SYMBOL, path.name, live_root=SHARED_LIVE_DIR, archive_root=SHARED_ARCHIVE_DIR)
                    found.add(path.name)
                except (ValueError, FileNotFoundError):
                    pass
    return sorted(found)


def run_backfill(*, start_date: str = DEFAULT_START, end_date: str | None = None, limit: int = 10,
                 rebuild: bool = False) -> dict:
    """Replay the next `limit` stored Tuesdays not yet in the book, oldest first. `rebuild` first
    clears the book from `start_date` on."""
    end_date = end_date or _default_end_date()
    conn = get_conn()
    _ensure_tables(conn)
    try:
        if rebuild:
            conn.execute("DELETE FROM nifty_tuesday_put_trades WHERE trade_date>=?", (start_date,))
            conn.commit()
        seen = {row[0] for row in conn.execute(
            "SELECT trade_date FROM nifty_tuesday_put_trades WHERE trade_date>=? AND trade_date<=? AND status!='open'",
            (start_date, end_date))}
    finally:
        conn.close()
    pending = [d for d in tuesdays_with_data(start_date, end_date) if d not in seen]
    done, errors = [], {}
    for session in pending[:max(1, min(int(limit), 40))]:
        try:
            done.append(run_day(session))
        except Exception as exc:                                   # noqa: BLE001
            errors[session] = f"{type(exc).__name__}: {exc}"
            break
    return {"done": done, "errors": errors, "remaining": 0 if errors else max(0, len(pending) - len(done))}


# -------------------------------------------------------------------- view ---
def _summary(rows: list[dict], field: str) -> dict:
    nets = [float(r[field] or 0) for r in rows if r["status"] == "closed" and r[field] is not None]
    if not nets:
        return {"days": 0, "net_rs": 0.0, "wins": 0, "win_pct": None, "avg_rs": None, "worst_rs": None, "best_rs": None, "t": None}
    mean = sum(nets) / len(nets)
    sd = float(np.std(nets, ddof=1)) if len(nets) > 1 else 0.0
    return {"days": len(nets), "net_rs": round(sum(nets), 2), "wins": sum(1 for n in nets if n > 0),
            "win_pct": round(100 * sum(1 for n in nets if n > 0) / len(nets)), "avg_rs": round(mean, 2),
            "worst_rs": round(min(nets), 2), "best_rs": round(max(nets), 2),
            "t": round(mean / (sd / math.sqrt(len(nets))), 2) if sd > 0 else None}


def retirement(rows: list[dict]) -> dict:
    """The rule stated before the forward test: the first 26 forward Tuesdays must net above zero."""
    fwd = sorted((r for r in rows if r["sample"] == "forward" and r["status"] == "closed"), key=lambda r: r["trade_date"])
    first = fwd[:RETIRE_AFTER]
    net = round(sum(float(r["net_rs"] or 0) for r in first), 2)
    if len(first) < RETIRE_AFTER:
        verdict = "running"
    else:
        verdict = "retire" if net <= 0 else "keep"
    return {"n": len(first), "of": RETIRE_AFTER, "net_rs": net, "verdict": verdict}


def tab_data(conn: sqlite3.Connection, date_clause: str = "", date_params=()) -> tuple:
    """(rows newest first, summaries, page stats) for the /labs/live tab."""
    cur = conn.execute(f"SELECT * FROM nifty_tuesday_put_trades WHERE 1=1 {date_clause} ORDER BY trade_date DESC",
                       tuple(date_params))
    cols = [c[0] for c in cur.description]
    rows = [dict(zip(cols, r)) for r in cur.fetchall()]
    if not rows:
        return rows, [], {}
    summaries = []
    for tag in ("research sample", "forward"):
        mine = [r for r in rows if r["sample"] == tag]
        summaries.append({"sample": tag, "headline": _summary(mine, "net_rs"), "research": _summary(mine, "research_net_rs"),
                          "skipped": sum(1 for r in mine if r["status"] == "skipped")})
    lo, hi = RESERVED_WINDOW
    days = sorted(r["trade_date"] for r in rows)
    have = set(days)
    probe, missing = date.fromisoformat(days[0]), []
    while probe.isoformat() <= days[-1]:
        if probe.isoformat() not in have:
            missing.append(probe.isoformat())           # a holiday, or a Tuesday the collector missed
        probe += timedelta(days=7)
    stats = {"first_date": days[0], "last_date": days[-1], "lots": LOTS, "qty": QTY, "sample_end": SAMPLE_END,
             "retirement": retirement(rows), "all": _summary(rows, "net_rs"),
             "open": next((r for r in rows if r["status"] == "open"), None),
             "skipped": [r for r in rows if r["status"] == "skipped"],
             "reserved_read": sorted(r["trade_date"] for r in rows if lo <= r["trade_date"] <= hi),
             "reserved_window": RESERVED_WINDOW, "no_data": missing}
    return rows, summaries, stats
