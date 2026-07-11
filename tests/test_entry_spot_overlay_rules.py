"""Entry-spot stop overlay rules (2026-07-11 tick-vs-paper study).

v2.13 -> fast tick stop past a 5-pt noise buffer.
v2.12 -> paper cadence: fire once per completed candle (first poll of a new
         minute, after the entry candle), boundary close vs anchor.
Plus the phantom-P&L guard: an exit with no captured entry_price must NOT book
a trade or move realized_pnl (2026-07-10 +Rs34k phantom that blinded the
daily-loss kill-switch).
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import live.live_runner as lr

IST = timezone(timedelta(hours=5, minutes=30))
CONN = "user-x:angel"
ENTRY_TS = "2026-07-10T11:30:00+05:30"  # entry candle = 11:30 IST


def _hit(**kw):
    base = dict(conn_id=CONN, entry_time=ENTRY_TS)
    base.update(kw)
    return lr._entry_spot_stop_hit(**base)


# ── v2.13: 5-pt buffer ────────────────────────────────────────────────────
def test_v213_call_within_buffer_does_not_fire():
    # anchor 24160, buffer 5 -> a 3-pt dip must NOT stop.
    assert _hit(side="CALL", anchor=24160.0, tick=24157.0,
                v213_additive=True, now=datetime(2026, 7, 10, 11, 31, tzinfo=IST)) is False


def test_v213_call_beyond_buffer_fires():
    assert _hit(side="CALL", anchor=24160.0, tick=24155.0,
                v213_additive=True, now=datetime(2026, 7, 10, 11, 31, tzinfo=IST)) is True


def test_v213_put_buffer_symmetry():
    now = datetime(2026, 7, 10, 11, 31, tzinfo=IST)
    assert _hit(side="PUT", anchor=24160.0, tick=24163.0, v213_additive=True, now=now) is False
    assert _hit(side="PUT", anchor=24160.0, tick=24165.0, v213_additive=True, now=now) is True


def test_missing_anchor_or_tick_never_fires():
    now = datetime(2026, 7, 10, 11, 31, tzinfo=IST)
    assert _hit(side="CALL", anchor=None, tick=24100.0, v213_additive=True, now=now) is False
    assert _hit(side="CALL", anchor=24160.0, tick=None, v213_additive=True, now=now) is False


# ── v2.12: minute-boundary close-check ────────────────────────────────────
def _seed(prev_minute):
    lr._OVERLAY_MIN_SEEN.clear()
    if prev_minute is not None:
        lr._OVERLAY_MIN_SEEN[CONN] = prev_minute


def test_v212_fires_at_minute_rollover_after_entry():
    _seed(datetime(2026, 7, 10, 11, 30))          # last seen 11:30
    # first poll of 11:31, spot below anchor -> completed candle stop
    assert _hit(side="CALL", anchor=24160.0, tick=24155.0, v213_additive=False,
                now=datetime(2026, 7, 10, 11, 31, tzinfo=IST)) is True


def test_v212_no_refire_within_same_minute():
    _seed(datetime(2026, 7, 10, 11, 31))          # already saw 11:31
    # same minute again, still breached -> must NOT re-fire (paper cadence)
    assert _hit(side="CALL", anchor=24160.0, tick=24150.0, v213_additive=False,
                now=datetime(2026, 7, 10, 11, 31, tzinfo=IST)) is False


def test_v212_first_poll_of_position_syncs_without_firing():
    _seed(None)                                    # no prior minute recorded
    assert _hit(side="CALL", anchor=24160.0, tick=24150.0, v213_additive=False,
                now=datetime(2026, 7, 10, 11, 31, tzinfo=IST)) is False
    # ... and the sync means the NEXT rollover fires
    assert _hit(side="CALL", anchor=24160.0, tick=24150.0, v213_additive=False,
                now=datetime(2026, 7, 10, 11, 32, tzinfo=IST)) is True


def test_v212_entry_candle_is_suppressed():
    _seed(datetime(2026, 7, 10, 11, 29))          # rollover INTO the entry minute
    # cur minute == entry minute (11:30) -> paper only evaluates completed
    # candles, so the entry candle must not stop.
    assert _hit(side="CALL", anchor=24160.0, tick=24150.0, v213_additive=False,
                now=datetime(2026, 7, 10, 11, 30, tzinfo=IST)) is False


def test_v212_boundary_but_not_breached_does_not_fire():
    _seed(datetime(2026, 7, 10, 11, 30))
    assert _hit(side="CALL", anchor=24160.0, tick=24162.0, v213_additive=False,
                now=datetime(2026, 7, 10, 11, 31, tzinfo=IST)) is False


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
