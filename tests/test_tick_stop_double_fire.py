"""Hotfix regressions (2026-07-10 incident): two tick stops in one minute
collided on the minute-granular idem key; the SKIP echoed the earlier order's
'complete' status, so the runner recorded a second trade and reset state
WITHOUT placing any broker order — DB flat while the broker stayed long, the
anchor self-heal re-armed the flat row, and the canonical EXIT later booked
+34k phantom P&L from the empty state.
"""
import live.live_runner as lr
from live.brokers.base import OrderResult


def _res(status, *, skip=False, order_id="X1"):
    return OrderResult(broker_order_id=order_id, status=status,
                       avg_fill_price=100.0,
                       raw={"idempotent_skip": True} if skip else {})


def test_idempotent_skip_is_never_freshly_applied():
    # The exact incident shape: SKIP echoing a prior COMPLETE exit.
    assert lr._freshly_applied(_res("complete", skip=True), dry_run=False) is False
    # A genuinely fresh terminal fill still applies.
    assert lr._freshly_applied(_res("COMPLETE"), dry_run=False) is True
    # Non-terminal fresh results still don't apply.
    assert lr._freshly_applied(_res("PLACED"), dry_run=False) is False
    # Dry-run semantics unchanged.
    assert lr._freshly_applied(_res("DRY_RUN"), dry_run=True) is True
    assert lr._freshly_applied(_res("DRY_RUN", skip=True), dry_run=True) is False


def test_overlay_gated_on_db_open_not_broker_open():
    """The overlay block must require the DB state row to be OPEN; a
    broker-open/DB-flat lag window must never re-fire it. Source-level guard:
    the condition uses the persisted state, not current_open alone."""
    import inspect

    src = inspect.getsource(lr.process_connection)
    overlay_idx = src.index("ENTRY_SPOT_SL_TICK")
    gate_idx = src.index('_db_open = (st.get("position") or "NONE").upper() == "OPEN"')
    assert gate_idx < overlay_idx
    assert "if current_open and _db_open and use_champion and v212_recovery:" in src


def test_anchor_self_heal_requires_db_open():
    import inspect

    src = inspect.getsource(lr.process_connection)
    heal_idx = src.index("anchor synced to replay")
    guard = src.index('and (st.get("position") or "NONE").upper() == "OPEN"')
    assert guard < heal_idx


def test_champion_exit_never_books_pnl_from_empty_state():
    """Flattening an orphan broker position is correct; recording a trade
    from a default-flat state (entry_price None -> 0 cost basis) is not."""
    import inspect

    src = inspect.getsource(lr.process_connection)
    arm = src.index('sig["action"] == "EXIT" and current_open')
    guarded = src.index('if _as_float(st.get("entry_price")) is not None:')
    assert arm < guarded
    assert "no ledger entry" in src
