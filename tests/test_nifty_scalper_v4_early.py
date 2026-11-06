"""Nifty Scalper V4 at the 1.5% and 2.5% day targets: out of a trade NIFTY 35 points against within 600 s of the fill
(Vipin, 2026-11-06; replaced 25 points / 180 s at 2.5%). 2% and 3% and other presets unchanged. Orders use fakes or
DRY_RUN."""
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


@pytest.mark.parametrize("version,pct,on", [(V4, 0.025, True), (V4, 0.015, True), (V4, None, True), (V4, 0.02, False),
                                            (V4, 0.03, False), (V3, 0.025, False), (V3, 0.015, False)])
def test_on_for_v4_at_1_5_and_2_5_percent(version, pct, on):
    p = pe.params_for(version, pct)
    assert (p.early_adverse_pts, p.early_adverse_secs) == ((35.0, 600.0) if on else (0.0, 0.0))
    assert ex.PROPOSER_DECISION_ABI == "proposer-dt25-live-v19"


@pytest.mark.parametrize("pct", [0.015, 0.025])
@pytest.mark.parametrize("side,move,secs,fires", [
    ("CE", -35.0, 600, True), ("CE", -34.9, 60, False), ("CE", -60.0, 601, False), ("CE", -25.0, 60, False),
    ("PE", 35.0, 1, True), ("PE", -40.0, 10, False), ("CE", 40.0, 10, False)])
def test_engine_rule_bounds(pct, side, move, secs, fires):
    eng = pe.ProposerEngine(pe.params_for(V4, pct))
    eng.skip_cd_arm = True
    pos = pe.Position(side, 100.0, 24000.0, 65)
    sig = eng.early_adverse(T0 + timedelta(seconds=secs), T0, pos, 24000.0 + move)
    assert (sig is not None) is fires
    if fires:
        assert (sig.action, sig.side, sig.reason) == ("EXIT", "CALL" if side == "CE" else "PUT", "early_adverse")
        assert not eng.skip_cd_arm                                       # re-entry cooldown re-arms like a loss
    assert pe.ProposerEngine(pe.params_for(V4, 0.02)).early_adverse(T0, T0, pos, 23000.0) is None
    assert eng.early_adverse(T0, None, pos, 23000.0) is None


def test_both_paper_books_take_the_rule_from_1_july():
    for book in (tr.NIFTY_V4, tr.NIFTY_V4_DT25):
        assert tr.early_adverse_on("2026-06-30", book) is None
        for day in ("2026-07-01", "2026-11-09"):
            p = tr.params_on(day, book)
            assert (p.early_adverse_pts, p.early_adverse_secs) == (35.0, 600.0)
    assert tr.params_on("2026-10-20", tr.NIFTY_V3).early_adverse_pts == 0.0


def test_early_adverse_steps_apply_in_date_order(monkeypatch):
    monkeypatch.setitem(tr.EARLY_ADVERSE_FROM, tr.NIFTY_V4_DT25.key,
                        (("2026-07-01", (25.0, 180.0)), ("2026-11-09", (35.0, 600.0))))
    assert tr.early_adverse_on("2026-11-06", tr.NIFTY_V4_DT25) == (25.0, 180.0)
    assert tr.early_adverse_on("2026-11-09", tr.NIFTY_V4_DT25) == (35.0, 600.0)


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


@pytest.mark.parametrize("pct,secs", [("2.5", 90), ("1.5", 540)])
def test_live_exits_within_ten_minutes_on_the_tick_spot(db, monkeypatch, pct, secs):  # noqa: F811
    ctx, market, feed = _open(monkeypatch, pct)
    market.nifty -= 35
    assert not cycle(ctx, market, feed, NOW + timedelta(seconds=secs))
    assert svc.get_trade_state(USER, CONN)["position"] == "NONE" and _exit_reason() == "early_adverse"


@pytest.mark.parametrize("pct,move,secs", [("2.5", 35, 620), ("1.5", 35, 620), ("2", 35, 90), ("3", 35, 90),
                                           ("2.5", 25, 90), ("1.5", 25, 90)])
def test_live_holds_after_the_window_short_of_35_or_at_other_targets(db, monkeypatch, pct, move, secs):  # noqa: F811
    ctx, market, feed = _open(monkeypatch, pct)
    market.nifty -= move                                                  # inside the ~46-point floor
    assert cycle(ctx, market, feed, NOW + timedelta(seconds=secs))
    assert svc.get_trade_state(USER, CONN)["position"] == "OPEN" and _exit_reason() is None


def test_entry_time_is_read_as_ist():
    assert pr.entry_time_ist("2026-11-06T04:00:05+00:00") == datetime(2026, 11, 6, 9, 30, 5)
    assert pr.entry_time_ist(None) is None and pr.entry_time_ist("bad") is None
