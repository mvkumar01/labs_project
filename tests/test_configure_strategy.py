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
    assert "Alpha 2.11 replay (B)" in page
    assert "Alpha v2.12" in page
    assert "Alpha v2.12 (close-confirmed)" in page
    assert "Alpha v2.13" in page
    assert "checked with the broker" in page


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


def test_broker_open_rejects_strategy_change_without_mutating_config(client, monkeypatch):
    from types import SimpleNamespace
    from labs.ui import live_routes as routes
    monkeypatch.setattr(routes, "_adapter_for", lambda *a, **k: SimpleNamespace(
        connect=lambda: None, is_connected=lambda: True,
        account_ref=lambda: "angel:strategy-test", configuration_is_clear=lambda: False))
    _set_strategy("champion_replay", "v2.11")
    svc.set_config(USER_ID, CONN_ID, "lots", 1)
    state = svc.get_trade_state(USER_ID, CONN_ID)
    state["position"] = "OPEN"
    svc.save_trade_state(USER_ID, CONN_ID, state)

    response = _configure(client, "champion_v212", lots=2)

    assert response.status_code == 200
    assert "broker currently reports an open position or working order" in response.get_data(as_text=True)
    assert _strategy_pair() == ("champion_replay", "v2.11")
    assert svc.get_lots(USER_ID, CONN_ID) == 1


def test_broker_flat_allows_switch_and_clears_stale_local_position(client, monkeypatch):
    from types import SimpleNamespace
    from labs.ui import live_routes as routes
    _set_strategy("champion_replay", "v2.11")
    state = svc.get_trade_state(USER_ID, CONN_ID)
    state.update(position="OPEN", symbol="STALE", qty=65)
    svc.save_trade_state(USER_ID, CONN_ID, state)
    monkeypatch.setattr(routes, "_adapter_for", lambda *a, **k: SimpleNamespace(
        connect=lambda: None, is_connected=lambda: True,
        account_ref=lambda: "angel:strategy-test", configuration_is_clear=lambda: True))
    response = _configure(client, "champion_v212", lots=2)
    assert response.status_code == 302
    assert _strategy_pair() == ("champion_replay", "v2.12")
    assert svc.get_trade_state(USER_ID, CONN_ID)["position"] == "NONE"


def test_broker_read_failure_never_clears_stale_position(client, monkeypatch):
    from labs.ui import live_routes as routes
    _set_strategy("champion_replay", "v2.11")
    state = svc.get_trade_state(USER_ID, CONN_ID)
    state["position"] = "OPEN"
    svc.save_trade_state(USER_ID, CONN_ID, state)
    def unavailable(*a, **k):
        raise RuntimeError("unavailable")
    monkeypatch.setattr(routes, "_adapter_for", unavailable)
    response = _configure(client, "champion_v212")
    assert b"Could not refresh the broker" in response.data
    assert _strategy_pair() == ("champion_replay", "v2.11")
    assert svc.get_trade_state(USER_ID, CONN_ID)["position"] == "OPEN"


def test_stale_uncertain_request_and_gold_mirror_are_archived_on_broker_flat(client, monkeypatch):
    import json
    from types import SimpleNamespace
    from labs.ui import live_routes as routes
    # The actual Gold engine key is supplied by the registry.
    engine, version = routes.STRATEGY_PRESETS['gold_cci_v2']
    _set_strategy(engine, version)
    with routes.cp.transaction() as c:
        c.execute("INSERT INTO live_order_requests VALUES('req','route',1,?,?,'entry','intent',1,'uncertain')",
                  (USER_ID, CONN_ID))
        c.execute("CREATE TABLE live_gold_v2_state(user_id TEXT,conn_id TEXT,payload TEXT,PRIMARY KEY(user_id,conn_id))")
        c.execute("INSERT INTO live_gold_v2_state VALUES(?,?,?)", (USER_ID, CONN_ID, json.dumps({
            'position': {'symbol': 'GOLD', 'qty': 1}, 'pending': {'key': 'stale'}, 'days': {'retained': 1}, 'seq': 2})))
    monkeypatch.setattr(routes, "_adapter_for", lambda *a, **k: SimpleNamespace(
        connect=lambda: None, is_connected=lambda: True,
        account_ref=lambda: "angel:strategy-test", configuration_is_clear=lambda: True))
    response = _configure(client, "champion_v212")
    assert response.status_code == 302
    with routes.cp.transaction() as c:
        gold = json.loads(c.execute('SELECT payload FROM live_gold_v2_state').fetchone()[0])
        assert gold['position'] is None and gold['pending'] is None and gold['days'] == {'retained': 1}
        audit = json.loads(c.execute("SELECT detail FROM live_admin_audit WHERE action='broker_flat_configuration_refresh'").fetchone()[0])
        assert audit['gold_state']['pending'] == {'key': 'stale'}
        assert c.execute("SELECT outcome FROM live_order_requests WHERE request_id='req'").fetchone()[0] == 'uncertain'


