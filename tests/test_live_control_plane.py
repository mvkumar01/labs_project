import contextlib
import os
from concurrent.futures import ThreadPoolExecutor
from types import SimpleNamespace

import pytest
from cryptography.fernet import Fernet
from flask import Flask

from live import control_plane as cp, live_service as svc, live_executor as ex
from live.brokers import order_transport as transport
from storage import live_db


@pytest.fixture
def env(tmp_path, monkeypatch):
    monkeypatch.setattr(live_db, "LIVE_DB_PATH", tmp_path / "live.db")
    monkeypatch.setattr(svc, "STATE_DIR", tmp_path)
    monkeypatch.setattr(svc, "_CRED_STORE", tmp_path / "credentials.json")
    monkeypatch.setenv("LABS_CRED_KEY", Fernet.generate_key().decode())
    monkeypatch.setenv("LIVE_ADMIN_USER_IDS", "admin")
    monkeypatch.setenv("LIVE_ORDERS_ENABLED", "1")
    svc.ensure_schema()
    with cp.transaction() as c:
        for uid in ("admin", "alice", "bob"):
            c.execute(
                "INSERT INTO live_users VALUES(?,?,?,?)", (uid, uid, "unused", "now")
            )
    for uid in ("alice", "bob"):
        for broker in ("angel", "zerodha"):
            svc.upsert_connection(
                uid,
                uid + ":" + broker,
                broker=broker,
                status="connected",
                account_ref=broker + ":" + uid,
            )
    return tmp_path


def route(actor="admin", **overrides):
    values = {
        "label": "Sydney",
        "endpoint": "http://user:secret@ap-southeast-static-01.quotaguard.com:9293",
        "expected_ips": "13.238.166.208,52.62.19.138",
        "per_second": "5",
        "per_minute": "100",
        "daily_quota": "1000",
        "monthly_quota": "10000",
        "exit_reserve": "20",
    }
    values.update(overrides)
    return cp.save_route(actor, values)


def ready_route(uid="alice", broker="angel", **overrides):
    rid = route(**overrides)
    cid = uid + ":" + broker
    with cp.transaction() as c:
        c.execute(
            "UPDATE proxy_routes SET observed_ip=?,verified_at=1 WHERE route_id=?",
            ("13.238.166.208", rid),
        )
    cp.assign_route("admin", uid, cid, rid, 1, True)
    return rid, cid


def arm(uid, cid):
    svc.set_config(uid, cid, "mode", "LIVE_ARMED")
    svc.set_config(uid, cid, "armed", "1")


def test_missing_assignment_does_not_fall_back_to_env(env, monkeypatch):
    monkeypatch.setenv("LIVE_ORDER_PROXY_URL", "http://legacy:secret@host:9293")
    assert not cp.route_status("alice", "alice:angel")["ready"]
    arm("alice", "alice:angel")
    with pytest.raises(cp.ControlError, match="No order proxy"):
        cp.reserve("alice", "alice:angel", "entry", "one")


def test_secret_encrypted_and_scope_enforced(env):
    rid, cid = ready_route()
    with contextlib.closing(live_db.get_live_conn()) as c:
        assert (
            b"secret"
            not in c.execute("SELECT endpoint_enc FROM proxy_routes").fetchone()[0]
        )
    assert cp.route_status("alice", cid)["ready"]
    assert not cp.route_status("bob", cid)["ready"]
    with pytest.raises(cp.ControlError):
        cp.assign_route("admin", "bob", cid, rid, 1, True)


def test_rotation_requires_idle_and_invalidates_confirmation(env):
    rid, cid = ready_route()
    arm("alice", cid)
    with pytest.raises(cp.ControlError, match="Disarm"):
        route(route_id=rid, revision=1)
    svc.set_config("alice", cid, "armed", "0")
    svc.set_config("alice", cid, "mode", "DISARMED")
    route(route_id=rid, revision=1)
    assert not cp.route_status("alice", cid)["ready"]
    with pytest.raises(cp.ControlError):
        cp.assign_route("admin", "alice", cid, rid, 1, True)


