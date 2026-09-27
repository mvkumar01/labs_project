"""Market structure at each fresh Alpha entry, logged on the paper trades. Observation only.

Why: in the 59-day study (research/experiments/2026-09-24_s55_vs_paper/matrix_rb.py) entries
that started 10-25 points short of a level in their own direction lost as a group (27
episodes, -Rs8.8k, 81% stopped) while entries at or far from a level did well. The buckets
are small and non-monotonic, so no rule was changed; these columns collect forward
evidence instead.

Recorded at the entry mark from data known then (completed 5-minute bins of the previous
session and today, the previous session's levels and the option chain at the mark):
  room / cushion   distance (spot points) to the nearest level in the trade's favour /
                   against it, and which level: previous-session high/low/close, CPR
                   pivot/BC/TC, OI walls (strike with the most CE OI above spot, most PE
                   OI below)
  chop             Choppiness Index(14) on 5-minute bins (> 61.8 choppy, < 38.2 trending)
  bbw_rank         Bollinger(20, 2) width percentile over the last 75 bins
  atr              ATR(14) on 5-minute bins
"""
from __future__ import annotations

from datetime import date, timedelta
import math
import sqlite3

import numpy as np
import pandas as pd

from live.engine import champion_inputs


COLUMNS = (
    ("entry_room_pts", "REAL"), ("entry_room_level", "TEXT"),
    ("entry_cushion_pts", "REAL"), ("entry_cushion_level", "TEXT"),
    ("entry_chop", "REAL"), ("entry_bbw_rank", "REAL"), ("entry_atr", "REAL"),
)
FIELDS = tuple(name for name, _ in COLUMNS)


def ensure_columns(conn: sqlite3.Connection, table: str) -> None:
    have = {row[1] for row in conn.execute(f"PRAGMA table_info({table})")}
    for name, ddl in COLUMNS:
        if name not in have:
            conn.execute(f"ALTER TABLE {table} ADD COLUMN {name} {ddl}")


def _bars5(by_minute: dict, day: str) -> pd.DataFrame:
    rows = [(pd.Timestamp(f"{day} {k}"), *v[:4]) for k, v in by_minute.items()
            if "09:15" <= k <= "15:29"]
    if not rows:
        return pd.DataFrame(columns=["open", "high", "low", "close"])
    df = pd.DataFrame(rows, columns=["ts", "open", "high", "low", "close"]).set_index("ts").sort_index()
    return df.resample("5min", origin=pd.Timestamp(f"{day} 09:15")).agg(
        {"open": "first", "high": "max", "low": "min", "close": "last"}).dropna()


def _previous_session(trade_date: str, max_back: int = 10) -> tuple[str, dict] | tuple[None, dict]:
    d = date.fromisoformat(trade_date)
    for k in range(1, max_back + 1):
        day = (d - timedelta(days=k)).isoformat()
        minutes = champion_inputs.ohlc_by_minute(day)
        if minutes:
            return day, minutes
    return None, {}


def day_frame(trade_date: str, today_minutes: dict | None = None) -> dict:
    """5-minute indicator frame (previous session + today) and previous-session levels."""
    prev_day, prev_minutes = _previous_session(trade_date)
    parts, levels = [], {}
    if prev_day:
        pb = _bars5(prev_minutes, prev_day)
        if len(pb):
            parts.append(pb)
            H, L, C = float(pb.high.max()), float(pb.low.min()), float(pb.close.iloc[-1])
            P, BC = (H + L + C) / 3.0, (H + L) / 2.0
            levels = {"pdh": H, "pdl": L, "pdc": C, "pivot": P, "bc": BC, "tc": 2 * P - BC}
    today = champion_inputs.ohlc_by_minute(trade_date) if today_minutes is None else today_minutes
    parts.append(_bars5(today, trade_date))
    b = pd.concat(parts)
    if b.empty:
        return {"bars": b, "levels": levels}
    tr = pd.concat([b.high - b.low, (b.high - b.close.shift()).abs(),
                    (b.low - b.close.shift()).abs()], axis=1).max(axis=1)
    n = 14
    b = b.assign(atr=tr.ewm(alpha=1 / n, adjust=False).mean())
    rng = b.high.rolling(n).max() - b.low.rolling(n).min()
    with np.errstate(divide="ignore", invalid="ignore"):
        b["chop"] = 100 * np.log10(tr.rolling(n).sum() / rng) / np.log10(n)
    mid = b.close.rolling(20).mean()
    sd = b.close.rolling(20).std(ddof=0)
    b["bbw_rank"] = (100 * 4 * sd / mid).rolling(75, min_periods=20).rank(pct=True) * 100
    return {"bars": b, "levels": levels}


def _naive(ts) -> pd.Timestamp:
    t = pd.Timestamp(ts)
    return t.tz_convert("Asia/Kolkata").tz_localize(None) if t.tzinfo is not None else t


def _num(x):
    return None if x is None or (isinstance(x, float) and not math.isfinite(x)) or pd.isna(x) \
        else round(float(x), 2)


def at_entry(frame: dict, entry_ts, spot: float, side: str,
             ce_at: dict | None = None, pe_at: dict | None = None) -> dict:
    """The structure fields for one entry (side 'call' | 'put'); unknowns are None."""
    out = dict.fromkeys(FIELDS)
    b = frame["bars"]
    if not b.empty:
        done = b[b.index + pd.Timedelta(minutes=5) <= _naive(entry_ts)]
        if not done.empty:
            r = done.iloc[-1]
            out.update(entry_chop=_num(r.get("chop")), entry_bbw_rank=_num(r.get("bbw_rank")),
                       entry_atr=_num(r.get("atr")))
    above = [(name, v - spot) for name, v in frame["levels"].items() if v > spot]
    below = [(name, spot - v) for name, v in frame["levels"].items() if v < spot]
    ce_wall = max(((k, v) for k, v in (ce_at or {}).items() if k > spot),
                  key=lambda kv: kv[1], default=None)
    pe_wall = max(((k, v) for k, v in (pe_at or {}).items() if k < spot),
                  key=lambda kv: kv[1], default=None)
    if ce_wall:
        above.append(("oi_wall_ce", ce_wall[0] - spot))
    if pe_wall:
        below.append(("oi_wall_pe", spot - pe_wall[0]))
    fav, adv = (above, below) if side == "call" else (below, above)
    if fav:
        name, dist = min(fav, key=lambda nd: nd[1])
        out.update(entry_room_pts=_num(dist), entry_room_level=name)
    if adv:
        name, dist = min(adv, key=lambda nd: nd[1])
        out.update(entry_cushion_pts=_num(dist), entry_cushion_level=name)
    return out


def annotate(trades: list[dict], segments: list[dict], fresh: list[bool], trade_date: str,
             oi_maps: tuple | None = None) -> None:
    """Add the structure fields to each priced trade of a fresh Alpha entry (in place).
    Never raises: a trade must be saved even when its structure cannot be computed."""
    for t in trades:
        for name in FIELDS:
            t.setdefault(name, None)
    try:
        if not any(fresh):
            return
        frame = day_frame(trade_date)
        ce_map, pe_map = oi_maps if oi_maps else ({}, {})
        for t, seg, is_fresh in zip(trades, segments, fresh):
            if is_fresh:
                ts = seg["entry_ts"]
                t.update(at_entry(frame, ts, float(seg["entry_spot"]), seg["pos"],
                                  ce_map.get(ts, {}), pe_map.get(ts, {})))
    except Exception:                                           # noqa: BLE001
        return


__all__ = ["COLUMNS", "FIELDS", "annotate", "at_entry", "day_frame", "ensure_columns"]