def test_concurrent_order_state_change_during_broker_refresh_does_not_switch(client, monkeypatch):
    from types import SimpleNamespace
    from labs.ui import live_routes as routes
    _set_strategy("champion_replay", "v2.11")
    state = svc.get_trade_state(USER_ID, CONN_ID)
    state['position'] = 'OPEN'
    svc.save_trade_state(USER_ID, CONN_ID, state)
    def refresh():
        changed = svc.get_trade_state(USER_ID, CONN_ID)
        changed['symbol'] = 'NEW_BROKER_ORDER'
        svc.save_trade_state(USER_ID, CONN_ID, changed)
        return True
    monkeypatch.setattr(routes, '_adapter_for', lambda *a, **k: SimpleNamespace(
        connect=lambda: None, is_connected=lambda: True,
        account_ref=lambda: 'angel:strategy-test', configuration_is_clear=refresh))
    response = _configure(client, 'champion_v212')
    assert b'Account state changed during the broker refresh' in response.data
    assert _strategy_pair() == ('champion_replay', 'v2.11')
    assert svc.get_trade_state(USER_ID, CONN_ID)['symbol'] == 'NEW_BROKER_ORDER'


def test_flat_strategy_change_supports_v213(client):
    _set_strategy("champion_replay", "v2.12")

    response = _configure(client, "champion_v213")

    assert response.status_code == 302
    assert _strategy_pair() == ("champion_replay", "v2.13")


def test_flat_strategy_change_supports_v211b(client):
    _set_strategy("champion_replay", "v2.11")

    response = _configure(client, "champion_v211b")

    assert response.status_code == 302
    assert _strategy_pair() == ("champion_replay", "v2.11b")

    response = client.get("/live/configure")
    page = response.get_data(as_text=True)
    assert 'value="champion_v211b" selected' in page


def test_flat_strategy_change_supports_v212_close_confirmed(client):
    _set_strategy("champion_replay", "v2.12")

    response = _configure(client, "champion_v212_close_confirmed")

    assert response.status_code == 302
    assert _strategy_pair() == (
        "champion_replay", "v2.12_closed_confirmed"
    )


def test_flat_strategy_change_supports_v212_b10(client):
    _set_strategy("champion_replay", "v2.12")

    response = _configure(client, "champion_v212_b10")

    assert response.status_code == 302
    assert _strategy_pair() == ("champion_replay", "v2.12_b10")
    # The reverse lookup must re-select B10, not fall through to plain v2.12.
    page = client.get("/live/configure").get_data(as_text=True)
    assert 'value="champion_v212_b10" selected' in page


def test_flat_strategy_change_supports_v214(client):
    _set_strategy("champion_replay", "v2.12_b10")

    response = _configure(client, "champion_v214")

    assert response.status_code == 302
    assert _strategy_pair() == ("champion_replay", "v2.14")
    page = client.get("/live/configure").get_data(as_text=True)
    assert 'value="champion_v214" selected' in page
    assert "Alpha v2.14 B (v2.11 replay (B) + B10)" in page
    assert "Alpha v2.14 A (B10: 10-pt stop buffer)" in page


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
    _set_strategy("champion_replay", "v2.13")

    response = client.get("/live/")

    assert response.status_code == 200
    assert "Alpha v2.13" in response.get_data(as_text=True)


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
