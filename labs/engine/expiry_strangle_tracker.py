"""Far-OTM expiry strangle on NIFTY and SENSEX: paper book.

Paper only. This module never calls a broker order API (Kite is used for daily index candles only).

From the V2 Strategy Tester (2026-10-19, asked by Vipin; research/scripts/expiry_sale_bidask.py). The
style of the most consistent sellers: sell far out-of-the-money options into every expiry. Rules, fixed:

  events   every NIFTY weekly expiry (Tuesday) and SENSEX weekly expiry (Thursday) - the day the contract
           actually expires, holidays included (an event is the expiring contract's last session; its
           entry day is the last session with quotes before it)
  entry    15:25 on that session (its snapshot, or the last one before it with a bid): sell 1 lot of the
           call and 1 lot of the put of the expiring contract
  strikes  call nearest spot x (1 + k sigma), put nearest spot x (1 - k sigma), among strikes with a bid;
           spot = the index in the 15:25 snapshot; sigma = sd of the last 20 daily close-to-close log
           returns of the index, ending at the previous session's close (known at entry)
  fills    sold at the BID, bought back at the ASK (the "mid" version uses (bid + ask) / 2 both ways)
  exit     15:25 on the expiry day at the ask; a leg with no ask settles at its intrinsic value on the
           expiry day's official close
  tail     on the expiry day from 09:15, the first snapshot whose pair ask (call ask + put ask) reaches
           3 x the entry credit at the bid: both legs bought back there. An overnight gap is not
           protected - it shows as it opens.
  books    k2 (k = 2, tail rule) - the book; shadows k3 (k = 3, tail rule) and k2_notail
  costs    the short-option charge model a leg (NSE schedule for NIFTY, BSE for SENSEX); 1 current lot
  margin   approximate: 10% of the index notional (spot x lot), the labs short-premium capital model

Evidence (research): NSE daily files 2019-2026, 806 expiries on 4 indices, the 2-sigma strangle made
+2.7 bps of notional an event (t 2.4), 96% up; on bid/ask Apr-Sep 2026 NIFTY +3.3 bps (t 4.1), SENSEX
about zero. The price is the tail: one bad expiry cost 3-6% of notional in 2020. Retirement, stated
2026-10-19 before the forward period: "retire" if one forward event loses more than 1.5% of the
strangle's underlying notional, or if the first 26 forward events net to zero or less after charges.

Data: the labs option archive (1-minute snapshots with bid and ask) - on the server from 1 June 2026, so
the backfill starts with the first expiries after it; earlier quotes have no bid/ask.
"""
from __future__ import annotations

import calendar
import math
import sqlite3
from datetime import date, datetime, time, timedelta, timezone

import numpy as np
import pandas as pd

from config.labs_config import SHARED_ARCHIVE_DIR, SHARED_LIVE_DIR, UNDERLYINGS
from labs.engine.charges import sensex_round_trip_charges, short_option_round_trip_charges
from market_data.expiry import expiry_sort_date, is_monthly_expiry
from market_data.shared_store import load_options_frame, resolve_options_source
from storage.db import get_conn

IST = timezone(timedelta(hours=5, minutes=30))
STRATEGY_VERSION = "expiry_strangle_2sigma_v1"
ENTRY_TIME = EXIT_TIME = time(15, 25)
TAIL_FROM = time(9, 15)
ENTRY_WAIT_MIN = 10                     # the last snapshot with a bid up to this long before 15:25
TAIL_MULTIPLE = 3.0
SIGMA_DAYS = 20
MARGIN_RATE = 0.10
FORWARD_FROM = "2026-10-19"             # entries from this session on are the forward period
RETIRE_LOSS = 0.015                     # of the strangle's underlying notional
RETIRE_AFTER = 26
RESERVED = {"NIFTY": ("2026-09-29", "2026-10-27")}       # the Strategy Tester's reserved NIFTY expiries
VARIANTS = {"k2": (2.0, True), "k3": (3.0, True), "k2_notail": (2.0, False)}
FILLS = ("bidask", "mid")
PRIMARY = ("k2", "bidask")
UNDS = {
    "NIFTY": {"token": 256265, "exchange": "NSE", "weekday": 1, "start": "2026-06-01"},
    "SENSEX": {"token": 265, "exchange": "BSE", "weekday": 3, "start": "2026-06-01"},
}


