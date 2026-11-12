"""Archived live presets (Vipin, 2026-11-06): the configure page offers only ACTIVE_PRESETS, an account already on an
archived preset keeps it, and the engine versions stay known so history and old runners still resolve."""
from __future__ import annotations

from labs.ui import live_routes as lr
from live import live_service as svc
from live.engine import proposer_engine as pe
from tests.test_configure_strategy import CONN_ID, CSRF_TOKEN, USER_ID, _set_strategy, _strategy_pair
from tests.test_configure_strategy import client  # noqa: F401

KEEP = {"gold_cci_v2", "crudem_combo_v1", "champion_v214e", "proposer_sensex_v45", "nifty_scalper_v4",
        "oi_unwind_nifty", "oi_unwind_sensex", "nifty_scalper_v3_reverse"}


def _select(client):  # noqa: F811
    page = client.get("/live/configure").get_data(as_text=True)
    return page.split('id="strategy"')[1].split("</select>")[0]


def _post(client, strategy):  # noqa: F811
    return client.post("/live/configure", data={"csrf_token": CSRF_TOKEN, "lots": "1", "daily_loss_cap": "3000",
                                                "strategy": strategy, "day_target": "1.5"})


def test_keep_list_and_archived_code_stays():
    assert set(lr.ACTIVE_PRESETS) == KEEP and KEEP <= set(lr.STRATEGY_PRESETS)
    assert len(lr.STRATEGY_PRESETS) > len(KEEP)                             # archived presets are still defined
    for version in (pe.STRATEGY_VERSION_NIFTY_SCALPER_V1, pe.STRATEGY_VERSION_NIFTY_SCALPER_V3, "proposer_dt25"):
        assert version in pe.KNOWN_VERSIONS and lr.strategy_name(version) != version


def test_only_the_keep_list_is_offered(client, monkeypatch):  # noqa: F811
    monkeypatch.setattr(lr, "ACTIVE_PRESETS", tuple(KEEP))
    _set_strategy("proposer", pe.STRATEGY_VERSION_NIFTY_SCALPER_V4)
    offered = {k for k in lr.STRATEGY_PRESETS if f'value="{k}"' in _select(client)}
    assert offered == KEEP


def test_an_account_on_an_archived_preset_keeps_it(client, monkeypatch):  # noqa: F811
    monkeypatch.setattr(lr, "ACTIVE_PRESETS", tuple(KEEP))
    _set_strategy("champion_replay", "v2.12")
    select = _select(client)
    assert 'value="champion_v212" selected' in select and 'value="champion_v211"' not in select
    assert _post(client, "champion_v212").status_code == 302 and _strategy_pair() == ("champion_replay", "v2.12")


def test_choosing_an_archived_preset_is_refused(client, monkeypatch):  # noqa: F811
    monkeypatch.setattr(lr, "ACTIVE_PRESETS", tuple(KEEP))
    _set_strategy("proposer", pe.STRATEGY_VERSION_NIFTY_SCALPER_V4)
    page = _post(client, "nifty_scalper_v3").get_data(as_text=True)
    assert "is archived. Nothing was changed." in page
    assert _strategy_pair() == ("proposer", pe.STRATEGY_VERSION_NIFTY_SCALPER_V4)
    assert svc.get_config(USER_ID, CONN_ID, "strategy_version") == pe.STRATEGY_VERSION_NIFTY_SCALPER_V4
