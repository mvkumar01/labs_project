"""MCX GOLD short on a 5-minute CCI spike: signals, the stop-to-entry exit and the replay.

Pure (numpy / pandas only, no I/O, no broker). The rule, from Strategy Tester v2's gold runs
(results_v2/gold/20261007_171048 and its four rotated folds, rule
``short|6f0f6e1560f9|1b16a8f1dbbf,7469667c0947``):

  trigger  5-minute CCI(14) above 100           - the bar on which it turns on
  gates    15-minute RSI(5) above 50            - both on at that bar
           1-hour DI- above DI+ (ADX length 7)
  entry    short at the next 1-minute bar's open; 30-bar cooldown between signals; one position
  exit     stop 0.25% of the entry price above it; once a completed bar has traded one stop
           distance in favour the stop moves to the entry price (from the next bar); target three
           stop distances; flat at the session's last bar (23:29)

Bars, timeframes, indicator conventions, operators, the firing rule and the contract join are the
crude combination's (live/engine/crudem_combo_engine.py, the port of the same engine); this
module adds CCI and the managed stop of strategy_tester/outcomes/simulate.py::simulate_managed
(kind breakeven, activate 1R, offset 0). tests/test_gold_cci.py checks it against the Tester:
states bar by bar, every trade's exit, and the run's 135 trades.

Evidence status: a FIT. The rule was found on GOLD26OCTFUT candles from 10 Mar to 25 Sep 2026 and
kept because it was found again from five different sets of training weeks and held on each
run's own unseen weeks. Sessions from 28 Sep 2026 are the first it has not seen. Nothing here may
be tuned from paper or live results.
"""
from __future__ import annotations

import math
from datetime import date, datetime
from types import SimpleNamespace

import numpy as np
import pandas as pd

from live.engine import crudem_combo_engine as base

LOT_QTY = 100                        # one GOLD lot = 1 kg, quoted per 10 g
TICK_SIZE = 1.0
SIDE = -1
STOP_PCT = 0.25
TARGET_R = 3.0
ACTIVATE_R = 1.0                     # gain, in stop distances, after which the stop moves to entry
CCI_N, CCI_TF, CCI_LEVEL = 14, 5, 100.0
RSI_N, RSI_TF, RSI_LEVEL = 5, 15, 50.0
ADX_N, ADX_TF = 7, 60
TRIGGER = "cci14@5m>100"
GATES = ("rsi5@15m>50", "adx7@1h DI->DI+")
RULE = SimpleNamespace(triggers=(TRIGGER,), gates=GATES)
RULE_ID = "short|6f0f6e1560f9|1b16a8f1dbbf,7469667c0947"
EXIT_DESC = "stop 0.25%, stop to entry after +1 stop, target 3R"
COOLDOWN_BARS, MAX_HOLD_BARS = base.COOLDOWN_BARS, base.MAX_HOLD_BARS

stitch = base.stitch


# -------------------------------------------------------------- indicators ---
def rolling_meandev(x: np.ndarray, n: int) -> np.ndarray:
    """Mean absolute deviation from the window's own mean; NaN until n finite values."""
    out = np.full(x.shape[0], np.nan)
    for i in range(n - 1, x.shape[0]):
        w = x[i - n + 1:i + 1]
        if np.isfinite(w).all():
            out[i] = np.abs(w - w.mean()).mean()
    return out


def cci(high, low, close, n: int = CCI_N) -> np.ndarray:
    tp = (high + low + close) / 3.0
    return base._safe_div(tp - base.sma(tp, n), 0.015 * rolling_meandev(tp, n))


def _on_valid(bars: dict, func) -> np.ndarray:
    """An indicator computed on the valid bars only, NaN on the gaps (as the engine does)."""
    idx = np.flatnonzero(bars["valid"])
    out = np.full(bars["n"], np.nan)
    out[idx] = func(*(np.ascontiguousarray(bars[c][idx]) for c in ("high", "low", "close")))
    return out


