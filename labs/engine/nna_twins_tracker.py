"""Nifty New Alpha V1 paper twins: C1 "win rate", C2 "balanced", C3 "points". Paper only: never calls a broker
order API, no live preset, no account or runner change.

Asked for by Vipin on 2026-11-07 ("Mark it as third and get them added to the paper"), handed over by the alphaIMB
research session (research/experiments/2026-10-24_nifty_bare_range/nna_candidates_reference.py, the parity
reference). Each twin is Nifty New Alpha V1 (labs.engine.nna_tracker: prev close +-350, standard alpha on the
nearest weekly, entry on the +-30 cross at the 5-minute mark, alpha target +-100, |alpha| > 200 skipped, EOD at the
15:25 mark, ATM nearest-weekly option leg, ask in / bid out, NSE charges) with:

  all three   at most one trade a day (no new entry once the day's first trade has closed); spot stops checked
              every minute from the minute after entry with the level known before that minute; barrier fill =
              the level, or the minute's open if it gapped through (the research engine; nna_v1 itself checks
              its trail on the window between marks).
  C1 win rate 2 lots. Lot A sells at entry spot +10 (CALL; PUT -10). Once A is filled, lot B's stop = the entry
              spot, or the trail stop if tighter. B keeps the trail (arm 30, trail 15). Alpha stop -15 (a CALL
              exits at alpha <= -15, a PUT at >= +15). Before A fills both lots exit together.
  C2 balanced as C1 with lot A at +15 and the alpha stop at -30.
  C3 points   1 lot, alpha stop -30, no trailing: VIX at the open >= 15 -> once the trade is +40 the stop is fixed at
              entry +20; otherwise once +30 the stop is fixed at entry +15.

Points: per full position, as the research (C1 / C2 = 0.5 x lot A + 0.5 x lot B).

Option leg. One-minute path (before 27 Oct, or a day without ticks): entries, alpha exits and EOD at the mark
minute's quote (as nna_v1); a stop or lot A, which happen inside a minute, at the bid of the first one-minute
quote after that minute (the quote of the minute itself was taken at its start, before the event).
Ticks (from 27 Oct, labs.engine.tick_replay): the minute's index path is the recorded NIFTY_50 ticks (stops, the
trail and lot A tick by tick, in recorded order; the stop fills at the crossing tick), and every fill is the
option's recorded ask / bid FILL_LATENCY_S after the moment the rule acts (the close of the mark minute for
entries, alpha exits and EOD). A minute with fewer than 2 index ticks, or an option without a recent tick, falls
back to the one-minute rule / quote, counted per day.

VIX at the open (C3): the v2.14 E paper book's locked session context (alpha_v214e_daily.context_json, as the
research used), else the locked hybrid state of today, else alphaIMB vix_history.csv for that exact date; missing
-> the low-VIX setting, flagged.

Each session is independent (no carried book), so any day can be recomputed; the whole day is replayed on every run.
"""
from __future__ import annotations

import bisect
import json
import sqlite3
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta

import pandas as pd

from labs.engine import bn_alpha_tracker as core
from labs.engine import nna_tracker as nna
from labs.engine import tick_replay
from labs.engine.charges import round_trip_charges
from storage.db import get_conn

IST = core.IST
LOT_SIZE, STRIKE_STEP, HALF_WIDTH = nna.LOT_SIZE, nna.STRIKE_STEP, nna.HALF_WIDTH
ENTRY_TH, ALPHA_TGT, ALPHA_SKIP = core.ENTRY_TH, core.ALPHA_TGT, core.ALPHA_SKIP
TRAIL_ARM, TRAIL_DIST = 30.0, 15.0
VIX_HIGH = 15.0
PAPER_START = nna.PAPER_START
FIRST_UNSEEN = "2026-10-26"          # the research data ends 2026-10-23 (the three were picked on 7 Nov)
RESEARCH_END = "2026-10-23"


@dataclass(frozen=True)
class Twin:
    key: str
    label: str
    lots: int
    book: float | None        # lot A's offset (spot points), None = one lot, no lot A
    alpha_stop: float         # CALL exits at alpha <= this; PUT at >= -this
    lock: bool                # C3: fixed stop once up ARM (VIX regime), no trailing
    version: str
    reference: dict


