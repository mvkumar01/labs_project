"""Live preset Sensex V4 / V5: one configuration whose loss floor picks the version - -20% of the
premium (proposer_dt35_v3, Sensex V4) or SENSEX 150 points against the entry (proposer_dt35_v5,
Sensex V5). The engine's spot floor must trade exactly as the Sensex V5 paper book's."""
from __future__ import annotations

import dataclasses
from datetime import datetime, timedelta

import numpy as np
import pytest

from labs.engine import proposer_px_tracker as tr
from live import live_executor as ex
from live import live_service as svc
from live.engine import proposer_engine as pe
from tests.test_configure_strategy import CONN_ID, CSRF_TOKEN, USER_ID, _set_strategy, _strategy_pair
from tests.test_configure_strategy import client  # noqa: F401  (fixture)


def test_v5_is_v4_with_the_premium_floor_swapped_for_150_points():
    v4, v5 = pe.params_for(pe.STRATEGY_VERSION_V3_DT35), pe.params_for(pe.STRATEGY_VERSION_V5)
    assert pe.STRATEGY_VERSION_V5 == "proposer_dt35_v5"
    assert (v4.loss_floor_pct, v4.spot_floor_pts) == (-0.20, 0.0)
    assert (v5.loss_floor_pct, v5.spot_floor_pts) == (-0.999, 150.0)
    assert dataclasses.replace(v5, loss_floor_pct=v4.loss_floor_pct, spot_floor_pts=0.0) == v4
    assert pe.STRATEGY_VERSION_V5 in pe.KNOWN_VERSIONS
    # the paper book's settings for its live-era sessions are the same rules
    book = tr.params_on("2026-10-19", tr.V5)
    assert dataclasses.replace(book, spot_floor_pts=150.0) == v5 and tr.SPOT_FLOOR_PTS["proposer_v5"] == 150.0


@pytest.mark.parametrize("side,espot,spot,fires", [
    ("CE", 80000.0, 79850.0, True), ("CE", 80000.0, 79850.5, False),
    ("PE", 80000.0, 80150.0, True), ("PE", 80000.0, 80149.0, False),
    ("CE", 80000.0, 80300.0, False)])
def test_engine_spot_floor(side, espot, spot, fires):
    eng = pe.ProposerEngine(pe.params_for(pe.STRATEGY_VERSION_V5))
    pos = pe.Position(side, 400.0, espot, 20)
    p = {"regime": "neutral", "x5": "neutral", "x5_conf": 0.9, "x5_asof": "2026-10-19T10:00:00"}
    sig = eng._evaluate_open(p, pos, 150.0, spot)          # premium -62%: V5 has no premium floor
    if fires:
        assert (sig.action, sig.reason) == ("EXIT", "spot_floor") and eng.skip_cd_arm is False
    else:
        assert sig.reason != "spot_floor"
    v4 = pe.ProposerEngine(pe.params_for(pe.STRATEGY_VERSION_V3_DT35))
    assert v4._evaluate_open(p, pos, 380.0, spot).reason != "spot_floor"   # V4 never uses it


def test_the_engine_floor_trades_exactly_as_the_paper_book():
    """Random sessions: the live engine on proposer_dt35_v5 and the V5 book's engine + spot_floor_exit
    give the same signal at every step."""
    rng = np.random.RandomState(7)
    regimes = ["bullish", "bearish", "neutral"]
    x5s = ["strong_bull", "mild_bull", "neutral", "mild_bear", "strong_bear"]
    floors = 0
    for _ in range(40):
        live = pe.ProposerEngine(pe.params_for(pe.STRATEGY_VERSION_V5))
        paper = pe.ProposerEngine(tr.params_on("2026-10-19", tr.V5))
        t0, spot, pos, drift, p = datetime(2026, 10, 19, 9, 20), 80000.0, None, 0.0, None
        for step in range(300):
            now = t0 + timedelta(seconds=10 * step)
            if step % 30 == 0:                                      # a new print every 5 minutes
                drift = rng.normal(0, 4)
                p = {"regime": regimes[rng.randint(3)], "x5": x5s[rng.randint(5)], "x5_conf": rng.uniform(0.3, 1),
                     "x5_asof": now.isoformat()}
            spot += drift + rng.normal(0, 8)
            ltp = None
            if pos:
                ltp = max(0.05, pos["ein"] + (1 if pos["side"] == "CE" else -1) * 0.6 * (spot - pos["espot"]))
            P = pe.Position(pos["side"], pos["ein"], pos["espot"], 20) if pos else pe.Position()
            move = rng.normal(0, 150)
            a = live.evaluate(now, p, P, option_ltp=ltp, spot=spot, day_move=move)
            b = paper.evaluate(now, p, P, option_ltp=ltp, spot=spot, day_move=move)
            if pos:
                b = tr.spot_floor_exit(paper, b, pos, spot, tr.V5)
            assert (a.action, a.side, a.reason) == (b.action, b.side, b.reason)
            assert live.skip_cd_arm == paper.skip_cd_arm
            if a.action == "ENTER":
                live.mark_entry(p)
                paper.mark_entry(p)
                pos = {"side": "CE" if a.side == "CALL" else "PE", "ein": 400.0, "espot": spot}
            elif a.action == "EXIT":
                floors += a.reason == "spot_floor"
                pos = None
    assert floors                                                   # the floor was exercised