class StrangleInputError(RuntimeError):
    """Inputs the book needs are unavailable."""


def lot_size(und: str) -> int:
    return int(UNDERLYINGS[und]["lot_size"])


# ------------------------------------------------------------------- schema ---
def _ensure_tables(conn: sqlite3.Connection) -> None:
    conn.executescript(
        """
        CREATE TABLE IF NOT EXISTS expiry_strangle_events (
            und TEXT NOT NULL, expiry TEXT NOT NULL, variant TEXT NOT NULL, fill TEXT NOT NULL,
            entry_date TEXT, status TEXT NOT NULL, reason TEXT, k REAL, tail INTEGER,
            spot0 REAL, parity_spot0 REAL, sigma REAL, call_strike REAL, put_strike REAL,
            call_in REAL, put_in REAL, credit REAL, entry_minute TEXT,
            call_out REAL, put_out REAL, buyback REAL, exit_minute TEXT, exit_reason TEXT, close_exit REAL,
            worst_pair REAL, worst_multiple REAL, worst_minute TEXT,
            qty INTEGER, notional_rs REAL, margin_rs REAL, gross_rs REAL, charges_rs REAL, net_rs REAL,
            net_bps REAL, return_on_margin REAL, period TEXT, reserved INTEGER, strategy_version TEXT, updated_at TEXT,
            PRIMARY KEY (und, expiry, variant, fill)
        );
        CREATE TABLE IF NOT EXISTS expiry_strangle_closes (
            und TEXT NOT NULL, trade_date TEXT NOT NULL, close REAL NOT NULL, source TEXT, fetched_at TEXT,
            PRIMARY KEY (und, trade_date)
        );
        CREATE TABLE IF NOT EXISTS expiry_strangle_state (k TEXT PRIMARY KEY, v TEXT);
        """
    )
    conn.commit()


# ------------------------------------------------------------- expiry dates ---
def expiry_date_of(code: str, und: str) -> date | None:
    """The day a contract code expires: weekly codes carry it; a monthly code is the month's last
    expiry weekday."""
    key = expiry_sort_date(str(code).strip().upper())
    if key is None:
        return None
    if not is_monthly_expiry(code):
        return key
    last = date(key.year, key.month, calendar.monthrange(key.year, key.month)[1])
    return last - timedelta(days=(last.weekday() - UNDS[und]["weekday"]) % 7)


def sessions(und: str, start: str, end: str) -> list[str]:
    """Weekdays with an option file for `und`, oldest first."""
    found = set()
    for root in (SHARED_ARCHIVE_DIR, SHARED_LIVE_DIR):
        if root.exists():
            for p in root.iterdir():
                if p.is_dir() and len(p.name) == 10 and start <= p.name <= end:
                    try:
                        if date.fromisoformat(p.name).weekday() < 5:
                            found.add(p.name)
                    except ValueError:
                        pass
    out = []
    for s in sorted(found):
        try:
            resolve_options_source(und, s, live_root=SHARED_LIVE_DIR, archive_root=SHARED_ARCHIVE_DIR)
            out.append(s)
        except FileNotFoundError:
            pass
    return out