BOOKS = {
    "nna_c1": Twin("nna_c1", "NNA V1 · win rate", 2, 10.0, -15.0, False,
                   "nna_c1_max1_lotA10_entrystop_sl15_trail30_15_atm_weekly_v1",
                   {"trades": 247, "sessions": 266, "points": 1913.2, "win_pct": 93.1, "losing_days": 15}),
    "nna_c2": Twin("nna_c2", "NNA V1 · balanced", 2, 15.0, -30.0, False,
                   "nna_c2_max1_lotA15_entrystop_sl30_trail30_15_atm_weekly_v1",
                   {"trades": 247, "sessions": 266, "points": 2333.6, "win_pct": 90.3, "losing_days": 22}),
    "nna_c3": Twin("nna_c3", "NNA V1 · points", 1, None, -30.0, True,
                   "nna_c3_max1_lock_vix15_40_20_else_30_15_sl30_atm_weekly_v1",
                   {"trades": 247, "sessions": 266, "points": 4733.3, "win_pct": 83.4, "losing_days": 39}),
}
# The research calibration run (plain V1 exits + max 1); not a paper book, used by the parity checks.
REF_MAX1 = Twin("ref_max1", "V1 + max 1", 1, None, -30.0, False, "ref", {})


# ------------------------------------------------------------------- VIX ---
def vix_open(day: str, conn: sqlite3.Connection | None = None) -> tuple[float | None, str]:
    """(VIX at the open, source) for `day`; (None, 'missing') when no source has that exact date."""
    own = conn is None
    conn = conn or get_conn()
    try:
        for table in ("alpha_v214e_daily", "alpha_v212_daily"):
            try:
                row = conn.execute(f"SELECT context_json FROM {table} WHERE trade_date=?", (day,)).fetchone()
            except sqlite3.OperationalError:
                continue
            if row and row[0]:
                try:
                    v = json.loads(row[0]).get("vix_open")
                except ValueError:
                    v = None
                if v is not None and 5 <= float(v) <= 90:
                    return float(v), f"{table}.context"
    finally:
        if own:
            conn.close()
    try:
        from labs.engine.paper_strategy_tracker import _read_locked_hybrid_state
        state = _read_locked_hybrid_state(day)
        if state and state.get("vix_at_open") is not None and 5 <= float(state["vix_at_open"]) <= 90:
            return float(state["vix_at_open"]), "locked_hybrid_state"
    except Exception:
        pass
    try:
        path = core._alpha_base_dir() / "data" / "analytics" / "vix_history.csv"
        f = pd.read_csv(path, dtype={"date": str})
        exact = f[f["date"] == day]
        if not exact.empty and pd.notna(exact.iloc[-1].get("vix_open")):
            v = float(exact.iloc[-1]["vix_open"])
            if 5 <= v <= 90:
                return v, "vix_history"
    except Exception:
        pass
    return None, "missing"


# ------------------------------------------------------------------ engine ---
def _minute_ticks(ticks, t: datetime) -> list | None:
    """[(time, price)] of the index ticks inside minute t, or None (fewer than 2: one-minute rule)."""
    if ticks is None or ticks.idx is None:
        return None
    ts, ltp = ticks.idx[0], ticks.idx[1]
    a, b = bisect.bisect_left(ts, t), bisect.bisect_left(ts, t + timedelta(minutes=1))
    pts = [(ts[k], float(ltp[k])) for k in range(a, b) if ltp[k] == ltp[k] and ltp[k] > 0]
    return pts if len(pts) >= 2 else None


