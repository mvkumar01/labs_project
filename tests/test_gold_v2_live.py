"""Gold live integration with isolated SQLite and fake broker responses only."""
import sqlite3
from datetime import datetime, timedelta
from types import SimpleNamespace

import pytest

from live import gold_v2_runner as g
from live import gold_v2_contract as gc
from live import live_executor as ex, live_service as svc, live_runner as lr
from live.brokers.base import Position, OrderResult
from live.brokers.gold_zerodha import GoldZerodhaAdapter
from storage.live_db import init_live_db

NOW = datetime(2026, 10, 29, 10, 15, 2)
SYMBOL = "GOLD26DECFUT"
USER, CID = "gold-user", "gold-user:zerodha"


@pytest.fixture
def conn():
    c = sqlite3.connect(":memory:")
    c.row_factory = sqlite3.Row
    init_live_db(c)
    g.ensure_schema(c)
    svc.upsert_connection(USER, CID, broker="zerodha", account_label="test", account_ref="test",
                          status="connected", conn=c)
    svc.set_config(USER, CID, "strategy_version", gc.VERSION, c)
    svc.set_config(USER, CID, "decision_engine", gc.ENGINE, c)
    yield c
    c.close()


def enter(c, qty=1, dry=True):
    s = g.load(c, USER, CID)
    g.queue(c, USER, CID, s, action="ENTER", symbol=SYMBOL, qty=qty, price=140000,
            dry=dry, now=NOW, margin=300000 * qty)
    g.apply_fill(c, USER, CID, s, s["pending"], qty, 140000, NOW)
    return s


def test_margin_goal_and_short_pnl_and_atomic_restart(conn):
    s = enter(conn)
    assert s["days"][g.day_key(NOW, True)]["target"] == 9000
    assert svc.get_trade_state(USER, CID, conn)["qty"] == 100
    g.queue(conn, USER, CID, s, action="EXIT", symbol=SYMBOL, qty=1, price=139800,
            dry=True, now=NOW, reason="daily_target")
    g.apply_fill(conn, USER, CID, s, s["pending"], 1, 139800, NOW)
    fresh = g.load(conn, USER, CID)
    assert fresh["position"] is None and fresh["pending"] is None
    trade = dict(conn.execute("SELECT * FROM live_trades").fetchone())
    assert trade["gross_pnl"] == 20000 and 0 < trade["net_pnl"] < 20000
    assert trade["side"] == "SHORT" and trade["strategy"] == gc.VERSION
    assert svc.get_day_pnl(USER, CID, conn=conn)["realized_pnl"] == 0
    assert svc.get_day_pnl(USER, CID, conn=conn)["realized_pnl_dry"] == trade["net_pnl"]
    assert fresh["days"][g.day_key(NOW, True)]["hit"]


def test_partial_ioc_entry_and_exit_use_filled_qty(conn):
    s = g.load(conn, USER, CID)
    g.queue(conn, USER, CID, s, action="ENTER", symbol=SYMBOL, qty=4, price=140000,
            dry=False, now=NOW, margin=1200000)
    g.apply_fill(conn, USER, CID, s, s["pending"], 2, 140000, NOW)
    assert s["position"]["qty"] == 2
    assert s["days"][g.day_key(NOW, False)]["capital"] == 600000
    g.queue(conn, USER, CID, s, action="EXIT", symbol=SYMBOL, qty=2, price=140350,
            dry=False, now=NOW, reason="stop")
    g.apply_fill(conn, USER, CID, s, s["pending"], 1, 140350, NOW)
    assert g.load(conn, USER, CID)["position"]["qty"] == 1
    assert conn.execute("SELECT gross_pnl FROM live_trades").fetchone()[0] == -35000


def test_user_and_mode_buckets_are_separate(conn):
    enter(conn)
    assert g.load(conn, "other", CID)["position"] is None
    assert g.day_key(NOW, True) != g.day_key(NOW, False)


def test_exit_rules_margin_target_entry_stop_and_eod(conn):
    s = enter(conn)
    p, d = s["position"], s["days"][g.day_key(NOW, True)]
    assert g.reason_for(p, d, 140351, NOW) == "stop"
    assert g.reason_for(p, d, 139800, NOW) == "daily_target"
    p.update(stop=140000, moved=True)
    assert g.reason_for(p, d, 140000, NOW) == "stop at entry"
    assert g.reason_for(p, d, 140000, NOW.replace(hour=23, minute=29)) == "eod"