def test_quota_is_atomic_across_connections_and_preserves_exit_reserve(env):
    rid, cid = ready_route(daily_quota="3", exit_reserve="1")
    cp.assign_route("admin", "bob", "bob:angel", rid, 1, True)
    for uid in ("alice", "bob"):
        arm(uid, uid + ":angel")

    def consume(uid):
        req, _ = cp.reserve(uid, uid + ":angel", "entry", "entry-" + uid)
        cp.finish_request(req, "acknowledged")

    with ThreadPoolExecutor(max_workers=2) as pool:
        list(pool.map(consume, ("alice", "bob")))
    with pytest.raises(cp.ControlError, match="budget"):
        cp.reserve("alice", cid, "entry", "third")
    req, _ = cp.reserve("alice", cid, "exit", "exit")
    cp.finish_request(req, "acknowledged")
    with pytest.raises(cp.ControlError, match="budget"):
        cp.reserve("alice", cid, "exit", "fourth")


def test_ambiguous_request_blocks_new_intents(env):
    _, cid = ready_route()
    arm("alice", cid)
    rid, _ = cp.reserve("alice", cid, "entry", "one")
    cp.finish_request(rid, "uncertain")
    with pytest.raises(cp.ControlError, match="uncertain"):
        cp.reserve("alice", cid, "exit", "new-key")


def test_switch_rejects_active_connection(env):
    cp.activate_broker("alice", "angel")
    arm("alice", "alice:angel")
    with pytest.raises(cp.ControlError, match="Disarm"):
        cp.activate_broker("alice", "zerodha")
    assert svc.get_selected_broker("alice") == "angel"


def test_readiness_enrolment_does_not_enrol_disarmed_strategy(env):
    assert ("alice", "alice:angel") in svc.readiness_connections()
    assert ("alice", "alice:angel") not in svc.runner_connections()


@pytest.mark.parametrize(
    "endpoint",
    [
        "http://u:p@127.0.0.1:80",
        "http://u:p@evil.test:80",
        "http://u:p@quotaguard.com.evil.test:80",
        "socks5://u:p@a.quotaguard.com:1080",
    ],
)
def test_unapproved_proxy_endpoints_rejected(env, endpoint):
    with pytest.raises(cp.ControlError):
        route(endpoint=endpoint)


def test_transport_isolated_and_never_retries(env, monkeypatch):
    _, cid = ready_route(broker="zerodha")
    arm("alice", cid)
    adapter = SimpleNamespace(
        user_id="alice",
        conn_id=cid,
        broker_name="zerodha",
        _creds={"api_key": "key"},
        _kite=SimpleNamespace(access_token="token", proxies=None),
    )
    svc.store_credentials("alice", cid, "zerodha", {"api_key": "key"})
    before = dict(os.environ)
    calls = []

    class Session:
        def __enter__(self):
            return self

        def __exit__(self, *args):
            pass

        def request(self, *args, **kwargs):
            calls.append((args, kwargs))
            assert self.trust_env is False
            assert self.proxies["https"].startswith("http://user:secret@")
            assert kwargs["allow_redirects"] is False
            raise RuntimeError("secret proxy URL must not escape")

    monkeypatch.setattr(transport.requests, "Session", Session)
    with pytest.raises(transport.OrderTransportError) as err:
        transport.send_order(adapter, "entry", {"quantity": 65}, "test")
    assert "secret" not in str(err.value)
    assert len(calls) == 1
    assert os.environ == before
    assert adapter._kite.proxies is None
    with pytest.raises(cp.ControlError):
        transport.send_order(adapter, "entry", {"quantity": 65}, "different")
    assert len(calls) == 1


@pytest.fixture
def client(env):
    from labs.ui.live_routes import live_bp
    from live.auth_gate import register_auth_gate

    app = Flask(__name__, template_folder="../templates")
    app.secret_key = "test-only"
    app.testing = True
    app.register_blueprint(live_bp)
    register_auth_gate(app)
    client = app.test_client()
    with client.session_transaction() as s:
        s["user_id"] = "alice"
        s["live_csrf"] = "csrf"
    return client


def test_admin_auth_csrf_and_secret_redaction(client):
    assert client.get("/live/admin").status_code == 403
    with client.session_transaction() as s:
        s["user_id"] = "admin"
    assert client.post("/live/admin", data={"action": "route"}).status_code == 400
    route()
    response = client.get("/live/admin")
    assert response.status_code == 200
    assert b"http://user:secret" not in response.data
    assert b"Sydney" in response.data


