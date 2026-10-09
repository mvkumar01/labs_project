"""Cross-sectional taker buy/sell imbalance on Binance USDT perpetuals: the rule, pure.

numpy / pandas only, no I/O. A port of the Crypto_Analysis research code the strategy was handed
over from (research_engine/xs_backtest.py build_market / rank_weights / smooth / run,
xs_signals.py S05_taker1, scripts/xs_robustness.py target_weights), kept in the same shapes so the
two can be compared day by day.

Hourly panels (index = UTC instant t, columns = contracts): ``close[t]`` is the price at t (the
close of the bar that ends at t), ``quote_volume[t]`` and ``taker_buy[t]`` are that bar's, and
``funding[t]`` is the rate settled at t.

Each day, decision at 00:00 UTC from rows at or before it:
  universe  the 50 most liquid contracts by the mean of the last 30 midnight 24-hour quote
            volumes (all 30 needed), listed 35 days or more, with a price at 01:00
  signal    (2 x taker-buy quote volume - quote volume) / quote volume over the last 24 hours
  target    rank the universe by the signal; weight = centred rank / sum of |centred ranks|
            (most buying = long); nothing if fewer than 10 contracts
  book      the average of the last 7 daily targets, rescaled to gross exposure 1
  trade     at the 01:00 price, held to the next day's 01:00
  result    sum(w x price return) - sum(w x funding settled in the hold) - 7 bps x traded notional
"""
from __future__ import annotations

from dataclasses import dataclass

import numpy as np
import pandas as pd

TOP, MIN_AGE_DAYS, VOLUME_DAYS = 50, 35, 30
SIGNAL_HOURS, SIGNAL_MIN_HOURS = 24, 12
HOLD_DAYS = 7
MIN_NAMES = 10
COST_BPS = 7.0
LAG_HOURS = 1
EXCLUDED = frozenset({"USDCUSDT", "BUSDUSDT", "TUSDUSDT", "FDUSDUSDT", "USDPUSDT", "USD1USDT", "USDEUSDT",
                      "BTCDOMUSDT", "DEFIUSDT", "BLUEBIRDUSDT", "FOOTBALLUSDT", "ALLUSDT"})


def tradable(symbols) -> list[str]:
    """USDT perpetuals that are single-asset contracts (no stable pairs, no exchange indices)."""
    return [s for s in symbols if s.endswith("USDT") and "_" not in s and s not in EXCLUDED]


@dataclass(frozen=True)
class Market:
    decisions: pd.DatetimeIndex     # 00:00 UTC rows whose 01:00 entry exists
    symbols: list
    entry_price: np.ndarray         # price at decision + 1 hour
    forward_return: np.ndarray      # price return of each hold; NaN on the last row (hold not over)
    forward_funding: np.ndarray     # funding settled during the hold (long pays positive)
    eligible: np.ndarray
    complete: np.ndarray            # per decision: is the hold over (next entry price row exists)


def build_market(close: pd.DataFrame, funding: pd.DataFrame, quote_volume: pd.DataFrame) -> Market:
    index = close.index
    decisions = index[index.hour == 0]
    entries = decisions + pd.Timedelta(hours=LAG_HOURS)
    keep = entries <= index[-1]
    decisions, entries = decisions[keep], entries[keep]
    entry_price = close.reindex(entries).to_numpy()
    # a contract delisted mid-hold exits at its last traded price
    filled = close.ffill(limit=24).reindex(entries).to_numpy()
    forward_return = np.full(entry_price.shape, np.nan)
    forward_return[:-1] = filled[1:] / entry_price[:-1] - 1.0
    paid = funding.reindex(index).fillna(0.0).cumsum().reindex(entries).to_numpy()
    forward_funding = np.zeros(entry_price.shape)
    forward_funding[:-1] = paid[1:] - paid[:-1]
    daily_volume = quote_volume.rolling(24, min_periods=1).sum()
    at_midnight = daily_volume[daily_volume.index.hour == 0]
    trailing = at_midnight.rolling(VOLUME_DAYS, min_periods=VOLUME_DAYS).mean().reindex(index, method="ffill").reindex(decisions)
    rank = trailing.rank(axis=1, ascending=False).to_numpy()
    age = close.notna().cumsum().reindex(decisions).to_numpy() / 24.0
    with np.errstate(invalid="ignore"):
        eligible = (rank <= TOP) & (age >= MIN_AGE_DAYS) & np.isfinite(entry_price)
    complete = np.ones(len(decisions), dtype=bool)
    complete[-1:] = False
    return Market(decisions, list(close.columns), entry_price, forward_return, forward_funding, eligible, complete)


