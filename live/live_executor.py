"""
live_executor.py — THE SINGLE ORDER CHOKEPOINT (per user / per connection).

Every live order intent flows through `place_idempotent` here. This module:
  * Calls brokers/* only (never an SDK directly; never labs.engine.*).
  * Evaluates the calling user's OWN 3-mode state machine + 6 pre-trade gates,
    scoped to (user_id, conn_id) — one user's mode / kill / lots / armed /
    daily-loss can NEVER block or unblock another user.
  * Routes DRY_RUN to a logged-intent path (NO broker call) — writes a
    dry_run=1 ledger row and returns a synthetic OrderResult.
  * In LIVE_ARMED, enforces ALL SIX gates immediately before any real
    `place_order`; if any gate fails it logs the failing gates and does NOT
    call the broker.
  * Builds + checks a user-scoped idempotency key (INSERT OR IGNORE ledger)
    so a web double-click, PA restart re-fire, or same-bar re-entry can never
    place a duplicate order. The key embeds conn_id (== "<user_id>:<broker>")
    so two users' identical signals never collide.

MULTI-USER (spec §2, §4, §6, §9): there is NO global state here. Every public
function takes an explicit (user_id, conn_id) and scopes all DB reads/writes to
that owner via live_service.

Isolation (spec §1.4): imports ONLY live.brokers.base, live.live_service, and
neutral infra. NEVER imports labs.engine.* / labs.services.* and NEVER imports
a broker SDK. This file folds the spec's live_state / gates / rails surface
into one per-user chokepoint per the build task's file list.

DRY-RUN ONLY (Phase 0): even if this module decided to place a real order,
every adapter's place_order raises NotImplementedError until Phase-1
enablement — so no real order can fire in this build.
"""
import logging
import sys
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from enum import Enum
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from live.brokers.base import OrderResult
from live import live_service as svc

log = logging.getLogger("live.executor")

# ── hard limits / configured constants (spec §6, §10, §13) ────────────────
LOTS_HARD_CAP = 2


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


# ══════════════════════════════════════════════════════════════════════════
# PER-USER 3-MODE STATE MACHINE (spec §4)
# Persisted in live_config[(user_id, conn_id, 'mode')], default DISARMED.
# Every transition is scoped to one (user_id, conn_id).
# ══════════════════════════════════════════════════════════════════════════
class Mode(str, Enum):
    DISARMED = "DISARMED"
    DRY_RUN = "DRY_RUN"
    LIVE_ARMED = "LIVE_ARMED"


_VALID = {
    Mode.DISARMED:   {Mode.DRY_RUN},                    # arm_dry_run
    Mode.DRY_RUN:    {Mode.LIVE_ARMED, Mode.DISARMED},  # arm_live / disarm
    Mode.LIVE_ARMED: {Mode.DISARMED},                   # only disarm; re-arm via DRY_RUN
}


class InvalidTransition(Exception):
    ...


def get_mode(user_id: str, conn_id: str, conn=None) -> Mode:
    """This connection's persisted mode (DISARMED if unset/invalid)."""
    try:
        return Mode(svc.get_mode(user_id, conn_id, conn))
    except ValueError:
        return Mode.DISARMED


def can_transition(current: Mode, target: Mode) -> bool:
    return target in _VALID.get(current, set())


def set_mode(user_id: str, conn_id: str, target: Mode, conn=None) -> Mode:
    """Validate via can_transition, else raise InvalidTransition. Writes the
    new mode to THIS connection's live_config only."""
    current = get_mode(user_id, conn_id, conn)
    if current == target:
        return current
    if not can_transition(current, target):
        raise InvalidTransition(f"{current.value} -> {target.value} not allowed")
    svc.set_config(user_id, conn_id, "mode", target.value, conn)
    return target


def arm_dry_run(user_id: str, conn_id: str, conn=None) -> Mode:
    """DISARMED -> DRY_RUN."""
    return set_mode(user_id, conn_id, Mode.DRY_RUN, conn)