def simulate(inp: dict, bars: dict, tw: Twin, vix: float | None, ticks=None) -> tuple[list[dict], dict | None, dict]:
    """The research rule (nna_candidates_reference.run) on one session: (closed trades, open trade). At most one
    trade. Times: entry_ts / exit_ts = the mark (or the minute of a stop); fill_at = when the order is sent."""
    marks, alpha, mspot = inp["marks"], inp["alpha"], inp["spot"]
    minutes = sorted(bars)
    hi_vix = vix is not None and vix >= VIX_HIGH
    arm, lock_off = (40.0, 20.0) if (tw.lock and hi_vix) else (TRAIL_ARM, 15.0)
    t, pos, esp, best = None, 0, 0.0, 0.0
    fb = {"min": 0}

    def level():
        lvl = None
        if (best - esp) * pos >= arm:
            lvl = (esp + lock_off * pos) if tw.lock else (best - TRAIL_DIST * pos)
        if tw.book and t["lotA_ts"] is not None:
            lvl = esp if lvl is None else (max(lvl, esp) if pos == 1 else min(lvl, esp))
        return lvl

    def stop_reason(lvl):
        if tw.lock:
            return "LOCK"
        return "ENTRY_STOP" if (tw.book and t["lotA_ts"] is not None and abs(lvl - esp) < 1e-9) else "TRAIL"

    def finish(px, why, exit_ts, fill_at, mark_i=None):
        nonlocal pos
        b_pts = round((px - esp) * pos, 2)
        t.update(exit_ts=exit_ts, exit_fill_at=fill_at, exit_mark_i=mark_i, exit_spot=round(float(px), 2),
                 exit_alpha=float(alpha[mark_i]) if mark_i is not None else None, exit_reason=why, lotB_pts=b_pts)
        if t["lotA_ts"] is not None:
            t["spot_pts"] = round(0.5 * t["lotA_pts"] + 0.5 * b_pts, 2)
        else:
            t["lotA_pts"] = b_pts if tw.book else None
            t["spot_pts"] = b_pts
        pos = 0

    for i in range(1, len(marks)):
        lo = bisect.bisect_right(minutes, marks[i - 1])
        hi = bisect.bisect_right(minutes, marks[i])
        for k in range(lo, hi):
            if not pos:
                break
            m = minutes[k]
            o, h, l, _c = bars[m]
            path = _minute_ticks(ticks, m)
            if ticks is not None and path is None:
                fb["min"] += 1
            if path is None:                         # the one-minute rule (the research engine)
                lvl = level()
                if lvl is not None and ((pos == 1 and l <= lvl) or (pos == -1 and h >= lvl)):
                    px = min(lvl, o) if pos == 1 else max(lvl, o)
                    finish(px, stop_reason(lvl), m, m + timedelta(minutes=1))
                    break
                if tw.book and t["lotA_ts"] is None and ((pos == 1 and h >= esp + tw.book) or
                                                         (pos == -1 and l <= esp - tw.book)):
                    t.update(lotA_ts=m, lotA_fill_at=m + timedelta(minutes=1), lotA_pts=float(tw.book))
                best = max(best, h) if pos == 1 else min(best, l)
                continue
            for when, p in path:                     # the recorded ticks, in order
                lvl = level()
                if lvl is not None and ((pos == 1 and p <= lvl) or (pos == -1 and p >= lvl)):
                    finish(p, stop_reason(lvl), m, when)
                    break
                if tw.book and t["lotA_ts"] is None and ((pos == 1 and p >= esp + tw.book) or
                                                         (pos == -1 and p <= esp - tw.book)):
                    t.update(lotA_ts=m, lotA_fill_at=when, lotA_pts=float(tw.book))
                best = max(best, p) if pos == 1 else min(best, p)
            if not pos:
                break
        ca, pa = alpha[i], alpha[i - 1]
        if abs(ca) > ALPHA_SKIP:
            continue
        px = mspot[i]
        close_at = marks[i] + timedelta(minutes=1)    # the mark minute's close: when the rule acts on ticks
        if pos:
            if (pos == 1 and (ca >= ALPHA_TGT or ca <= tw.alpha_stop)) or \
                    (pos == -1 and (ca <= -ALPHA_TGT or ca >= -tw.alpha_stop)):
                finish(px, "ALPHA_TGT" if abs(ca) >= ALPHA_TGT else "ALPHA_SL", marks[i], close_at, i)
            continue
        if t is not None:
            continue                                   # max 1 a day
        side = 1 if pa <= ENTRY_TH < ca else (-1 if pa >= -ENTRY_TH > ca else 0)
        if side:
            pos, esp, best = side, float(px), float(px)
            t = dict(side="CALL" if side == 1 else "PUT", entry_ts=marks[i], entry_fill_at=close_at, entry_i=i,
                     entry_spot=round(float(px), 2), entry_alpha=float(ca), lotA_ts=None, lotA_fill_at=None,
                     lotA_pts=None)
    info = {"fb_min": fb["min"]}
    if pos and len(marks):
        last = len(marks) - 1
        if inp["session_done"]:
            finish(mspot[last], "EOD", marks[last], marks[last] + timedelta(minutes=1), last)
            return [t], None, info
        t = dict(t, exit_ts=marks[last], exit_fill_at=None, exit_mark_i=last, exit_spot=round(float(mspot[last]), 2),
                 exit_alpha=float(alpha[last]), exit_reason="open",
                 lotB_pts=round((mspot[last] - esp) * pos, 2))
        t["spot_pts"] = (round(0.5 * t["lotA_pts"] + 0.5 * t["lotB_pts"], 2) if t["lotA_ts"] is not None
                         else t["lotB_pts"])
        return [], t, info
    return ([t] if t is not None else []), None, info


