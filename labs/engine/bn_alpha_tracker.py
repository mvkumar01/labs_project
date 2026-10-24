"""BANKNIFTY Alpha paper book ("BN Alpha PC400 T30"). Paper only: it never calls a broker order API.

Handed over by the alphaIMB research session "New Alpha" (research/experiments/2026-10-22_banknifty_range,
HANDOFF_PAPER_BOOK.md, reference_book.py), asked for by Vipin on 2026-10-23 to run forward from
Mon 2026-10-26. Every decision is deterministic:

  range     c = round(prev_close / 100) * 100, strikes [c - 400, c + 400] inclusive, locked before the open.
            prev_close = the official previous-session close of NIFTY BANK (Kite daily candle; the research
            used the NSE F&O bhavcopy underlying close, which is the same number).
  alpha     the standard alphaIMB formula on the nearest BANKNIFTY monthly expiry alive today (on expiry
            day the expiring one): baseline = the previous session's settled OI (alphaIMB codeB
            prev_day_oi_BANKNIFTY_<prev session>.csv = NSE bhavcopy), OI read AT each 5-minute mark
            09:15..15:25 (never aggregated), delta = oi - baseline (a strike missing from the baseline
            counts 0; a strike with no row at a mark contributes 0),
            alpha = (sum dPE - sum dCE) * 100 / (sum dPE + sum dCE) over the range, 0 when the sum is 0.
  entry     flat only: CALL when alpha crosses above +30 (prev <= 30 < now), PUT when it crosses below -30.
            Entry spot = the 1-minute close of the mark minute. Marks with |alpha| > 200 are skipped for
            entries and alpha exits. Re-entry on the next fresh cross after an exit (not on the exit mark).
  exits     at each mark, in this order (puts mirrored):
            1. spot trail: once the best 1-min high since entry (as known BEFORE this mark's window) is
               >= entry + 100, stop = best - 50, checked on the 1-min lows of the minutes after the previous
               mark up to this mark; fill = min(stop, previous mark's spot)
            2. no spot target
            3. alpha >= +100 (target) or alpha <= -30 (stop) at the mark; fill = mark spot
            4. EOD at the 15:25 mark
  option    paper leg (not part of the research; proposed by the research session, to be confirmed by
            Vipin): the ATM strike (nearest the entry spot, 100-point step) of the same monthly expiry,
            CE for CALL / PE for PUT, 1 lot of LOT_SIZE, bought at the mark's ask, sold at the exit mark's
            bid, NSE option charges. Spot points are logged too: the research and its parity are in points.

Evidence status (from the handoff): in-sample, 115 sessions 2026-03-03 -> 2026-10-21, spot points, no costs:
+7,368.3 points, 63.5% of days profitable, 126 trades, worst day -383. The range width and the stop settings
were chosen on those days. Sessions from FIRST_UNSEEN are the real test.

Each session is independent (no carried state), so a day can be recomputed at any time; the whole day is
replayed on every run from the data stored so far (idempotent).
"""
from __future__ import annotations

import io
import sqlite3
import tarfile
from datetime import date, datetime, time, timedelta, timezone
from pathlib import Path

import numpy as np
import pandas as pd

from config.labs_config import ARCHIVE_DIR, DATA_DIR, SHARED_ARCHIVE_DIR, SHARED_LIVE_DIR, UNDERLYINGS
from labs.engine.charges import round_trip_charges
from market_data.expiry import expiry_sort_date
from market_data.shared_store import load_options_frame
from storage.db import get_conn

IST = timezone(timedelta(hours=5, minutes=30))
SYMBOL = "BANKNIFTY"
KITE_TOKEN = int(UNDERLYINGS[SYMBOL]["instrument_token"])          # NSE:NIFTY BANK
LOT_SIZE = 30                     # NSE BANKNIFTY option lot (Angel / NSE instrument list, Oct 2026)
LOTS = 1
STRIKE_STEP = 100
HALF_WIDTH = 400
ENTRY_TH, ALPHA_TGT, ALPHA_SL, ALPHA_SKIP = 30.0, 100.0, -30.0, 200.0
TRAIL_ARM, TRAIL_DIST = 100.0, 50.0
FIRST_MARK, LAST_MARK = time(9, 15), time(15, 25)
STRATEGY_VERSION = "bn_alpha_pc400_t30_tgt100_sl30_trail100_50_atm_monthly_v1"
PAPER_START = "2026-06-01"        # the first sessions the labs store holds BANKNIFTY quotes for
FIRST_UNSEEN = "2026-10-22"       # the first session after the research data (which ends 2026-10-21)
REFERENCE = {"trades": 126, "sessions": 115, "points": 7368.3, "green_pct": 63.5}


