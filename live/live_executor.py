"""
live_executor.py — THE SINGLE ORDER CHOKEPOINT (per user / per connection).

Every live order intent flows through `place_idempotent` here. This module:
  * Calls brokers/* only (never an SDK directly; never labs.engine.*).
  * Evaluates the calling user's OWN 3-mode state machine + 7 pre-trade gates,
    scoped to (user_id, conn_id) — one user's mode / kill / lots / armed /
    daily-loss can NEVER block or unblock another user.
  * Routes DRY_RUN to a logged-intent path (NO broker call) — writes a
    dry_run=1 ledger row and returns a synthetic OrderResult.
  * In LIVE_ARMED, enforces every gate immediately before any real
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
import contextlib
import re
import sys
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from enum import Enum
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from live.brokers.base import OrderResult
from live import live_service as svc
from live import control_plane as cp

log = logging.getLogger("live.executor")

# ── configured constants (spec §6, §10, §13) ──────────────────────────────
# No lot ceiling (operator, 2026-10-02): size is bounded by the daily loss cap,
# broker margin and the exchange freeze quantity per order.
LIVE_DECISION_ABI = "alpha-v2.14ab-entrybar-live-v1"
# The SENSEX Proposer runs in its own runner process with its own decision contract.
PROPOSER_STRATEGY_VERSION = "proposer_dt25"
# v2 (2026-10-06): the runner also trades the proposer_dt25_px variant (1-min bar exit and
# one-loss-per-day stop); a runner on the v1 contract must not place its orders.
# v3 (2026-10-08): gap regime read at the close of the broker's 09:15 candle.
# v4 (2026-10-08): the runner also trades proposer_dt25_v3 (entries only with the day).
# v5 (2026-10-10): proposer_dt25_v3's premium floor is -20%.
PROPOSER_DECISION_ABI = "proposer-dt25-live-v5"
RUNNER_HEARTBEAT_MAX_AGE_SECONDS = 30


def is_proposer_strategy(strategy_version) -> bool:
    return str(strategy_version or "").startswith("proposer")


def expected_decision_abi(strategy_version) -> str:
    """The decision contract the owning runner must have loaded for this strategy."""
    return PROPOSER_DECISION_ABI if is_proposer_strategy(strategy_version) else LIVE_DECISION_ABI


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
    with cp.transaction() if conn is None else contextlib.nullcontext(conn) as c:
        current = get_mode(user_id, conn_id, c)
        selected = svc.get_selected_broker(user_id, c)
        if target != Mode.DISARMED and selected and conn_id != svc.conn_id_for(user_id, selected):
            raise InvalidTransition('Broker selection changed; reload the page')
        if current != target and not can_transition(current, target):
            raise InvalidTransition(f"{current.value} -> {target.value} not allowed")
        for key, value in [('mode', target.value), ('armed', '1' if target == Mode.LIVE_ARMED else '0')]:
            c.execute('INSERT INTO live_config VALUES(?,?,?,?,?) ON CONFLICT(user_id,conn_id,key) DO UPDATE SET value=excluded.value,updated_at=excluded.updated_at',
                      (user_id, conn_id, key, value, _now_iso()))
    return target


def arm_dry_run(user_id: str, conn_id: str, conn=None) -> Mode:
    """DISARMED -> DRY_RUN."""
    return set_mode(user_id, conn_id, Mode.DRY_RUN, conn)


def arm_live(user_id: str, conn_id: str, conn=None) -> Mode:
    """DRY_RUN -> LIVE_ARMED; also sets armed=1 for this connection.

    Caller (route) must have evaluated all 7 gates first. There is
    no DISARMED -> LIVE_ARMED edge — going live always passes through DRY_RUN.
    """
    m = set_mode(user_id, conn_id, Mode.LIVE_ARMED, conn)
    return m


def disarm(user_id: str, conn_id: str, conn=None) -> Mode:
    """Any -> DISARMED; clears this connection's armed flag."""
    m = set_mode(user_id, conn_id, Mode.DISARMED, conn)
    return m


# ══════════════════════════════════════════════════════════════════════════
# PER-USER PRE-TRADE GATES — ALL must pass before any live order.
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
    """6. lots is a whole number >= 1 for this connection (no upper ceiling)."""
    lots = svc.get_lots(user_id, conn_id, conn)
    ok = lots >= 1
    return GateResult("lots_within_cap", ok, f"lots={lots}")