def frame(und: str, day: str) -> pd.DataFrame:
    try:
        f = load_options_frame(und, day, live_root=SHARED_LIVE_DIR, archive_root=SHARED_ARCHIVE_DIR)
    except Exception as exc:
        raise StrangleInputError(f"{und} quotes for {day}: {type(exc).__name__}: {exc}") from exc
    f = f.copy()
    ts = pd.to_datetime(f["timestamp"], errors="coerce")
    if ts.dt.tz is not None:
        ts = ts.dt.tz_convert("Asia/Kolkata").dt.tz_localize(None)
    f["minute"] = ts.dt.floor("min")
    f["expiry"] = f["expiry"].astype(str).str.upper()
    f["cp"] = f["option_type"].astype(str).str.upper().str[0]
    for c in ("spot", "strike", "ltp", "bid", "ask"):
        f[c] = pd.to_numeric(f[c], errors="coerce")
    return f.dropna(subset=["minute", "strike"])


def events(und: str, start: str, end: str) -> list[tuple[str, str, str]]:
    """(expiry day, entry day, contract code): every session that is the last for its nearest contract,
    with the session before it."""
    days = sessions(und, start, end)
    out = []
    for i, day in enumerate(days[1:], 1):
        f = frame(und, day)
        codes = {c: expiry_date_of(c, und) for c in f["expiry"].unique()}
        today = [c for c, d in codes.items() if d is not None and d.isoformat() == day]
        if today:
            out.append((day, days[i - 1], today[0]))
    return out


# ------------------------------------------------------------- daily closes ---
def update_closes(conn: sqlite3.Connection, kite, today: date, start: str = "2026-03-01") -> int:
    """Kite daily candles of both indices from a week before the last stored close (a day is stored
    once it is over)."""
    n, stamp = 0, datetime.now(IST).isoformat(timespec="seconds")
    for und, cfg in UNDS.items():
        last = conn.execute("SELECT MAX(trade_date) FROM expiry_strangle_closes WHERE und=?", (und,)).fetchone()[0]
        frm = date.fromisoformat(last) - timedelta(days=7) if last else date.fromisoformat(start)
        rows = kite.historical_data(cfg["token"], datetime.combine(frm, time(0, 0)), datetime.combine(today, time(23, 59)), "day")
        now = datetime.now(IST).replace(tzinfo=None)
        out = [(und, pd.Timestamp(c["date"]).date().isoformat(), float(c["close"]), f"kite {cfg['exchange']} daily", stamp)
               for c in rows if pd.Timestamp(c["date"]).date() < now.date() or now.time() >= time(15, 35)]
        conn.executemany("INSERT OR REPLACE INTO expiry_strangle_closes (und,trade_date,close,source,fetched_at) VALUES (?,?,?,?,?)", out)
        n += len(out)
    conn.commit()
    return n


def closes(conn: sqlite3.Connection, und: str) -> pd.Series:
    r = conn.execute("SELECT trade_date, close FROM expiry_strangle_closes WHERE und=? ORDER BY trade_date", (und,)).fetchall()
    return pd.Series([c for _, c in r], index=pd.Index([d for d, _ in r], dtype=object), dtype=float)


def sigma_before(close: pd.Series, entry_day: str) -> float:
    """sd of the last SIGMA_DAYS daily log returns ending at the close before `entry_day`."""
    c = close[close.index < entry_day]
    if len(c) < SIGMA_DAYS + 1:
        return float("nan")
    r = np.log(c.iloc[-(SIGMA_DAYS + 1):]).diff().dropna()
    return float(r.std())


# ------------------------------------------------------------------- replay ---
def _grid(f: pd.DataFrame, code: str) -> dict:
    g = f[f["expiry"] == code].drop_duplicates(["minute", "strike", "cp"], keep="last")
    out = {}
    for cp in ("C", "P"):
        x = g[g["cp"] == cp]
        out[cp] = {name: x.pivot(index="minute", columns="strike", values=name).sort_index() for name in ("bid", "ask", "ltp")}
    out["spot"] = g.groupby("minute")["spot"].median().sort_index()
    return out