def test_connect_and_failed_login_keep_active_broker(client, monkeypatch):
    from labs.ui import live_routes as routes

    cp.activate_broker("alice", "angel")
    response = client.post(
        "/live/connect", data={"csrf_token": "csrf", "broker": "zerodha"}
    )
    assert response.status_code == 302
    assert svc.get_selected_broker("alice") == "angel"
    svc.store_credentials(
        "alice",
        "alice:zerodha",
        "zerodha",
        {"api_key": "key", "api_secret": "secret", "access_token": "OLD"},
    )
    monkeypatch.setattr(
        routes,
        "exchange_request_token",
        lambda **kwargs: (_ for _ in ()).throw(ValueError("failed")),
    )
    client.get("/live/zerodha/login")
    with client.session_transaction() as s:
        state = s["kite_login_state"]
    response = client.get("/live/zerodha/callback?request_token=bad&state=" + state)
    assert response.status_code == 302
    assert svc.get_selected_broker("alice") == "angel"
    assert "Kite+login+failed" in response.location


def test_db_selection_overrides_stale_browser_session(client):
    from labs.ui.live_routes import _current_conn_id

    cp.activate_broker("alice", "angel")
    with client.application.test_request_context():
        from flask import session

        session["user_id"] = "alice"
        session["live_broker"] = "zerodha"
        assert _current_conn_id() == ("alice", "angel", "alice:angel")


def test_risk_settings_validation_and_audit(env):
    with pytest.raises(cp.ControlError):
        cp.update_settings("bob", "alice", "alice:angel", 1, 3000)
    with pytest.raises(cp.ControlError):
        cp.update_settings("admin", "alice", "alice:angel", 1, float("nan"))
    cp.update_settings("admin", "alice", "alice:angel", 1, 2500)
    assert svc.get_daily_loss_cap("alice", "alice:angel") == 2500
    with contextlib.closing(live_db.get_live_conn()) as c:
        assert (
            c.execute(
                "SELECT count(*) FROM live_admin_audit WHERE action='risk_settings'"
            ).fetchone()[0]
            == 1
        )


def test_credential_change_revokes_route_and_requires_idle(env):
    _, cid = ready_route()
    original = {"api_key": "old", "client_code": "alice"}
    svc.store_credentials("alice", cid, "angel", original)
    arm("alice", cid)
    with pytest.raises(cp.ControlError, match="Disarm"):
        cp.save_connection_credentials(
            "alice", cid, "angel", dict(original, api_key="new"), original
        )
    assert svc.load_credentials("alice", cid) == original
    ex.disarm("alice", cid)
    cp.save_connection_credentials(
        "alice", cid, "angel", dict(original, api_key="new"), original
    )
    assert not cp.route_status("alice", cid)["ready"]


@pytest.mark.parametrize(
    "broker,operation",
    [("angel", "entry"), ("angel", "exit"), ("zerodha", "entry"), ("zerodha", "exit")],
)
def test_transport_contract_and_success(env, monkeypatch, broker, operation):
    _, cid = ready_route(broker=broker)
    arm("alice", cid)
    creds = {"api_key": "key"}
    svc.store_credentials("alice", cid, broker, creds)
    adapter = SimpleNamespace(
        user_id="alice",
        conn_id=cid,
        broker_name=broker,
        _creds=creds,
        _kite=SimpleNamespace(access_token="kite-token"),
        _smart=SimpleNamespace(
            access_token="angel-token", requestHeaders=lambda: {"X-PrivateKey": "key"}
        ),
    )
    calls = []

    class Session:
        def __enter__(self):
            return self

        def __exit__(self, *args):
            pass

        def request(self, method, url, **kwargs):
            calls.append((method, url, kwargs))
            body = (
                {"status": "success", "data": {"order_id": "123"}}
                if broker == "zerodha"
                else {"status": True, "data": {"orderid": "123"}}
            )
            return SimpleNamespace(status_code=200, json=lambda: body)

    monkeypatch.setattr(transport.requests, "Session", Session)
    result = transport.send_order(adapter, operation, {"quantity": 65}, "key")
    assert result
    assert len(calls) == 1
    assert calls[0][0] == "POST"
    assert calls[0][1].startswith(
        "https://api.kite.trade/"
        if broker == "zerodha"
        else "https://apiconnect.angelone.in/"
    )
    assert calls[0][2]["headers"]["Authorization"].startswith(
        "token " if broker == "zerodha" else "Bearer "
    )
    assert ex.gate_transport_clear("alice", cid).passed
    with pytest.raises(cp.ControlError, match="already acknowledged"):
        transport.send_order(adapter, operation, {"quantity": 65}, "key")
    assert len(calls) == 1


