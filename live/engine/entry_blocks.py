"""Nifty Scalper V4's two entry blocks, shared by the paper book and the live runner (Vipin, 2026-11-02).

V4 = Nifty Scalper V3 with these blocks and nothing else changed. Each applies only to a fresh ENTER; a blocked
signal becomes HOLD and the print is spent (the next fresh print decides again).

  cpr room  a CPR level within CPR_ROOM_PTS NIFTY points (inclusive) on the side the trade must travel: the nearest
            level below spot for a PUT, above spot for a CALL. Levels S3..R3 with BC, P, TC of the previous
            session's NIFTY 1-min high, low and last close. No CPR for the day: this block passes.
  candle    the last finished 1-min NIFTY candle (anchored at 09:15; at 09:22:25 the 09:21 candle) is a doji,
            inverted hammer or hammer (small_body). The forming candle is never used.

Research: alphaIMB research/experiments/2026-09-09_proposer_v1_reverse_engineering, support_exit.py variant
"none|t|n|on|0|0|nrc15|nc1" (REBUILD.md sec. 56-57). 1 Jul - 30 Oct 2026, in-sample: 88 trades, +10.90L against V3's
+1.29L, carried by three days (10 Aug, 11 Sep, 25 Sep); the same blocks do not help V1 or Sensex V5.
"""
from __future__ import annotations

import bisect
from datetime import date, datetime, time, timedelta
from typing import Callable, Optional, Sequence

CPR_ROOM_PTS = 15.0
CPR_KEYS = ("S3", "S2", "S1", "BC", "P", "TC", "R1", "R2", "R3")
SESSION_OPEN = time(9, 15)
REASON_CPR, REASON_CANDLE = "cpr_room", "reversal_candle"


def small_body(o: float, h: float, l: float, c: float) -> bool:
    """Doji (body <= 15% of the range), inverted hammer or hammer (body <= 35%, one wick >= 2 bodies, the other
    <= 25% of the range). A candle with no range is none of these."""
    r, body = h - l, abs(c - o)
    up, lo = h - max(o, c), min(o, c) - l
    return r > 0 and (body <= 0.15 * r or (body <= 0.35 * r and ((up >= 2 * body and lo <= 0.25 * r and up > 0)
                                                                 or (lo >= 2 * body and up <= 0.25 * r and lo > 0))))


def cpr_from_bars(bars: Sequence[tuple]) -> Optional[dict]:
    """The session's CPR grid from the previous session's 1-min bars (t, o, h, l, c) from 09:15."""
    bars = [b for b in bars if b[0].time() >= SESSION_OPEN]
    if not bars:
        return None
    from labs.engine.oi_unwind_tracker import cpr_levels
    return cpr_levels(max(b[2] for b in bars), min(b[3] for b in bars), bars[-1][4])


def previous_cpr(day: date, bars_fn: Callable[[str], Sequence[tuple]]) -> Optional[dict]:
    """CPR from the latest session in the 7 days before `day` that has bars."""
    for back in range(1, 8):
        try:
            bars = bars_fn((day - timedelta(days=back)).isoformat())
        except Exception:
            bars = None
        cpr = cpr_from_bars(bars or [])
        if cpr:
            return cpr
    return None


def cpr_room_blocked(side: str, spot: float, cpr: Optional[dict], pts: float = CPR_ROOM_PTS) -> bool:
    if not cpr:
        return False
    levels = [float(cpr[k]) for k in CPR_KEYS]
    if side == "CALL":
        ahead = [x for x in levels if x >= spot + 0.01]
        level = min(ahead) if ahead else None
    else:
        ahead = [x for x in levels if x <= spot - 0.01]
        level = max(ahead) if ahead else None
    return level is not None and abs(level - spot) <= pts


def last_finished(bars: Sequence[tuple], now: datetime) -> Optional[tuple]:
    """(o, h, l, c) of the last 1-min bar (t, o, h, l, c) that ended at or before `now`, bars from 09:15."""
    bars = [b for b in bars if b[0].time() >= SESSION_OPEN]
    ends = [b[0].replace(second=0, microsecond=0) + timedelta(minutes=1) for b in bars]
    i = bisect.bisect_right(ends, now) - 1
    return tuple(bars[i][1:5]) if i >= 0 else None


def block_reason(side: str, spot: float, now: datetime, cpr: Optional[dict], bars: Sequence[tuple]) -> Optional[str]:
    """Why a fresh ENTER on `side` at `now` is held, or None."""
    if cpr_room_blocked(side, spot, cpr):
        return REASON_CPR
    candle = last_finished(bars, now)
    if candle is not None and small_body(*candle):
        return REASON_CANDLE
    return None