def _parity(grid: dict, minute) -> float:
    def mids(cp):
        b, a = grid[cp]["bid"].loc[minute], grid[cp]["ask"].loc[minute]
        return ((b + a) / 2).where((b > 0) & (a >= b))
    try:
        c, p = mids("C"), mids("P")
    except KeyError:
        return float("nan")
    k = c.dropna().index.intersection(p.dropna().index)
    if not len(k):
        return float("nan")
    best = k[np.argmin(np.abs(c[k].to_numpy() - p[k].to_numpy()))]
    return float(c[best] - p[best] + best)


def _charges(und: str, sold: float, bought: float, qty: int) -> float:
    if und == "SENSEX":           # the BSE schedule: STT on the sell (here the opening leg), stamp on the buy
        return float(sensex_round_trip_charges(bought, sold, qty)["raw_total"])
    return float(short_option_round_trip_charges(sold, bought, qty)["raw_total"])


def simulate(und: str, expiry: str, entry_day: str, code: str, close: pd.Series) -> list[dict]:
    """Every variant x fill of one event."""
    qty = lot_size(und)
    f0, f1 = frame(und, entry_day), frame(und, expiry)
    g0, g1 = _grid(f0, code), _grid(f1, code)
    base = {"und": und, "expiry": expiry, "entry_date": entry_day, "qty": qty, "strategy_version": STRATEGY_VERSION,
            "period": "forward" if entry_day >= FORWARD_FROM else "research",
            "reserved": int(und in RESERVED and RESERVED[und][0] <= expiry <= RESERVED[und][1])}
    sig = sigma_before(close, entry_day)
    t_entry = pd.Timestamp(datetime.combine(date.fromisoformat(entry_day), ENTRY_TIME))
    t_exit = pd.Timestamp(datetime.combine(date.fromisoformat(expiry), EXIT_TIME))
    t_tail = pd.Timestamp(datetime.combine(date.fromisoformat(expiry), TAIL_FROM))
    close_exit = float(close.get(expiry, np.nan))
    rows = []

    def skip(variant, fill, reason, **more):
        rows.append({**base, "variant": variant, "fill": fill, "k": VARIANTS[variant][0], "tail": int(VARIANTS[variant][1]),
                     "status": "skipped", "reason": reason, "sigma": sig, **more})

    minutes0 = g0["spot"].index[(g0["spot"].index <= t_entry) & (g0["spot"].index >= t_entry - pd.Timedelta(minutes=ENTRY_WAIT_MIN))]
    if not np.isfinite(sig) or not len(minutes0):
        for v in VARIANTS:
            for fl in FILLS:
                skip(v, fl, "no sigma (too few daily closes)" if not np.isfinite(sig) else "no snapshot near 15:25 on the entry day")
        return rows
    m0 = minutes0[-1]
    spot0 = float(g0["spot"].loc[m0])
    parity0 = _parity(g0, m0)
    for variant, (k, tail) in VARIANTS.items():
        legs = {}
        for cp, target in (("C", spot0 * (1 + k * sig)), ("P", spot0 * (1 - k * sig))):
            bid = g0[cp]["bid"].loc[m0] if m0 in g0[cp]["bid"].index else pd.Series(dtype=float)
            ask = g0[cp]["ask"].loc[m0] if m0 in g0[cp]["ask"].index else pd.Series(dtype=float)
            ok = bid[(bid > 0) & (ask.reindex(bid.index) >= bid)].index
            if not len(ok):
                break
            strike = float(ok[np.argmin(np.abs(ok.to_numpy() - target))])
            legs[cp] = {"strike": strike, "bid": float(bid[strike]), "ask": float(ask[strike])}
        if len(legs) < 2:
            for fl in FILLS:
                skip(variant, fl, "no quoted strike on one side at entry", spot0=spot0)
            continue
        credit_bid = legs["C"]["bid"] + legs["P"]["bid"]
        # the expiry day: both legs' asks minute by minute (a missing ask carries the last one)
        day = {}
        for cp in ("C", "P"):
            s = legs[cp]["strike"]
            a = g1[cp]["ask"][s] if s in g1[cp]["ask"].columns else pd.Series(dtype=float)
            b = g1[cp]["bid"][s] if s in g1[cp]["bid"].columns else pd.Series(dtype=float)
            day[cp] = (a.where(a > 0), b.where(b > 0))
        idx = g1["spot"].index[(g1["spot"].index >= t_tail) & (g1["spot"].index <= t_exit)]
        ask_c, ask_p = day["C"][0].reindex(idx).ffill(), day["P"][0].reindex(idx).ffill()
        pair = ask_c + ask_p
        trigger = None
        if tail and len(pair.dropna()):
            hit = pair[pair >= TAIL_MULTIPLE * credit_bid].dropna()
            trigger = hit.index[0] if len(hit) else None
        exit_at = trigger if trigger is not None else (idx[-1] if len(idx) else None)
        window = pair[pair.index <= exit_at] if exit_at is not None else pair.iloc[0:0]
        worst = window.dropna()
        for fill in FILLS:
            if fill == "bidask":
                sold = {cp: legs[cp]["bid"] for cp in legs}
            else:
                sold = {cp: (legs[cp]["bid"] + legs[cp]["ask"]) / 2 for cp in legs}
            out_px, how = {}, []
            for cp in ("C", "P"):
                strike = legs[cp]["strike"]
                a = day[cp][0].get(exit_at, np.nan) if exit_at is not None else np.nan
                b = day[cp][1].get(exit_at, np.nan) if exit_at is not None else np.nan
                if np.isfinite(a) and a > 0:
                    out_px[cp] = a if fill == "bidask" else ((a + b) / 2 if np.isfinite(b) and b > 0 and a >= b else a)
                else:
                    if not np.isfinite(close_exit):
                        break
                    out_px[cp] = max(close_exit - strike, 0.0) if cp == "C" else max(strike - close_exit, 0.0)
                    how.append(f"{cp} settled at intrinsic")
            if len(out_px) < 2:
                skip(variant, fill, "no ask and no official close on the expiry day", spot0=spot0)
                continue
            credit = sold["C"] + sold["P"]
            buyback = out_px["C"] + out_px["P"]
            gross = (credit - buyback) * qty
            charges = _charges(und, sold["C"], out_px["C"], qty) + _charges(und, sold["P"], out_px["P"], qty)
            net = gross - charges
            notional = spot0 * qty
            margin = notional * MARGIN_RATE
            rows.append({**base, "variant": variant, "fill": fill, "k": k, "tail": int(tail), "status": "closed",
                         "reason": "; ".join(how) or None, "spot0": spot0, "parity_spot0": parity0, "sigma": sig,
                         "call_strike": legs["C"]["strike"], "put_strike": legs["P"]["strike"],
                         "call_in": sold["C"], "put_in": sold["P"], "credit": credit,
                         "entry_minute": m0.isoformat(), "call_out": out_px["C"], "put_out": out_px["P"], "buyback": buyback,
                         "exit_minute": exit_at.isoformat() if exit_at is not None else None,
                         "exit_reason": "tail (pair ask >= 3x credit)" if trigger is not None else "15:25 on expiry day",
                         "close_exit": close_exit,
                         "worst_pair": float(worst.max()) if len(worst) else None,
                         "worst_multiple": float(worst.max() / credit_bid) if len(worst) and credit_bid > 0 else None,
                         "worst_minute": worst.idxmax().isoformat() if len(worst) else None,
                         "notional_rs": notional, "margin_rs": margin, "gross_rs": gross, "charges_rs": charges,
                         "net_rs": net, "net_bps": net / notional * 1e4, "return_on_margin": net / margin})
    return rows


