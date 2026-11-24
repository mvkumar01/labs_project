"""Nifty Omega 3 sizing twins (paper only; never calls a broker order API). Vipin 2026-11-24: "yes, build both paper
twins", from alphaIMB research/experiments/2026-11-24_omega_unseen_2025q2 (trend_follows.py, trend_dir.py).

Both twins take Nifty Omega 3's own paper trades (the nifty_omega_trades rows: same entries, strikes, quotes and
exits) and change only the size. A trade's lots are split evenly between the +12.5 and +20 premium targets, so every
size keeps both exits. Gross and charges are recomputed for the twin's quantity with the same formula as the
Omega book.

Day type of a past session, from its daily candle: efficiency = (close - open) / (high - low). Trend day when
|efficiency| >= 0.4 (direction = its sign), choppy day otherwise (high == low counts as choppy). Candle source:
Kite's NIFTY 50 daily candle (official close), else the collector's one-minute bars (first open, high, low, last
close). The two previous sessions' candles are stored per trade date in omega_twins_context and reused.

  omega_chop_half    4 lots (2 per target); 2 lots (1 per target, = Omega 3) when the previous session was choppy.
                     Research (403 sessions, half size after a choppy day): Rs 242.8k vs 256.3k, max DD 39.7k vs
                     63.5k (Omega 3 at 2 lots throughout).
  omega_fade_streak  2 lots (= Omega 3); 4 lots when the two previous sessions were trend days in the same direction
                     and the trade is against it (PUT after two up days, CALL after two down days).
                     Research: against-streak trades Rs 2,259/trade (31) vs with-streak 682 (28), p 0.02, 5/5 periods;
                     double size on them Rs 326.3k vs 256.3k, DD 62.2k vs 63.5k. Post-hoc split, small sample.

Forward paper starts START (25 Nov 2026). Rows dated before it are a backfill derived from Omega's stored rows (the
Omega book's own backfill before 12 Nov, its forward rows after) and are labelled backfill.
"""
from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta

from labs.engine import bn_alpha_tracker as core, nna_tracker as nna, nna_twins_tracker as twins
from labs.engine.charges import BROKERAGE_PER_LEG, GST, round_trip_charges
from live.engine import nifty_omega as omega
from storage.db import get_conn

START = "2026-11-25"
TREND_EFF = 0.4
CONTEXT = "omega_twins_context"
SOURCE_KEY = omega.KEY


@dataclass(frozen=True)
class SizeTwin:
    key: str
    label: str
    version: str
    rule: str


BOOKS = {
    "omega_chop_half": SizeTwin("omega_chop_half", "Omega 3 · half after chop", "omega3-size-4lots-2after-chop-eff0.4",
                                "chop_half"),
    "omega_fade_streak": SizeTwin("omega_fade_streak", "Omega 3 · fade 2-day streak",
                                  "omega3-size-2lots-4against-2day-trend-streak-eff0.4", "fade_streak"),
}


# ------------------------------------------------------------------ day type ---
def efficiency(candle) -> float:
    """(close - open) / (high - low) of an (open, high, low, close) candle; 0 when high == low."""
    o, h, l, c = (float(x) for x in candle)
    return (c - o) / (h - l) if h > l else 0.0


def day_type(candle) -> tuple[bool, int]:
    """(trend day?, direction +1 / -1 / 0)."""
    e = efficiency(candle)
    return abs(e) >= TREND_EFF, (e > 0) - (e < 0)


def lots_per_target(rule: str, side: int, prev1, prev2) -> int:
    """Lots on each of the two premium targets for a trade on `side` (+1 CALL / -1 PUT), given the previous
    session's candle prev1 and the one before it prev2 (open, high, low, close)."""
    t1, d1 = day_type(prev1)
    if rule == "chop_half":
        return 2 if t1 else 1
    if rule == "fade_streak":
        t2, d2 = day_type(prev2)
        return 2 if (t1 and t2 and d1 == d2 and d1 != 0 and side == -d1) else 1
    raise ValueError(rule)


def _kite_candles(day: str, kite=None) -> list[tuple]:
    if kite is None:
        from auth.session_manager import get_kite
        kite = get_kite()
    d0 = date.fromisoformat(day)
    rows = kite.historical_data(nna.KITE_TOKEN, datetime.combine(d0 - timedelta(days=15), time(0)),
                                datetime.combine(d0 - timedelta(days=1), time(23, 59)), "day")
    out = []
    for r in rows:
        d = r["date"].date() if hasattr(r["date"], "date") else date.fromisoformat(str(r["date"])[:10])
        if d < d0:
            out.append((d.isoformat(), float(r["open"]), float(r["high"]), float(r["low"]), float(r["close"])))
    return out[-2:]