def test_probe_uses_no_broker_credentials_and_is_counted(env, monkeypatch):
    rid = route()

    class Session:
        def __enter__(self):
            return self

        def __exit__(self, *args):
            pass

        def get(self, url, **kwargs):
            assert url == "https://api.ipify.org?format=json"
            assert "headers" not in kwargs
            assert self.trust_env is False
            return SimpleNamespace(
                raise_for_status=lambda: None, json=lambda: {"ip": "13.238.166.208"}
            )

    monkeypatch.setattr(transport.requests, "Session", Session)
    transport.verify_route("admin", rid, 1)
    cp.assign_route("admin", "alice", "alice:angel", rid, 1, True)
    assert cp.route_status("alice", "alice:angel")["usage"]["day"] == 1


def test_oauth_callback_rejects_missing_state(client, monkeypatch):
    from labs.ui import live_routes as routes

    cp.activate_broker("alice", "angel")
    svc.store_credentials(
        "alice", "alice:zerodha", "zerodha", {"api_key": "key", "api_secret": "secret"}
    )
    monkeypatch.setattr(
        routes,
        "exchange_request_token",
        lambda **kw: pytest.fail("Must not exchange without state"),
    )
    client.get("/live/zerodha/login")
    response = client.get("/live/zerodha/callback?request_token=stolen")
    assert "Login+expired" in response.location
    assert svc.get_selected_broker("alice") == "angel"


def test_no_sdk_mutations_bypass_transport():
    import ast
    from pathlib import Path

    forbidden = {
        "place_order",
        "placeOrder",
        "modify_order",
        "modifyOrder",
        "cancel_order",
        "cancelOrder",
    }
    folder = Path(__file__).resolve().parents[1] / "live" / "brokers"
    for path in folder.glob("*.py"):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            assert not (
                isinstance(node, ast.Call)
                and isinstance(node.func, ast.Attribute)
                and node.func.attr in forbidden
            ), str(path)


@pytest.mark.parametrize(
    "broker,operation",
    [
        ("angel", "modify"),
        ("angel", "cancel"),
        ("zerodha", "modify"),
        ("zerodha", "cancel"),
    ],
)
def test_mutations_check_order_ownership(env, monkeypatch, broker, operation):
    _, cid = ready_route(broker=broker)
    arm("alice", cid)
    creds = {"api_key": "key"}
    svc.store_credentials("alice", cid, broker, creds)
    adapter = SimpleNamespace(
        user_id="alice",
        conn_id=cid,
        broker_name=broker,
        _creds=creds,
        _kite=SimpleNamespace(access_token="token"),
        _smart=SimpleNamespace(access_token="token", requestHeaders=lambda: {}),
    )
    field = "order_id" if broker == "zerodha" else "orderid"
    params = {field: "123", "variety": "regular" if broker == "zerodha" else "NORMAL"}
    with pytest.raises(cp.ControlError, match="belong"):
        transport.send_order(adapter, operation, params, "mutation")
    with cp.transaction() as c:
        c.execute(
            "INSERT INTO live_orders(idem_key,user_id,conn_id,broker_order_id,dry_run) VALUES(?,?,?,?,0)",
            ("original", "alice", cid, "123"),
        )
    calls = []

    class Session:
        def __enter__(self):
            return self

        def __exit__(self, *args):
            pass

        def request(self, method, url, **kwargs):
            calls.append((method, url))
            return SimpleNamespace(
                status_code=200,
                json=lambda: (
                    {"status": "success", "data": {"order_id": "123"}}
                    if broker == "zerodha"
                    else {"status": True, "data": {"orderid": "123"}}
                ),
            )

    monkeypatch.setattr(transport.requests, "Session", Session)
    transport.send_order(adapter, operation, params, "mutation")
    assert len(calls) == 1
    if broker == "zerodha":
        assert calls[0] == (
            "PUT" if operation == "modify" else "DELETE",
            "https://api.kite.trade/orders/regular/123",
        )
    else:
        assert calls[0][0] == "POST"
        assert calls[0][1].endswith("/" + operation + "Order")


def test_explicit_proxy_import_is_encrypted(env, monkeypatch):
    endpoint = "http://import:secret@ap-southeast-static-01.quotaguard.com:9293"
    monkeypatch.setenv("LIVE_ORDER_PROXY_URL", endpoint)
    rid = route(endpoint="", import_existing="1")
    with contextlib.closing(live_db.get_live_conn()) as c:
        blob = c.execute(
            "SELECT endpoint_enc FROM proxy_routes WHERE route_id=?", (rid,)
        ).fetchone()[0]
        assert svc._fernet().decrypt(blob).decode() == endpoint
        assert "secret" not in str(
            c.execute("SELECT detail FROM live_admin_audit").fetchall()
        )
