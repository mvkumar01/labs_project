"""
Strategy Type 3 — EMA Crossover + RSI filter + SMA trend confirmation

CALL when EMA_fast crosses above EMA_slow  AND  RSI < overbought  AND  spot > SMA
PUT  when EMA_fast crosses below EMA_slow  AND  RSI > oversold    AND  spot < SMA

Exit: opposite EMA cross, or hard SL/target.
"""
from typing import Any

import pandas as pd

from labs.strategies.base import Strategy, evaluate_rules, evaluate_exit_rules


class EmaCrossoverStrategy(Strategy):
    name = "ema_crossover"

    def entry_signal(self, df: pd.DataFrame, params: dict[str, Any]) -> str | None:
        if len(df) < params.get("sma_period", 50):
            return None

        row = df.iloc[-1]
        rsi         = float(row.get("rsi", 50))
        overbought  = float(params.get("rsi_overbought", 75))
        oversold    = float(params.get("rsi_oversold", 25))
        spot        = float(row.get("close", 0))
        sma         = float(row.get("sma", 0))

        cross_up   = bool(row.get("ema_cross_up",   False))
        cross_down = bool(row.get("ema_cross_down",  False))

        if cross_up   and rsi < overbought and spot > sma:
            return "CE"
        if cross_down and rsi > oversold   and spot < sma:
            return "PE"
        return None

    def exit_signal(
        self,
        df: pd.DataFrame,
        params: dict[str, Any],
        position_side: str,
    ) -> str | None:
        default_exit = ["ema_fast_crosses_below"] if position_side == "CE" else ["ema_fast_crosses_above"]
        exit_rules   = params.get("exit_rules", default_exit)
        return evaluate_exit_rules(df, exit_rules, position_side)