def _configure(client, strategy, floor=None):
    data = {"csrf_token": CSRF_TOKEN, "lots": "1", "daily_loss_cap": "3000", "strategy": strategy}
    if floor is not None:
        data["loss_floor"] = floor
    return client.post("/live/configure", data=data)


def test_configure_offers_one_preset_with_the_floor_choice(client):          # noqa: F811
    _set_strategy("proposer", "proposer_dt35_v3")
    page = client.get("/live/configure").get_data(as_text=True)
    assert page.count('value="proposer_sensex_v45"') == 1 and 'value="proposer_dt35_v3"' not in page
    assert 'name="loss_floor"' in page and 'value="premium_20" selected' in page and "150 points" in page

    assert _configure(client, "proposer_sensex_v45", "spot_150").status_code == 302
    assert _strategy_pair() == ("proposer", "proposer_dt35_v5")
    assert 'value="spot_150" selected' in client.get("/live/configure").get_data(as_text=True)
    assert "loss floor: SENSEX 150 points" in client.get("/live/").get_data(as_text=True)
    assert _configure(client, "proposer_sensex_v45", "premium_20").status_code == 302
    assert _strategy_pair() == ("proposer", "proposer_dt35_v3")
    assert _configure(client, "proposer_sensex_v45", "bogus").status_code == 302      # keeps the floor
    assert _strategy_pair() == ("proposer", "proposer_dt35_v3")
    assert _configure(client, "proposer_sensex_v45").status_code == 302                # NIFTY-style POST
    assert _strategy_pair() == ("proposer", "proposer_dt35_v3")


def test_floor_cannot_change_with_a_position_open_or_on_an_old_runner(client):     # noqa: F811
    _set_strategy("proposer", "proposer_dt35_v3")
    svc.set_config(USER_ID, CONN_ID, "runner_decision_abi", "proposer-dt25-live-v8")
    page = _configure(client, "proposer_sensex_v45", "spot_150").get_data(as_text=True)
    assert "restarted first" in page and _strategy_pair() == ("proposer", "proposer_dt35_v3")
    svc.set_config(USER_ID, CONN_ID, "runner_decision_abi", ex.PROPOSER_DECISION_ABI)
    state = svc.get_trade_state(USER_ID, CONN_ID)
    state["position"] = "OPEN"
    svc.save_trade_state(USER_ID, CONN_ID, state)
    page = _configure(client, "proposer_sensex_v45", "spot_150").get_data(as_text=True)
    assert "open position" in page and _strategy_pair() == ("proposer", "proposer_dt35_v3")


def _configure_target(client, choice, typed=None, floor="spot_150"):
    data = {"csrf_token": CSRF_TOKEN, "lots": "1", "daily_loss_cap": "3000", "strategy": "proposer_sensex_v45",
            "loss_floor": floor, "day_target": choice}
    if typed is not None:
        data["day_target_custom"] = typed
    return client.post("/live/configure", data=data)


def test_day_target_choice_and_typed_value(client):                           # noqa: F811
    _set_strategy("proposer", "proposer_dt35_v5")
    page = client.get("/live/configure").get_data(as_text=True)
    assert 'name="day_target"' in page and 'value="3.5" selected' in page and 'name="day_target_custom"' in page

    assert _configure_target(client, "2.5").status_code == 302
    assert svc.get_config(USER_ID, CONN_ID, "proposer_day_target_pct") == "2.5"
    assert 'value="2.5" selected' in client.get("/live/configure").get_data(as_text=True)
    assert "day target 2.5%" in client.get("/live/").get_data(as_text=True)

    assert _configure_target(client, "other", "3.2%").status_code == 302
    assert svc.get_config(USER_ID, CONN_ID, "proposer_day_target_pct") == "3.2"
    page = client.get("/live/configure").get_data(as_text=True)
    assert 'value="other" selected' in page and 'value="3.2"' in page

    for bad in ("", "abc", "0.1", "25"):
        page = _configure_target(client, "other", bad).get_data(as_text=True)
        assert "is not a percent between" in page
        assert svc.get_config(USER_ID, CONN_ID, "proposer_day_target_pct") == "3.2"      # unchanged

    # a POST without the field keeps the stored target; the floor still changes
    assert _configure(client, "proposer_sensex_v45", "premium_20").status_code == 302
    assert _strategy_pair() == ("proposer", "proposer_dt35_v3")
    assert svc.get_config(USER_ID, CONN_ID, "proposer_day_target_pct") == "3.2"


def test_day_target_change_alone_needs_flat_and_a_current_runner(client):     # noqa: F811
    _set_strategy("proposer", "proposer_dt35_v5")
    svc.set_config(USER_ID, CONN_ID, "proposer_day_target_pct", "3.5")
    svc.set_config(USER_ID, CONN_ID, "runner_decision_abi", "proposer-dt25-live-v9")
    page = _configure_target(client, "2.5").get_data(as_text=True)
    assert "restarted first" in page and svc.get_config(USER_ID, CONN_ID, "proposer_day_target_pct") == "3.5"
    svc.set_config(USER_ID, CONN_ID, "runner_decision_abi", ex.PROPOSER_DECISION_ABI)
    state = svc.get_trade_state(USER_ID, CONN_ID)
    state["position"] = "OPEN"
    svc.save_trade_state(USER_ID, CONN_ID, state)
    page = _configure_target(client, "2.5").get_data(as_text=True)
    assert "open position" in page and svc.get_config(USER_ID, CONN_ID, "proposer_day_target_pct") == "3.5"
