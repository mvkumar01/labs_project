"""
Strategy Type 1 — RSI + SMA

CALL when RSI(period) < oversold  AND  spot > SMA(period)
PUT  when RSI(period) > overbought AND  spot < SMA(period)

Exit: configurable via exit_rules_json, or hard SL/target in position_manager.
"""
from typing import Any

import pandas as pd

from labs.strategies.base import Strategy, evaluate_rules, evaluate_exit_rules


class RsiSmaStrategy(Strategy):
    name = "rsi_sma"

    def entry_signal(self, df: pd.DataFrame, params: dict[str, Any]) -> str | None:
        if len(df) < params.get("sma_period", 50):
            return None

        call_rules = ["rsi_below_oversold", "spot_above_sma"]
        put_rules  = ["rsi_above_overbought", "spot_below_sma"]

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
        exit_rules = params.get("exit_rules", [])
        return evaluate_exit_rules(df, exit_rules, position_side)