def arm_live(user_id: str, conn_id: str, conn=None) -> Mode:
    """DRY_RUN -> LIVE_ARMED; also sets armed=1 for this connection.

    Caller (route) must have evaluated the 6 gates first (spec §11). There is
    no DISARMED -> LIVE_ARMED edge — going live always passes through DRY_RUN.
    """
    m = set_mode(user_id, conn_id, Mode.LIVE_ARMED, conn)
    svc.set_config(user_id, conn_id, "armed", "1", conn)
    return m


def disarm(user_id: str, conn_id: str, conn=None) -> Mode:
    """Any -> DISARMED; clears this connection's armed flag."""
    m = set_mode(user_id, conn_id, Mode.DISARMED, conn)
    svc.set_config(user_id, conn_id, "armed", "0", conn)
    return m


# ══════════════════════════════════════════════════════════════════════════
# SIX PER-USER PRE-TRADE GATES (spec §6) — ALL must pass before any live order.
# Evaluated for the specific (user_id, conn_id). One user's gate failure never
# blocks another user.
# ══════════════════════════════════════════════════════════════════════════
@dataclass
class GateResult:
    name: str
    passed: bool
    detail: str


def gate_mode_armed(user_id: str, conn_id: str, conn=None) -> GateResult:
    """1. mode == LIVE_ARMED AND armed == 1 for this connection."""
    m = get_mode(user_id, conn_id, conn)
    armed = svc.is_armed(user_id, conn_id, conn)
    ok = m == Mode.LIVE_ARMED and armed
    return GateResult("mode_armed", ok, f"mode={m.value} armed={int(armed)}")


def gate_kill_switch_clear(user_id: str, conn_id: str, conn=None) -> GateResult:
    """2. this connection's kill_switch == 0."""
    on = svc.is_kill_switch_on(user_id, conn_id, conn)
    return GateResult("kill_switch_clear", not on,
                      f"kill_switch={'ON' if on else 'OFF'}")


def gate_broker_connected(adapter, user_id: str, conn_id: str,
                          conn=None) -> GateResult:
    """3. adapter.is_connected() is True (cheap auth ping). Never leaks creds."""
    try:
        ok = bool(adapter.is_connected())
        return GateResult("broker_connected", ok, f"connected={ok}")
    except Exception as e:  # name only — never echo cred values
        return GateResult("broker_connected", False,
                          f"connect_error={type(e).__name__}")


def gate_account_isolation(adapter, user_id: str, conn_id: str,
                           conn=None) -> GateResult:
    """4. adapter.account_ref() is not claimed by another live connection."""
    try:
        ref = adapter.account_ref()
    except Exception as e:
        return GateResult("account_isolation", False,
                          f"ref_error={type(e).__name__}")
    if svc.account_ref_claimed_by_other(user_id, conn_id, ref, conn):
        return GateResult("account_isolation", False,
                          f"account_ref {ref} already claimed by another user")
    return GateResult("account_isolation", True, f"account_ref={ref}")


def gate_daily_loss_ok(adapter, user_id: str, conn_id: str,
                       conn=None) -> GateResult:
    """5. realized_pnl > -daily_loss_cap AND halted == 0 for today's IST date."""
    day = svc.get_day_pnl(user_id, conn_id, conn=conn)
    cap = svc.get_daily_loss_cap(user_id, conn_id, conn)
    realized = float(day.get("realized_pnl") or 0.0)
    halted = int(day.get("halted") or 0)
    ok = realized > -abs(cap) and halted == 0
    return GateResult("daily_loss_ok", ok,
                      f"realized={realized} cap=-{abs(cap)} halted={halted}")


def gate_lots_within_cap(user_id: str, conn_id: str, conn=None) -> GateResult:
    """6. 1 <= lots <= LOTS_HARD_CAP (==2) for this connection."""
    lots = svc.get_lots(user_id, conn_id, conn)
    ok = 1 <= lots <= LOTS_HARD_CAP
    return GateResult("lots_within_cap", ok, f"lots={lots} cap={LOTS_HARD_CAP}")


