"""Unit tests for the multi-source order manager core (ledger/reconcile/netting).

The same-strike overlap cases are the reason this layer exists — they're what a
symbol-scoped single-position model gets wrong. Run: python tests/test_order_manager.py
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from live.engine.order_manager import (
    SourcePos, OrderIntent, current_net, reconcile, merge_desired, plan_orders,
)

n = 0
def ok(c, m):
    global n
    assert c, f"FAIL: {m}"
    n += 1
    print(f"  ok: {m}")


def P(source, symbol, qty, side="CALL"):
    return SourcePos(source=source, symbol=symbol, side=side, qty=qty)


CE = "NIFTY25000CE"
CE2 = "NIFTY24800CE"
PE = "NIFTY24800PE"

# --- current_net -----------------------------------------------------------
ok(current_net({"main": P("main", CE, 65), "r2": None}) == {CE: 65}, "net: one open, one flat")
ok(current_net({"main": P("main", CE, 65), "r2": P("r2", CE, 65)}) == {CE: 130},
   "net: SAME symbol overlap sums (130)")
ok(current_net({"main": P("main", CE, 65), "r2": P("r2", PE, 65)}) == {CE: 65, PE: 65},
   "net: different symbols kept separate")

# --- reconcile -------------------------------------------------------------
led = {"main": P("main", CE, 65), "r2": P("r2", PE, 65)}
ok(reconcile(led, {CE: 65, PE: 65})[0] is True, "reconcile ok when broker == sum(ledger)")
ok(reconcile(led, {CE: 65, PE: 65, "X": 0})[0] is True, "reconcile ignores zero broker legs")
ok(reconcile(led, {CE: 65})[0] is False, "reconcile blocks: broker missing a leg")
ok(reconcile(led, {CE: 130, PE: 65})[0] is False, "reconcile blocks: broker qty mismatch")
ok(reconcile({"main": None, "r2": None}, {})[0] is True, "reconcile ok when all flat")

# --- plan_orders: independent entries (two symbols) ------------------------
orders, new = plan_orders(
    ledger={"main": None, "r2": None},
    desired={"main": P("main", CE, 65), "r2": P("r2", PE, 65)},
    broker_net={},
)
by = {o.symbol: o for o in orders}
ok(len(orders) == 2 and by[CE].txn == "BUY" and by[CE].qty == 65
   and by[PE].txn == "BUY" and by[PE].qty == 65, "two independent BUYs")

# --- plan_orders: SAME-strike overlap entry (the key case) -----------------
# main already long CE 65; R2 also enters CE 65 -> BUY only the +65 delta.
orders, new = plan_orders(
    ledger={"main": P("main", CE, 65), "r2": None},
    desired={"r2": P("r2", CE, 65)},                 # main absent -> HOLD
    broker_net={CE: 65},
)
ok(len(orders) == 1 and orders[0].txn == "BUY" and orders[0].qty == 65, "overlap entry buys only delta +65")
ok(current_net(new) == {CE: 130}, "ledger net after overlap = 130")
ok(new["main"].qty == 65 and new["r2"].qty == 65, "both sources attributed in ledger")

# --- plan_orders: ONE source exits a shared strike (attribution) -----------
# both long CE 65 (broker 130); main exits -> SELL 65, R2 still holds 65.
orders, new = plan_orders(
    ledger={"main": P("main", CE, 65), "r2": P("r2", CE, 65)},
    desired={"main": None},                          # r2 absent -> HOLD
    broker_net={CE: 130},
)
ok(len(orders) == 1 and orders[0].txn == "SELL" and orders[0].qty == 65, "shared-strike partial exit sells 65")
ok(current_net(new) == {CE: 65} and new["r2"].qty == 65 and new["main"] is None,
   "after main exit: R2 keeps its 65, main flat")

# --- plan_orders: full flat ------------------------------------------------
orders, new = plan_orders(
    ledger={"main": P("main", CE, 65), "r2": P("r2", PE, 65)},
    desired={"main": None, "r2": None},
    broker_net={CE: 65, PE: 65},
)
ok({(o.symbol, o.txn, o.qty) for o in orders} == {(CE, "SELL", 65), (PE, "SELL", 65)},
   "flat-all sells both legs")
ok(current_net(new) == {}, "ledger empty after flat-all")

# --- plan_orders: HOLD (no desired) places nothing -------------------------
orders, new = plan_orders(
    ledger={"main": P("main", CE, 65), "r2": P("r2", PE, 65)},
    desired={},
    broker_net={CE: 65, PE: 65},
)
ok(orders == [], "no desired -> no orders (everyone holds)")

# --- plan_orders: source switches strike (sell old, buy new) ---------------
orders, new = plan_orders(
    ledger={"main": P("main", CE, 65)},
    desired={"main": P("main", CE2, 65)},
    broker_net={CE: 65},
)
ok({(o.symbol, o.txn) for o in orders} == {(CE, "SELL"), (CE2, "BUY")},
   "strike switch: sell old + buy new")

print(f"\nAll {n} assertions passed — order manager core verified.")