# ---------------------------------------------------------------- pricing ---
def quote_frame(frame: pd.DataFrame, expiry: str) -> pd.DataFrame:
    """The session's one-minute quotes of one expiry: minute, strike, type, bid, ask, tradingsymbol (done once a run)."""
    f = frame[frame["expiry"].astype(str).str.upper() == expiry]
    ts = pd.to_datetime(f["timestamp"])
    if ts.dt.tz is not None:
        ts = ts.dt.tz_convert(IST).dt.tz_localize(None)
    return pd.DataFrame({"minute": ts.dt.floor("min"), "strike": pd.to_numeric(f["strike"], errors="coerce"),
                         "type": f["option_type"].astype(str).str.lower(), "bid": f["bid"], "ask": f["ask"],
                         "tradingsymbol": f["tradingsymbol"] if "tradingsymbol" in f.columns else None})


def minute_quotes(qf: pd.DataFrame, strike: float, typ: str) -> dict:
    """{minute: (bid, ask, tradingsymbol)} of one contract, every one-minute quote of the session."""
    f = qf[(qf["strike"] == strike) & (qf["type"] == typ)]
    if f.empty:
        return {}
    f = f.sort_values("minute", kind="stable").groupby("minute", as_index=False).last()
    return {pd.Timestamp(m).to_pydatetime(): (b, a, s)
            for m, b, a, s in zip(f["minute"], f["bid"], f["ask"], f["tradingsymbol"])}


def _px(v):
    try:
        v = float(v)
    except (TypeError, ValueError):
        return None
    return v if v == v and v > 0 else None


