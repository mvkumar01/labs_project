"""Multi-source order manager — net N strategy books onto ONE broker account.

Alpha v2.10+. Lets the main (RECO/Run-F) book and the R2 consistency book (and
future sources) trade the SAME broker account safely, instead of needing a
separate connection per book. The broker shows ONE net book per symbol; this
module keeps a PER-SOURCE ledger and reconciles the broker net against the SUM
of source positions, so same-strike overlap is attributed correctly.

This module is PURE LOGIC — no broker I/O, no DB. The runner supplies the
current ledger + each source's desired target + the broker net book; this
returns the netted BUY/SELL intents and the resulting ledger. That makes the
risk-bearing netting/reconcile logic fully unit-testable offline.

Invariant (long-only options book): for every symbol,
    broker_net[symbol] == Σ_source ledger[source].qty(symbol)
If that fails on ANY symbol, reconcile() returns not-ok and the runner MUST
block (place nothing) — same fail-closed posture as the single-position guard.
"""
from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class SourcePos:
    """One source's open long position. qty > 0 (we only buy options)."""
    source: str            # "main" | "r2" | ...
    symbol: str            # broker tradingsymbol (one side per symbol)
    side: str              # "CALL" | "PUT"
    qty: int               # absolute, long
    entry_price: float = 0.0
    entry_spot: float | None = None
    entry_rule: str | None = None


@dataclass(frozen=True)
class OrderIntent:
    symbol: str
    txn: str               # "BUY" (open/add) | "SELL" (exit/reduce)
    qty: int               # > 0
    reason: str
    sources: tuple         # sources whose change produced this delta


def _sym(sp: SourcePos | None) -> str | None:
    return sp.symbol if sp is not None else None


def current_net(ledger: dict) -> dict:
    """Per-symbol long quantity summed across sources. ledger: source -> SourcePos|None."""
    net: dict = {}
    for sp in ledger.values():
        if sp is None:
            continue
        if sp.qty <= 0:
            continue
        net[sp.symbol] = net.get(sp.symbol, 0) + int(sp.qty)
    return net


def reconcile(ledger: dict, broker_net: dict, tol: int = 0) -> tuple:
    """broker_net (symbol -> qty) must equal the ledger's per-symbol sum.
    Returns (ok, message). Any symbol mismatch beyond tol -> (False, why)."""
    want = current_net(ledger)
    broker = {k: int(v) for k, v in broker_net.items() if int(v) != 0}
    diffs = []
    for s in sorted(set(want) | set(broker)):
        w, b = want.get(s, 0), broker.get(s, 0)
        if abs(w - b) > tol:
            diffs.append(f"{s}: ledger={w} broker={b}")
    if diffs:
        return False, "RECONCILE MISMATCH — " + "; ".join(diffs)
    return True, "ok"


def merge_desired(ledger: dict, desired: dict) -> dict:
    """New ledger after applying each source's target. Sources NOT present in
    `desired` keep their current ledger position (HOLD). A target of None flattens
    that source."""
    new = dict(ledger)
    for src, tgt in desired.items():
        new[src] = tgt
    return new


def plan_orders(ledger: dict, desired: dict, broker_net: dict) -> tuple:
    """Compute netted order intents to move the broker from its current net to
    the desired net, plus the resulting ledger.

    PRECONDITION: reconcile(ledger, broker_net) is ok (caller enforces). Orders
    are derived from the LEDGER (our source of truth once reconciled), so the
    broker delta equals target_net - current_ledger_net per symbol.

    Returns (orders: list[OrderIntent], new_ledger: dict).
    """
    new_ledger = merge_desired(ledger, desired)
    cur = current_net(ledger)
    tgt = current_net(new_ledger)
    orders: list = []
    for s in sorted(set(cur) | set(tgt)):
        delta = tgt.get(s, 0) - cur.get(s, 0)
        if delta == 0:
            continue
        srcs = tuple(sorted(
            src for src in desired
            if _sym(ledger.get(src)) == s or _sym(desired.get(src)) == s
        ))
        if delta > 0:
            orders.append(OrderIntent(s, "BUY", delta,
                                      f"open/add {s} for {','.join(srcs)}", srcs))
        else:
            orders.append(OrderIntent(s, "SELL", -delta,
                                      f"exit/reduce {s} for {','.join(srcs)}", srcs))
    return orders, new_ledger