class BnAlphaInputError(RuntimeError):
    """A session's market data or baseline is missing; nothing is traded on invented inputs."""


def _alpha_base_dir() -> Path:
    for p in (Path("/home/mvkumar01/alphaIMB"), Path.home() / "alphaIMB"):
        if p.exists():
            return p
    return Path(__file__).resolve().parents[3] / "alphaIMB"


PREV_DAY_DIR = _alpha_base_dir() / "data" / "prev_day"


# ----------------------------------------------------------------- sources ---
# Module-level so a backfill or a test can point them elsewhere.
def options_frame(day: str) -> pd.DataFrame:
    try:
        return load_options_frame(SYMBOL, day, live_root=SHARED_LIVE_DIR, archive_root=SHARED_ARCHIVE_DIR)
    except FileNotFoundError as exc:
        raise BnAlphaInputError(f"No {SYMBOL} option quotes for {day}") from exc


def _read_spot_csv(source) -> dict:
    f = pd.read_csv(source)
    f["ts"] = pd.to_datetime(f["timestamp"].astype(str).str.slice(0, 19))
    f = f.drop_duplicates("ts", keep="last").sort_values("ts")
    return {t.to_pydatetime(): (float(o), float(h), float(l), float(c))
            for t, o, h, l, c in zip(f["ts"], f["open"], f["high"], f["low"], f["close"])}


def spot_bars(day: str) -> dict:
    """The collector's NIFTY BANK 1-min bars {minute: (o, h, l, c)}: live dir first, then the archive."""
    live = DATA_DIR / f"{day}_{SYMBOL}_spot_1min.csv"
    if live.is_file():
        return _read_spot_csv(live)
    tar = ARCHIVE_DIR / f"{day}.tar.gz"
    if tar.is_file():
        with tarfile.open(tar) as tf:
            m = next((x for x in tf.getmembers() if x.name.endswith(f"_{SYMBOL}_spot_1min.csv")), None)
            if m is not None:
                return _read_spot_csv(io.BytesIO(tf.extractfile(m).read()))
    return {}


def _previous_weekdays(day: str, limit: int = 10) -> list[str]:
    out, cur = [], date.fromisoformat(day)
    while len(out) < limit:
        cur -= timedelta(days=1)
        if cur.weekday() < 5:
            out.append(cur.isoformat())
    return out


def baseline(day: str, expiry_code: str) -> tuple[pd.DataFrame, str]:
    """The previous session's settled OI for the expiry's month: (strike, type, oi), baseline date."""
    exp = expiry_sort_date(expiry_code)
    for prev in _previous_weekdays(day):
        for name in (f"prev_day_oi_{SYMBOL}_{prev}.csv", f"prev_day_oi_{SYMBOL}_MONTHLY_{prev}.csv"):
            path = PREV_DAY_DIR / name
            if not path.is_file():
                continue
            f = pd.read_csv(path)
            if "symbol" in f.columns:
                f = f[f["symbol"].astype(str).str.upper() == SYMBOL]
            if "expiry" in f.columns and exp is not None:
                months = pd.to_datetime(f["expiry"].astype(str), errors="coerce")
                f = f[(months.dt.year == exp.year) & (months.dt.month == exp.month)]
            f = f.assign(type=f["type"].astype(str).str.lower(),
                         strike=pd.to_numeric(f["strike"], errors="coerce"),
                         oi=pd.to_numeric(f["oi"], errors="coerce")).dropna(subset=["strike", "oi"])
            f = f[f["type"].isin(["ce", "pe"])]
            if not f.empty:
                return f.groupby(["strike", "type"], as_index=False)["oi"].sum(), prev
    raise BnAlphaInputError(f"No settled {SYMBOL} baseline for {expiry_code} before {day} under {PREV_DAY_DIR}")