def values(g: dict) -> dict[str, np.ndarray]:
    """The three readings on the 1-minute clock (each from its last finished higher-timeframe bar)."""
    tfs = {tf: base.resample(g, tf) for tf in (CCI_TF, RSI_TF, ADX_TF)}
    c = _on_valid(tfs[CCI_TF], lambda h, l, cl: cci(h, l, cl, CCI_N))
    r = _on_valid(tfs[RSI_TF], lambda h, l, cl: base._indicator("rsi", h, l, cl, n=RSI_N)["rsi"])
    adx = tfs[ADX_TF]
    idx = np.flatnonzero(adx["valid"])
    di = base._indicator("adx", *(np.ascontiguousarray(adx[k][idx]) for k in ("high", "low", "close")), n=ADX_N)
    plus, minus = np.full(adx["n"], np.nan), np.full(adx["n"], np.nan)
    plus[idx], minus[idx] = di["plus_di"], di["minus_di"]
    return {"cci": (c, tfs[CCI_TF]["map"]), "rsi": (r, tfs[RSI_TF]["map"]),
            "plus_di": (plus, adx["map"]), "minus_di": (minus, adx["map"])}


def states(g: dict, vals: dict | None = None) -> dict[str, np.ndarray]:
    """The trigger and gate states on the 1-minute clock (NaN -> off)."""
    v = vals or values(g)
    with np.errstate(invalid="ignore"):
        trig = v["cci"][0] > CCI_LEVEL
        rsi_on = v["rsi"][0] > RSI_LEVEL
        di_on = v["plus_di"][0] < v["minus_di"][0]
    return {TRIGGER: base.broadcast(np.asarray(trig, dtype=bool), v["cci"][1]) & g["valid"],
            GATES[0]: base.broadcast(np.asarray(rsi_on, dtype=bool), v["rsi"][1]) & g["valid"],
            GATES[1]: base.broadcast(np.asarray(di_on, dtype=bool), v["plus_di"][1]) & g["valid"]}


# ------------------------------------------------------------------- exits ---
def simulate(g: dict, i: int, stop_dist: float, side: int = SIDE, target_r: float = TARGET_R,
             activate_r: float = ACTIVATE_R) -> dict | None:
    """Walk one trade entered at open[i + 1] with the stop-to-entry exit.

    Per valid bar, in this order: the open against the stop and target carried from the bar
    before (a gap fills at the open); intrabar touches of those levels, the stop winning when
    both are touched; the maximum hold; only then, if still open, the bar's favourable extreme
    may move the stop to the entry price, effective from the NEXT bar. Flat at the close of the
    session's last valid bar. Returns status 'closed' or 'open' (session still in progress)."""
    if not (stop_dist > 0.0) or not math.isfinite(stop_dist):
        return None
    o_, h_, l_, c_, valid, day_id = g["open"], g["high"], g["low"], g["close"], g["valid"], g["day_id"]
    n, n_known = g["n"], g["n_known"]
    j0 = i + 1
    p = float(o_[j0])
    day = day_id[j0]
    stop0 = p - side * stop_dist
    stop, target, best, moved = stop0, p + side * target_r * stop_dist, p, False
    base_row = {"entry": p, "stop": stop0, "target": target, "stop_dist": stop_dist}
    k, last_j, j = 0, -1, j0

    def done(idx, price, reason):
        return {**base_row, "status": "closed", "exit_idx": idx, "exit": float(price), "reason": reason,
                "held": k, "moved": moved, "stop_now": stop}

    while True:
        if j >= n_known and day == g["live_day"]:
            return {**base_row, "status": "open", "held": k, "mark_idx": last_j, "moved": moved, "stop_now": stop}
        if j >= n or day_id[j] != day:
            return done(last_j, c_[last_j], "eod") if last_j >= 0 else None
        if not valid[j]:
            j += 1
            continue
        k += 1
        last_j = j
        o = float(o_[j])
        stop_reason = "stop at entry" if moved else "stop"
        if side * (o - stop) <= 0.0:                       # opened beyond the carried stop
            return done(j, o, stop_reason)
        if side * (o - target) >= 0.0:                     # opened beyond the target
            return done(j, o, "target")
        adverse, fav = (l_[j], h_[j]) if side > 0 else (h_[j], l_[j])
        if side * (adverse - stop) <= 0.0:                 # the stop wins when both are touched
            return done(j, stop, stop_reason)
        if side * (fav - target) >= 0.0:
            return done(j, target, "target")
        if k >= MAX_HOLD_BARS:
            return done(j, c_[j], "time")
        if side * (fav - best) > 0.0:                      # still open: this bar may move the stop
            best = float(fav)
        if side * (best - p) >= activate_r * stop_dist and side * (p - stop) > 0.0:
            stop, moved = p, True
        j += 1


