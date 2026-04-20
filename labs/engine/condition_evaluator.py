"""
Evaluates individual conditions and full leg condition sets from JSON.

Condition JSON format:
    {"type": "rsi_lt", "timeframe": "5m", "params": {"period": 3, "value": 30}, "enabled": true}

Supports:
    Entry conditions:  rsi_gt, rsi_lt, ema_gt, ema_lt, sma_gt, sma_lt,
                       adx_diplus_gt_diminus, adx_diminus_gt_diplus
    Entry gates:       spot_gt_sma, spot_lt_sma, spot_above_sma_by, spot_below_sma_by,
                       ema_gt, ema_lt, sma_gt, sma_lt
    Exit/SL:           spot_gain_gte, spot_loss_gte, ltp_gain_gte, ltp_loss_gte,
                       spot_gt_sma, spot_lt_sma,
                       rsi_gt, rsi_lt, ema_gt, ema_lt, sma_gt, sma_lt,
                       sma_gt_spot, sma_lt_spot
"""
import math
from typing import Any

import pandas as pd

from labs.engine.indicator_engine import rsi_value, ema_value, sma_value, adx_values
from labs.engine.resampler import get_resampled_data


# ── Cache of resampled DataFrames for one evaluation cycle ───────────────────

class TFCache:
    """Holds pre-resampled DataFrames keyed by timeframe. Built once per bot tick."""
    def __init__(self, df_1min: pd.DataFrame, now):
        self._df1 = df_1min
        self._now = now
        self._cache: dict[str, pd.DataFrame] = {}

    def get(self, timeframe: str) -> pd.DataFrame:
        if timeframe not in self._cache:
            self._cache[timeframe] = get_resampled_data(self._df1, timeframe, now=self._now)
        return self._cache[timeframe]


# ── Single condition evaluator ───────────────────────────────────────────────

def evaluate_condition(
    cond: dict[str, Any],
    tf_cache: TFCache,
    position: dict | None = None,
    current_ltp: float | None = None,
    current_spot: float | None = None,
) -> bool:
    """
    Returns True if the condition fires. Disabled conditions always return True
    (they don't block) for entry/gate contexts; callers decide semantics for exit/SL.
    """
    if not cond.get("enabled", True):
        return True

    ctype  = cond["type"]
    params = cond.get("params", {})
    tf     = cond.get("timeframe", "5m")
    df     = tf_cache.get(tf)

    if df.empty:
        return False

    close = float(df.iloc[-1]["close"])

    match ctype:
        # ── ENTRY / GATE indicator conditions ─────────────────────────────
        case "rsi_gt":
            period = int(params.get("period", 3))
            value  = float(params.get("value", 70))
            return rsi_value(df, period) > value

        case "rsi_lt":
            period = int(params.get("period", 3))
            value  = float(params.get("value", 30))
            return rsi_value(df, period) < value

        case "ema_gt":
            pa = int(params.get("period_a", 9))
            pb = int(params.get("period_b", 21))
            return ema_value(df, pa) > ema_value(df, pb)

        case "ema_lt":
            pa = int(params.get("period_a", 9))
            pb = int(params.get("period_b", 21))
            return ema_value(df, pa) < ema_value(df, pb)

        case "adx_diplus_gt_diminus":
            period = int(params.get("period", 14))
            _, dip, dim = adx_values(df, period)
            return (not math.isnan(dip)) and dip > dim

        case "adx_diminus_gt_diplus":
            period = int(params.get("period", 14))
            _, dip, dim = adx_values(df, period)
            return (not math.isnan(dim)) and dim > dip

        case "sma_gt":
            pa = int(params.get("period_a", 9))
            pb = int(params.get("period_b", 21))
            return sma_value(df, pa) > sma_value(df, pb)

        case "sma_lt":
            pa = int(params.get("period_a", 9))
            pb = int(params.get("period_b", 21))
            return sma_value(df, pa) < sma_value(df, pb)

        # ── GATE: spot vs SMA ─────────────────────────────────────────────
        case "spot_gt_sma":
            period = int(params.get("period", 50))
            return close > sma_value(df, period)

        case "spot_lt_sma":
            period = int(params.get("period", 50))
            return close < sma_value(df, period)

        case "spot_above_sma_by":
            period = int(params.get("period", 50))
            value  = float(params.get("value", 0))
            return (close - sma_value(df, period)) >= value

        case "spot_below_sma_by":
            period = int(params.get("period", 50))
            value  = float(params.get("value", 0))
            return (close - sma_value(df, period)) <= -abs(value)

        case "sma_gt_spot":
            period = int(params.get("period", 50))
            return sma_value(df, period) > close

        case "sma_lt_spot":
            period = int(params.get("period", 50))
            return sma_value(df, period) < close

        # ── EXIT / SL: spot P&L ───────────────────────────────────────────
        case "spot_gain_gte":
            if position is None or current_spot is None:
                return False
            value    = float(params.get("value", 50))
            side     = position.get("side", "CE")
            move     = (current_spot - float(position["entry_spot"])) * (1 if side == "CE" else -1)
            return move >= value

        case "spot_loss_gte":
            if position is None or current_spot is None:
                return False
            value    = float(params.get("value", 30))
            side     = position.get("side", "CE")
            move     = (current_spot - float(position["entry_spot"])) * (1 if side == "CE" else -1)
            return move <= -abs(value)

        # ── EXIT / SL: LTP P&L ───────────────────────────────────────────
        case "ltp_gain_gte":
            if position is None or current_ltp is None:
                return False
            value = float(params.get("value", 20))
            return (current_ltp - float(position["entry_ltp"])) >= value

        case "ltp_loss_gte":
            if position is None or current_ltp is None:
                return False
            value = float(params.get("value", 15))
            return (current_ltp - float(position["entry_ltp"])) <= -abs(value)

        case _:
            return False


# ── Leg condition set evaluator ───────────────────────────────────────────────

def evaluate_entry(
    conditions: list[dict],
    logic: str,           # "AND" | "OR"
    tf_cache: TFCache,
) -> bool:
    """Evaluate entry conditions with AND or OR logic. Empty list → False."""
    enabled = [c for c in conditions if c.get("enabled", True)]
    if not enabled:
        return False
    results = [evaluate_condition(c, tf_cache) for c in enabled]
    return all(results) if logic == "AND" else any(results)


def evaluate_gates(
    gates: list[dict],
    tf_cache: TFCache,
) -> bool:
    """All gates must pass (AND). Empty gate list → True (no restriction)."""
    enabled = [g for g in gates if g.get("enabled", True)]
    if not enabled:
        return True
    return all(evaluate_condition(g, tf_cache) for g in enabled)


def evaluate_exits(
    conditions: list[dict],
    tf_cache: TFCache,
    position: dict,
    current_ltp: float | None,
    current_spot: float,
) -> str | None:
    """First matching enabled condition wins. Returns condition type or None."""
    for c in conditions:
        if not c.get("enabled", True):
            continue
        if evaluate_condition(c, tf_cache, position, current_ltp, current_spot):
            return c["type"]
    return None