def gate_static_order_proxy(user_id=None, conn_id=None, conn=None, *, for_exit=False) -> GateResult:
    """7. Every real order must have an order-only static-IP route."""
    status = cp.route_status(user_id, conn_id, conn)
    configured = status['ready']
    if for_exit and status.get('label'):
        configured = True  # The transport enforces the remaining exit budget.
    return GateResult(
        "static_order_proxy",
        configured,
        status['detail'],
    )


def gate_runner_decision_abi(user_id: str, conn_id: str, conn=None) -> GateResult:
    """The active runner must have loaded the same decision contract as web.

    A Git pull changes files on disk but not an already-running Python process.
    This gate prevents arming after a strategy deployment until the runner has
    restarted and published a fresh, matching ABI heartbeat.
    """
    actual = svc.get_config(user_id, conn_id, "runner_decision_abi", conn) or ""
    published_owner = (
        svc.get_config(user_id, conn_id, "runner_decision_owner", conn) or ""
    )
    owner = svc.get_config(user_id, conn_id, "runner_owner", conn) or ""
    owner_task = owner.rsplit("@", 1)[0] if "@" in owner else ""
    heartbeat = owner.rsplit("@", 1)[-1] if "@" in owner else ""
    age = None
    try:
        parsed = datetime.fromisoformat(heartbeat.replace("Z", "+00:00"))
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
        age = max(0.0, (datetime.now(timezone.utc) - parsed.astimezone(timezone.utc)).total_seconds())
    except (TypeError, ValueError):
        pass
    fresh = age is not None and age <= RUNNER_HEARTBEAT_MAX_AGE_SECONDS
    owner_matches = bool(owner_task) and published_owner == owner_task
    expected = expected_decision_abi(svc.get_config(user_id, conn_id, "strategy_version", conn))
    ok = actual == expected and fresh and owner_matches
    age_text = "missing" if age is None else f"{age:.1f}s"
    return GateResult(
        "runner_decision_abi",
        ok,
        f"loaded={actual or 'missing'} expected={expected} "
        f"owner_match={int(owner_matches)} heartbeat_age={age_text}",
    )


def evaluate_all(adapter, user_id: str, conn_id: str, conn=None) -> list:
    """All gates for this (user_id, conn_id)."""
    return [
        gate_mode_armed(user_id, conn_id, conn),
        gate_kill_switch_clear(user_id, conn_id, conn),
        gate_broker_connected(adapter, user_id, conn_id, conn),
        gate_account_isolation(adapter, user_id, conn_id, conn),
        gate_daily_loss_ok(adapter, user_id, conn_id, conn),
        gate_lots_within_cap(user_id, conn_id, conn),
        gate_static_order_proxy(user_id, conn_id, conn),
        gate_runner_decision_abi(user_id, conn_id, conn),
        gate_transport_clear(user_id, conn_id, conn),
    ]


def gate_transport_clear(user_id, conn_id, conn=None):
    from storage.live_db import get_live_conn
    with contextlib.closing(get_live_conn()) if conn is None else contextlib.nullcontext(conn) as c:
        pending = c.execute("SELECT 1 FROM live_order_requests WHERE user_id=? AND conn_id=? AND outcome IN ('reserved','uncertain') LIMIT 1", (user_id,conn_id)).fetchone()
    return GateResult('order_transport_clear', not pending, 'Order request needs broker reconciliation' if pending else 'No uncertain order requests')


def all_passed(results: list) -> bool:
    return all(r.passed for r in results)


def _is_final_status(status: str) -> bool:
    return str(status or "").upper() in {
        "COMPLETE", "COMPLETED", "FILLED", "EXECUTED", "REJECTED",
        "CANCELLED", "CANCELED", "FAILED",
    }


# An EXIT whose prior same-bar attempt ended in one of these DID NOT place at
# the broker (throttled / gate-blocked / crashed mid-place) — so the stop is
# still needed and must be re-attempted, not permanently SKIPped by its idem
# key (2026-07-07: a throttled exit was abandoned for the rest of the bar). The
# pre-exit long reconcile (_verify_matching_long_before_exit) guards the retry
# against a double-sell. Entries never auto-retry (no such reconcile).
_RETRIABLE_EXIT_STATUSES = {"FAILED", "GATE_BLOCKED", "PENDING"}


