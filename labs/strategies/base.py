"""
Abstract Strategy base class.
All strategies implement entry_signal() and exit_signal().
"""
from abc import ABC, abstractmethod
from typing import Any

import pandas as pd


class Strategy(ABC):
    """
    Subclasses receive a completed 5-min bar DataFrame (with indicator columns)
    and a params dict derived from bot_params.

    entry_signal() is called when the bot is flat.
    exit_signal()  is called when the bot has an open position.

    Returns:
        entry_signal → "CE" | "PE" | None
        exit_signal  → exit_reason string | None
    """

    @abstractmethod
    def entry_signal(self, df: pd.DataFrame, params: dict[str, Any]) -> str | None:
        """Return 'CE', 'PE', or None."""

    @abstractmethod
    def exit_signal(
        self,
        df: pd.DataFrame,
        params: dict[str, Any],
        position_side: str,
    ) -> str | None:
        """Return exit reason string or None to hold."""


def _check_token(df: pd.DataFrame, token: str, side: str) -> bool:
    """
    Evaluate a single entry/exit rule token against the latest bar of df.
    side is 'CE' (bullish) or 'PE' (bearish) — used for directional tokens.
    """
    row = df.iloc[-1]

    match token:
        case "rsi_below_oversold":
            return float(row.get("rsi", 50)) < float(row.get("_rsi_oversold", 25))
        case "rsi_above_overbought":
            return float(row.get("rsi", 50)) > float(row.get("_rsi_overbought", 75))
        case "ema_fast_above_slow":
            return float(row.get("ema_fast", 0)) > float(row.get("ema_slow", 0))
        case "ema_fast_below_slow":
            return float(row.get("ema_fast", 0)) < float(row.get("ema_slow", 0))
        case "ema_fast_crosses_above":
            return bool(row.get("ema_cross_up", False))
        case "ema_fast_crosses_below":
            return bool(row.get("ema_cross_down", False))
        case "spot_above_sma":
            return float(row.get("close", 0)) > float(row.get("sma", 0))
        case "spot_below_sma":
            return float(row.get("close", 0)) < float(row.get("sma", 0))
        case _:
            return False


def evaluate_rules(df: pd.DataFrame, rules: list[str], side: str) -> bool:
    """AND-join all rules. Returns True only if every rule fires."""
    return all(_check_token(df, r, side) for r in rules)


def evaluate_exit_rules(df: pd.DataFrame, rules: list[str], side: str) -> str | None:
    """OR-join exit rules. Returns the first matching rule token or None."""
    for r in rules:
        if _check_token(df, r, side):
            return r
    return None
