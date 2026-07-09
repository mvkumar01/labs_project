"""Configure-page strategy selection safety checks."""
from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest
from flask import Flask

from labs.ui.live_routes import live_bp
from live import live_service as svc
from live.auth_gate import register_auth_gate
import storage.live_db as live_db


USER_ID = "strategy-user"
CONN_ID = f"{USER_ID}:angel"
CSRF_TOKEN = "test-csrf-token"


@pytest.fixture
def client(monkeypatch, tmp_path):
    monkeypatch.setattr(live_db, "LIVE_DB_PATH", tmp_path / "live.db")
    live_db.init_live_db()
    svc.upsert_connection(
        USER_ID,
        CONN_ID,
        broker="angel",
        account_label="Strategy test",
        account_ref="angel:strategy-test",
        status="connected",
    )
    svc.set_selected_broker(USER_ID, "angel")

    root = Path(__file__).resolve().parents[1]
    app = Flask(
        __name__,
        template_folder=str(root / "templates"),
        static_folder=str(root / "static"),
    )
    app.secret_key = "test-secret"
    register_auth_gate(app)
    app.register_blueprint(live_bp)

    client = app.test_client()
    with client.session_transaction() as session:
        session["user_id"] = USER_ID
        session["live_broker"] = "angel"
        session["live_csrf"] = CSRF_TOKEN
    return client


def _configure(client, strategy: str, *, lots: int = 1, daily_loss_cap: int = 3000):
    return client.post(
        "/live/configure",
        data={
            "csrf_token": CSRF_TOKEN,
            "lots": str(lots),
            "daily_loss_cap": str(daily_loss_cap),
            "strategy": strategy,
        },
    )


def _set_strategy(decision_engine: str, strategy_version: str) -> None:
    svc.set_config(USER_ID, CONN_ID, "decision_engine", decision_engine)
    svc.set_config(USER_ID, CONN_ID, "strategy_version", strategy_version)


def _strategy_pair() -> tuple[str, str]:
    return (
        svc.get_config(USER_ID, CONN_ID, "decision_engine"),
        svc.get_config(USER_ID, CONN_ID, "strategy_version"),
    )


def test_configure_renders_one_strategy_select_without_bot_variant(client):
    response = client.get("/live/configure")

    assert response.status_code == 200
    page = response.get_data(as_text=True)
    assert page.count('name="strategy"') == 1
    assert 'name="bot_variant"' not in page
    assert 'name="book_role"' not in page
    assert 'name="exec_mode"' not in page
    assert 'name="om_r2_enabled"' not in page
    assert "Alpha v2.11" in page
    assert "Alpha v2.12" in page
    assert "Switch only while FLAT" in page


def test_flat_strategy_change_updates_config_and_clears_v212_latches(client):
    _set_strategy("signal_engine", "hybrid_alpha_v28")
    state = svc.get_trade_state(USER_ID, CONN_ID)
    state.update({
        "recovery_armed": 1,
        "recovery_level": 24500.0,
        "recovery_side": "PUT",
        "spot_stop_bar": "2026-07-10T09:20:00+00:00",
        "champion_trade_date": "2026-07-10",
        "champion_closed_count": 4,
    })
    svc.save_trade_state(USER_ID, CONN_ID, state)

    response = _configure(client, "champion_v212")

    assert response.status_code == 302
    assert _strategy_pair() == ("champion_replay", "v2.12")
    state = svc.get_trade_state(USER_ID, CONN_ID)
    assert state["recovery_armed"] == 0
    assert state["recovery_level"] is None
    assert state["recovery_side"] is None
    assert state["spot_stop_bar"] is None
    assert state["champion_trade_date"] == "2026-07-10"
    assert state["champion_closed_count"] == 4


def test_open_position_rejects_strategy_change_without_mutating_config(client):
    _set_strategy("champion_replay", "v2.11")
    svc.set_config(USER_ID, CONN_ID, "lots", 1)
    state = svc.get_trade_state(USER_ID, CONN_ID)
    state["position"] = "OPEN"
    svc.save_trade_state(USER_ID, CONN_ID, state)

    response = _configure(client, "champion_v212", lots=2)

    assert response.status_code == 200
    assert "cannot change while this connection has an open position" in response.get_data(as_text=True)
    assert _strategy_pair() == ("champion_replay", "v2.11")
    assert svc.get_lots(USER_ID, CONN_ID) == 1


def test_open_position_allows_same_strategy_to_save_other_configuration(client):
    _set_strategy("champion_replay", "v2.11")
    state = svc.get_trade_state(USER_ID, CONN_ID)
    state["position"] = "OPEN"
    svc.save_trade_state(USER_ID, CONN_ID, state)

    response = _configure(client, "champion_v211", daily_loss_cap=4200)

    assert response.status_code == 302
    assert _strategy_pair() == ("champion_replay", "v2.11")
    assert svc.get_daily_loss_cap(USER_ID, CONN_ID) == 4200


def test_unknown_strategy_does_not_change_existing_config(client):
    _set_strategy("champion_replay", "v2.11")

    response = _configure(client, "not-a-preset")

    assert response.status_code == 302
    assert _strategy_pair() == ("champion_replay", "v2.11")


def test_dashboard_displays_active_strategy_label(client):
    _set_strategy("champion_replay", "v2.12")

    response = client.get("/live/")

    assert response.status_code == 200
    assert "Alpha v2.12" in response.get_data(as_text=True)


def test_new_live_schema_omits_retired_r2_source_ledger():
    conn = sqlite3.connect(":memory:")
    try:
        live_db.init_live_db(conn)
        table = conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' "
            "AND name='live_source_ledger'"
        ).fetchone()
        assert table is None
    finally:
        conn.close()