# ── Entry size step-down (2026-10-05) ─────────────────────────────────────
# With no lot cap, a large entry can be refused for its SIZE: above the
# exchange freeze quantity (one order may not exceed it), or beyond the
# account's margin. Entries are first sized down to the freeze quantity, then
# a broker REJECTION that names size is re-sent at half the lots, down to one.
# Only a definitive "no" steps down: an uncertain transport outcome may have
# placed the order (re-sending could double it), and a throttle or an IOC
# no-fill is not about size.
FREEZE_QTY = {"NIFTY": 1800, "SENSEX": 1000}   # Angel instrument master, 2026-10-02
LOT_STEP_DOWN_MAX = 4
_SIZE_REJECTION = re.compile(
    r"quantit|\bqty\b|freeze|margin|fund|insufficient|exposure|\blots?\b", re.I)


def freeze_capped_lots(lots: int, lot_size: int, underlying: str) -> int:
    """The most lots one order may carry under the exchange freeze quantity."""
    freeze = FREEZE_QTY.get(str(underlying or "").upper())
    if not freeze or lot_size <= 0:
        return max(1, int(lots))
    return max(1, min(int(lots), freeze // lot_size))


def rejection_reason(result: OrderResult) -> str | None:
    """The broker's reason when it definitively refused the order, else None.

    Two shapes: refused at placement (the transport raised 'Broker rejected the
    order (...)' -> status FAILED), or accepted then rejected by the broker's
    RMS (status REJECTED, reason in the order-book snapshot)."""
    raw = result.raw or {}
    if raw.get("idempotent_skip"):
        return None
    status = str(result.status or "").upper()
    if status == "REJECTED":
        snap = raw.get("status_snapshot") or {}
        return str(snap.get("status_message") or snap.get("text")
                   or snap.get("status_message_raw") or "rejected")
    if status == "FAILED":
        msg = str(raw.get("message") or "")
        if msg.startswith("Broker rejected the order"):
            return msg
    return None


def stepped_down_lots(lots: int, result: OrderResult) -> int | None:
    """Half the lots when the broker refused this entry for its size, else None."""
    reason = rejection_reason(result)
    if int(lots) <= 1 or not reason or not _SIZE_REJECTION.search(reason):
        return None
    return max(1, int(lots) // 2)


def filled_qty(result: OrderResult) -> int | None:
    """Quantity the broker reports filled (Kite filled_quantity / Angel
    filledshares), or None when the snapshot does not say."""
    snap = (result.raw or {}).get("status_snapshot") or {}
    for key in ("filled_quantity", "filledshares", "filledqty"):
        value = snap.get(key)
        if value not in (None, ""):
            try:
                return int(float(value))
            except (TypeError, ValueError):
                return None
    return None


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
def _blocked_exit(idem_key: str, status: str, raw: dict, conn=None) -> OrderResult:
    svc.update_order_ledger(idem_key, status=status, placed_at=_now_iso(), conn=conn)
    return OrderResult(
        broker_order_id=None,
        status=status,
        avg_fill_price=None,
        raw=raw,
    )


def _verify_matching_long_before_exit(adapter, *, symbol: str, qty: int,
                                      conn_id: str, idem_key: str,
                                      conn=None) -> OrderResult | None:
    """Fail closed before any live SELL.

    This live strategy is long-options only. If the broker is flat, short, on a
    different contract, or has less quantity than requested, sending SELL could
    create an unintended written option. In that case, block without touching
    the broker order endpoint.
    """
    try:
        pos = adapter.get_position()
    except Exception as e:
        log.warning(
            "EXIT blocked: broker position read failed | conn=%s symbol=%s key=%s type=%s",
            conn_id, symbol, idem_key, type(e).__name__,
        )
        return _blocked_exit(
            idem_key,
            "POSITION_CHECK_FAILED",
            {
                "requested_symbol": symbol,
                "requested_qty": qty,
                "error_type": type(e).__name__,
            },
            conn,
        )

    requested_symbol = str(symbol or "").strip().upper()
    broker_symbol = str(pos.symbol or "").strip().upper()
    requested_qty = abs(int(qty or 0))
    broker_qty = int(pos.qty or 0)
    if broker_symbol != requested_symbol or broker_qty <= 0:
        log.warning(
            "EXIT blocked: no matching long position | conn=%s requested=%s/%s "
            "broker=%s/%s key=%s",
            conn_id, requested_symbol, requested_qty, broker_symbol, broker_qty, idem_key,
        )
        return _blocked_exit(
            idem_key,
            "NO_LONG_POSITION",
            {
                "requested_symbol": symbol,
                "requested_qty": requested_qty,
                "broker_symbol": pos.symbol,
                "broker_qty": pos.qty,
            },
            conn,
        )
    if requested_qty <= 0 or requested_qty > broker_qty:
        log.warning(
            "EXIT blocked: qty exceeds broker long position | conn=%s symbol=%s "
            "requested=%s broker=%s key=%s",
            conn_id, requested_symbol, requested_qty, broker_qty, idem_key,
        )
        return _blocked_exit(
            idem_key,
            "EXIT_QTY_EXCEEDS_POSITION",
            {
                "requested_symbol": symbol,
                "requested_qty": requested_qty,
                "broker_symbol": pos.symbol,
                "broker_qty": pos.qty,
            },
            conn,
        )
    return None


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
                     conn=None, price_fn=None) -> OrderResult:
    """THE single order chokepoint, per (user_id, conn_id).

    `price_fn` (live ENTRY only): called after every gate has passed, right
    before the broker call, and returns the limit to send -- or None to skip the
    entry (quote unavailable / beyond the chase cap). The gates call the broker
    and can take tens of seconds; a price read before them is stale by the time
    an IOC entry reaches the exchange (2026-09-29: three cancelled entries).

    1. INSERT OR IGNORE a PENDING live_orders row keyed by idem_key.
    2. If the row already existed (not newly inserted) -> SKIP the broker
       call entirely; return the recorded result (defends double-click /
       restart re-fire / same-bar re-entry).
    3. Else if dry_run -> synthetic OrderResult(status='DRY_RUN', avg=price),
       NO broker call (logged-intent path).
    4. Else (LIVE_ARMED real path) -> evaluate all 7 gates for THIS user/conn;
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
        status = str(existing.get("status") or "").upper()
        # Retry a still-needed EXIT that did not place last time; everything
        # else (entries, already-placed/working orders) stays SKIPped so the
        # idempotency double-order guard holds.
        retry_exit = (
            action == "EXIT"
            and not existing.get("broker_order_id")
            and status in _RETRIABLE_EXIT_STATUSES
        )
        if not retry_exit:
            log.info("idempotent SKIP (key seen) | conn=%s key=%s status=%s",
                     conn_id, idem_key, existing.get("status"))
            return OrderResult(
                broker_order_id=existing.get("broker_order_id"),
                status=existing.get("status", "PENDING"),
                avg_fill_price=existing.get("avg_fill_price"),
                raw={"idempotent_skip": True},
            )
        log.info("idempotent EXIT retry (prior=%s, nothing placed) | conn=%s key=%s",
                 status, conn_id, idem_key)
        # fall through: re-run gates + verify-long + exit_all; the update_order_
        # ledger call below overwrites this row with the new outcome.

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
            gate_static_order_proxy(user_id, conn_id, conn, for_exit=True),
        ]
    else:
        gates_started = time.monotonic()
        gates = evaluate_all(adapter, user_id, conn_id, conn)
        gates_took = time.monotonic() - gates_started
        if gates_took > 3.0:
            log.info("entry gates took %.1fs | conn=%s key=%s", gates_took, conn_id, idem_key)

    if not all_passed(gates):
        failed = [g.name for g in gates if not g.passed]
        log.warning("Gates FAILED — not placing | conn=%s failing=%s key=%s",
                    conn_id, failed, idem_key)
        svc.update_order_ledger(idem_key, status="GATE_BLOCKED", conn=conn)
        return OrderResult(broker_order_id=None, status="GATE_BLOCKED",
                           avg_fill_price=None, raw={"failed_gates": failed})

    if action == "EXIT":
        blocked = _verify_matching_long_before_exit(
            adapter, symbol=symbol, qty=qty, conn_id=conn_id,
            idem_key=idem_key, conn=conn,
        )
        if blocked is not None:
            return blocked
    elif price_fn is not None:
        fresh = price_fn()
        if fresh is None:
            log.warning("entry not placed: no quote or beyond chase cap | conn=%s key=%s",
                        conn_id, idem_key)
            svc.update_order_ledger(idem_key, status="PRICE_SKIP", conn=conn)
            return OrderResult(broker_order_id=None, status="PRICE_SKIP",
                               avg_fill_price=None, raw={"price_skip": True})
        price = fresh
        svc.update_order_ledger(idem_key, limit_price=price, conn=conn)

    try:
        if action == "EXIT":
            result = adapter.exit_all(symbol=symbol, qty=qty, reason="exit",
                                      idempotency_key=idem_key, price=price)
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
