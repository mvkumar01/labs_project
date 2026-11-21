"""Intraday rule backtests on one-minute index bars.

A rule (rules.Rule) = an entry event (a fixed time, an opening-range / previous-day / CPR breakout, an EMA cross or
an RSI extreme), optional day filters (gap, weekday, VIX, entry window, move from the open, CPR position) and exits
(stop / target / trailing in index points, a time exit, a daily loss cap, trades per day).

Mechanics (no look-ahead):
  * every event is decided on a COMPLETED one-minute bar and entered at the NEXT minute's open; a fixed-time entry
    enters at that minute's open;
  * exits are checked bar by bar after the entry; a minute that touches both the stop and the target counts as the
    stop (worst case); a bar that opens through the stop fills at its open; the trailing stop uses the best price
    up to the PREVIOUS bar;
  * re-entries need a fresh event (a new crossing) after the previous exit;
  * costs per side in index points: Indian futures statutory charges (~1.3 bps) + slippage (0.4 bp).
Days are independent, so a rule's daily P&L series is computed once and reused by the validation checks.
"""
from __future__ import annotations

from dataclasses import asdict
from datetime import time

import numpy as np
import pandas as pd

from . import store
from .rules import Rule

MINUTES = 375                       # 09:15 .. 15:29
EOD_K = 370                         # 15:25: last exit
COST_BPS = (1.3, 0.4)               # statutory, slippage - per side, of the index level


def k_of(hhmm: str) -> int:
    h, m = (int(x) for x in hhmm.split(":"))
    return (h * 60 + m) - (9 * 60 + 15)