COLUMNS = ["und", "expiry", "variant", "fill", "entry_date", "status", "reason", "k", "tail", "spot0", "parity_spot0",
           "sigma", "call_strike", "put_strike", "call_in", "put_in", "credit", "entry_minute", "call_out", "put_out",
           "buyback", "exit_minute", "exit_reason", "close_exit", "worst_pair", "worst_multiple", "worst_minute", "qty",
           "notional_rs", "margin_rs", "gross_rs", "charges_rs", "net_rs", "net_bps", "return_on_margin", "period",
           "reserved", "strategy_version", "updated_at"]


def _persist(conn: sqlite3.Connection, rows: list[dict]) -> None:
    stamp = datetime.now(IST).isoformat(timespec="seconds")
    conn.executemany(f"INSERT OR REPLACE INTO expiry_strangle_events ({','.join(COLUMNS)}) VALUES ({','.join('?' * len(COLUMNS))})",
                     [tuple({**r, "updated_at": stamp}.get(c) for c in COLUMNS) for r in rows])
    conn.commit()


# ---------------------------------------------------------------------- run ---
def _state(conn, k, v=None):
    if v is None:
        row = conn.execute("SELECT v FROM expiry_strangle_state WHERE k=?", (k,)).fetchone()
        return row[0] if row else None
    conn.execute("INSERT OR REPLACE INTO expiry_strangle_state (k, v) VALUES (?, ?)", (k, v))


