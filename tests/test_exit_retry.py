"""Fix C — a still-needed EXIT that did not place must be retriable across polls
(2026-07-07: a throttled exit was abandoned for the rest of the bar). Guards:
already-placed orders never re-place (double-sell protection); entries never
auto-retry (no pre-place reconcile to guard them).
"""
from __future__ import annotations

import sqlite3
from pathlib import Path
import sys

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from live.brokers.base import OrderResult, Position
from live import live_executor as ex
from live import live_service as svc
from storage.live_db import init_live_db

USER_ID = "user-1"
CONN_ID = "user-1:angel"
SYMBOL = "NIFTY16JUN2623050CE"
THROTTLE = RuntimeError("Access denied because of exceeding access rate")


@pytest.fixture(autouse=True)
def _static_order_proxy(monkeypatch):
    monkeypatch.setenv("LIVE_ORDER_PROXY_URL", "http://static.test:1234")


class FlakyExitAdapter:
    """Long position; exit_all raises a throttle `fail_times` times then places."""

    def __init__(self, position, fail_times=1):
        self.position = position
        self.exit_calls = 0
        self._fail_times = fail_times

    def is_connected(self):
        return True

    def account_ref(self):
        return "angel:TEST"

    def get_position(self):
        return self.position

    def get_order_status(self, broker_order_id):
        return {}

    def exit_all(self, *, symbol, qty, reason, idempotency_key, price=None):
        self.exit_calls += 1
        self.last_exit_price = price
        if self.exit_calls <= self._fail_times:
            raise THROTTLE
        return OrderResult(broker_order_id="OID123", status="PLACED",
                           avg_fill_price=100.0, raw={})


class FlakyEntryAdapter(FlakyExitAdapter):
    def __init__(self, fail_times=1):
        super().__init__(Position(symbol=None, qty=0, side=None), fail_times)
        self.place_calls = 0

    def place_order(self, *, side, symbol, qty, price, idempotency_key):
        self.place_calls += 1
        if self.place_calls <= self._fail_times:
            raise THROTTLE
        return OrderResult(broker_order_id="OID999", status="PLACED",
                           avg_fill_price=100.0, raw={})


def _conn():
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    init_live_db(conn)
    svc.upsert_connection(USER_ID, CONN_ID, broker="angel", account_label="TEST",
                          account_ref="angel:TEST", status="connected", conn=conn)
    for k, v in (("mode", "LIVE_ARMED"), ("armed", "1"), ("kill_switch", "0"),
                 ("lots", "1"), ("daily_loss_cap", "50000")):
        svc.set_config(USER_ID, CONN_ID, k, v, conn)
    return conn


def _place(conn, adapter, *, action, key):
    return ex.place_idempotent(
        adapter, user_id=USER_ID, conn_id=CONN_ID, idem_key=key, side="CALL",
        symbol=SYMBOL, qty=65, price=100.0, action=action, dry_run=False,
        trade_date="2026-06-12", strategy_version="test",
        bar_timestamp="2026-06-12T09:18:00+05:30", conn=conn)


def test_failed_exit_retries_until_placed():
    conn = _conn()
    adapter = FlakyExitAdapter(Position(SYMBOL, 65, "CALL"), fail_times=1)

    r1 = _place(conn, adapter, action="EXIT", key="ex1")     # throttled -> FAILED
    assert r1.status == "FAILED"
    assert svc.get_order_ledger("ex1", conn)["status"] == "FAILED"

    r2 = _place(conn, adapter, action="EXIT", key="ex1")     # same key -> retry
    assert r2.status == "PLACED"
    assert adapter.exit_calls == 2
    assert svc.get_order_ledger("ex1", conn)["status"] == "PLACED"


def test_placed_exit_is_not_replaced():
    conn = _conn()
    adapter = FlakyExitAdapter(Position(SYMBOL, 65, "CALL"), fail_times=0)

    r1 = _place(conn, adapter, action="EXIT", key="ex2")     # PLACED
    assert r1.status == "PLACED" and adapter.exit_calls == 1

    r2 = _place(conn, adapter, action="EXIT", key="ex2")     # same key -> SKIP
    assert r2.raw.get("idempotent_skip") is True
    assert adapter.exit_calls == 1                            # never double-sold


def test_failed_entry_is_not_auto_retried():
    conn = _conn()
    adapter = FlakyEntryAdapter(fail_times=1)

    r1 = _place(conn, adapter, action="ENTER", key="en1")    # throttled -> FAILED
    assert r1.status == "FAILED"

    r2 = _place(conn, adapter, action="ENTER", key="en1")    # same key -> SKIP
    assert r2.raw.get("idempotent_skip") is True
    assert adapter.place_calls == 1                           # NOT retried