def price_trade(t: dict, frame: pd.DataFrame, expiry: str, tw: Twin, ticks=None, fb: dict | None = None) -> dict:
    """The paper option leg: ATM strike (nearest 50) of the session's weekly, ask in, bid out, each lot of 65."""
    fb = fb if fb is not None else {"opt": 0}
    typ = "ce" if t["side"] == "CALL" else "pe"
    strike = float(round(t["entry_spot"] / STRIKE_STEP) * STRIKE_STEP)
    qf = frame if "minute" in frame.columns else quote_frame(frame, expiry)
    q = minute_quotes(qf, strike, typ)
    sym = next((v[2] for v in q.values() if v[2]), None)

    def snap(minute, side, after=False):
        """The one-minute quote: of `minute`, or (after) of the first minute after it."""
        keys = sorted(q)
        if after:
            j = bisect.bisect_right(keys, minute)
            cand = keys[j] if j < len(keys) else None
            if cand is None:
                cand = minute if minute in q else None
        else:
            cand = minute if minute in q else None
        if cand is None:
            return None
        return _px(q[cand][0 if side == "bid" else 1])

    def tick(when, side):
        if ticks is None or when is None:
            return None
        got = ticks.quote(sym, when + timedelta(seconds=ticks.lat))
        if got is None:
            fb["opt"] += 1
            return None
        mid, half = got
        return _px(mid + half if side == "ask" else mid - half)

    ask = tick(t["entry_fill_at"], "ask") or snap(t["entry_ts"], "ask")
    exit_after = t["exit_reason"] in ("TRAIL", "ENTRY_STOP", "LOCK")
    bid_b = tick(t.get("exit_fill_at"), "bid") or snap(t["exit_ts"], "bid", after=exit_after)
    bid_a = None
    if tw.book:
        if t["lotA_ts"] is not None:
            bid_a = tick(t["lotA_fill_at"], "bid") or snap(t["lotA_ts"], "bid", after=True)
        else:
            bid_a = bid_b
    lot_q = LOT_SIZE
    qty = lot_q * tw.lots
    row = dict(t, strike=int(strike), tradingsymbol=sym, entry_ask=ask, exit_bid=bid_b, lotA_bid=bid_a,
               option_pts=None, gross_rs=None, charges_rs=None, net_rs=None, qty=qty, lots=tw.lots,
               quote_status="priced")
    if ask is None or bid_b is None or (tw.book and bid_a is None):
        row["quote_status"] = "no_entry_ask" if ask is None else "no_exit_bid"
        return row
    legs = [(bid_a, lot_q), (bid_b, lot_q)] if tw.book else [(bid_b, qty)]
    gross = sum((b - ask) * n for b, n in legs)
    charges = sum(round_trip_charges(ask, b, n)["total"] for b, n in legs)
    row.update(option_pts=round(gross / qty, 2), gross_rs=round(gross, 2), charges_rs=round(charges, 2),
               net_rs=round(gross - charges, 2))
    return row


# ------------------------------------------------------------------ ledger ---
def _ensure_tables(conn: sqlite3.Connection, key: str) -> None:
    conn.executescript(
        f"""
        CREATE TABLE IF NOT EXISTS {key}_daily (
            trade_date TEXT PRIMARY KEY, status TEXT NOT NULL, expiry_code TEXT, prev_close REAL,
            prev_close_date TEXT, prev_close_source TEXT, range_lo INTEGER, range_hi INTEGER, baseline_date TEXT,
            vix_open REAL, vix_source TEXT, n_marks INTEGER, latest_mark TEXT, latest_alpha REAL, latest_spot REAL,
            n_trades INTEGER NOT NULL DEFAULT 0, spot_pnl_pts REAL, gross_rs REAL, charges_rs REAL, net_rs REAL,
            qty INTEGER, strategy_version TEXT NOT NULL, error TEXT, updated_at TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS {key}_trades (
            trade_date TEXT NOT NULL, seq INTEGER NOT NULL, status TEXT NOT NULL, side TEXT NOT NULL,
            entry_ts TEXT NOT NULL, exit_ts TEXT, entry_spot REAL, exit_spot REAL, entry_alpha REAL, exit_alpha REAL,
            lotA_ts TEXT, lotA_pts REAL, lotB_pts REAL, spot_pnl_pts REAL, exit_reason TEXT, strike INTEGER,
            tradingsymbol TEXT, entry_ask REAL, lotA_bid REAL, exit_bid REAL, option_pts REAL, gross_rs REAL,
            charges_rs REAL, net_rs REAL, qty INTEGER, lots INTEGER, quote_status TEXT,
            PRIMARY KEY (trade_date, seq)
        );
        """
    )
    conn.commit()


def session(day: str, *, now: datetime | None = None, kite=None, known_close: tuple | None = None,
            vix: tuple | None = None, conn: sqlite3.Connection | None = None) -> dict:
    """The session's inputs, shared by the three twins (one read of the option quotes per run)."""
    close, close_date, close_source = known_close or nna.prev_close(day, kite)
    frame = nna.options_frame(day)
    expiry = core.nearest_expiry(frame["expiry"].astype(str).str.upper().unique(), day)
    if expiry is None:
        raise core.BnAlphaInputError(f"No live {nna.SYMBOL} expiry on {day}")
    base, base_date = nna.baseline(day, expiry)
    bars = nna.spot_bars(day)
    if not bars:
        raise core.BnAlphaInputError(f"No {nna.SYMBOL} 1-min spot bars for {day}")
    inp = core.session_inputs(day, close=close, frame=frame, bars=bars, base=base, now=now,
                              half_width=HALF_WIDTH, step=STRIKE_STEP)
    if now is not None:                               # completed minutes only
        cut = now.astimezone(IST).replace(tzinfo=None) if now.tzinfo else now
        bars = {m: b for m, b in bars.items() if m + timedelta(minutes=1) <= cut}
    vix_v, vix_src = vix if vix is not None else vix_open(day, conn)
    return {"frame": frame, "inp": inp, "bars": bars, "prev_close": close, "prev_close_date": close_date,
            "prev_close_source": close_source, "baseline_date": base_date, "vix_open": vix_v, "vix_source": vix_src}