# ------------------------------------------------------------------ replay ---
def replay(frame: pd.DataFrame, start: date | str, cutoff: datetime | None = None,
           end: date | str | None = None) -> dict:
    """Run the rule over ``frame`` (1-minute bars: ts naive IST, open, high, low, close).

    Sessions before ``start`` (and after ``end``) are warm-up only: their signals take no trades
    but still run the cooldown. Returns ``{"grid", "trades", "signals"}``: the trades and every
    turn-on of the trigger with what became of it (taken, gate, cooldown, ineligible, in_trade)."""
    g = base.build_bars(frame, cutoff)
    start = date.fromisoformat(str(start)[:10])
    end = date.fromisoformat(str(end)[:10]) if end is not None else None
    in_range = np.array([d >= start and (end is None or d <= end) for d in g["days"]], dtype=bool) & g["day_ok"]
    n = g["n"]
    if n < 2:
        return {"grid": g, "trades": [], "signals": []}
    window = in_range[g["day_id"]]
    eligible = base.entry_ok(g) & window
    eligible[:-1] &= window[1:]
    eligible[n - 1] = False
    eligible[g["n_known"] - 1:] = False                    # the next bar's open is not known yet
    vals = values(g)
    st = states(g, vals)
    reading = {k: base.broadcast(v[0], v[1]) for k, v in vals.items()}
    ts = g["ts"]
    trades, signals, busy_until = [], [], -1
    for x, what in base.member_events(RULE, st, eligible):
        row = {"signal_idx": x, "signal_ts": pd.Timestamp(ts[x]), "outcome": what,
               "cci": float(reading["cci"][x]), "rsi": float(reading["rsi"][x]),
               "plus_di": float(reading["plus_di"][x]), "minus_di": float(reading["minus_di"][x])}
        if what == "fire":
            if busy_until is None or x < busy_until:
                row["outcome"] = "in_trade"
            else:
                dist = float(g["open"][x + 1]) * STOP_PCT / 100.0
                res = simulate(g, x, dist)
                if res is None:
                    row["outcome"] = "no_stop_distance"
                else:
                    closed = res["status"] == "closed"
                    mark = res["exit"] if closed else \
                        (float(g["close"][res["mark_idx"]]) if res["mark_idx"] >= 0 else res["entry"])
                    trades.append({
                        "side": SIDE, "signal_idx": x, "entry_idx": x + 1,
                        "exit_idx": res["exit_idx"] if closed else None,
                        "signal_ts": pd.Timestamp(ts[x]), "entry_ts": pd.Timestamp(ts[x + 1]),
                        "exit_ts": pd.Timestamp(ts[res["exit_idx"]]) if closed else None,
                        "trade_date": g["days"][g["day_id"][x + 1]].isoformat(),
                        "entry_price": res["entry"], "exit_price": mark, "stop_price": res["stop"],
                        "target_price": res["target"], "stop_dist": dist, "stop_moved": bool(res["moved"]),
                        "stop_now": float(res["stop_now"]),
                        "status": res["status"], "reason": res.get("reason"), "bars_held": res["held"],
                        "points": SIDE * (mark - res["entry"]),
                    })
                    busy_until = res["exit_idx"] if closed else None
                    row["outcome"] = "taken"
        signals.append(row)
    return {"grid": g, "trades": trades, "signals": signals}


def levels(entry_price: float) -> tuple[float, float, float]:
    """(stop distance, stop price, target price) for a short filled at ``entry_price``."""
    dist = entry_price * STOP_PCT / 100.0
    return dist, entry_price - SIDE * dist, entry_price + SIDE * TARGET_R * dist