def run_backfill(*, end_date: str | None = None, limit: int = 6, rebuild: bool = False, kite=None,
                 connection: sqlite3.Connection | None = None) -> dict:
    """Replay the next `limit` events not yet recorded, oldest first (bounded so a web request returns)."""
    now = datetime.now(IST).replace(tzinfo=None)
    end_date = end_date or now.date().isoformat()
    own = connection is None
    conn = connection or get_conn()
    _ensure_tables(conn)
    try:
        if rebuild:
            conn.execute("DELETE FROM expiry_strangle_events")
            conn.commit()
        if kite is not False:
            try:
                if kite is None:
                    from auth.session_manager import get_kite
                    kite = get_kite()
                update_closes(conn, kite, now.date())
            except Exception as exc:                                  # noqa: BLE001 - closes may already be stored
                _state(conn, "closes_error", f"{type(exc).__name__}: {exc}"[:300])
                conn.commit()
        done_keys = {(u, e) for u, e in conn.execute("SELECT DISTINCT und, expiry FROM expiry_strangle_events")}
        todo = []
        for und, cfg in UNDS.items():
            for expiry, entry, code in events(und, cfg["start"], end_date):
                if (und, expiry) in done_keys:
                    continue
                if expiry == now.date().isoformat() and now.time() < time(15, 35):
                    continue                                          # the expiry day is not over
                todo.append((expiry, und, entry, code))
        todo.sort()
        done, errors = [], {}
        for expiry, und, entry, code in todo[:max(1, int(limit))]:
            close = closes(conn, und)
            if not np.isfinite(sigma_before(close, entry)) or expiry not in close.index:
                # the daily closes are not in yet (Kite token, or the expiry day not over): try again later,
                # never record the event as skipped for it
                errors[f"{und} {expiry}"] = "daily closes not available yet"
                break
            try:
                rows = simulate(und, expiry, entry, code, close)
                _persist(conn, rows)
                done.append(f"{und} {expiry}")
            except Exception as exc:                                  # noqa: BLE001
                errors[f"{und} {expiry}"] = f"{type(exc).__name__}: {exc}"
                break
        _state(conn, "built_at", now.isoformat(timespec="seconds"))
        conn.commit()
        return {"done": done, "errors": errors, "remaining": 0 if errors else max(0, len(todo) - len(done))}
    finally:
        if own:
            conn.close()


def run_day(trade_date: str | None = None, *, now: datetime | None = None, **kwargs) -> dict:
    """The paper loop's entry point: after 15:35 on a weekday, record any finished event not yet recorded
    (at most once an hour)."""
    now = (now or datetime.now(IST)).replace(tzinfo=None)
    if now.weekday() >= 5 or now.time() < time(15, 35):
        return {"status": "waiting"}
    conn = get_conn()
    try:
        _ensure_tables(conn)
        last = _state(conn, "loop_checked_at")
        if last and now - datetime.fromisoformat(last) < timedelta(hours=1):
            return {"status": "checked"}
        _state(conn, "loop_checked_at", now.isoformat(timespec="seconds"))
        conn.commit()
    finally:
        conn.close()
    return run_backfill(**kwargs)


