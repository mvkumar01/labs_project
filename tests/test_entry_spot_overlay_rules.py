"""Live-only overlay and canonical v2.12 policy regression tests.

v2.13 -> fast tick stop past a 5-pt noise buffer and next-open fallback.
v2.12 -> no live-only authority; completed one-minute replay owns every event.
Plus the phantom-P&L guard: an exit with no captured entry_price must NOT book
a trade or move realized_pnl (2026-07-10 +Rs34k phantom that blinded the
daily-loss kill-switch).
"""
from __future__ import annotations

from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import live.live_runner as lr

CONN = "user-x:angel"


def _hit(**kw):
    return lr._entry_spot_stop_hit(**kw)


# ── v2.13: 5-pt buffer ────────────────────────────────────────────────────
def test_v213_call_within_buffer_does_not_fire():
    # anchor 24160, buffer 5 -> a 3-pt dip must NOT stop.
    assert _hit(side="CALL", anchor=24160.0, tick=24157.0) is False


def test_v213_call_beyond_buffer_fires():
    assert _hit(side="CALL", anchor=24160.0, tick=24155.0) is True


def test_v213_put_buffer_symmetry():
    assert _hit(side="PUT", anchor=24160.0, tick=24163.0) is False
    assert _hit(side="PUT", anchor=24160.0, tick=24165.0) is True


def test_missing_anchor_or_tick_never_fires():
    assert _hit(side="CALL", anchor=None, tick=24100.0) is False
    assert _hit(side="CALL", anchor=24160.0, tick=None) is False


# ── v2.12: minute-boundary close-check ────────────────────────────────────
def test_v212_has_no_live_only_execution_authority():
    policy = lr.champion_live_policy("v2.12")
    assert policy.fast_stop_overlay is False
    assert policy.next_open_fallback is False


def test_v213_retains_explicit_additive_risk_authority():
    policy = lr.champion_live_policy("v2.13")
    assert policy.fast_stop_overlay is True
    assert policy.next_open_fallback is True


# ── phantom-P&L guard ─────────────────────────────────────────────────────
def _track_svc(monkeypatch):
    calls = {"record": [], "day_pnl": []}
    monkeypatch.setattr(lr.svc, "record_trade", lambda *a, **k: calls["record"].append(k))
    monkeypatch.setattr(lr.svc, "add_day_pnl", lambda *a, **k: calls["day_pnl"].append((a, k)))
    monkeypatch.setattr(lr, "notify_telegram", lambda *_a, **_k: None)
    monkeypatch.setattr(lr.svc, "get_config", lambda *a, **k: "v2.12")
    return calls


def test_phantom_exit_with_zero_entry_price_books_nothing(monkeypatch):
    calls = _track_svc(monkeypatch)
    lr._record_exit_result(
        "u", CONN, {"entry_price": 0.0, "symbol": "NIFTY2671424000CE", "side": "CALL"},
        exit_price=257.5, qty=65, reason="ENTRY_SPOT_SL_TICK", dry_run=False)
    assert calls["record"] == []      # no ledger row
    assert calls["day_pnl"] == []     # realized_pnl untouched


def test_phantom_exit_with_missing_entry_price_books_nothing(monkeypatch):
    calls = _track_svc(monkeypatch)
    lr._record_exit_result(
        "u", CONN, {"symbol": "NIFTY2671424000CE"},  # no entry_price at all
        exit_price=270.0, qty=65, reason="ENTRY_SPOT_SL", dry_run=False)
    assert calls["record"] == []
    assert calls["day_pnl"] == []


def test_real_entry_price_still_books(monkeypatch):
    calls = _track_svc(monkeypatch)
    monkeypatch.setattr(lr.svc, "calc_net_option_pnl", lambda *a, **k: {
        "gross_pnl": -211.25, "net_pnl": -291.3,
        "charges": {"total_charges": 80.05}})
    monkeypatch.setattr(lr, "check_daily_loss", lambda *a, **k: True)
    lr._record_exit_result(
        "u", CONN, {"entry_price": 274.45, "symbol": "NIFTY2671424300PE", "side": "PUT"},
        exit_price=271.2, qty=65, reason="ENTRY_SPOT_SL_TICK", dry_run=False)
    assert len(calls["record"]) == 1   # a real captured entry DOES book
    assert len(calls["day_pnl"]) == 1
