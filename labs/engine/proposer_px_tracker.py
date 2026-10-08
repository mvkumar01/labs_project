"""SENSEX Proposer + price-action exit: paper book.

Paper only. This module never calls a broker order API.

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

import io
import math
import sqlite3
import tarfile
from datetime import date, datetime, time, timedelta, timezone

import pandas as pd

from config.labs_config import ARCHIVE_DIR, DATA_DIR, SHARED_ARCHIVE_DIR, SHARED_LIVE_DIR, UNDERLYINGS
from labs.engine.charges import sensex_round_trip_charges
from live.engine import proposer_engine as pe
from live.engine import proposer_predictor as pp
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


class ProposerPxInputError(RuntimeError):
    """Required market data is unavailable for the session."""


# ------------------------------------------------------------------ schema ---
def _ensure_tables(conn: sqlite3.Connection) -> None:
    conn.executescript(
        """
        CREATE TABLE IF NOT EXISTS proposer_px_daily (
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
        CREATE TABLE IF NOT EXISTS proposer_px_trades (
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
        """
    )
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
            through: datetime) -> tuple[list[dict], pp.Regime]:
    """The live feed's prints, replayed: gap-rule regime, a 5-class call every 5 minutes from the
    first valid one, each from completed bars only (bars before its minute)."""
    regime = pp.regime_from_gap(prior[-1][1] if prior else None, open_ref)
    out, due = [], None
    m = datetime.combine(date.fromisoformat(day), SESSION_OPEN) + timedelta(minutes=1)
    while m <= through and m.time() <= PRINT_UNTIL:
        if due is None or m >= due:
            done = [b for b in today if b[0] < m]
            chain = None
            if done and m in chains:
                k, ce, pe_ = chains[m]
                idx = abs(k - done[-1][1]).argsort()[:7]
                chain = {"spot": done[-1][1],
                         "atm_pcr": (pe_[idx].sum() / ce[idx].sum()) if ce[idx].sum() > 0 else None}
            x = pp.predict_x5(prior, done, chain, regime) if done else None
            if x:
                out.append({"t": m, "regime": regime.label, "x5": x["x5"], "x5_conf": x["x5_conf"],
                            "x5_asof": m.isoformat(), "drift_state": pp.drift_state(x["micro"])})
                due = m + timedelta(minutes=PRINT_EVERY_MIN)
        m += timedelta(minutes=1)
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


def _rest_of_minute(sec: float, spot: float, bar) -> list[tuple[float, float]]:
    _, o, h, l, c = bar
    return [(sec, spot), ((sec + 60) / 2, h if c >= o else l), (60.0, c)]


# --------------------------------------------------------------------- day ---
def simulate_day(day: str, *, book_before: float = 0.0, now: datetime | None = None) -> dict:
    """Replay one session to its last recorded minute. Pure: reads market data, writes nothing."""
    if date.fromisoformat(day).weekday() >= 5:
        raise ProposerPxInputError(f"{day} is not a trading weekday")
    frame = _session_frame(day)
    expiry = select_expiry_code(frame["expiry"].unique(), day, "nearest_weekly")
    if expiry is None:
        raise ProposerPxInputError(f"No nearest SENSEX expiry for {day}")
    snap_spot = {t.to_pydatetime(): float(v) for t, v in frame.groupby("timestamp")["spot"].first().items()}
    near = frame[(frame["expiry"] == str(expiry)) & (frame["ltp"] > 0)]
    cols = ["ltp", "bid", "ask"] + (["tradingsymbol"] if "tradingsymbol" in near.columns else [])
    book = {(typ, int(k)): g.drop_duplicates("timestamp", keep="last").set_index("timestamp")[cols]
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
    prints, regime = _prints(day, prior, closes_today, open_ref, chains, min(through, session_end))
    by_min = {}
    for p in prints:
        by_min.setdefault(p["t"], []).append(p)

    eng = pe.ProposerEngine(pe.params_for(STRATEGY_VERSION))
    pos, trades, realized, snap, hist = None, [], 0.0, {}, []

    def book_state():
        eng.set_book(day_realized=realized, book_net=book_before + realized,
                     day_losses=sum(1 for x in trades if (x.get("gross_rs") or 0) < 0 and x.get("exit_ts")))

    def close(now_, px, spot_, rule):
        nonlocal realized, pos
        tr = trades[-1]
        gross = (px - pos["ein"]) * QTY
        charges = sensex_round_trip_charges(pos["ein"], px, QTY)["total"]
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
        path, i = _dense(_nodes(bar)), 0
        closes = [b[4] for b in hist]
        while i < len(path):
            sec, spot = path[i]
            now_ = t + timedelta(seconds=sec)
            fresh = False
            if pending and sec >= PRINT_LAG_S:
                snap, pending, fresh = pending[-1], [], True
            book_state()
            if pos:
                ltp = None
                if anchor:
                    sgn = 1 if pos["side"] == "CE" else -1
                    ltp = max(0.05, anchor[0] + sgn * anchor[2] * (spot - anchor_spot))
                    pos["last"], pos["half"] = ltp, anchor[1]
                if snap:
                    sig = eng.evaluate(now_, snap, pe.Position(pos["side"], pos["ein"], pos["espot"], QTY),
                                       option_ltp=ltp, spot=spot, closes=closes, entry_idx=pos["idx"])
                    if sig.action == "EXIT":
                        mark = ltp if ltp else pos["last"]
                        if sig.reason == "daily_target":
                            # the bank fires at its threshold; a jump past it at a re-anchor happened
                            # somewhere inside the previous minute, so fill at the threshold
                            need = max(eng.params.daily_target_pct * pos["ein"] * QTY - realized,
                                       -(book_before + realized))
                            mark = min(mark, pos["ein"] + need / QTY)
                        close(now_, mark - pos["half"], spot, sig.reason)
                        book_state()
                        eng.evaluate(now_, snap, pe.Position(), option_ltp=None, spot=spot)
            elif fresh and snap:
                sig = eng.evaluate(now_, snap, pe.Position(), option_ltp=None, spot=spot)
                if sig.action == "ENTER" and now_.time() < ENTRY_CUTOFF:
                    strike, typ = pe.itm_strike(spot, sig.side)
                    quotes = book.get((typ, strike))
                    c = _Contract(quotes, snap_spot) if quotes is not None else None
                    q = c.quote(t) if c else None
                    if q:
                        d = c.delta(t)
                        est = max(0.05, q[0] + (1 if typ == "CE" else -1) * d * (spot - anchor_spot))
                        pos = {"c": c, "side": typ, "ein": est + q[1], "espot": spot, "last": est,
                               "half": q[1], "idx": len(hist)}
                        anchor = (q[0], q[1], d)
                        eng.mark_entry(snap)
                        trades.append({"signal": sig.reason, "side": typ, "strike": strike,
                                       "tradingsymbol": c.symbol(t), "expiry_code": str(expiry),
                                       "entry_ts": now_.isoformat(timespec="seconds"),
                                       "entry_spot": round(spot, 2), "entry_price": round(est + q[1], 2),
                                       "exit_ts": None})
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
                               "gross_rs": round((mark - pos["ein"]) * QTY, 2)})
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
        "through_ts": through.isoformat(timespec="minutes"), "trades": trades,
    }


def _book_before(conn: sqlite3.Connection, day: str) -> float:
    row = conn.execute("SELECT COALESCE(SUM(gross_rs), 0) FROM proposer_px_daily "
                       "WHERE trade_date < ? AND status IN ('closed', 'no_trade')", (day,)).fetchone()
    return float(row[0] or 0.0)


def run_day(trade_date: str | None = None, *, persist: bool = True,
            connection: sqlite3.Connection | None = None, now: datetime | None = None) -> dict:
    """Replay a session (to now, for today) and store it. Idempotent."""
    now = now or datetime.now(IST)
    trade_date = trade_date or now.date().isoformat()
    own = connection is None
    conn = connection or get_conn()
    _ensure_tables(conn)
    try:
        result = simulate_day(trade_date, book_before=_book_before(conn, trade_date), now=now)
        if persist:
            _persist(conn, result)
        return {k: result[k] for k in ("trade_date", "status", "regime", "n_trades", "gross_rs", "net_rs")}
    finally:
        if own:
            conn.close()


def _persist(conn: sqlite3.Connection, r: dict) -> None:
    conn.execute("DELETE FROM proposer_px_trades WHERE trade_date=?", (r["trade_date"],))
    for seq, t in enumerate(r["trades"], start=1):
        conn.execute(
            "INSERT INTO proposer_px_trades (trade_date,seq,signal,side,strike,tradingsymbol,expiry_code,"
            "entry_ts,exit_ts,entry_spot,exit_spot,entry_price,exit_price,gross_rs,charges_rs,net_rs,exit_rule) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (r["trade_date"], seq, t["signal"], t["side"], t["strike"], t.get("tradingsymbol"),
             t.get("expiry_code"), t["entry_ts"], t.get("exit_ts"), t.get("entry_spot"), t.get("exit_spot"),
             t.get("entry_price"), t.get("exit_price"), t.get("gross_rs"), t.get("charges_rs"),
             t.get("net_rs"), t.get("exit_rule")))
    params = pe.params_for(STRATEGY_VERSION)
    conn.execute(
        "INSERT INTO proposer_px_daily (trade_date,status,expiry_code,regime,gap_pct,n_trades,n_losses,"
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
         r["book_gross_before"], r["through_ts"], LOTS, QTY, params.bar_exit, STRATEGY_VERSION,
         datetime.now(IST).isoformat(timespec="seconds")))
    conn.commit()


def record_unavailable(trade_date: str, error: str, *, connection: sqlite3.Connection | None = None) -> None:
    """An auditable no-result day; never an invented trade."""
    own = connection is None
    conn = connection or get_conn()
    _ensure_tables(conn)
    try:
        conn.execute("DELETE FROM proposer_px_trades WHERE trade_date=?", (trade_date,))
        conn.execute(
            "INSERT INTO proposer_px_daily (trade_date,status,n_trades,gross_rs,charges_rs,net_rs,lots,qty,"
            "strategy_version,error,updated_at) VALUES (?,?,0,0,0,0,?,?,?,?,?) "
            "ON CONFLICT(trade_date) DO UPDATE SET status=excluded.status,n_trades=0,n_losses=0,gross_rs=0,"
            "charges_rs=0,net_rs=0,error=excluded.error,updated_at=excluded.updated_at",
            (trade_date, "unavailable", LOTS, QTY, STRATEGY_VERSION, str(error)[:500],
             datetime.now(IST).isoformat(timespec="seconds")))
        conn.commit()
    finally:
        if own:
            conn.close()