def prev_close(day: str, kite=None) -> tuple[float, str, str]:
    """(close, its session, source). The official close from Kite's daily candle; if Kite is not
    reachable, the collector's last 1-min close of the previous session (flagged)."""
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
    for prev in _previous_weekdays(day):
        bars = spot_bars(prev)
        if bars:
            return float(bars[max(bars)][3]), prev, "collector_last_1min"
    raise BnAlphaInputError(f"No previous {SYMBOL} close before {day}")


# ------------------------------------------------------------------ inputs ---
def nearest_expiry(codes, day: str) -> str | None:
    d0 = date.fromisoformat(day)
    alive = sorted((expiry_sort_date(c), c) for c in {str(x).upper() for x in codes}
                   if expiry_sort_date(c) is not None and expiry_sort_date(c) >= d0)
    return alive[0][1] if alive else None


def range_for(close: float, half_width: int = HALF_WIDTH, step: int = STRIKE_STEP) -> tuple[int, int]:
    c = int(round(close / step) * step)
    return c - half_width, c + half_width


def session_inputs(day: str, *, close: float, frame: pd.DataFrame, bars: dict, base: pd.DataFrame,
                   now: datetime | None = None, half_width: int = HALF_WIDTH, abs_den: bool = False,
                   step: int = STRIKE_STEP) -> dict:
    """Marks, alpha, mark spots, window highs/lows and the mark quotes for one session, from completed
    minutes only (a mark counts once its 1-min spot bar exists, and before `now`). `close` is the
    range centre before rounding (to `step`); `abs_den` divides by |sum dPE| + |sum dCE| (the SENSEX
    books)."""
    f = frame.copy()
    f["timestamp"] = pd.to_datetime(f["timestamp"])
    if f["timestamp"].dt.tz is not None:
        f["timestamp"] = f["timestamp"].dt.tz_convert(IST).dt.tz_localize(None)
    f["expiry"] = f["expiry"].astype(str).str.upper()
    expiry = nearest_expiry(f["expiry"].unique(), day)
    if expiry is None:
        raise BnAlphaInputError(f"No live {SYMBOL} expiry on {day}")
    f = f[(f["expiry"] == expiry) & (f["timestamp"].dt.minute % 5 == 0)].copy()
    f["minute"] = f["timestamp"].dt.floor("min")
    hm = f["minute"].dt.time
    f = f[(hm >= FIRST_MARK) & (hm <= LAST_MARK)]
    f["type"] = f["option_type"].astype(str).str.lower()
    f = f[f["type"].isin(["ce", "pe"])]
    f = f.sort_values("timestamp").groupby(["minute", "strike", "type"], as_index=False).last()
    lo, hi = range_for(close, half_width, step)
    bmap = {(float(s), t): float(o) for s, t, o in zip(base["strike"], base["type"], base["oi"])}
    cutoff = None if now is None else (now.replace(tzinfo=None) if now.tzinfo is None
                                       else now.astimezone(IST).replace(tzinfo=None))
    marks = []
    for m in sorted(f["minute"].unique()):
        m = pd.Timestamp(m).to_pydatetime()
        if m not in bars:
            continue
        if cutoff is not None and m + timedelta(minutes=1) > cutoff:
            continue
        marks.append(m)
    inr = f[(f["strike"] >= lo) & (f["strike"] <= hi)]
    alpha, spot, wh, wl = [], [], [], []
    by_min = {pd.Timestamp(k).to_pydatetime(): g for k, g in inr.groupby("minute")}
    minutes = sorted(bars)
    for j, m in enumerate(marks):
        g = by_min.get(m)
        dce = dpe = 0.0
        if g is not None:
            base_v = np.array([bmap.get((float(s), t), 0.0) for s, t in zip(g["strike"], g["type"])])
            delta = g["oi"].to_numpy(float) - base_v
            is_pe = (g["type"] == "pe").to_numpy()
            dpe, dce = float(delta[is_pe].sum()), float(delta[~is_pe].sum())
        den = (abs(dpe) + abs(dce)) if abs_den else (dpe + dce)
        alpha.append((dpe - dce) * 100.0 / den if den != 0 else 0.0)
        spot.append(bars[m][3])
        start = marks[j - 1] + timedelta(minutes=1) if j else m
        win = [bars[t] for t in minutes if start <= t <= m]
        wh.append(max(b[1] for b in win) if win else np.nan)
        wl.append(min(b[2] for b in win) if win else np.nan)
    quotes = {(pd.Timestamp(r.minute).to_pydatetime(), float(r.strike), r.type):
              (r.bid, r.ask, getattr(r, "tradingsymbol", None)) for r in f.itertuples(index=False)}
    return {"day": day, "expiry": expiry, "range_lo": lo, "range_hi": hi, "marks": marks,
            "alpha": np.array(alpha, float), "spot": np.array(spot, float),
            "win_high": np.array(wh, float), "win_low": np.array(wl, float), "quotes": quotes,
            # a past session is over even when its data stops before 15:25 (closed at its last mark)
            "session_done": bool(marks) and (now is None or marks[-1].time() >= LAST_MARK)}


