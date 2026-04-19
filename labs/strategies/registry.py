from labs.strategies.rsi_sma import RsiSmaStrategy
from labs.strategies.rsi_ema_sma import RsiEmaSmaStrategy
from labs.strategies.ema_crossover import EmaCrossoverStrategy
from labs.strategies.trend_pullback import TrendPullbackStrategy
from labs.strategies.base import Strategy

_REGISTRY: dict[str, type[Strategy]] = {
    "rsi_sma":       RsiSmaStrategy,
    "rsi_ema_sma":   RsiEmaSmaStrategy,
    "ema_crossover": EmaCrossoverStrategy,
    "trend_pullback": TrendPullbackStrategy,
}


def get_strategy(strategy_type: str) -> Strategy:
    cls = _REGISTRY.get(strategy_type)
    if cls is None:
        raise ValueError(f"Unknown strategy type: {strategy_type!r}")
    return cls()
