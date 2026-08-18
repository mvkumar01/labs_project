"""Replay-to-now decision engine for the live runner.

On every new exact-mark alpha snapshot or completed one-minute spot candle the
runner rebuilds the day's alpha frame and replays it through the source of truth,
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

from live.engine import champion_inputs, champion_sim, champion_v213
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
                "vix": override.get("vix"), "vix_source": "backfill_override",
                "previous_session_date": override.get("previous_session_date"),
                "prev_close": override.get("prev_close"),
                "prev_close_source": override.get("prev_close_source"),
                "open_spot": override.get("open_spot"),
                "open_spot_source": override.get("open_spot_source"),
                "validate_shared_context": not bool(
                    override.get("_trust_historical_context")
                ),
                "biggap": bool(override.get("pc400_v210_biggap"))}
    state = _read_locked_hybrid_state(trade_date)
    if state is None:
        return None
    return {"lower": float(state["lower"]), "upper": float(state["upper"]),
            "bucket": state.get("bucket") or "PC50", "direction": state.get("direction"),
            "vix": state.get("vix_at_open"), "vix_source": "locked_hybrid_state",
            "previous_session_date": state.get("prev_close_ref_date"),
            "prev_close": state.get("prev_close"),
            "prev_close_source": state.get("prev_close_source"),
            "open_spot": state.get("verified_open"),
            "open_spot_source": state.get("verified_open_source"),
            "validate_shared_context": False,
            "biggap": bool(state.get("pc400_v210_biggap"))}


def champion_target(trade_date: str | None = None, now_ist: datetime | None = None,
                    override: dict | None = None,
                    enable_entry_spot_recovery: bool = False,
                    entry_spot_close_confirmed: bool = False,
                    enable_v211_risk_authority: bool = False,
                    live_execution_spot: float | None = None,
                    extra_minutes: dict | None = None) -> dict | None:
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

    tier = day["bucket"]
    # Resolve cell context first (sgap needs 09:15 open + prev close) so the
    # alpha source (regime vs gemini_c2) + formula follow the Run F routing.
    # `extra_minutes` carries minutes the live runner already froze from its
    # two-second spot stream. They only fill minutes the collector has not
    # written yet, letting a 60-second boundary decision execute at :00-:05
    # instead of waiting on the CSV. Empty for paper/backfill callers.
    ohlc = champion_sim.OHLC(
        champion_inputs.ohlc_by_minute(trade_date, extra_minutes=extra_minutes)
    )
    try:
        context = champion_inputs.resolve_day_context(
            trade_date,
            ohlc,
            day["direction"],
            day["vix"],
            vix_source=day.get("vix_source", "supplied"),
            previous_session_date=day.get("previous_session_date"),
            prev_close=day.get("prev_close"),
            prev_close_source=day.get("prev_close_source"),
            open_spot=day.get("open_spot"),
            open_spot_source=day.get("open_spot_source"),
            validate_shared_context=day.get("validate_shared_context", True),
        )
    except champion_inputs.ContextInputError as exc:
        return {
            "position": "UNAVAILABLE",
            "bucket": tier,
            "direction": day["direction"],
            "context_error": str(exc),
        }
    range_source, use_abs = champion_inputs.alpha_source(
        tier, context.direction, context.vix_open, context.sgap, day["biggap"])
    try:
        _, adf, ce_map, pe_map = champion_inputs.build_sim_inputs(
            trade_date, day["lower"], day["upper"], use_abs, range_source=range_source)
    except Exception as exc:
        # Transient input failure (shared-store CSV mid-write, IO blip) must
        # NOT read as FLAT: a None target with an open position would flatten a
        # healthy real-money book. UNAVAILABLE -> reconcile HOLDs (same
        # treatment as ContextInputError above).
        return {
            "position": "UNAVAILABLE",
            "bucket": tier,
            "direction": day["direction"],
            "context_error": f"{type(exc).__name__}: {exc}",
        }
    if adf is None or adf.empty:
        return {
            "position": "UNAVAILABLE",
            "bucket": tier,
            "direction": day["direction"],
            "context_error": "alpha frame empty/not yet available",
        }

    # OI alpha is a point snapshot at the five-minute mark; unlike OHLC it does
    # not wait for the bucket to close. Spot stops/recoveries still use only
    # completed one-minute OHLC rows supplied by champion_inputs.
    cutoff = champion_sim.exact_mark_cutoff(trade_date, now_ist)
    if len(adf) < 2:
        return {"position": "FLAT", "bucket": tier, "direction": context.direction,
                "last_closed_reason": None, "n_closed": 0}

    next_open_fallback = (
        (now_ist, live_execution_spot)
        if enable_v211_risk_authority and live_execution_spot is not None
        else None
    )
    if enable_v211_risk_authority:
        _, trades, open_state = champion_v213.simulate(
            adf, ce_map, pe_map, ohlc, trade_date, context.use_trail,
            context.sgap, tier, context.weekday, context.regime,
            day["lower"], day["upper"], close_eod=False, return_state=True,
            entries_until_ts=cutoff, next_open_fallback=next_open_fallback)
    else:
        _, trades, open_state = champion_sim.simulate(
            adf, ce_map, pe_map, ohlc, trade_date, context.use_trail,
            context.sgap, tier, context.weekday, context.regime,
            day["lower"], day["upper"],
            enable_entry_spot_recovery=enable_entry_spot_recovery,
            entry_spot_close_confirmed=entry_spot_close_confirmed,
            close_eod=False, return_state=True, entries_until_ts=cutoff)

    economic_trades = [
        trade for trade in trades
        if trade.get("reason") != "RECOVERY_CANCEL_ALPHA"
    ]
    last_trade = economic_trades[-1] if economic_trades else None
    last_reason = last_trade["reason"] if last_trade else None
    last_event_id = None
    if last_trade is not None:
        last_event_id = "|".join((
            str(last_trade.get("entry_ts") or ""),
            str(last_trade.get("exit_ts") or ""),
            str(last_trade.get("pos") or ""),
            str(last_reason or ""),
        ))
    replay_meta = {
        "last_closed_reason": last_reason,
        "last_closed_event_id": last_event_id,
        "n_closed": len(economic_trades),
    }
    if open_state is None:
        return {"position": "FLAT", "bucket": tier, "direction": context.direction,
                **replay_meta}
    entry_spot = open_state["entry_spot"]
    target = {"position": open_state["side"], "bucket": tier,
              "direction": context.direction,
              "entry_spot": entry_spot, "entry_rule": open_state["entry_rule"],
              "tier": open_state["tier"], "gap_direction": open_state["gap_direction"],
              **replay_meta}
    if enable_v211_risk_authority:
        # v2.13 keeps the original v2.11 signal spot as the stop barrier while
        # using the causal recovery fill spot for option contract selection.
        target["entry_spot"] = open_state.get("signal_entry_spot", entry_spot)
        target["execution_entry_spot"] = entry_spot
    return target


def reconcile_replay_event(target: dict | None, current_side: str | None,
                           closed_count_seen: int = 0) -> dict:
    """Reconcile final position plus newly closed canonical segments.

    A target-only comparison collapses a same-side stop/re-entry cycle to HOLD:
    the replay was PUT before and after the cycle even though paper records an
    EXIT followed by a new PUT. The persisted closed-count cursor makes that
    transition observable and restart-safe.
    """
    if target and target.get("position") == "UNAVAILABLE":
        return reconcile(target, current_side)
    closed_count = int((target or {}).get("n_closed") or 0)
    if current_side is not None and closed_count > int(closed_count_seen or 0):
        return {
            "action": "EXIT",
            "side": None,
            "reason": (target or {}).get("last_closed_reason") or "champion_exit",
            "rule": None,
        }
    return reconcile(target, current_side)


def reconcile(target: dict | None, current_side: str | None) -> dict:
    """Compare the replay target to the bot's actual position -> one action.

    current_side: "CALL"|"PUT" if the bot holds a position, else None.
    Returns {"action": "ENTER"|"EXIT"|"HOLD", "side", "reason", "rule"}.
    """
    if target and target.get("position") == "UNAVAILABLE":
        return {
            "action": "HOLD",
            "side": current_side,
            "reason": "context_unavailable",
            "rule": None,
        }
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
