"""Price-action exit for the SENSEX Proposer: read the 1-min bars between the 5-min prints.

Pure, broker-free. The Proposer's own flip exit waits for an opposite 5-class print, and that
signal is mostly a 50-minute average: on the losing trades of Jun-Sep 2026 it turned a median 35-46
minutes after entry. These detectors read SENSEX 1-min closes directly and fired 2-16 minutes in
(alphaIMB research/experiments/2026-09-09_proposer_v1_reverse_engineering, REBUILD.md sec. 12).

A spec is a short string:

  renko-B[-R]  Renko bricks of B SENSEX points built on the session's 1-min closes from its first
               bar (a brick in the same direction every B points, a reversal after 2 x B, as
               usual). Fires when the last R bricks (default 1) are against the position and all
               of them formed in or after the entry bar.
  consec-K     the last K 1-min closes, all in or after the entry bar, each moved against the
               position (any size).

"Against" is against the open position on SENSEX spot: falling for a call (CE), rising for a put.
`closes` are COMPLETED 1-min bar closes of the session in time order; `entry_idx` is the index of
the bar the entry happened in.
"""
from __future__ import annotations

from typing import Sequence


def renko_bricks(closes: Sequence[float], brick: float) -> list[tuple[int, int]]:
    """[(index of the bar that formed the brick, +1 up / -1 down), ...]"""
    if not closes or brick <= 0:
        return []
    level, direction, out = float(closes[0]), 0, []
    for i, c in enumerate(closes):
        while True:
            up = level + (brick if direction >= 0 else 2 * brick)
            down = level - (brick if direction <= 0 else 2 * brick)
            if c >= up:
                level, direction = up, 1
            elif c <= down:
                level, direction = down, -1
            else:
                break
            out.append((i, direction))
    return out


def fires(spec: str, closes: Sequence[float], entry_idx: int, side: str) -> bool:
    """True when the bars say exit a `side` ('CE' / 'PE') position opened in bar `entry_idx`."""
    if not spec or entry_idx is None or entry_idx < 0 or len(closes) <= entry_idx:
        return False
    kind, *args = spec.split("-")
    adverse = -1 if side == "CE" else 1
    if kind == "renko":
        brick = float(args[0])
        need = int(args[1]) if len(args) > 1 else 1
        tail = renko_bricks(closes, brick)[-need:]
        return len(tail) == need and all(d == adverse and i >= entry_idx for i, d in tail)
    if kind == "consec":
        k = int(args[0])
        if len(closes) - entry_idx < k or len(closes) < k + 1:
            return False
        return all((closes[-j] - closes[-j - 1]) * adverse > 0 for j in range(1, k + 1))
    raise ValueError(f"unknown bar-exit spec: {spec!r}")
