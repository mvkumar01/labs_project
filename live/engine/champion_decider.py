"""Replay-to-now decision engine for the live runner.

On every completed-bar tick the runner rebuilds the day's alpha frame from all
COMPLETED 5-min bars and replays it through the single source of truth,
`champion_sim.simulate(close_eod=False)`. The resulting in-flight position is the
*target*; `reconcile()` compares it to the bot's actual position and emits one
ENTER / EXIT / HOLD action. This makes the live bot == the paper tracker ==
research by construction, and is inherently restart-safe (the target is
recomputed from scratch each tick — no persisted signal-engine state to drift).

Pure decision logic: no broker, no order placement, no labs imports. The runner
owns execution (symbol resolution, qty, routing, ledger).
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pandas as pd

from live.engine import champion_inputs, champion_sim
from live.engine.alpha_hybrid import _read_locked_hybrid_state

IST = timezone(timedelta(hours=5, minutes=30))


def _resolve_day(trade_date: str, override: dict | None) -> dict | None:
    """Range + cell context from the backfill override or the locked hybrid
    state. None => no-trade (SKIP / not locked / no range)."""
    if override is not None:
        if override.get("skip") or override.get("bucket") == "SKIP":
            return None
        return {"lower": float(override["lower"]), "upper": float(override["upper"]),
                "bucket": override["bucket"], "direction": override["direction"],
                "vix": override.get("vix"), "biggap": bool(override.get("pc400_v210_biggap"))}
    state = _read_locked_hybrid_state(trade_date)
    if state is None:
        return None
    return {"lower": float(state["lower"]), "upper": float(state["upper"]),
            "bucket": state.get("bucket") or "PC50", "direction": state.get("direction"),
            "vix": state.get("vix_at_open"), "biggap": bool(state.get("pc400_v210_biggap"))}


def champion_target(trade_date: str | None = None, now_ist: datetime | None = None,
                    override: dict | None = None) -> dict | None:
    """Replay completed bars up to `now_ist` and return the target position.

    Returns:
      None                                     -> no-trade day (SKIP / unlocked)
      {"position": "FLAT", ...}                -> flat after the last bar
      {"position": "CALL"|"PUT", entry_spot,
       entry_rule, tier, gap_direction, ...}   -> in-flight position is target
    The dict also carries `bucket`, `direction`, `last_closed_reason`, and
    `n_closed` for logging.
    """
    trade_date = trade_date or datetime.now(IST).date().isoformat()
    now_ist = now_ist or datetime.now(IST)
    day = _resolve_day(trade_date, override)
    if day is None:
        return None

    tier, direction = day["bucket"], day["direction"]
    # Resolve cell context first (sgap needs 09:15 open + prev close) so the
    # alpha source (regime vs gemini_c2) + formula follow the Run F routing.
    ohlc = champion_sim.OHLC(champion_inputs.ohlc_by_minute(trade_date))
    sgap, weekday, use_trail, regime = champion_inputs.day_context(
        trade_date, ohlc, direction, day["vix"])
    range_source, use_abs = champion_inputs.alpha_source(
        tier, direction, day["vix"], sgap, day["biggap"])
    try:
        _, adf, ce_map, pe_map = champion_inputs.build_sim_inputs(
            trade_date, day["lower"], day["upper"], use_abs, range_source=range_source)
    except Exception:
        return None
    if adf is None or adf.empty:
        return None

    # Completed-bar discipline: the in-progress bucket (start + 5min > now) is
    # excluded so we only ever act on closed bars — matches alpha_hybrid.
    if trade_date == now_ist.date().isoformat():
        cutoff = pd.Timestamp(now_ist) - pd.Timedelta(minutes=5)
        adf = adf[adf["timestamp"] <= cutoff].reset_index(drop=True)
    if len(adf) < 2:
        return {"position": "FLAT", "bucket": tier, "direction": direction,
                "last_closed_reason": None, "n_closed": 0}

    _, trades, open_state = champion_sim.simulate(
        adf, ce_map, pe_map, ohlc, trade_date, use_trail, sgap, tier,
        weekday, regime, day["lower"], day["upper"],
        close_eod=False, return_state=True)

    last_reason = trades[-1]["reason"] if trades else None
    if open_state is None:
        return {"position": "FLAT", "bucket": tier, "direction": direction,
                "last_closed_reason": last_reason, "n_closed": len(trades)}
    return {"position": open_state["side"], "bucket": tier, "direction": direction,
            "entry_spot": open_state["entry_spot"], "entry_rule": open_state["entry_rule"],
            "tier": open_state["tier"], "gap_direction": open_state["gap_direction"],
            "last_closed_reason": last_reason, "n_closed": len(trades)}


def reconcile(target: dict | None, current_side: str | None) -> dict:
    """Compare the replay target to the bot's actual position -> one action.

    current_side: "CALL"|"PUT" if the bot holds a position, else None.
    Returns {"action": "ENTER"|"EXIT"|"HOLD", "side", "reason", "rule"}.
    """
    if not target or target.get("position") in (None, "FLAT"):
        if current_side is not None:
            return {"action": "EXIT", "side": None,
                    "reason": (target or {}).get("last_closed_reason") or "champion_exit",
                    "rule": None}
        return {"action": "HOLD", "side": None, "reason": "flat", "rule": None}

    tgt_side = target["position"]            # CALL or PUT
    if current_side is None:
        return {"action": "ENTER", "side": tgt_side,
                "reason": f"champion_{(target.get('entry_rule') or 'entry').lower()}",
                "rule": target.get("entry_rule")}
    if current_side != tgt_side:
        # Opposite side held — exit first; the next tick re-enters the target.
        return {"action": "EXIT", "side": None, "reason": "champion_flip", "rule": None}
    return {"action": "HOLD", "side": current_side, "reason": "champion_hold", "rule": None}
