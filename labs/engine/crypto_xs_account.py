"""Quantity-based paper account alongside the unchanged research weight model.

Unavailable contracts retain their quantity and receive no imaginary fill.
Missing boundary quotes carry the last observed valuation, explicitly provisional.
When quotes resume, the whole price change is recognised. Funding uses hourly
valuation prices; fees are 7 bps of actual simulated traded notional.
"""
import numpy as np
import pandas as pd

from labs.engine import crypto_xs_engine as engine


def run(out, funding, start, initial=100_000.0):
    market, close = out["market"], out["close"]
    decisions = market.decisions
    entries = decisions + pd.Timedelta(hours=engine.LAG_HOURS)
    volume = out["quote_volume"].reindex(entries).to_numpy()
    raw = market.entry_price
    marks = close.ffill().reindex(entries).to_numpy()
    rates = funding.reindex(index=close.index, columns=market.symbols).fillna(0)
    # Funding is paid on the marked contract notional at each settlement.
    per_unit = (rates * close.ffill()).fillna(0).cumsum().reindex(entries).to_numpy()
    quantity = np.zeros(len(market.symbols))
    equity = initial
    daily, lines = [], []
    first = np.searchsorted(decisions, pd.Timestamp(start, tz="UTC"))
    unresolved = False
    events = out["manifest"].get("settlements", {})
    for i in range(first, len(decisions)):
        day = decisions[i].strftime("%Y-%m-%d")
        executable = np.isfinite(raw[i]) & (raw[i] > 0) & (np.nan_to_num(volume[i]) > 0)
        for j, symbol in enumerate(market.symbols):
            if symbol in events and pd.Timestamp(events[symbol]["time"]) <= entries[i]:
                executable[j] = False
        desired = np.divide(out["weights"][i] * equity, raw[i],
                            out=np.zeros(len(quantity)), where=executable)
        before = quantity.copy()
        quantity = np.where(executable, desired, quantity)
        traded = np.where(executable, np.abs(quantity - before) * np.nan_to_num(raw[i]), 0)
        weight = np.nan_to_num(quantity * marks[i] / equity)
        settled = np.zeros(len(quantity), dtype=bool)
        exit_marks = marks[i + 1].copy() if market.complete[i] else np.full(len(quantity), np.nan)
        settlement_estimated = False
        if market.complete[i]:
            for j, symbol in enumerate(market.symbols):
                event = events.get(symbol)
                if event and entries[i] < pd.Timestamp(event["time"]) <= entries[i + 1] and abs(quantity[j]) > 1e-12:
                    settled[j] = True
                    exit_marks[j] = event["price"]
                    traded[j] += abs(quantity[j]) * event["price"]
                    settlement_estimated |= not event["verified"]
        cost = traded.sum() * engine.COST_BPS / 1e4
        active = (np.abs(quantity) > 1e-12) | (traded > 1e-12)
        blocked = active & ~executable
        unfilled = (np.abs(out["weights"][i]) > 1e-12) & ~executable & ~active
        active |= unfilled
        complete = bool(market.complete[i])
        missing_exit = (np.abs(quantity) > 1e-12) & ~np.isfinite(raw[i + 1]) if complete else np.zeros(len(quantity), bool)
        provisional = bool(blocked.any() or (missing_exit & ~settled).any() or settlement_estimated)
        flags = []
        if blocked.any():
            flags.append("no fill; retained quantity: " + ", ".join(np.array(market.symbols)[blocked]))
        if unfilled.any():
            flags.append("target unavailable; no position or fill: " + ", ".join(np.array(market.symbols)[unfilled]))
        if missing_exit.any():
            flags.append("provisional last-price valuation; settlement unknown: " + ", ".join(np.array(market.symbols)[missing_exit]))
        if settled.any():
            flags.append("automatic settlement at confirmed exchange time; estimated price: " + ", ".join(np.array(market.symbols)[settled]))
        if complete:
            price = np.nan_to_num(quantity * (exit_marks - marks[i]))
            paid = -quantity * np.nan_to_num(per_unit[i + 1] - per_unit[i])
            net = (price.sum() + paid.sum() - cost) / equity
            if equity * (1 + net) <= 0:
                raise ValueError("Paper account insolvent; stop rather than rebalance negative equity")
        else:
            price, paid, net = np.zeros(len(quantity)), np.zeros(len(quantity)), None
        contribution = (price + paid) / equity
        top = int(np.argmax(np.abs(contribution))) if complete and active.any() else None
        large = active & (np.abs(contribution) > .03)
        if large.any():
            flags.append("one contract over 3% of equity: " + ", ".join(np.array(market.symbols)[large]))
        unresolved = unresolved or provisional
        daily.append(dict(decision_date=day, complete=int(complete), n_eligible=int(market.eligible[i].sum()),
                          n_held=int((np.abs(quantity) > 1e-12).sum()), exposure=float(np.abs(weight).sum()),
                          long_exposure=float(weight[weight > 0].sum()), turnover=float(traded.sum() / equity),
                          gross=float(price.sum() / equity) if complete else None,
                          funding=float(paid.sum() / equity) if complete else None, cost=float(cost / equity), net=net,
                          equity=float(equity * (1 + net)) if complete else None, provisional=int(provisional),
                          equity_provisional=int(unresolved), top_symbol=market.symbols[top] if top is not None else None,
                          top_contribution=float(contribution[top]) if top is not None else None,
                          flags="; ".join(flags) or None))
        for j in np.flatnonzero(active):
            lines.append(dict(decision_date=day, symbol=market.symbols[j], weight=float(weight[j]),
                              target=float(out["targets"][i, j]), signal=_f(out["signal"][i, j]),
                              quantity=float(quantity[j]), previous_quantity=float(before[j]),
                              entry_price=_f(marks[i, j]), exit_price=_f(exit_marks[j]) if complete else None,
                              entry_source="hourly_close" if executable[j] else "last_observed_valuation_no_fill",
                              exit_source=("estimated_settlement" if settled[j] else ("last_observed_valuation" if missing_exit[j] else "hourly_close")) if complete else None,
                              price_return=_f(exit_marks[j] / marks[i, j] - 1) if complete else None,
                              price_pnl=float(price[j] / equity) if complete else None,
                              funding_pnl=float(paid[j] / equity) if complete else None,
                              traded=float(traded[j] / equity), tradable=int(executable[j])))
        if complete:
            equity *= 1 + net
            quantity[settled] = 0
    return daily, lines


def _f(value):
    return float(value) if np.isfinite(value) else None
