"""
Strategy Type 4 — Trend Pullback

Trend defined by: spot > SMA  AND  EMA_fast > EMA_slow  (bullish) / mirror for bearish.
Entry: RSI pulls back below oversold threshold while trend is bullish → CALL.
Exit: RSI overbought OR EMA cross reversal OR spot breaks SMA.
"""
from typing import Any

import pandas as pd

from labs.strategies.base import Strategy, evaluate_exit_rules


class TrendPullbackStrategy(Strategy):
    name = "trend_pullback"

    def entry_signal(self, df: pd.DataFrame, params: dict[str, Any]) -> str | None:
        if len(df) < params.get("sma_period", 50):
            return None

        row        = df.iloc[-1]
        rsi        = float(row.get("rsi", 50))
        oversold   = float(params.get("rsi_oversold", 30))
        overbought = float(params.get("rsi_overbought", 70))
        spot       = float(row.get("close", 0))
        sma        = float(row.get("sma", 0))
        ema_fast   = float(row.get("ema_fast", 0))
        ema_slow   = float(row.get("ema_slow", 0))

        bullish_trend = (spot > sma) and (ema_fast > ema_slow)
        bearish_trend = (spot < sma) and (ema_fast < ema_slow)

        if bullish_trend and rsi < oversold:
            return "CE"
        if bearish_trend and rsi > overbought:
            return "PE"
        return None

    def exit_signal(
        self,
        df: pd.DataFrame,
        params: dict[str, Any],
        position_side: str,
    ) -> str | None:
        if position_side == "CE":
            default_exits = ["rsi_above_overbought", "ema_fast_crosses_below", "spot_below_sma"]
        else:
            default_exits = ["rsi_below_oversold", "ema_fast_crosses_above", "spot_above_sma"]

        exit_rules = params.get("exit_rules", default_exits)
        return evaluate_exit_rules(df, exit_rules, position_side)