class Market:
    """One index's minute bars as day x minute matrices plus the daily context the filters need."""

    def __init__(self, symbol: str, bars: pd.DataFrame, vix: pd.Series | None = None):
        self.symbol = symbol
        days = bars.index.normalize()
        k = ((bars.index.hour * 60 + bars.index.minute) - (9 * 60 + 15)).to_numpy()
        ok = (k >= 0) & (k < MINUTES)
        bars, days, k = bars[ok], days[ok], k[ok]
        self.days = pd.DatetimeIndex(sorted(days.unique()))
        di = self.days.get_indexer(days)
        n = len(self.days)
        M = {c: np.full((n, MINUTES), np.nan) for c in ("Open", "High", "Low", "Close")}
        for c in M:
            M[c][di, k] = bars[c].to_numpy(float)
        # fill missing minutes flat at the last close (no invented movement)
        c = pd.DataFrame(M["Close"]).ffill(axis=1).to_numpy()
        first_open = pd.DataFrame(M["Open"]).bfill(axis=1).to_numpy()[:, 0]
        c = np.where(np.isnan(c), first_open[:, None], c)
        for name in ("Open", "High", "Low"):
            M[name] = np.where(np.isnan(M[name]), c, M[name])
        self.o, self.h, self.l, self.c = M["Open"], M["High"], M["Low"], c
        self.day_open = self.o[:, 0]
        self.prev_close = np.r_[np.nan, self.c[:-1, -1]]
        ph, pl, pc = np.r_[np.nan, self.h.max(axis=1)[:-1]], np.r_[np.nan, self.l.min(axis=1)[:-1]], self.prev_close
        self.prev_high, self.prev_low = ph, pl
        p = (ph + pl + pc) / 3
        bc = (ph + pl) / 2
        tc = 2 * p - bc
        self.tc, self.bc = np.maximum(tc, bc), np.minimum(tc, bc)
        self.r1, self.s1 = 2 * p - pl, 2 * p - ph
        self.gap_pct = (self.day_open / self.prev_close - 1) * 100
        self.weekday = self.days.weekday.to_numpy()
        if vix is not None and len(vix):
            vix = vix[~pd.DatetimeIndex(vix.index).normalize().duplicated(keep="last")]
            vix.index = pd.DatetimeIndex(vix.index).normalize()
            self.vix = vix.reindex(self.days).to_numpy(float)
        else:
            self.vix = np.full(n, np.nan)
        self._ind_cache: dict = {}

    def slice_days(self, start: str | None, end: str | None) -> np.ndarray:
        m = np.ones(len(self.days), bool)
        if start:
            m &= self.days >= pd.Timestamp(start)
        if end:
            m &= self.days <= pd.Timestamp(end)
        m[0] = False                       # the first stored day has no previous session
        return np.flatnonzero(m)

    # ---- tf-minute indicators on the continuous series (completed tf bars only), mapped back to minutes
    def _tf_closes(self, tf: int):
        key = ("tf", tf)
        if key not in self._ind_cache:
            ks = np.arange(tf - 1, MINUTES, tf)                 # minute index at which each tf bar completes
            closes = self.c[:, ks].reshape(-1)                  # day-major, continuous across days
            self._ind_cache[key] = (ks, closes)
        return self._ind_cache[key]

    def ema_cross(self, fast: int, slow: int, tf: int) -> np.ndarray:
        """+1 where the fast EMA crosses above the slow on a completed tf bar, -1 crosses below; at the completing
        minute (entry the minute after)."""
        ks, closes = self._tf_closes(tf)
        s = pd.Series(closes)
        f, sl = s.ewm(span=fast, adjust=False).mean().to_numpy(), s.ewm(span=slow, adjust=False).mean().to_numpy()
        above = f > sl
        ev = np.zeros(len(closes))
        ev[1:] = np.where(above[1:] & ~above[:-1], 1, np.where(~above[1:] & above[:-1], -1, 0))
        ev[: slow * 3] = 0                                      # warm-up
        out = np.zeros_like(self.c)
        out[:, ks] = ev.reshape(len(self.days), len(ks))
        return out

    def rsi_extreme(self, period: int, tf: int, lower: float, upper: float) -> np.ndarray:
        """+1 when RSI crosses below `lower` (oversold, buy), -1 when it crosses above `upper` (sell)."""
        ks, closes = self._tf_closes(tf)
        d = np.diff(closes, prepend=closes[0])
        up = pd.Series(np.clip(d, 0, None)).ewm(alpha=1 / period, adjust=False).mean().to_numpy()
        dn = pd.Series(np.clip(-d, 0, None)).ewm(alpha=1 / period, adjust=False).mean().to_numpy()
        rsi = 100 - 100 / (1 + up / np.where(dn > 0, dn, np.nan))
        rsi = np.where(np.isnan(rsi), 100.0, rsi)
        ev = np.zeros(len(closes))
        ev[1:] = np.where((rsi[1:] < lower) & (rsi[:-1] >= lower), 1,
                          np.where((rsi[1:] > upper) & (rsi[:-1] <= upper), -1, 0))
        ev[: period * 3] = 0
        out = np.zeros_like(self.c)
        out[:, ks] = ev.reshape(len(self.days), len(ks))
        return out


def _cross_up(c: np.ndarray, lvl: np.ndarray) -> np.ndarray:
    prev = np.c_[np.full((c.shape[0], 1), np.inf), c[:, :-1]]
    return (c > lvl[:, None]) & (prev <= lvl[:, None])


def _cross_dn(c: np.ndarray, lvl: np.ndarray) -> np.ndarray:
    prev = np.c_[np.full((c.shape[0], 1), -np.inf), c[:, :-1]]
    return (c < lvl[:, None]) & (prev >= lvl[:, None])