# -------------------------------------------------------------------- rule ---
def simulate(inp: dict, arm: float = TRAIL_ARM, dist: float = TRAIL_DIST) -> tuple[list[dict], dict | None]:
    """The reference rule (reference_book.run_day) on one session. Returns (closed trades, open trade):
    the last mark closes an open trade at EOD only once the session is done."""
    alpha, spot, wh, wl, marks = inp["alpha"], inp["spot"], inp["win_high"], inp["win_low"], inp["marks"]
    trades, pos, esp, best, ei = [], 0, 0.0, 0.0, 0

    def record(i, px, reason):
        return dict(side="CALL" if pos == 1 else "PUT", entry_ts=marks[ei], entry_spot=float(esp),
                    entry_alpha=float(alpha[ei]), exit_ts=marks[i], exit_spot=float(px), exit_alpha=float(alpha[i]),
                    exit_reason=reason, spot_pts=round(float((px - esp) * pos), 2), entry_i=ei, exit_i=i)

    for i in range(1, len(alpha)):
        if pos:
            armed = (best - esp) * pos >= arm
            stop = best - dist * pos if armed else None
            if stop is not None and ((pos == 1 and wl[i] <= stop) or (pos == -1 and wh[i] >= stop)):
                px = min(stop, spot[i - 1]) if pos == 1 else max(stop, spot[i - 1])
                trades.append(record(i, px, "TRAIL"))
                pos = 0
                continue
            best = max(best, wh[i]) if pos == 1 else min(best, wl[i])
            ca = alpha[i]
            if abs(ca) <= ALPHA_SKIP:
                if (pos == 1 and ca >= ALPHA_TGT) or (pos == -1 and ca <= -ALPHA_TGT):
                    trades.append(record(i, spot[i], "ALPHA_TGT"))
                    pos = 0
                elif (pos == 1 and ca <= ALPHA_SL) or (pos == -1 and ca >= -ALPHA_SL):
                    trades.append(record(i, spot[i], "ALPHA_SL"))
                    pos = 0
            continue
        pa, ca = alpha[i - 1], alpha[i]
        if abs(ca) > ALPHA_SKIP:
            continue
        if pa <= ENTRY_TH < ca:
            pos, esp, best, ei = 1, spot[i], spot[i], i
        elif pa >= -ENTRY_TH > ca:
            pos, esp, best, ei = -1, spot[i], spot[i], i
    if pos and len(alpha):
        last = len(alpha) - 1
        if inp["session_done"]:
            trades.append(record(last, spot[last], "EOD"))
            return trades, None
        return trades, dict(record(last, spot[last], "open"), status="open")
    return trades, None