def replay_twin(sess: dict, tw: Twin, ticks=None) -> dict:
    """One twin on the session's inputs. Pure: writes nothing."""
    inp = sess["inp"]
    if "qframe" not in sess:
        sess["qframe"] = quote_frame(sess["frame"], inp["expiry"])
    frame = sess["qframe"]
    closed, open_, info = simulate(inp, sess["bars"], tw, sess["vix_open"], ticks)
    fb = {"opt": 0}
    trades = [dict(price_trade(x, frame, inp["expiry"], tw, ticks, fb), status="closed") for x in closed]
    if open_:
        trades.append(dict(price_trade(open_, frame, inp["expiry"], tw, ticks, fb), status="open"))
    replay = tick_replay.ONE_MINUTE if ticks is None else dict(ticks.summary(), fb_min=info["fb_min"],
                                                                 fb_opt=fb["opt"])
    return dict({k: v for k, v in sess.items() if k not in ("frame", "qframe", "bars")}, trades=trades, replay=replay)


def replay_day(day: str, tw: Twin, *, ticks=None, **kwargs) -> dict:
    """Read the session's data and replay one twin. Pure: writes nothing."""
    return replay_twin(session(day, **kwargs), tw, ticks)


def _save(conn: sqlite3.Connection, day: str, tw: Twin, r: dict, error: str | None = None) -> None:
    key = tw.key
    stamp = datetime.now(IST).isoformat(timespec="seconds")
    conn.execute(f"DELETE FROM {key}_trades WHERE trade_date=?", (day,))
    if error:
        conn.execute(f"INSERT OR REPLACE INTO {key}_daily (trade_date,status,n_trades,strategy_version,error,"
                     "updated_at) VALUES (?,?,?,?,?,?)", (day, "unavailable", 0, tw.version, error[:300], stamp))
        conn.commit()
        return
    inp, trades = r["inp"], r["trades"]
    fmt = lambda ts: ts.isoformat(timespec="minutes") if ts is not None else None   # noqa: E731
    for seq, t in enumerate(trades, 1):
        closed = t["status"] == "closed"
        conn.execute(
            f"INSERT INTO {key}_trades (trade_date,seq,status,side,entry_ts,exit_ts,entry_spot,exit_spot,entry_alpha,"
            "exit_alpha,lotA_ts,lotA_pts,lotB_pts,spot_pnl_pts,exit_reason,strike,tradingsymbol,entry_ask,lotA_bid,"
            "exit_bid,option_pts,gross_rs,charges_rs,net_rs,qty,lots,quote_status) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (day, seq, t["status"], t["side"], fmt(t["entry_ts"]), fmt(t["exit_ts"]) if closed else None,
             t["entry_spot"], t["exit_spot"], round(t["entry_alpha"], 2),
             round(t["exit_alpha"], 2) if t.get("exit_alpha") is not None else None, fmt(t["lotA_ts"]),
             t["lotA_pts"], t["lotB_pts"], t["spot_pts"], t["exit_reason"] if closed else "holding", t["strike"],
             t["tradingsymbol"], t["entry_ask"], t["lotA_bid"], t["exit_bid"], t["option_pts"], t["gross_rs"],
             t["charges_rs"], t["net_rs"], t["qty"], t["lots"], t["quote_status"]))
    closed = [t for t in trades if t["status"] == "closed"]
    status = "live" if not inp["session_done"] else ("traded" if trades else "no_trade")
    if inp["session_done"] and any(t["quote_status"] != "priced" for t in closed):
        status = "partial_quotes"
    last = len(inp["marks"]) - 1
    conn.execute(
        f"INSERT OR REPLACE INTO {key}_daily (trade_date,status,expiry_code,prev_close,prev_close_date,"
        "prev_close_source,range_lo,range_hi,baseline_date,vix_open,vix_source,n_marks,latest_mark,latest_alpha,"
        "latest_spot,n_trades,spot_pnl_pts,gross_rs,charges_rs,net_rs,qty,strategy_version,error,updated_at) "
        "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (day, status, inp["expiry"], r["prev_close"], r["prev_close_date"], r["prev_close_source"], inp["range_lo"],
         inp["range_hi"], r["baseline_date"], r["vix_open"], r["vix_source"], len(inp["marks"]),
         fmt(inp["marks"][last]) if last >= 0 else None,
         round(float(inp["alpha"][last]), 2) if last >= 0 else None,
         float(inp["spot"][last]) if last >= 0 else None, len(trades), round(sum(t["spot_pts"] for t in closed), 2),
         round(sum(t["gross_rs"] or 0 for t in closed), 2), round(sum(t["charges_rs"] or 0 for t in closed), 2),
         round(sum(t["net_rs"] or 0 for t in closed), 2), LOT_SIZE * tw.lots, tw.version, None, stamp))
    tick_replay.record(conn, key, day, r["replay"])
    conn.commit()