def _collector_candles(day: str) -> list[tuple]:
    out = []
    for prev in core._previous_weekdays(day, limit=10):
        bars = nna.spot_bars(prev)
        if bars:
            ks = sorted(bars)
            out.append((prev, float(bars[ks[0]][0]), max(float(b[1]) for b in bars.values()),
                        min(float(b[2]) for b in bars.values()), float(bars[ks[-1]][3])))
        if len(out) == 2:
            break
    return list(reversed(out))


def _ensure_context(conn: sqlite3.Connection) -> None:
    conn.execute(f"""CREATE TABLE IF NOT EXISTS {CONTEXT} (
        trade_date TEXT PRIMARY KEY, d1_date TEXT, d1_open REAL, d1_high REAL, d1_low REAL, d1_close REAL,
        d2_date TEXT, d2_open REAL, d2_high REAL, d2_low REAL, d2_close REAL, source TEXT, updated_at TEXT)""")


def context(conn: sqlite3.Connection, day: str, kite=None, candles=None) -> dict:
    """The two previous sessions' candles for `day`: stored once Kite's daily candles were used; a collector
    fallback is re-read on later runs. candles: [(date, o, h, l, c) older, newer] to use instead (tests)."""
    _ensure_context(conn)
    row = conn.execute(f"SELECT * FROM {CONTEXT} WHERE trade_date=?", (day,)).fetchone()
    if row is not None and candles is None:
        row = dict(zip([c[0] for c in conn.execute(f"SELECT * FROM {CONTEXT} LIMIT 0").description], row))
        if row["source"] == "kite_daily":
            return row
    source = "given"
    if candles is None:
        try:
            candles, source = _kite_candles(day, kite), "kite_daily"
        except Exception:
            candles = []
        if len(candles) < 2:
            candles, source = _collector_candles(day), "collector_1min"
    if len(candles) < 2:
        raise core.BnAlphaInputError(f"Omega twins: no two previous daily candles before {day}")
    d2, d1 = candles[-2], candles[-1]
    stamp = datetime.now(core.IST).isoformat(timespec="seconds")
    conn.execute(f"INSERT OR REPLACE INTO {CONTEXT} VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
                 (day, *d1, *d2, source, stamp))
    conn.commit()
    return dict(trade_date=day, d1_date=d1[0], d1_open=d1[1], d1_high=d1[2], d1_low=d1[3], d1_close=d1[4],
                d2_date=d2[0], d2_open=d2[1], d2_high=d2[2], d2_low=d2[3], d2_close=d2[4], source=source,
                updated_at=stamp)


def _candle(ctx: dict, n: int) -> tuple:
    return tuple(ctx[f"d{n}_{k}"] for k in ("open", "high", "low", "close"))


# ------------------------------------------------------------------ pricing ---
def price(trade: dict, per_target: int) -> dict:
    """An Omega trade row re-sized to `per_target` lots on each premium target. Same arithmetic as
    labs.engine.nifty_omega_book._trade (per_target 1 gives Omega's own gross / charges / net)."""
    out = dict(trade)
    out["qty"], out["lots"] = 2 * omega.LOT * per_target, 2 * per_target
    if trade["status"] != "closed":
        out["gross_rs"] = out["charges_rs"] = out["net_rs"] = None
        return out
    ask, a, b = float(trade["entry_ask"]), float(trade["lotA_bid"]), float(trade["exit_bid"])
    leg = omega.LOT * per_target
    gross = (a + b - 2 * ask) * leg
    if trade["lotA_ts"] == trade["exit_ts"]:
        fee = round_trip_charges(ask, b, 2 * leg)["raw_total"]
    else:
        fee = sum(round_trip_charges(ask, x, leg)["raw_total"] for x in (a, b)) - BROKERAGE_PER_LEG * (1 + GST)
    out["gross_rs"], out["charges_rs"], out["net_rs"] = gross, fee, round(gross - fee, 2)
    return out


# ------------------------------------------------------------------ book ---
def _rows(conn, sql, params=()):
    cur = conn.execute(sql, params)
    names = [c[0] for c in cur.description]
    return [dict(zip(names, r)) for r in cur.fetchall()]