def test_dry_intent_never_calls_broker_and_restarts_once(conn):
    s = g.load(conn, USER, CID)
    g.queue(conn, USER, CID, s, action="ENTER", symbol=SYMBOL, qty=1, price=140000,
            dry=True, now=NOW, margin=300000)
    class NoOrders:
        def __getattr__(self, name):
            raise AssertionError("Dry run touched broker: " + name)
    g.settle(conn, USER, CID, g.load(conn, USER, CID), NoOrders(), NOW)
    fresh = g.load(conn, USER, CID)
    assert fresh["position"]["qty"] == 1 and fresh["pending"] is None
    assert conn.execute("SELECT count(*) FROM live_orders").fetchone()[0] == 1


def test_uncertain_entry_never_resends(conn, monkeypatch):
    s = g.load(conn, USER, CID)
    g.queue(conn, USER, CID, s, action="ENTER", symbol=SYMBOL, qty=1, price=140000,
            dry=False, now=NOW, margin=300000)
    key = s["pending"]["key"]
    svc.insert_order_ledger(key, user_id=USER, conn_id=CID, trade_date="2026-10-29",
        strategy_version=gc.VERSION, bar_timestamp="10:15", action="ENTER", side="SHORT",
        entry_rule=gc.VERSION, intent_seq=1, symbol=SYMBOL, qty=1, order_type="LIMIT", limit_price=140000, dry_run=0, conn=conn)
    svc.update_order_ledger(key, status="FAILED", conn=conn)
    monkeypatch.setattr(ex, "place_idempotent", lambda *a, **k: pytest.fail("resent uncertain order"))
    for _ in range(2):
        g.settle(conn, USER, CID, s, object(), NOW)
    assert g.load(conn, USER, CID)["pending"]["key"] == key


def test_terminal_order_fill_is_not_assumed_from_ack(conn):
    s = g.load(conn, USER, CID)
    g.queue(conn, USER, CID, s, action="ENTER", symbol=SYMBOL, qty=2, price=140000,
            dry=False, now=NOW, margin=600000)
    key = s["pending"]["key"]
    svc.insert_order_ledger(key, user_id=USER, conn_id=CID, trade_date="2026-10-29",
        strategy_version=gc.VERSION, bar_timestamp="10:15", action="ENTER", side="SHORT",
        entry_rule=gc.VERSION, intent_seq=1, symbol=SYMBOL, qty=2, order_type="LIMIT", limit_price=140000, dry_run=0, conn=conn)
    svc.update_order_ledger(key, status="PLACED", broker_order_id="oid", conn=conn)
    a = SimpleNamespace(get_order_status=lambda oid: dict(status="OPEN", filled_quantity=0))
    g.settle(conn, USER, CID, s, a, NOW)
    assert s["pending"] and not s["position"]
    a.get_order_status = lambda oid: dict(status="CANCELLED", filled_quantity=1, average_price=140001)
    g.settle(conn, USER, CID, s, a, NOW)
    assert s["position"]["qty"] == 1 and s["pending"] is None


def test_short_mtm_has_correct_sign_and_costs(conn):
    s = enter(conn)
    s["position"].update(last_price=139800, last_time=g.stamp(NOW))
    g.save(conn, USER, CID, s)
    mtm = svc.open_position_mtm(USER, CID, conn)
    assert mtm["gross_pnl"] == 20000 and mtm["net_pnl"] > 0
    assert mtm["qty"] == 100 and mtm["side"] == "SHORT"


def test_selected_strategy_uses_own_abi_and_dispatch(monkeypatch):
    assert ex.expected_decision_abi(gc.VERSION) == gc.ABI
    assert ex.expected_decision_abi("v2.14") == ex.LIVE_DECISION_ABI
    seen = []
    monkeypatch.setattr(lr, "publish_runner_heartbeat", lambda *a, **k: True)
    monkeypatch.setattr(svc, "get_config", lambda *a, **k: gc.VERSION)
    monkeypatch.setattr(g, "process", lambda *a, **k: seen.append(a))
    lr.process_connection(USER, CID, adapters={}, reconciled=set(), task_id="test",
                          signal_engines={}, alpha_seen={})
    assert seen == [(USER, CID)]