def evaluate_all(adapter, user_id: str, conn_id: str, conn=None) -> list:
    """All six gates for this (user_id, conn_id)."""
    return [
        gate_mode_armed(user_id, conn_id, conn),
        gate_kill_switch_clear(user_id, conn_id, conn),
        gate_broker_connected(adapter, user_id, conn_id, conn),
        gate_account_isolation(adapter, user_id, conn_id, conn),
        gate_daily_loss_ok(adapter, user_id, conn_id, conn),
        gate_lots_within_cap(user_id, conn_id, conn),
    ]


def all_passed(results: list) -> bool:
    return all(r.passed for r in results)


def _is_final_status(status: str) -> bool:
    return str(status or "").upper() in {
        "COMPLETE", "COMPLETED", "FILLED", "EXECUTED", "REJECTED",
        "CANCELLED", "CANCELED", "FAILED",
    }


def _refresh_order_result(adapter, result: OrderResult, *,
                          polls: int = 6, delay_s: float = 1.0) -> OrderResult:
    """Best-effort broker-side status enrichment after placement.

    Polls briefly so ledger/trade-state use broker fill status and average
    price rather than assuming a LIMIT order filled immediately.
    """
    if not result.broker_order_id:
        return result
    enriched = result
    for attempt in range(max(1, polls)):
        try:
            snap = adapter.get_order_status(result.broker_order_id)
        except Exception:
            snap = {}
        if snap:
            status = (
                snap.get("status")
                or snap.get("orderstatus")
                or snap.get("order_status")
                or enriched.status
            )
            avg = (
                snap.get("average_price")
                or snap.get("averageprice")
                or snap.get("avgprice")
                or enriched.avg_fill_price
            )
            try:
                avg = float(avg) if avg not in (None, "") else None
            except (TypeError, ValueError):
                avg = enriched.avg_fill_price
            raw = dict(enriched.raw or {})
            raw["status_snapshot"] = snap
            enriched = OrderResult(
                broker_order_id=result.broker_order_id,
                status=str(status),
                avg_fill_price=avg,
                raw=raw,
            )
            if _is_final_status(enriched.status):
                return enriched
        if attempt < polls - 1:
            time.sleep(delay_s)
    return enriched


# ══════════════════════════════════════════════════════════════════════════
# USER-SCOPED IDEMPOTENCY (spec §9) — key build + single-chokepoint placement
# ══════════════════════════════════════════════════════════════════════════
def build_idem_key(*, conn_id, trade_date, strategy_version, bar_timestamp,
                   action, side, entry_rule, symbol) -> str:
    """Operator-mandated key format (spec §9). conn_id == "<user_id>:<broker>"
    already encodes the user, so the key is inherently user-scoped."""
    return ":".join([
        str(conn_id), str(trade_date), str(strategy_version), str(bar_timestamp),
        str(action), str(side), str(entry_rule or "none"), str(symbol or "none"),
    ])


