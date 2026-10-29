"""Paper-only Gold CCI V2: V1 entries/exits plus a 3% daily NET margin target.

Pure replay: no I/O, broker or mutable V1 settings. The caller supplies the
session's frozen estimated margin ratio. Historical margins are not invented.
"""
from __future__ import annotations

from datetime import date, datetime
import math

import numpy as np
import pandas as pd

from labs.engine.charges import mcx_futures_round_trip_charges
from live.engine import gold_cci_engine as v1

DAILY_TARGET_PCT = 3.0
QTY = v1.LOT_QTY
SLIPPAGE_RS = 2 * v1.TICK_SIZE * QTY


def net_pnl(entry: float, exit_: float) -> float:
    charges = mcx_futures_round_trip_charges(exit_, entry, QTY)["raw_total"]
    return round((entry - exit_) * QTY - charges - SLIPPAGE_RS, 2)


def daily_exit_price(entry: float, remaining_net: float) -> float:
    """Highest whole-tick buyback price meeting the remaining daily NET goal."""
    lo, hi = 0.0, entry
    for _ in range(60):
        mid = (lo + hi) / 2
        if net_pnl(entry, mid) >= remaining_net:
            lo = mid
        else:
            hi = mid
    return math.floor(lo / v1.TICK_SIZE) * v1.TICK_SIZE


def replay(frame: pd.DataFrame, start: date | str, *, margin_rate: float,
           cutoff: datetime | None = None, end: date | str | None = None,
           capital_by_day: dict[str, float] | None = None) -> dict:
    if not isinstance(margin_rate, (int, float)) or not math.isfinite(margin_rate) or not 0 < margin_rate <= 1:
        raise ValueError("A valid frozen estimated margin ratio is required")
    g = v1.base.build_bars(frame, cutoff)
    first = date.fromisoformat(str(start)[:10])
    last = date.fromisoformat(str(end)[:10]) if end is not None else None
    window = np.array([d >= first and (last is None or d <= last) for d in g["days"]], dtype=bool) & g["day_ok"]
    if g["n"] < 2:
        return {"grid": g, "trades": [], "signals": [], "daily": {}}
    eligible = v1.base.entry_ok(g) & window[g["day_id"]]
    eligible[:-1] &= window[g["day_id"]][1:]
    eligible[-1] = False
    eligible[g["n_known"] - 1:] = False
    vals = v1.values(g)
    readings = {k: v1.base.broadcast(v[0], v[1]) for k, v in vals.items()}
    trades, signals, days = [], [], {}
    busy_until = -1
    for i, outcome in v1.base.member_events(v1.RULE, v1.states(g, vals), eligible):
        signal = {"signal_idx": i, "signal_ts": pd.Timestamp(g["ts"][i]), "outcome": outcome,
                  **{k: float(readings[k][i]) for k in ("cci", "rsi", "plus_di", "minus_di")}}
        if outcome == "fire":
            key = g["days"][g["day_id"][i + 1]].isoformat()
            state = days.get(key)
            if state and state["target_hit"]:
                signal["outcome"] = "daily_target"
            elif busy_until is None or i < busy_until:
                signal["outcome"] = "in_trade"
            else:
                entry = float(g["open"][i + 1])
                if state is None:
                    capital = (capital_by_day or {}).get(key, entry * QTY * margin_rate)
                    if not math.isfinite(capital) or capital <= 0:
                        raise ValueError("Invalid frozen daily capital")
                    state = days[key] = {"capital_rs": round(capital, 2),
                        "target_rs": round(capital * DAILY_TARGET_PCT / 100, 2),
                        "realized_net_rs": 0.0, "target_hit": False}
                dist = entry * v1.STOP_PCT / 100
                original_target = entry - v1.TARGET_R * dist
                daily_target = daily_exit_price(entry, state["target_rs"] - state["realized_net_rs"])
                target = max(original_target, daily_target)
                res = v1.simulate(g, i, dist, target_r=(entry - target) / dist)
                closed = res["status"] == "closed"
                mark = res["exit"] if closed else (
                    float(g["close"][res["mark_idx"]]) if res["mark_idx"] >= 0 else entry)
                reason = res.get("reason")
                if closed:
                    state["realized_net_rs"] = round(state["realized_net_rs"] + net_pnl(entry, mark), 2)
                    state["target_hit"] = state["realized_net_rs"] >= state["target_rs"] - .005
                    if reason == "target" and state["target_hit"]:
                        reason = "daily_target"
                trades.append({"side": v1.SIDE, "signal_idx": i, "entry_idx": i + 1,
                    "exit_idx": res["exit_idx"] if closed else None,
                    "signal_ts": pd.Timestamp(g["ts"][i]), "entry_ts": pd.Timestamp(g["ts"][i + 1]),
                    "exit_ts": pd.Timestamp(g["ts"][res["exit_idx"]]) if closed else None,
                    "trade_date": key, "entry_price": entry, "exit_price": mark,
                    "stop_price": res["stop"], "target_price": res["target"], "stop_dist": dist,
                    "stop_moved": bool(res["moved"]), "stop_now": float(res["stop_now"]),
                    "status": res["status"], "reason": reason, "bars_held": res["held"],
                    "points": entry - mark, "margin_rs": round(entry * QTY * margin_rate, 2)})
                busy_until = res["exit_idx"] if closed else None
                signal["outcome"] = "taken"
        signals.append(signal)
    return {"grid": g, "trades": trades, "signals": signals, "daily": days}