def test_arming_real_while_gold_virtual_open_rejected(conn):
    enter(conn)
    with pytest.raises(ex.InvalidTransition, match="dry-run position"):
        ex.arm_live(USER, CID, conn)


def test_entry_deadline_is_rechecked_after_slow_gates():
    a = SimpleNamespace(limit=lambda symbol: 140000)
    intent = dict(symbol=SYMBOL, created=g.stamp(NOW))
    assert g.entry_limit(a, intent, NOW + timedelta(seconds=5)) == 140000
    assert g.entry_limit(a, intent, NOW + timedelta(seconds=20)) is None


def test_expired_unsent_intent_after_restart_is_skipped(conn, monkeypatch):
    s = g.load(conn, USER, CID)
    g.queue(conn, USER, CID, s, action="ENTER", symbol=SYMBOL, qty=1, price=140000,
            dry=False, now=NOW, margin=300000)
    monkeypatch.setattr(ex, "place_idempotent", lambda *a, **k: pytest.fail("sent stale signal"))
    g.settle(conn, USER, CID, s, object(), NOW + timedelta(minutes=1))
    assert s["pending"] is None and s["position"] is None


@pytest.mark.parametrize("symbol", ["GOLDM26DECFUT", "NIFTY26DECFUT", "GOLD26DEC140000CE"])
def test_adapter_rejects_wrong_instruments(symbol):
    assert not GoldZerodhaAdapter.valid_symbol(symbol)


def test_adapter_mcx_short_buyback_and_position_guard(monkeypatch):
    from live.brokers import gold_zerodha as mod
    a = GoldZerodhaAdapter(user_id=USER, conn_id=CID, creds={})
    a._kite = SimpleNamespace(positions=lambda: {"net": []})
    sent = []
    monkeypatch.setattr(mod.z, "_live_orders_enabled", lambda: True)
    monkeypatch.setattr(mod, "send_order", lambda adapter, op, params, key: sent.append((op, params)) or "oid")
    a.place_order(side="SHORT", symbol=SYMBOL, qty=1, price=140000, idempotency_key="entry")
    assert sent[0][1]["exchange"] == "MCX" and sent[0][1]["transaction_type"] == "SELL"
    assert sent[0][1]["product"] == "NRML" and sent[0][1]["validity"] == "IOC"
    assert a.exit_all(symbol=SYMBOL, qty=1, reason="stop", idempotency_key="bad", price=140350).status == "NO_SHORT_POSITION"
    a._kite.positions = lambda: {"net": [dict(exchange="MCX", product="NRML", tradingsymbol=SYMBOL, quantity=-1)]}
    a.exit_all(symbol=SYMBOL, qty=1, reason="stop", idempotency_key="exit", price=140350)
    assert sent[-1][1]["transaction_type"] == "BUY"
    assert a.exit_all(symbol=SYMBOL, qty=2, reason="stop", idempotency_key="over", price=140350).status == "NO_SHORT_POSITION"


def test_minute_decisions_use_completed_candles_and_both_filters():
    import pandas as pd
    from labs.engine import gold_cci_tracker as paper
    frame = pd.read_parquet(paper.SEED_FILE)
    replay = g.engine.replay(frame, "2026-03-10", end="2026-09-25", margin_rate=.092)
    for outcome in ("taken", "bullish_filter"):
        event = next(s for s in reversed(replay["signals"]) if s["outcome"] == outcome)
        cutoff = event["signal_ts"] + pd.Timedelta(minutes=1)
        prefix = frame[frame.ts < cutoff].copy()
        fire = g.pending_fire(prefix, cutoff)
        assert bool(fire) == (outcome == "taken")
        # The current minute is incomplete; changing it cannot change the decision.
        incomplete = pd.DataFrame([dict(ts=cutoff, open=999999, high=999999, low=1, close=1, volume=1)])
        assert g.pending_fire(pd.concat([prefix, incomplete]), cutoff) == fire


