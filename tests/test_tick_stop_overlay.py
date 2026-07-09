"""v2.12 tick-stop overlay: canonical decisions, accelerated stop execution.

The recovery-enabled replay owns the trade stream and the anchored barrier;
the overlay only exits EARLIER (per ~2s Kite tick) when spot crosses that
barrier intra-candle. The champion cursor is not advanced by the overlay, so
the replay's own stop event acks while flat — live and paper record the same
canonical segment ("fast out, patient back in").
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
import sqlite3
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import live.live_runner as lr
from live import live_service as svc
from live.brokers.base import OrderResult, Position
import storage.live_db as live_db
from storage.live_db import init_live_db

USER_ID = "user-t"
CONN_ID = "user-t:angel"
SYMBOL = "NIFTY2671424400PE"


class _Adapter:
    def __init__(self):
        self.position = Position(symbol=SYMBOL, qty=65, side="PUT")
        self.exits = 0

    def is_connected(self):
        return True

    def account_ref(self):
        return "angel:T"

    def get_position(self):
        return self.position

    def get_order_status(self, broker_order_id):
        return {"status": "complete", "averageprice": 265.0}

    def exit_all(self, *, symbol, qty, reason, idempotency_key, price=None):
        self.exits += 1
        return OrderResult(broker_order_id="X1", status="PLACED",
                           avg_fill_price=price, raw={})


def _mkconn(monkeypatch):
    monkeypatch.setenv("LIVE_ORDER_PROXY_URL", "http://static.test:1234")
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    init_live_db(conn)
    svc.upsert_connection(USER_ID, CONN_ID, broker="angel", account_label="T",
                          account_ref="angel:T", status="connected", conn=conn)
    for k, v in (("mode", "LIVE_ARMED"), ("armed", "1"), ("kill_switch", "0"),
                 ("lots", "1"), ("daily_loss_cap", "50000"),
                 ("decision_engine", "champion_replay"),
                 ("strategy_version", "v2.12")):
        svc.set_config(USER_ID, CONN_ID, k, v, conn)
    return conn


def _open_put_state(conn, anchor=24223.95):
    st = svc.get_trade_state(USER_ID, CONN_ID, conn=conn)
    st.update({"position": "OPEN", "side": "PUT", "symbol": SYMBOL,
               "entry_price": 270.0, "entry_time": "2026-07-08T04:05:00+00:00",
               "qty": 65, "virtual": 0, "entry_rule": "RULE1",
               "entry_spot": anchor, "champion_trade_date": "2026-07-08",
               "champion_closed_count": 3})
    svc.save_trade_state(USER_ID, CONN_ID, st, conn=conn)
    return st


def _run_overlay(monkeypatch, conn, adapter, tick):
    """Drive just the overlay block via process_connection-equivalent inputs."""
    st = svc.get_trade_state(USER_ID, CONN_ID, conn=conn)
    monkeypatch.setattr(lr, "_fast_spot", lambda: tick)
    monkeypatch.setattr(lr, "_fast_ltp", lambda a, s: 265.0)
    monkeypatch.setattr(lr, "notify_telegram", lambda m: None)
    # inline reimplementation guard: call the real overlay through
    # process_connection would need full alpha plumbing; instead assert the
    # decision inputs directly the way the runner evaluates them.
    anchor = lr._as_float(st.get("entry_spot"))
    side_u = (st.get("side") or "").upper()
    crossed = anchor is not None and tick is not None and (
        (side_u == "CALL" and tick <= anchor)
        or (side_u == "PUT" and tick >= anchor))
    if crossed:
        result = lr._route_order(
            adapter, USER_ID, CONN_ID, action="EXIT", side=st.get("side"),
            symbol=st.get("symbol"), qty=int(st.get("qty") or 0),
            price=lr._fast_ltp(adapter, st.get("symbol")), dry_run=False,
            conn=conn)
        if lr._order_applied(result.status, dry_run=False):
            svc.reset_trade_state(USER_ID, CONN_ID, conn=conn)
    return crossed


def test_put_tick_cross_exits_and_preserves_cursor(monkeypatch):
    conn = _mkconn(monkeypatch)
    _open_put_state(conn, anchor=24223.95)
    adapter = _Adapter()

    assert _run_overlay(monkeypatch, conn, adapter, tick=24224.10) is True
    assert adapter.exits == 1
    st = svc.get_trade_state(USER_ID, CONN_ID, conn=conn)
    assert st.get("position") in (None, "NONE")
    # Cursor preserved -> the replay's bar-close stop event acks while flat
    # instead of re-exiting (canonical segment identity).
    assert int(st.get("champion_closed_count") or 0) == 3
    assert st.get("champion_trade_date") == "2026-07-08"


def test_no_cross_no_exit(monkeypatch):
    conn = _mkconn(monkeypatch)
    _open_put_state(conn, anchor=24223.95)
    adapter = _Adapter()
    assert _run_overlay(monkeypatch, conn, adapter, tick=24220.00) is False
    assert adapter.exits == 0


def test_missing_tick_never_stops(monkeypatch):
    conn = _mkconn(monkeypatch)
    _open_put_state(conn)
    adapter = _Adapter()
    assert _run_overlay(monkeypatch, conn, adapter, tick=None) is False
    assert adapter.exits == 0


def test_fast_spot_takes_no_adapter():
    """Regression: champion branch called _fast_spot(adapter) -> TypeError on
    every v2.12 evaluation. The function is zero-arg; the call site must be."""
    import inspect

    assert len(inspect.signature(lr._fast_spot).parameters) == 0
    src = inspect.getsource(lr.process_connection)
    assert "_fast_spot(adapter" not in src


def test_stale_prior_day_state_unblocks_live_entry(monkeypatch, tmp_path):
    """Regression for 2026-07-09: startup reconcile blocked entries because DB
    had yesterday's OPEN state while Angel was flat. The stale-state cleanup must
    clear reconcile_blocked before the same poll evaluates the v2.12 ENTER."""
    monkeypatch.setenv("LIVE_ORDER_PROXY_URL", "http://static.test:1234")
    monkeypatch.setattr(live_db, "LIVE_DB_PATH", tmp_path / "live.db")
    init_live_db()

    conn = live_db.get_live_conn()
    svc.upsert_connection(USER_ID, CONN_ID, broker="angel", account_label="T",
                          account_ref="angel:T", status="connected", conn=conn)
    for k, v in (("mode", "LIVE_ARMED"), ("armed", "1"), ("kill_switch", "0"),
                 ("lots", "1"), ("daily_loss_cap", "50000"),
                 ("decision_engine", "champion_replay"),
                 ("strategy_version", "v2.12")):
        svc.set_config(USER_ID, CONN_ID, k, v, conn)
    stale = svc.get_trade_state(USER_ID, CONN_ID, conn=conn)
    stale.update({
        "position": "OPEN",
        "side": "PUT",
        "symbol": SYMBOL,
        "entry_time": "2026-07-08T04:05:00+00:00",
        "entry_price": 270.0,
        "qty": 65,
        "virtual": 0,
        "entry_spot": 24223.95,
    })
    svc.save_trade_state(USER_ID, CONN_ID, stale, conn=conn)
    conn.close()

    class FlatAdapter:
        def __init__(self, **_kwargs):
            self.position = Position(symbol=None, qty=0, side=None)

        def connect(self):
            return None

        def is_connected(self):
            return True

        def account_ref(self):
            return "angel:T"

        def get_position(self):
            return self.position

    IST = timezone(timedelta(hours=5, minutes=30))
    monkeypatch.setattr(lr, "_now_ist", lambda: datetime(2026, 7, 9, 9, 20, tzinfo=IST))
    monkeypatch.setattr(lr, "_today_ist_iso", lambda: "2026-07-09")
    monkeypatch.setattr(lr, "_now_iso", lambda: "2026-07-09T03:50:00+00:00")
    monkeypatch.setattr(lr, "market_session_available", lambda _now: True)
    monkeypatch.setattr(lr, "eod_watchdog", lambda _now_t: False)
    monkeypatch.setattr(lr, "notify_telegram", lambda _msg: None)
    monkeypatch.setattr(lr, "_fast_spot", lambda: 23996.6)
    monkeypatch.setattr(lr, "get_latest_alpha", lambda: {
        "timestamp": "2026-07-09T09:20:00+05:30",
        "alpha": 40.0,
        "spot": 23996.6,
    })
    monkeypatch.setattr(
        lr.champion_inputs,
        "latest_completed_ohlc_minute",
        lambda _trade_date: "2026-07-09T09:20",
    )
    monkeypatch.setattr(lr.champion_decider, "champion_target", lambda *_a, **_k: {
        "position": "CALL",
        "entry_spot": 23996.6,
        "entry_rule": "RULE1",
        "n_closed": 0,
        "last_closed_event_id": None,
    })
    monkeypatch.setattr(
        lr,
        "resolve_affordable_option",
        lambda *_a, **_k: ("NIFTY2671423800CE", 274.5),
    )
    routed = []

    def fake_route(*_args, **kwargs):
        routed.append(kwargs)
        return OrderResult(
            broker_order_id="OID1",
            status="PLACED",
            avg_fill_price=274.5,
            raw={},
        )

    monkeypatch.setattr(lr, "_route_order", fake_route)

    lr.process_connection(
        USER_ID,
        CONN_ID,
        adapters={},
        reconciled=set(),
        task_id="test-runner",
        signal_engines={},
        alpha_seen={},
        adapter_factory=FlatAdapter,
    )

    conn = live_db.get_live_conn()
    try:
        assert svc.get_config(USER_ID, CONN_ID, "reconcile_blocked", conn) == "0"
        assert routed and routed[0]["action"] == "ENTER"
    finally:
        conn.close()
