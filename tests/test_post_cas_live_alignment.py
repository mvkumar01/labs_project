from datetime import datetime, timedelta, timezone
from pathlib import Path
import sqlite3
import sys


ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from live import live_executor as ex
from live import live_runner as runner
from live import live_service as svc
from live.brokers.base import Position
import storage.live_db as live_db
from storage.live_db import init_live_db


USER_ID = "post-cas-user"
CONN_ID = f"{USER_ID}:angel"


def _memory_conn():
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    init_live_db(conn)
    svc.upsert_connection(
        USER_ID,
        CONN_ID,
        broker="angel",
        account_label="post-cas",
        account_ref="angel:post-cas",
        status="connected",
        conn=conn,
    )
    return conn


def test_runner_decision_gate_requires_matching_fresh_heartbeat():
    conn = _memory_conn()
    try:
        missing = ex.gate_runner_decision_abi(USER_ID, CONN_ID, conn)
        assert missing.passed is False

        svc.set_config(USER_ID, CONN_ID, "runner_decision_abi", "pre-cas", conn)
        svc.set_config(
            USER_ID,
            CONN_ID,
            "runner_owner",
            f"old@{datetime.now(timezone.utc).isoformat()}",
            conn,
        )
        svc.set_config(
            USER_ID, CONN_ID, "runner_decision_owner", "old", conn
        )
        mismatch = ex.gate_runner_decision_abi(USER_ID, CONN_ID, conn)
        assert mismatch.passed is False
        assert "loaded=pre-cas" in mismatch.detail

        stale_at = datetime.now(timezone.utc) - timedelta(minutes=2)
        svc.set_config(
            USER_ID,
            CONN_ID,
            "runner_decision_abi",
            ex.LIVE_DECISION_ABI,
            conn,
        )
        svc.set_config(
            USER_ID,
            CONN_ID,
            "runner_owner",
            f"old@{stale_at.isoformat()}",
            conn,
        )
        stale = ex.gate_runner_decision_abi(USER_ID, CONN_ID, conn)
        assert stale.passed is False

        svc.set_config(
            USER_ID, CONN_ID, "runner_decision_owner", "different", conn
        )
        svc.set_config(
            USER_ID,
            CONN_ID,
            "runner_owner",
            f"current@{datetime.now(timezone.utc).isoformat()}",
            conn,
        )
        wrong_owner = ex.gate_runner_decision_abi(USER_ID, CONN_ID, conn)
        assert wrong_owner.passed is False

        svc.set_config(
            USER_ID, CONN_ID, "runner_decision_owner", "current", conn
        )
        current = ex.gate_runner_decision_abi(USER_ID, CONN_ID, conn)
        assert current.passed is True
    finally:
        conn.close()


def test_v212_policy_is_canonical_completed_minute_only():
    policy = runner.champion_live_policy("v2.12")
    assert policy.fast_stop_overlay is False
    assert policy.next_open_fallback is False


def test_runner_keeps_disarmed_real_open_state_visible_until_reconciled():
    conn = _memory_conn()
    try:
        state = svc.get_trade_state(USER_ID, CONN_ID, conn=conn)
        state.update({"position": "OPEN", "qty": 65, "virtual": 0})
        svc.save_trade_state(USER_ID, CONN_ID, state, conn=conn)
        assert svc.runner_connections(conn) == [(USER_ID, CONN_ID)]

        svc.reset_trade_state(USER_ID, CONN_ID, conn=conn)
        assert svc.runner_connections(conn) == []
    finally:
        conn.close()