def price_trade(t: dict, inp: dict, lot_size: int = LOT_SIZE, lots: int = LOTS, charges_fn=None,
                step: int = STRIKE_STEP) -> dict:
    """The paper option leg: ATM strike of the session's expiry, ask in, bid out."""
    charges_fn = charges_fn or round_trip_charges
    typ = "ce" if t["side"] == "CALL" else "pe"
    strike = float(round(t["entry_spot"] / step) * step)
    q_in = inp["quotes"].get((t["entry_ts"], strike, typ))
    q_out = inp["quotes"].get((t["exit_ts"], strike, typ))
    qty = lot_size * lots
    row = dict(t, strike=int(strike), tradingsymbol=(q_in or q_out or (None, None, None))[2],
               entry_ask=None, exit_bid=None, option_pts=None, gross_rs=None, charges_rs=None, net_rs=None,
               qty=qty, quote_status="priced")
    ask = q_in[1] if q_in and q_in[1] and q_in[1] > 0 else None
    bid = q_out[0] if q_out and q_out[0] and q_out[0] > 0 else None
    row["entry_ask"], row["exit_bid"] = ask, bid
    if ask is None or bid is None:
        row["quote_status"] = "no_entry_ask" if ask is None else "no_exit_bid"
        return row
    pts = bid - ask
    charges = charges_fn(ask, bid, qty)["total"]
    row.update(option_pts=round(pts, 2), gross_rs=round(pts * qty, 2), charges_rs=round(charges, 2),
               net_rs=round(pts * qty - charges, 2))
    return row


# ------------------------------------------------------------------ ledger ---
def _ensure_tables(conn: sqlite3.Connection, prefix: str = "bn_alpha") -> None:
    conn.executescript(
        f"""
        CREATE TABLE IF NOT EXISTS {prefix}_daily (
            trade_date TEXT PRIMARY KEY, status TEXT NOT NULL, expiry_code TEXT, prev_close REAL,
            prev_close_date TEXT, prev_close_source TEXT, range_lo INTEGER, range_hi INTEGER, baseline_date TEXT,
            n_marks INTEGER, latest_mark TEXT, latest_alpha REAL, latest_spot REAL, n_trades INTEGER NOT NULL DEFAULT 0,
            spot_pnl_pts REAL, gross_rs REAL, charges_rs REAL, net_rs REAL, qty INTEGER, strategy_version TEXT NOT NULL,
            error TEXT, updated_at TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS {prefix}_trades (
            trade_date TEXT NOT NULL, seq INTEGER NOT NULL, status TEXT NOT NULL, side TEXT NOT NULL,
            entry_ts TEXT NOT NULL, exit_ts TEXT, entry_spot REAL, exit_spot REAL, entry_alpha REAL, exit_alpha REAL,
            spot_pnl_pts REAL, exit_reason TEXT, strike INTEGER, tradingsymbol TEXT, entry_ask REAL, exit_bid REAL,
            option_pts REAL, gross_rs REAL, charges_rs REAL, net_rs REAL, qty INTEGER, quote_status TEXT,
            PRIMARY KEY (trade_date, seq)
        );
        """
    )
    conn.commit()


def replay_day(day: str, *, now: datetime | None = None, kite=None, known_close: tuple | None = None) -> dict:
    """Read the session's data and replay it. Pure: writes nothing."""
    close, close_date, close_source = known_close or prev_close(day, kite)
    frame = options_frame(day)
    expiry = nearest_expiry(frame["expiry"].astype(str).str.upper().unique(), day)
    if expiry is None:
        raise BnAlphaInputError(f"No live {SYMBOL} expiry on {day}")
    base, base_date = baseline(day, expiry)
    bars = spot_bars(day)
    if not bars:
        raise BnAlphaInputError(f"No {SYMBOL} 1-min spot bars for {day}")
    inp = session_inputs(day, close=close, frame=frame, bars=bars, base=base, now=now)
    closed, open_ = simulate(inp)
    trades = [dict(price_trade(t, inp), status="closed") for t in closed]
    if open_:
        trades.append(dict(price_trade(open_, inp), status="open"))
    return {"inp": inp, "trades": trades, "prev_close": close, "prev_close_date": close_date,
            "prev_close_source": close_source, "baseline_date": base_date}


