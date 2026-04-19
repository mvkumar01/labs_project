"""
Strategy Type 2 — RSI + EMA trend + SMA filter

CALL when RSI < oversold  AND  EMA_fast > EMA_slow  AND  spot > SMA
PUT  when RSI > overbought AND  EMA_fast < EMA_slow  AND  spot < SMA

Exit: EMA cross reversal, or hard SL/target.
"""
from typing import Any

import pandas as pd

from labs.strategies.base import Strategy, evaluate_rules, evaluate_exit_rules


class RsiEmaSmaStrategy(Strategy):
    name = "rsi_ema_sma"

    def entry_signal(self, df: pd.DataFrame, params: dict[str, Any]) -> str | None:
        if len(df) < params.get("sma_period", 50):
            return None

        call_rules = ["rsi_below_oversold", "ema_fast_above_slow", "spot_above_sma"]
        put_rules  = ["rsi_above_overbought", "ema_fast_below_slow", "spot_below_sma"]

        if evaluate_rules(df, call_rules, "CE"):
            return "CE"
        if evaluate_rules(df, put_rules, "PE"):
            return "PE"
        return None

    def exit_signal(
        self,
        df: pd.DataFrame,
        params: dict[str, Any],
        position_side: str,
    ) -> str | None:
        exit_rules = params.get("exit_rules", ["ema_fast_crosses_below" if position_side == "CE" else "ema_fast_crosses_above"])
        return evaluate_exit_rules(df, exit_rules, position_side)