def run_days(day: str, keys, *, now: datetime | None = None, kite=None, persist: bool = True,
             connection: sqlite3.Connection | None = None, ticks="auto") -> dict:
    """Replay the twins `keys` on one session (inputs read once) and store them. ticks='auto' =
    labs.engine.tick_replay decides: a past tick day whose ticks are not here raises TicksMissing (rebuild it where
    the ticks are and import the result)."""
    now = now or datetime.now(IST)
    today = now.astimezone(IST).date().isoformat()
    keys = list(keys)
    own = connection is None
    conn = connection or get_conn()
    out = {}
    try:
        known = None
        for key in keys:
            _ensure_tables(conn, key)
        for table in [f"{key}_daily" for key in keys] + [f"{nna.KEY}_daily"]:    # then nna_v1's (the same range)
            try:
                row = conn.execute(f"SELECT prev_close, prev_close_date, prev_close_source FROM {table} "
                                   "WHERE trade_date=? AND prev_close_source='kite_daily'", (day,)).fetchone()
            except sqlite3.OperationalError:
                row = None
            if row:
                known = tuple(row)                              # the range is locked once known
                break
        if ticks == "auto":
            got = [tick_replay.ticks_for(key, day, today) for key in keys]
            shared = next((x for x in got if x is not None), None)    # one reader: the same NIFTY_50 ticks
            per_key = {key: (shared if g is not None else None) for key, g in zip(keys, got)}
        else:
            per_key = {key: ticks for key in keys}
        try:
            sess = session(day, now=now if day == today else None, kite=kite, known_close=known, conn=conn)
        except core.BnAlphaInputError as exc:
            for key in keys:
                if persist:
                    _save(conn, day, BOOKS[key], {}, error=str(exc))
                out[key] = {"trade_date": day, "status": "unavailable", "error": str(exc)}
            return out
        for key in keys:
            r = replay_twin(sess, BOOKS[key], per_key[key])
            if persist:
                _save(conn, day, BOOKS[key], r)
            closed = [t for t in r["trades"] if t["status"] == "closed"]
            out[key] = {"trade_date": day, "replay": r["replay"]["replay"], "trades": len(r["trades"]),
                        "open": sum(t["status"] == "open" for t in r["trades"]),
                        "spot_pts": round(sum(t["spot_pts"] for t in closed), 2),
                        "net_rs": round(sum(t["net_rs"] or 0 for t in closed), 2)}
        return out
    finally:
        if own:
            conn.close()


def run_day(trade_date: str | None = None, *, key: str, now: datetime | None = None, **kwargs) -> dict:
    now = now or datetime.now(IST)
    day = trade_date or now.astimezone(IST).date().isoformat()
    return run_days(day, [key], now=now, **kwargs)[key]