def test_disarmed_runner_reconciles_manual_flat_without_order(monkeypatch, tmp_path):
    monkeypatch.setattr(live_db, "LIVE_DB_PATH", tmp_path / "live.db")
    init_live_db()

    conn = live_db.get_live_conn()
    svc.upsert_connection(
        USER_ID,
        CONN_ID,
        broker="angel",
        account_label="post-cas",
        account_ref="angel:post-cas",
        status="connected",
        conn=conn,
    )
    state = svc.get_trade_state(USER_ID, CONN_ID, conn=conn)
    state.update(
        {
            "position": "OPEN",
            "side": "CALL",
            "symbol": "NIFTY2680625000CE",
            "entry_price": 250.0,
            "entry_spot": 24700.0,
            "entry_time": "2026-08-04T06:30:00+00:00",
            "qty": 65,
            "virtual": 0,
        }
    )
    svc.save_trade_state(USER_ID, CONN_ID, state, conn=conn)
    conn.close()

    class FlatAdapter:
        @staticmethod
        def get_position():
            return Position(symbol=None, qty=0, side=None)

    monkeypatch.setattr(
        runner,
        "_ensure_connected_adapter",
        lambda *_args, **_kwargs: FlatAdapter(),
    )
    monkeypatch.setattr(
        runner,
        "_route_order",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            AssertionError("disarmed reconciliation must never route an order")
        ),
    )

    runner.process_connection(
        USER_ID,
        CONN_ID,
        adapters={},
        reconciled=set(),
        task_id="post-cas-test-runner",
        signal_engines={},
        alpha_seen={},
    )

    conn = live_db.get_live_conn()
    try:
        state = svc.get_trade_state(USER_ID, CONN_ID, conn=conn)
        assert state["position"] == "NONE"
        assert svc.get_config(USER_ID, CONN_ID, "reconcile_blocked", conn) == "0"
        assert (
            svc.get_config(USER_ID, CONN_ID, "runner_decision_abi", conn)
            == ex.LIVE_DECISION_ABI
        )
        assert (
            svc.get_config(USER_ID, CONN_ID, "runner_decision_owner", conn)
            == "post-cas-test-runner"
        )
    finally:
        conn.close()


def test_live_mode_clears_virtual_dry_state_before_reconcile(monkeypatch, tmp_path):
    """A DRY-to-LIVE switch must not compare simulated state with Angel."""
    monkeypatch.setattr(live_db, "LIVE_DB_PATH", tmp_path / "live.db")
    init_live_db()

    conn = live_db.get_live_conn()
    svc.upsert_connection(
        USER_ID,
        CONN_ID,
        broker="angel",
        account_label="post-cas",
        account_ref="angel:post-cas",
        status="connected",
        conn=conn,
    )
    for key, value in (
        ("mode", "LIVE_ARMED"),
        ("armed", "1"),
        ("kill_switch", "0"),
        ("lots", "1"),
    ):
        svc.set_config(USER_ID, CONN_ID, key, value, conn)
    state = svc.get_trade_state(USER_ID, CONN_ID, conn=conn)
    state.update(
        {
            "position": "OPEN",
            "side": "CALL",
            "symbol": "NIFTY2681124400CE",
            "entry_price": 218.3,
            "entry_spot": 24586.6,
            "entry_time": "2026-08-10T03:57:41+00:00",
            "qty": 65,
            "virtual": 1,
        }
    )
    svc.save_trade_state(USER_ID, CONN_ID, state, conn=conn)
    conn.close()

    class FlatAdapter:
        def __init__(self, **_kwargs):
            pass

        def connect(self):
            return None

        def is_connected(self):
            return True

        def account_ref(self):
            return "angel:post-cas"

        @staticmethod
        def get_position():
            return Position(symbol=None, qty=0, side=None)

    monkeypatch.setattr(runner, "market_session_available", lambda _now: True)
    monkeypatch.setattr(runner, "get_latest_alpha", lambda: None)

    reconciled = set()
    runner.process_connection(
        USER_ID,
        CONN_ID,
        adapters={},
        reconciled=reconciled,
        task_id="post-cas-test-runner",
        signal_engines={},
        alpha_seen={},
        adapter_factory=FlatAdapter,
    )

    conn = live_db.get_live_conn()
    try:
        state = svc.get_trade_state(USER_ID, CONN_ID, conn=conn)
        assert state["position"] == "NONE"
        assert svc.get_config(USER_ID, CONN_ID, "reconcile_blocked", conn) == "0"
        assert svc.get_config(USER_ID, CONN_ID, "reconcile_message", conn) == ""
        assert (CONN_ID, "LIVE_ARMED") in reconciled
    finally:
        conn.close()