def events(mk: Market, rule: Rule) -> np.ndarray:
    """Day x minute matrix of entry events: +1 long, -1 short, 0 none, placed at the minute ENTERED (open)."""
    e = rule.entry
    t = e.get("type")
    ev = np.zeros_like(mk.c)
    if t == "time":
        k = k_of(e["at"])
        sign = {"long": 1, "short": -1}.get(rule.side, 0)
        if rule.side == "gap":
            sign_vec = np.sign(mk.gap_pct)
            ev[:, k] = np.nan_to_num(sign_vec)
        elif rule.side == "trend":                 # with the move from the open at the entry minute
            ev[:, k] = np.sign(mk.c[:, max(k - 1, 0)] - mk.day_open) if k > 0 else 0
        else:
            ev[:, k] = sign
        return ev
    if t == "orb":
        n = int(e.get("minutes", 15))
        hi, lo = mk.h[:, :n].max(axis=1), mk.l[:, :n].min(axis=1)
        sig = _cross_up(mk.c, hi).astype(int) - _cross_dn(mk.c, lo).astype(int)
        sig[:, :n] = 0
    elif t == "prev_day":
        sig = _cross_up(mk.c, mk.prev_high).astype(int) - _cross_dn(mk.c, mk.prev_low).astype(int)
    elif t == "cpr":
        sig = _cross_up(mk.c, mk.tc).astype(int) - _cross_dn(mk.c, mk.bc).astype(int)
        sig[:, 0] = 0
    elif t == "ema_cross":
        sig = mk.ema_cross(int(e.get("fast", 9)), int(e.get("slow", 21)), int(e.get("tf", 5))).astype(int)
    elif t == "rsi":
        sig = mk.rsi_extreme(int(e.get("period", 14)), int(e.get("tf", 5)), float(e.get("lower", 30)),
                             float(e.get("upper", 70))).astype(int)
    else:
        raise ValueError(f"Unknown entry type '{t}'")
    if rule.side == "long":
        sig = np.where(sig > 0, 1, 0)
    elif rule.side == "short":
        sig = np.where(sig < 0, -1, 0)
    ev[:, 1:] = sig[:, :-1]                         # decided on the completed bar, entered at the next open
    return ev


def day_filter(mk: Market, rule: Rule, sign: int, d: int, k: int) -> bool:
    for f in rule.filters:
        t = f.get("type")
        if t == "gap":
            g = mk.gap_pct[d] * (sign if f.get("with_side", True) else 1)
            if f.get("min") is not None and not g >= f["min"]:
                return False
            if f.get("max") is not None and not g <= f["max"]:
                return False
        elif t == "weekday" and mk.weekday[d] not in f["days"]:
            return False
        elif t == "vix":
            v = mk.vix[d]
            if np.isnan(v) or (f.get("min") is not None and v < f["min"]) or (f.get("max") is not None and v > f["max"]):
                return False
        elif t == "window":
            if not (k_of(f["from"]) <= k <= k_of(f["to"])):
                return False
        elif t == "day_move":
            mv = (mk.c[d, max(k - 1, 0)] - mk.day_open[d]) * sign
            if f.get("min") is not None and not mv >= f["min"]:
                return False
            if f.get("max") is not None and not mv <= f["max"]:
                return False
        elif t == "cpr_position":
            px = mk.c[d, max(k - 1, 0)]
            inside = mk.bc[d] <= px <= mk.tc[d]
            if (f.get("where") == "outside" and inside) or (f.get("where") == "inside" and not inside):
                return False
    return True