def test_process_end_to_end_dry_entry_target_halt_restart(tmp_path, monkeypatch):
    import pandas as pd
    from storage import live_db
    monkeypatch.setattr(live_db, "LIVE_DB_PATH", tmp_path / "live.db")
    svc.ensure_schema()
    svc.upsert_connection(USER, CID, broker="zerodha", status="connected", account_ref="test")
    for key, value in (("mode", "DRY_RUN"), ("strategy_version", gc.VERSION), ("lots", "1")):
        svc.set_config(USER, CID, key, value)
    monkeypatch.setattr(svc, "load_credentials", lambda *a: {})
    price = [140000.0]
    a = SimpleNamespace(_creds={}, contract=lambda d: dict(tradingsymbol=SYMBOL),
        margin=lambda s, n: 300000 * n, limit=lambda s, buy=False: price[0], get_ltp=lambda s: price[0])
    series = SimpleNamespace(refresh=lambda now: None, front=lambda now: dict(tradingsymbol=SYMBOL),
        frame=lambda now: pd.DataFrame([dict(ts=pd.Timestamp(now).floor("min") - pd.Timedelta(minutes=1), close=price[0])]))
    monkeypatch.setattr(g, "pending_fire", lambda *a: "signal")
    g._runtime.pop(CID, None)
    adapters = {"gold:" + CID: a}
    g.process(USER, CID, adapters=adapters, now=NOW, series=series)
    with live_db.get_live_conn() as c:
        s = g.load(c, USER, CID)
        assert s["position"]["qty"] == 1
    price[0] = 139800
    g.process(USER, CID, adapters=adapters, now=NOW + timedelta(seconds=2), series=series)
    # New minute with another signal: a filled daily target keeps the book flat.
    g.process(USER, CID, adapters=adapters, now=NOW + timedelta(minutes=1), series=series)
    with live_db.get_live_conn() as c:
        assert g.load(c, USER, CID)["position"] is None
        assert c.execute("SELECT count(*) FROM live_trades").fetchone()[0] == 1
        assert c.execute("SELECT count(*) FROM live_orders").fetchone()[0] == 2


def test_configure_selects_gold_without_arming_and_blocks_angel(tmp_path, monkeypatch):
    from pathlib import Path
    from flask import Flask
    from storage import live_db
    from labs.ui import live_routes as routes
    monkeypatch.setattr(live_db, "LIVE_DB_PATH", tmp_path / "live.db")
    svc.ensure_schema()
    svc.upsert_connection(USER, CID, broker="zerodha", status="connected", account_ref="test")
    app = Flask(__name__, template_folder=str(Path(__file__).resolve().parents[1] / "templates"))
    app.secret_key = "test-only"
    app.register_blueprint(routes.live_bp)
    monkeypatch.setattr(routes, "_current_conn_id", lambda: (USER, "zerodha", CID))
    client = app.test_client()
    with client.session_transaction() as session:
        session["user_id"], session["live_csrf"] = USER, "test"
    page = client.get("/live/configure")
    assert page.status_code == 200 and b'Gold CCI V2' in page.data
    response = client.post("/live/configure", data=dict(csrf_token="test", strategy=gc.VERSION, lots="2", daily_loss_cap="40000"))
    assert response.status_code == 302
    assert svc.get_config(USER, CID, "decision_engine") == gc.ENGINE
    assert svc.get_config(USER, CID, "strategy_version") == gc.VERSION
    assert svc.get_mode(USER, CID) == "DISARMED"
    assert svc.get_lots(USER, CID) == 2
    assert routes.STRATEGY_PRESETS[gc.VERSION] == (gc.ENGINE, gc.VERSION)
    svc.set_config(USER, CID, "mode", "DRY_RUN")
    response = client.post("/live/configure", data=dict(csrf_token="test", strategy=gc.VERSION, lots="3"))
    assert b'Disarm this connection' in response.data and svc.get_lots(USER, CID) == 2
    svc.set_config(USER, CID, "mode", "DISARMED")
    monkeypatch.setattr(routes, "_current_conn_id", lambda: (USER, "angel", CID))
    response = client.post("/live/configure", data=dict(csrf_token="test", strategy=gc.VERSION, lots="3"))
    assert b'requires a Zerodha connection' in response.data and svc.get_lots(USER, CID) == 2