def derive_day(conn: sqlite3.Connection, day: str, keys=tuple(BOOKS), kite=None, candles=None) -> dict | None:
    """Write the twins' rows for `day` from Omega's stored rows. None when Omega has no row for the day."""
    twins._ensure_tables(conn, SOURCE_KEY)
    src = _rows(conn, f"SELECT * FROM {SOURCE_KEY}_daily WHERE trade_date=?", (day,))
    if not src:
        return None
    src = src[0]
    trades = _rows(conn, f"SELECT * FROM {SOURCE_KEY}_trades WHERE trade_date=? ORDER BY seq", (day,))
    ctx = context(conn, day, kite, candles) if trades else None
    stamp = datetime.now(core.IST).isoformat(timespec="seconds")
    out = {}
    for key in keys:
        tw = BOOKS[key]
        twins._ensure_tables(conn, key)
        conn.execute(f"DELETE FROM {key}_trades WHERE trade_date=?", (day,))
        sized = []
        for t in trades:
            side = 1 if t["side"] == "CALL" else -1
            sized.append(price(t, lots_per_target(tw.rule, side, _candle(ctx, 1), _candle(ctx, 2))))
        cols = list(trades[0]) if trades else []
        for t in sized:
            conn.execute(f"INSERT INTO {key}_trades ({','.join(cols)}) VALUES ({','.join('?' * len(cols))})",
                         tuple(t[c] for c in cols))
        closed = [t for t in sized if t["status"] == "closed"]
        daily = dict(src, gross_rs=round(sum(t["gross_rs"] for t in closed), 2),
                     charges_rs=round(sum(t["charges_rs"] for t in closed), 2),
                     net_rs=round(sum(t["net_rs"] for t in closed), 2),
                     qty=max((t["qty"] for t in sized), default=2 * omega.LOT * (2 if tw.rule == "chop_half" else 1)),
                     strategy_version=f"{tw.version}|{src['strategy_version']}", updated_at=stamp)
        names = list(daily)
        conn.execute(f"INSERT OR REPLACE INTO {key}_daily ({','.join(names)}) VALUES ({','.join('?' * len(names))})",
                     tuple(daily[c] for c in names))
        out[key] = dict(trades=len(sized), net_rs=daily["net_rs"], lots=[t["lots"] for t in sized])
    conn.commit()
    return out


def run_paper_day(trade_date: str | None = None, now: datetime | None = None, kite=None) -> dict | None:
    """The paper loop's entry: re-derive today's twin rows from Omega's (run it after nifty_omega)."""
    now = now or datetime.now(core.IST)
    day = trade_date or now.astimezone(core.IST).date().isoformat()
    if day < START:
        return None
    conn = get_conn()
    try:
        return derive_day(conn, day, kite=kite)
    finally:
        conn.close()


def run_backfill(start: str = "2000-01-01", end: str | None = None, kite=None) -> dict:
    """BACKFILL: derive every session Omega has stored before START (end inclusive, capped at START - 1)."""
    last = (date.fromisoformat(START) - timedelta(days=1)).isoformat()
    end = min(end or last, last)
    conn = get_conn()
    done, failed = {}, {}
    try:
        twins._ensure_tables(conn, SOURCE_KEY)
        days = [r[0] for r in conn.execute(f"SELECT trade_date FROM {SOURCE_KEY}_daily WHERE trade_date BETWEEN ? AND ?"
                                           " ORDER BY trade_date", (start, end)).fetchall()]
        for day in days:
            try:
                done[day] = derive_day(conn, day, kite=kite)
            except Exception as exc:                # one bad session must not stop the backfill
                failed[day] = f"{type(exc).__name__}: {exc}"
    finally:
        conn.close()
    return dict(done=done, failed=failed)


def tab_data(conn: sqlite3.Connection, key: str, date_clause: str = "", date_params=()) -> tuple:
    twins._ensure_tables(conn, key)
    _ensure_context(conn)
    rows, trades, stats = core.tab_data(conn, date_clause, date_params, prefix=key, first_unseen=START,
                                        extra={"label": BOOKS[key].label, "key": key, "backfill_before": START})
    ctx = {r["trade_date"]: r for r in _rows(conn, f"SELECT * FROM {CONTEXT}")}
    for r in rows:
        c = ctx.get(r["trade_date"])
        if c:
            e1, e2 = efficiency(_candle(c, 1)), efficiency(_candle(c, 2))
            r["prev_day"] = ("trend " + ("up" if e1 > 0 else "down") if abs(e1) >= TREND_EFF else "choppy") + f" {e1:+.2f}"
            r["prev2_day"] = ("trend " + ("up" if e2 > 0 else "down") if abs(e2) >= TREND_EFF else "choppy") + f" {e2:+.2f}"
        r["lots"] = int((r.get("qty") or 0) // omega.LOT)
    return rows, trades, stats