def run_live(now: datetime | None = None) -> dict | None:
    """The paper loop's entry: replay today (all three twins, one read of the inputs) whenever a new 5-minute
    mark has completed. Weekends and hours outside the session do nothing."""
    now = (now or datetime.now(IST)).astimezone(IST)
    if now.weekday() >= 5 or not (time(9, 16) <= now.time() <= time(15, 45)):
        return None
    day = now.date().isoformat()
    newest = (now - timedelta(seconds=70)).replace(second=0, microsecond=0, tzinfo=None)
    newest -= timedelta(minutes=newest.minute % 5)
    newest = min(newest, datetime.combine(now.date(), core.LAST_MARK))
    conn = get_conn()
    try:
        due = []
        for key in BOOKS:
            _ensure_tables(conn, key)
            row = conn.execute(f"SELECT latest_mark, status FROM {key}_daily WHERE trade_date=?", (day,)).fetchone()
            if not (row and row[1] != "unavailable" and row[0] and row[0] >= newest.isoformat(timespec="minutes")):
                due.append(key)
        if not due:
            return None
        res = run_days(day, due, now=now, connection=conn)
    finally:
        conn.close()
    return {k: {f: v.get(f) for f in ("replay", "trades", "open", "spot_pts", "net_rs", "error") if f in v}
            for k, v in res.items()}


def run_paper_day(trade_date: str | None = None) -> dict | None:
    """The paper loop's in-session entry (it passes today's date): see run_live."""
    return run_live(datetime.now(IST))


def run_backfill(start: str = PAPER_START, end: str | None = None, keys=None, kite=None) -> dict:
    """Past sessions, all twins per session. A tick day is refused (TicksMissing) unless its ticks are here."""
    end = end or (datetime.now(IST).date() - timedelta(days=1)).isoformat()
    keys = list(keys or BOOKS)
    done, failed = [], {}
    for day in nna.sessions_with_quotes(start, end):
        res = run_days(day, keys, kite=kite)
        err = next((v["error"] for v in res.values() if v.get("error")), None)
        (failed.__setitem__(day, err) if err else done.append(day))
    return {"keys": keys, "done": len(done), "failed": failed}


# ---------------------------------------------------------------- dashboard ---
EXIT_REASONS = ("TRAIL", "ENTRY_STOP", "LOCK", "ALPHA_TGT", "ALPHA_SL", "EOD")


def tab_data(conn: sqlite3.Connection, key: str, date_clause: str = "", date_params=()) -> tuple:
    """(day rows newest first, trades newest first, stats) for the /labs/live tab; the book's description is in the
    stats even when the date range holds no session."""
    tw = BOOKS[key]
    info = {"key": key, "label": tw.label, "lots": tw.lots, "book": tw.book, "alpha_stop": tw.alpha_stop,
            "lock": tw.lock, "lot_size": LOT_SIZE, "half_width": HALF_WIDTH, "reference": tw.reference,
            "paper_start": PAPER_START, "research_end": RESEARCH_END, "first_unseen": FIRST_UNSEEN,
            "tick_from": tick_replay.TICK_BOOKS.get(key)}
    _ensure_tables(conn, key)
    rows, trades, stats = core.tab_data(conn, date_clause, date_params, prefix=key, first_unseen=FIRST_UNSEEN,
                                        extra=info)
    if not rows:
        return rows, trades, info
    closed = [t for t in trades if t["status"] == "closed"]
    stats["exit_reasons"] = {k: sum(1 for t in closed if t["exit_reason"] == k) for k in EXIT_REASONS}
    stats["win_trades"] = sum(1 for t in closed if float(t["spot_pnl_pts"] or 0) > 0)
    stats["win_rs"] = sum(1 for t in closed if float(t["net_rs"] or 0) > 0)
    stats["lotA_filled"] = sum(1 for t in closed if t.get("lotA_ts"))
    rep = tick_replay.replays(conn, key)
    for r in rows:
        r["replay"] = (rep.get(r["trade_date"]) or {}).get("replay")
    return rows, trades, stats