def taker_imbalance(quote_volume: pd.DataFrame, taker_buy: pd.DataFrame, decisions: pd.DatetimeIndex) -> np.ndarray:
    total = quote_volume.rolling(SIGNAL_HOURS, min_periods=SIGNAL_MIN_HOURS).sum()
    buy = taker_buy.rolling(SIGNAL_HOURS, min_periods=SIGNAL_MIN_HOURS).sum()
    return ((2 * buy - total) / total.where(total > 0)).reindex(decisions).to_numpy()


def rank_weights(signal: np.ndarray, eligible: np.ndarray) -> np.ndarray:
    """Dollar-neutral rank-linear weights with unit gross exposure."""
    value = np.where(eligible & np.isfinite(signal), signal, np.nan)
    count = np.sum(np.isfinite(value), axis=1, keepdims=True)
    order = pd.DataFrame(value).rank(axis=1).to_numpy()
    centered = order - (count + 1) / 2.0
    gross = np.nansum(np.abs(centered), axis=1, keepdims=True)
    weights = np.where(gross > 0, centered / np.where(gross > 0, gross, 1.0), 0.0)
    weights[count[:, 0] < MIN_NAMES] = 0.0
    return np.nan_to_num(weights)


def book_weights(targets: np.ndarray) -> np.ndarray:
    """The average of the last HOLD_DAYS daily targets, rescaled to unit gross exposure."""
    combined = pd.DataFrame(targets).rolling(HOLD_DAYS, min_periods=1).mean().to_numpy()
    gross = np.abs(combined).sum(1, keepdims=True)
    return np.where(gross > 0, combined / np.where(gross > 0, gross, 1.0), 0.0)


def run(close: pd.DataFrame, funding: pd.DataFrame, quote_volume: pd.DataFrame, taker_buy: pd.DataFrame) -> dict:
    """The whole paper book over the panels. Returns the market, signal, targets, weights and the
    per-day frame (gross, funding, cost, turnover, net, exposure, n_eligible, complete) plus the
    per-contract pieces (price return, funding paid, traded notional). The last day's hold is
    still open: its weights and cost are known, its returns are not (complete = False)."""
    market = build_market(close, funding, quote_volume)
    signal = taker_imbalance(quote_volume, taker_buy, market.decisions)
    targets = rank_weights(signal, market.eligible)
    weights = book_weights(targets)
    returns = np.nan_to_num(market.forward_return)
    price_pnl = weights * returns
    funding_pnl = -weights * market.forward_funding
    drifted = np.vstack([np.zeros((1, weights.shape[1])), weights[:-1] * (1.0 + returns[:-1])])
    traded = np.abs(weights - drifted)
    turnover = traded.sum(1)
    frame = pd.DataFrame({"gross": price_pnl.sum(1), "funding": funding_pnl.sum(1), "cost": turnover * COST_BPS / 1e4,
                          "turnover": turnover, "exposure": np.abs(weights).sum(1),
                          "n_eligible": market.eligible.sum(1), "complete": market.complete}, index=market.decisions)
    frame["net"] = frame["gross"] + frame["funding"] - frame["cost"]
    return {"market": market, "signal": signal, "targets": targets, "weights": weights, "frame": frame,
            "price_pnl": price_pnl, "funding_pnl": funding_pnl, "traded": traded, "returns": market.forward_return}
