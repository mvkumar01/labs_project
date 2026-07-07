"""Regression tests for the 2026-07-07 late/untracked entry-fill incident.

A LIMIT entry placed at a stale LTP lagged the rising premium, filled LATE and
untracked: entry_spot never armed (no spot-SL) and the exit booked from a 0 cost
basis (phantom +11.5k). Fix has two parts:
  D1  _marketable_limit  — cross the spread so the order fills in the poll window
  D2  _order_accepted    — record a WORKING (not-yet-final) entry so entry_spot
                           arms immediately; exits stay on strict _order_applied
"""
from live.brokers.base import OrderResult
from live.live_runner import (
    _marketable_limit, _order_accepted, _order_applied, OPTION_TICK,
)


def _res(status, *, order_id="A1", avg=None):
    return OrderResult(broker_order_id=order_id, status=status,
                       avg_fill_price=avg, raw={})


# ── D1: marketable pricing ────────────────────────────────────────────────
def test_marketable_buy_pays_up_sell_gives_up():
    assert _marketable_limit("BUY", 200.0) == 201.2    # 200 * 1.006
    assert _marketable_limit("SELL", 200.0) == 198.8   # 200 * 0.994
    # BUY is always >= LTP, SELL always <= LTP.
    assert _marketable_limit("BUY", 204.2) >= 204.2
    assert _marketable_limit("SELL", 204.2) <= 204.2


def test_marketable_rounds_to_option_tick_and_guards_bad_price():
    px = _marketable_limit("BUY", 237.3)
    assert abs(round(px / OPTION_TICK) - px / OPTION_TICK) < 1e-9   # on the tick
    for bad in (None, 0, -5):
        assert _marketable_limit("BUY", bad) == bad                # no-op


# ── D2: entry-recording gate ──────────────────────────────────────────────
def test_working_entry_is_accepted_but_not_applied():
    """The incident order: broker accepted it (has an id) but it is still
    'open' — it MUST be recorded so entry_spot arms, even though it is not yet
    a terminal fill."""
    working = _res("open")
    assert _order_accepted(working, dry_run=False) is True
    assert _order_applied(working.status, dry_run=False) is False   # old gate dropped it


def test_filled_entry_accepted():
    assert _order_accepted(_res("COMPLETE", avg=204.2), dry_run=False) is True


def test_rejected_or_orphan_entry_not_accepted():
    assert _order_accepted(_res("REJECTED"), dry_run=False) is False
    assert _order_accepted(_res("FAILED"), dry_run=False) is False
    assert _order_accepted(_res("GATE_BLOCKED", order_id=None), dry_run=False) is False
    # A working status with NO broker id is not a real position.
    assert _order_accepted(_res("open", order_id=None), dry_run=False) is False


def test_dry_run_gate_unchanged():
    """Dry-run must behave exactly like the old gate (byte-identical replays)."""
    assert _order_accepted(_res("DRY_RUN"), dry_run=True) is True
    assert _order_accepted(_res("open"), dry_run=True) is False