# ---------------------------------------------------------------- dashboard ---
def _summary(rows: list[dict]) -> dict:
    closed = [r for r in rows if r["status"] == "closed"]
    if not closed:
        return {"events": 0}
    net = np.array([r["net_rs"] for r in closed])
    bps = np.array([r["net_bps"] for r in closed])
    worst = min(closed, key=lambda r: r["net_rs"])
    return {"events": len(closed), "net_rs": float(net.sum()), "mean_bps": float(bps.mean()),
            "t": float(bps.mean() / (bps.std(ddof=1) / math.sqrt(len(bps)))) if len(bps) > 2 and bps.std(ddof=1) > 0 else None,
            "up": int((net > 0).sum()), "worst_rs": float(worst["net_rs"]), "worst_bps": float(worst["net_bps"]),
            "worst_event": f"{worst['und']} {worst['expiry']}", "margin_rs": float(np.mean([r["margin_rs"] for r in closed])),
            "tails": sum(1 for r in closed if (r["exit_reason"] or "").startswith("tail")),
            "worst_multiple": float(max(r["worst_multiple"] or 0 for r in closed))}


def retirement(rows: list[dict]) -> dict:
    fwd = sorted([r for r in rows if r["period"] == "forward" and r["status"] == "closed"
                  and (r["variant"], r["fill"]) == PRIMARY], key=lambda r: (r["entry_date"], r["und"]))
    big = [r for r in fwd if r["net_rs"] < -RETIRE_LOSS * r["notional_rs"]]
    first = fwd[:RETIRE_AFTER]
    net = sum(r["net_rs"] for r in first)
    if big:
        verdict = f"retire: {big[0]['und']} {big[0]['expiry']} lost {100 * -big[0]['net_rs'] / big[0]['notional_rs']:.2f}% of notional"
    elif len(first) >= RETIRE_AFTER and net <= 0:
        verdict = f"retire: the first {RETIRE_AFTER} forward events netted Rs {net:,.0f}"
    else:
        verdict = "keep"
    return {"verdict": verdict, "n": len(fwd), "of": RETIRE_AFTER, "net_rs": net, "retire": verdict.startswith("retire")}


def tab_data(conn: sqlite3.Connection, date_clause: str = "", date_params=()) -> tuple:
    """(primary-book events newest first, every row, stats) for the /labs/live tab."""
    _ensure_tables(conn)
    clause = date_clause.replace("trade_date", "expiry")
    cur = conn.execute(f"SELECT * FROM expiry_strangle_events WHERE 1=1 {clause} ORDER BY expiry DESC, und", tuple(date_params))
    cols = [c[0] for c in cur.description]
    rows = [dict(zip(cols, r)) for r in cur.fetchall()]
    if not rows:
        return [], [], {}
    books = {}
    for v in VARIANTS:
        for fl in FILLS:
            sel = [r for r in rows if r["variant"] == v and r["fill"] == fl]
            books[f"{v}_{fl}"] = {"variant": v, "fill": fl,
                                  **{f"{p}_{u}": _summary([r for r in sel if r["period"] == p and (u == "ALL" or r["und"] == u)])
                                     for p in ("research", "forward") for u in ("ALL", "NIFTY", "SENSEX")}}
    primary = [r for r in rows if (r["variant"], r["fill"]) == PRIMARY]
    reserved = sorted({r["expiry"] for r in rows if r["reserved"]})
    stats = {"books": books, "retirement": retirement(rows), "forward_from": FORWARD_FROM, "reserved_read": reserved,
             "skipped": [r for r in primary if r["status"] != "closed"],
             "lots": {u: lot_size(u) for u in UNDS}, "built_at": _state(conn, "built_at"),
             "closes_error": _state(conn, "closes_error")}
    return primary, rows, stats
