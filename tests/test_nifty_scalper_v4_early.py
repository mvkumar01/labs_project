"""Nifty Scalper V4 at the 2.5% day target: out of a trade NIFTY 25 points against within 180 s of the fill (Vipin,
2026-11-06). Other day targets and presets unchanged. Orders use fakes or DRY_RUN."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from labs.engine import proposer_px_tracker as tr
from live import live_executor as ex, live_service as svc, proposer_runner as pr
from live.engine import proposer_engine as pe
from tests.test_nifty_scalper_v1 import NOW, cycle
from tests.test_nifty_scalper_v4 import _v4
from tests.test_proposer_live import USER, CONN, db  # noqa: F401

V3, V4 = pe.STRATEGY_VERSION_NIFTY_SCALPER_V3, pe.STRATEGY_VERSION_NIFTY_SCALPER_V4
T0 = datetime(2026, 11, 6, 9, 30, 0)


@pytest.mark.parametrize("version,pct,on", [(V4, 0.025, True), (V4, 0.015, False), (V4, 0.02, False), (V4, 0.03, False),
                                            (V4, None, False), (V3, 0.025, False)])
def test_on_only_for_v4_at_2_5_percent(version, pct, on):
    p = pe.params_for(version, pct)
    assert (p.early_adverse_pts, p.early_adverse_secs) == ((25.0, 180.0) if on else (0.0, 0.0))
    assert ex.PROPOSER_DECISION_ABI == "proposer-dt25-live-v18"


@pytest.mark.parametrize("side,move,secs,fires", [
    ("CE", -25.0, 180, True), ("CE", -24.9, 60, False), ("CE", -40.0, 181, False),
    ("PE", 25.0, 1, True), ("PE", -30.0, 10, False), ("CE", 30.0, 10, False)])
def test_engine_rule_bounds(side, move, secs, fires):
    eng = pe.ProposerEngine(pe.params_for(V4, 0.025))
    eng.skip_cd_arm = True
    pos = pe.Position(side, 100.0, 24000.0, 65)
    sig = eng.early_adverse(T0 + timedelta(seconds=secs), T0, pos, 24000.0 + move)
    assert (sig is not None) is fires
    if fires:
        assert (sig.action, sig.side, sig.reason) == ("EXIT", "CALL" if side == "CE" else "PUT", "early_adverse")
        assert not eng.skip_cd_arm                                       # re-entry cooldown re-arms like a loss
    assert pe.ProposerEngine(pe.params_for(V4, 0.015)).early_adverse(T0, T0, pos, 23000.0) is None
    assert eng.early_adverse(T0, None, pos, 23000.0) is None


def test_paper_book_takes_the_rule_v4_1_5_does_not():
    assert tr.params_on("2026-10-20", tr.NIFTY_V4_DT25).early_adverse_pts == 25.0
    assert tr.params_on("2026-10-20", tr.NIFTY_V4).early_adverse_pts == 0.0


def _open(monkeypatch, pct):
    ctx, market, feed, _ = _v4(monkeypatch)
    svc.set_config(USER, CONN, "proposer_day_target_pct", pct)
    assert cycle(ctx, market, feed)
    st = svc.get_trade_state(USER, CONN)
    st["entry_time"] = (NOW - timedelta(hours=5, minutes=30)).replace(tzinfo=timezone.utc).isoformat()
    svc.save_trade_state(USER, CONN, st)
    return ctx, market, feed


def _exit_reason():
    with pr.get_live_conn() as conn:
        row = conn.execute("SELECT reason FROM live_trades").fetchone()
    return row[0] if row else None


def test_live_exits_within_three_minutes_on_the_tick_spot(db, monkeypatch):  # noqa: F811
    ctx, market, feed = _open(monkeypatch, "2.5")
    market.nifty -= 25
    assert not cycle(ctx, market, feed, NOW + timedelta(seconds=90))
    assert svc.get_trade_state(USER, CONN)["position"] == "NONE" and _exit_reason() == "early_adverse"


@pytest.mark.parametrize("pct,secs", [("2.5", 200), ("1.5", 90)])
def test_live_holds_after_the_window_or_at_other_targets(db, monkeypatch, pct, secs):  # noqa: F811
    ctx, market, feed = _open(monkeypatch, pct)
    market.nifty -= 25                                                    # inside the ~46-point floor
    assert cycle(ctx, market, feed, NOW + timedelta(seconds=secs))
    assert svc.get_trade_state(USER, CONN)["position"] == "OPEN" and _exit_reason() is None


def test_entry_time_is_read_as_ist():
    assert pr.entry_time_ist("2026-11-06T04:00:05+00:00") == datetime(2026, 11, 6, 9, 30, 5)
    assert pr.entry_time_ist(None) is None and pr.entry_time_ist("bad") is None