def _save(conn: sqlite3.Connection, day: str, r: dict, error: str | None = None, *, prefix: str = "bn_alpha",
          version: str = STRATEGY_VERSION, qty: int = LOT_SIZE * LOTS, note: str | None = None) -> None:
    stamp = datetime.now(IST).isoformat(timespec="seconds")
    conn.execute(f"DELETE FROM {prefix}_trades WHERE trade_date=?", (day,))
    if error:
        conn.execute(f"INSERT OR REPLACE INTO {prefix}_daily (trade_date,status,n_trades,strategy_version,error,updated_at) "
                     "VALUES (?,?,?,?,?,?)", (day, "unavailable", 0, version, error[:300], stamp))
        conn.commit()
        return
    inp, trades = r["inp"], r["trades"]
    fmt = lambda ts: ts.isoformat(timespec="minutes") if ts is not None else None   # noqa: E731
    for seq, t in enumerate(trades, 1):
        conn.execute(
            f"INSERT INTO {prefix}_trades (trade_date,seq,status,side,entry_ts,exit_ts,entry_spot,exit_spot,entry_alpha,"
            "exit_alpha,spot_pnl_pts,exit_reason,strike,tradingsymbol,entry_ask,exit_bid,option_pts,gross_rs,charges_rs,"
            "net_rs,qty,quote_status) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (day, seq, t["status"], t["side"], fmt(t["entry_ts"]), fmt(t["exit_ts"]) if t["status"] == "closed" else None,
             t["entry_spot"], t["exit_spot"], round(t["entry_alpha"], 2), round(t["exit_alpha"], 2), t["spot_pts"],
             t["exit_reason"] if t["status"] == "closed" else "holding", t["strike"], t["tradingsymbol"], t["entry_ask"],
             t["exit_bid"], t["option_pts"], t["gross_rs"], t["charges_rs"], t["net_rs"], t["qty"], t["quote_status"]))
    closed = [t for t in trades if t["status"] == "closed"]
    status = "live" if not inp["session_done"] else ("traded" if trades else "no_trade")
    if any(t["quote_status"] != "priced" for t in closed):
        status = "partial_quotes" if inp["session_done"] else status
    last = len(inp["marks"]) - 1
    conn.execute(
        f"INSERT OR REPLACE INTO {prefix}_daily (trade_date,status,expiry_code,prev_close,prev_close_date,"
        "prev_close_source,range_lo,range_hi,baseline_date,n_marks,latest_mark,latest_alpha,latest_spot,n_trades,"
        "spot_pnl_pts,gross_rs,charges_rs,net_rs,qty,strategy_version,error,updated_at) "
        "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (day, status, inp["expiry"], r["prev_close"], r["prev_close_date"], r["prev_close_source"], inp["range_lo"],
         inp["range_hi"], r["baseline_date"], len(inp["marks"]), fmt(inp["marks"][last]) if last >= 0 else None,
         round(float(inp["alpha"][last]), 2) if last >= 0 else None, float(inp["spot"][last]) if last >= 0 else None,
         len(trades), round(sum(t["spot_pts"] for t in closed), 2),
         round(sum(t["gross_rs"] or 0 for t in closed), 2), round(sum(t["charges_rs"] or 0 for t in closed), 2),
         round(sum(t["net_rs"] or 0 for t in closed), 2), qty, version, note, stamp))
    conn.commit()


def run_day(trade_date: str | None = None, *, now: datetime | None = None, kite=None, persist: bool = True,
            connection: sqlite3.Connection | None = None) -> dict:
    now = now or datetime.now(IST)
    day = trade_date or now.astimezone(IST).date().isoformat()
    own = connection is None
    conn = connection or get_conn()
    _ensure_tables(conn)
    try:
        row = conn.execute("SELECT prev_close, prev_close_date, prev_close_source FROM bn_alpha_daily "
                           "WHERE trade_date=? AND prev_close IS NOT NULL", (day,)).fetchone()
        known = tuple(row) if row and row[2] == "kite_daily" else None     # the range is locked once known
        try:
            r = replay_day(day, now=now if day == now.astimezone(IST).date().isoformat() else None,
                           kite=kite, known_close=known)
        except BnAlphaInputError as exc:
            if persist:
                _save(conn, day, {}, error=str(exc))
            return {"trade_date": day, "status": "unavailable", "error": str(exc)}
        if persist:
            _save(conn, day, r)
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
    """The paper loop's entry: replay today whenever a new 5-minute mark has completed, and once
    after the close. Weekends and hours outside the session do nothing."""
    now = (now or datetime.now(IST)).astimezone(IST)
    if now.weekday() >= 5 or not (time(9, 16) <= now.time() <= time(15, 45)):
        return None
    day = now.date().isoformat()
    conn = get_conn()
    try:
        _ensure_tables(conn)
        row = conn.execute("SELECT latest_mark, status FROM bn_alpha_daily WHERE trade_date=?", (day,)).fetchone()
    finally:
        conn.close()
    newest = (now - timedelta(seconds=70)).replace(second=0, microsecond=0, tzinfo=None)
    newest -= timedelta(minutes=newest.minute % 5)
    newest = min(newest, datetime.combine(now.date(), LAST_MARK))
    if row and row[1] not in ("unavailable",) and row[0] and row[0] >= newest.isoformat(timespec="minutes"):
        return None
    return run_day(day, now=now)


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
        out = run_day(day, kite=kite)            # past sessions replay whole, closed at their last mark
        (failed.__setitem__(day, out["error"]) if out.get("error") else done.append(day))
    return {"done": len(done), "failed": failed}