def simulate(mk: Market, rule: Rule, day_idx: np.ndarray, ev: np.ndarray | None = None,
             stop: float | None = ..., target: float | None = ..., trail: float | None = ...) -> tuple[list[dict], np.ndarray]:
    """Trades and the per-day net P&L in index points for the given days. `stop` / `target` override the rule's
    (used by the sensitivity grid / walk-forward)."""
    ev = events(mk, rule) if ev is None else ev
    sp = rule.stop_pts if stop is ... else stop
    tp = rule.target_pts if target is ... else target
    tr = rule.trail_pts if trail is ... else trail
    tr_after = rule.trail_after_pts or 0.0
    k_exit = min(k_of(rule.exit_time), EOD_K) if rule.exit_time else EOD_K
    cost_frac = sum(COST_BPS) / 1e4
    trades: list[dict] = []
    pnl = np.zeros(len(day_idx))
    for j, d in enumerate(day_idx):
        row = ev[d]
        cand = np.flatnonzero(row[:k_exit])
        if not len(cand):
            continue
        o, h, l, c = mk.o[d], mk.h[d], mk.l[d], mk.c[d]
        k_from, n_tr, day_pts = 0, 0, 0.0
        for k in cand:
            if k < k_from or n_tr >= rule.max_trades:
                continue
            if rule.day_loss_pts and day_pts <= -rule.day_loss_pts:
                break
            sign = int(np.sign(row[k]))
            if not day_filter(mk, rule, sign, d, k):
                continue
            entry = o[k]
            seg_h, seg_l, seg_o = h[k:k_exit], l[k:k_exit], o[k:k_exit]
            if sign > 0:
                fav, adv = seg_h - entry, entry - seg_l
            else:
                fav, adv = entry - seg_l, seg_h - entry
            n = len(seg_h)

            def first(mask):
                return int(np.argmax(mask)) if mask.any() else n

            i_fix = first(adv >= sp) if sp else n
            i_tr, trail_lvl = n, None
            if tr:
                best_prev = np.r_[0.0, np.maximum.accumulate(fav)[:-1]]   # best excursion up to the PREVIOUS bar
                trail_lvl = entry + sign * (best_prev - tr)
                touched = (seg_l <= trail_lvl) if sign > 0 else (seg_h >= trail_lvl)
                i_tr = first((best_prev >= tr_after) & touched)
            i_s = min(i_fix, i_tr)
            i_t = first(fav >= tp) if tp else n
            if i_s <= i_t and i_s < n:                            # stop first (also when both in one bar)
                opts = []
                if i_fix == i_s:
                    opts.append((entry - sign * sp, "Stop loss"))
                if i_tr == i_s:
                    opts.append((float(trail_lvl[i_s]), "Trailing stop"))
                # both in the same bar: the level nearer the trade is reached first
                lvl, why = (max(opts) if sign > 0 else min(opts))
                gap_through = (seg_o[i_s] - lvl) * sign < 0 and i_s > 0
                exit_px = seg_o[i_s] if gap_through else lvl
                k_out = k + i_s
            elif i_t < n:
                lvl = entry + sign * tp
                gap_through = (seg_o[i_t] - lvl) * sign > 0 and i_t > 0
                exit_px, why, k_out = (seg_o[i_t] if gap_through else lvl), "Target", k + i_t
            else:
                exit_px, why, k_out = o[k_exit], ("Time exit" if rule.exit_time else "End of day"), k_exit
            gross = (exit_px - entry) * sign
            cost = (entry + exit_px) * cost_frac
            net = gross - cost
            day_pts += net
            n_tr += 1
            k_from = k_out + 1
            trades.append({"day": d, "k_in": int(k), "k_out": int(k_out), "side": "Long" if sign > 0 else "Short",
                           "entry_price": float(entry), "exit_price": float(exit_px), "gross_pts": float(gross),
                           "net_pts": float(net), "exit_reason": why})
        pnl[j] = day_pts
    return trades, pnl


def stamp(mk: Market, d: int, k: int) -> pd.Timestamp:
    return mk.days[d] + pd.Timedelta(minutes=9 * 60 + 15 + k)


def describe(rule: Rule) -> str:
    e = rule.entry
    t = e.get("type")
    side = {"long": "Buy", "short": "Sell", "both": "Trade", "gap": "Trade with the gap", "trend": "Trade with the day's move"}.get(rule.side, rule.side)
    what = {"time": f"at {e.get('at')}", "orb": f"on a {e.get('minutes', 15)}-minute opening-range breakout",
            "prev_day": "on a break of the previous day's high / low", "cpr": "on a break above TC / below BC of the CPR",
            "ema_cross": f"on an EMA {e.get('fast', 9)}/{e.get('slow', 21)} cross ({e.get('tf', 5)}-min bars)",
            "rsi": f"on RSI({e.get('period', 14)}) {e.get('lower', 30)}/{e.get('upper', 70)} ({e.get('tf', 5)}-min bars)"}.get(t, t)
    ex = []
    if rule.stop_pts:
        ex.append(f"stop {rule.stop_pts:g}")
    if rule.target_pts:
        ex.append(f"target {rule.target_pts:g}")
    if rule.trail_pts:
        ex.append(f"trail {rule.trail_pts:g}")
    ex.append(f"out by {rule.exit_time or '15:25'}")
    return f"{side} {store.INDICES[rule.symbol]['name']} {what} · " + ", ".join(ex) + " (points)"