def place_idempotent(adapter, *, user_id: str, conn_id: str, idem_key: str,
                     side: str, symbol: str, qty: int, price: float,
                     action: str, dry_run: bool, trade_date: str = "",
                     strategy_version: str = "", bar_timestamp: str = "",
                     entry_rule: str = "none", intent_seq: int = 0,
                     conn=None) -> OrderResult:
    """THE single order chokepoint, per (user_id, conn_id).

    1. INSERT OR IGNORE a PENDING live_orders row keyed by idem_key.
    2. If the row already existed (not newly inserted) -> SKIP the broker
       call entirely; return the recorded result (defends double-click /
       restart re-fire / same-bar re-entry).
    3. Else if dry_run -> synthetic OrderResult(status='DRY_RUN', avg=price),
       NO broker call (logged-intent path).
    4. Else (LIVE_ARMED real path) -> evaluate ALL 6 gates for THIS user/conn;
       only if all pass call adapter.place_order / exit_all (itself
       NotImplementedError-guarded until Phase 1).
    5. UPDATE the ledger row with broker_order_id / status / avg_fill_price.
    """
    order_type = "LIMIT"
    inserted = svc.insert_order_ledger(
        idem_key, user_id=user_id, conn_id=conn_id, trade_date=trade_date,
        strategy_version=strategy_version, bar_timestamp=bar_timestamp,
        action=action, side=side, entry_rule=entry_rule, intent_seq=intent_seq,
        symbol=symbol, qty=qty, order_type=order_type, limit_price=price,
        dry_run=1 if dry_run else 0, conn=conn,
    )

    if not inserted:
        existing = svc.get_order_ledger(idem_key, conn)
        log.info("idempotent SKIP (key seen) | conn=%s key=%s status=%s",
                 conn_id, idem_key, existing.get("status"))
        return OrderResult(
            broker_order_id=existing.get("broker_order_id"),
            status=existing.get("status", "PENDING"),
            avg_fill_price=existing.get("avg_fill_price"),
            raw={"idempotent_skip": True},
        )

    # ── DRY_RUN logged-intent path — never touches the broker ─────────────
    if dry_run:
        result = OrderResult(broker_order_id=None, status="DRY_RUN",
                             avg_fill_price=price, raw={"dry_run": True})
        svc.update_order_ledger(idem_key, status="DRY_RUN",
                                avg_fill_price=price, placed_at=_now_iso(),
                                conn=conn)
        log.info("DRY_RUN intent | conn=%s %s %s qty=%s px=%s key=%s",
                 conn_id, action, symbol, qty, price, idem_key)
        return result

    # ── LIVE real path — gate (entry only), then call the (guarded) adapter ──
    # Exits bypass entry gates 5/6 (daily-loss / lots) but still require
    # mode-armed, kill-clear, broker-connected, account-isolation (spec §6).
    if action == "EXIT":
        gates = [
            gate_mode_armed(user_id, conn_id, conn),
            gate_kill_switch_clear(user_id, conn_id, conn),
            gate_broker_connected(adapter, user_id, conn_id, conn),
            gate_account_isolation(adapter, user_id, conn_id, conn),
        ]
    else:
        gates = evaluate_all(adapter, user_id, conn_id, conn)

    if not all_passed(gates):
        failed = [g.name for g in gates if not g.passed]
        log.warning("Gates FAILED — not placing | conn=%s failing=%s key=%s",
                    conn_id, failed, idem_key)
        svc.update_order_ledger(idem_key, status="GATE_BLOCKED", conn=conn)
        return OrderResult(broker_order_id=None, status="GATE_BLOCKED",
                           avg_fill_price=None, raw={"failed_gates": failed})

    try:
        if action == "EXIT":
            result = adapter.exit_all(symbol=symbol, qty=qty, reason="exit",
                                      idempotency_key=idem_key)
        else:
            result = adapter.place_order(side=side, symbol=symbol, qty=qty,
                                         price=price, idempotency_key=idem_key)
    except Exception as e:
        log.warning(
            "Broker order FAILED | conn=%s action=%s symbol=%s type=%s msg=%s",
            conn_id, action, symbol, type(e).__name__, str(e)[:300],
        )
        svc.update_order_ledger(idem_key, status="FAILED",
                                placed_at=_now_iso(), conn=conn)
        return OrderResult(
            broker_order_id=None,
            status="FAILED",
            avg_fill_price=None,
            raw={"error_type": type(e).__name__, "message": str(e)[:300]},
        )
    result = _refresh_order_result(adapter, result)

    svc.update_order_ledger(idem_key, status=result.status,
                            broker_order_id=result.broker_order_id,
                            avg_fill_price=result.avg_fill_price,
                            placed_at=_now_iso(), conn=conn)
    return result
