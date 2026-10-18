"""Which Binance USDT perpetuals are crypto: the universe of the crypto-only twin book.

Binance now lists "TradFi" perpetuals next to the crypto ones - US / Hong Kong / Korean / Chinese stocks,
ETFs (including 3x leveraged ones), gold, silver, oil, FX - and by mid-2026 10 to 20 of them sat in the
top 50 by volume that the taker-imbalance rule trades. None existed in its 2021-2024 discovery data.

  label     Binance's own: exchangeInfo underlyingType "COIN" (contractType PERPETUAL) is crypto; EQUITY,
            HK_EQUITY, KR_EQUITY, CN_EQUITY, COMMODITY, FX (contractType TRADIFI_PERPETUAL), INDEX and
            PREMARKET are not. The labs server cannot read exchangeInfo (HTTP 451), so the labels are a
            committed snapshot (data/crypto_xs_contract_types.json, scripts/build_crypto_xs_contract_types.py).
  fallback  a contract missing from the snapshot (listed later, or delisted before it was taken) is judged
            from its own volume: crypto trades much the same every day of the week, a stock or commodity
            perpetual goes quiet while its home market is shut. Crypto if its mean daily quote volume on
            weekend days is at least WEEKEND_MIN of its weekday mean over the 28 days before the decision
            (Jun - Oct 2026: TradFi median 0.09, max 0.25; crypto median 0.79, lowest PAXG 0.21 - a gold
            token Binance labels COIN). Unknown with too little history -> not allowed.
"""
from __future__ import annotations

import json
from pathlib import Path

import pandas as pd

LABELS = Path(__file__).resolve().parents[2] / "data" / "crypto_xs_contract_types.json"
CRYPTO_TYPE = "COIN"
WEEKEND_MIN = 0.35
WINDOW_DAYS = 28


def load_labels(path: Path = LABELS) -> dict:
    """{symbol: underlyingType} from the snapshot (empty if there is none)."""
    if not path.exists():
        return {}
    return {s: v.get("underlyingType") for s, v in json.loads(path.read_text(encoding="utf-8"))["symbols"].items()}


def weekend_ratio(quote_volume: pd.DataFrame) -> pd.DataFrame:
    """Per UTC day: weekend-day / weekday mean daily quote volume over the WINDOW_DAYS days BEFORE it."""
    daily = quote_volume.resample("D").sum(min_count=1)
    weekend = daily.index.dayofweek >= 5
    we = daily.where(pd.Series(weekend, index=daily.index), axis=0).rolling(WINDOW_DAYS, min_periods=4).mean()
    wd = daily.where(pd.Series(~weekend, index=daily.index), axis=0).rolling(WINDOW_DAYS, min_periods=10).mean()
    return (we / wd.where(wd > 0)).shift(1)


def crypto_allowed(close: pd.DataFrame, quote_volume: pd.DataFrame, labels: dict | None = None) -> pd.DataFrame:
    """Decisions (00:00 UTC) x contracts: True where the contract counts as crypto."""
    labels = load_labels() if labels is None else labels
    decisions = close.index[close.index.hour == 0]
    ratio = weekend_ratio(quote_volume).reindex(decisions.normalize())
    ratio.index = decisions
    out = pd.DataFrame(False, index=decisions, columns=close.columns)
    for s in close.columns:
        kind = labels.get(s)
        if kind is not None:
            out[s] = kind == CRYPTO_TYPE
        elif s in ratio:
            out[s] = (ratio[s] >= WEEKEND_MIN).fillna(False)
    return out


def classification(close: pd.DataFrame, quote_volume: pd.DataFrame, labels: dict | None = None) -> pd.DataFrame:
    """For the tab: every contract, its label (or 'unlabelled'), its latest weekend ratio and the verdict."""
    labels = load_labels() if labels is None else labels
    allowed = crypto_allowed(close, quote_volume, labels)
    ratio = weekend_ratio(quote_volume)
    rows = []
    for s in close.columns:
        rows.append({"symbol": s, "label": labels.get(s) or "unlabelled",
                     "weekend_ratio": float(ratio[s].dropna().iloc[-1]) if s in ratio and ratio[s].notna().any() else None,
                     "crypto": bool(allowed[s].iloc[-1]) if len(allowed) else False})
    return pd.DataFrame(rows)