# ---------------------------------------------------------------- dashboard ---
def tab_data(conn: sqlite3.Connection, date_clause: str = "", date_params=(), *, prefix: str = "bn_alpha",
             first_unseen: str | None = None, extra: dict | None = None) -> tuple:
    """(day rows newest first, trades newest first, stats) for the /labs/live tab."""
    first_unseen = first_unseen or FIRST_UNSEEN
    cur = conn.execute(f"SELECT * FROM {prefix}_daily WHERE 1=1 {date_clause} ORDER BY trade_date DESC LIMIT 400",
                       tuple(date_params))
    rows = [dict(zip([c[0] for c in cur.description], r)) for r in cur.fetchall()]
    cur = conn.execute(f"SELECT * FROM {prefix}_trades WHERE 1=1 {date_clause} ORDER BY trade_date DESC, seq DESC",
                       tuple(date_params))
    trades = [dict(zip([c[0] for c in cur.description], r)) for r in cur.fetchall()]
    if not rows:
        return rows, trades, {}
    closed = [t for t in trades if t["status"] == "closed"]
    days = [r for r in rows if r["status"] in ("traded", "no_trade", "partial_quotes")]
    traded = [r for r in days if r["n_trades"]]
    net = [float(r["net_rs"] or 0) for r in reversed(days)]
    eq, peak, dd = 0.0, 0.0, 0.0
    for v in net:
        eq += v
        peak = max(peak, eq)
        dd = max(dd, peak - eq)
    unseen = [t for t in closed if t["trade_date"] >= first_unseen]
    stats = {
        "sessions": len(days), "traded_days": len(traded), "trades": len(closed),
        "spot_pts": round(sum(float(t["spot_pnl_pts"] or 0) for t in closed), 1),
        "green_pts_days": sum(1 for r in traded if float(r["spot_pnl_pts"] or 0) > 0),
        "net_rs": round(sum(float(t["net_rs"] or 0) for t in closed), 0),
        "charges_rs": round(sum(float(t["charges_rs"] or 0) for t in closed), 0),
        "unpriced": sum(1 for t in closed if t["quote_status"] != "priced"),
        "max_dd_rs": round(dd, 0), "worst_day_rs": round(min(net), 0) if net else 0,
        "unseen_trades": len(unseen), "unseen_pts": round(sum(float(t["spot_pnl_pts"] or 0) for t in unseen), 1),
        "unseen_net": round(sum(float(t["net_rs"] or 0) for t in unseen), 0),
        "exit_reasons": {k: sum(1 for t in closed if t["exit_reason"] == k) for k in ("TRAIL", "ALPHA_TGT", "ALPHA_SL", "EOD")},
        "latest": rows[0], "open_trades": [t for t in trades if t["status"] == "open"],
        "first_unseen": first_unseen, "paper_start": PAPER_START, "lot_size": LOT_SIZE, "lots": LOTS,
        "half_width": HALF_WIDTH, "reference": REFERENCE, **(extra or {}),
    }
    return rows, trades, stats


if __name__ == "__main__":
    import json
    import sys
    print(json.dumps(run_backfill(*(sys.argv[1:3])), indent=1, default=str))


def run_paper_day(trade_date: str | None = None) -> dict | None:
    """The paper loop's in-session entry (it passes today's date): see run_live."""
    return run_live(datetime.now(IST))
