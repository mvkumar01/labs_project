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


@pytest.fixture(autouse=True)
def _static_order_proxy(monkeypatch):
    monkeypatch.setenv("LIVE_ORDER_PROXY_URL", "http://static.test:1234")


class FakeAdapter:
    def __init__(self, position: Position):
        self.position = position
        self.exit_calls = 0

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
        return OrderResult(
            broker_order_id="OID123",
            status="PLACED",
            avg_fill_price=100.0,
            raw={"symbol": symbol, "qty": qty, "reason": reason},
        )


def _conn():
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    init_live_db(conn)
    svc.upsert_connection(
        USER_ID,
        CONN_ID,
        broker="angel",
        account_label="TEST",
        account_ref="angel:TEST",
        status="connected",
        conn=conn,
    )
    svc.set_config(USER_ID, CONN_ID, "mode", "LIVE_ARMED", conn)
    svc.set_config(USER_ID, CONN_ID, "armed", "1", conn)
    svc.set_config(USER_ID, CONN_ID, "kill_switch", "0", conn)
    svc.set_config(USER_ID, CONN_ID, "lots", "1", conn)
    svc.set_config(USER_ID, CONN_ID, "daily_loss_cap", "50000", conn)
    return conn


def _place_exit(conn, adapter, key="k1", qty=65):
    return ex.place_idempotent(
        adapter,
        user_id=USER_ID,
        conn_id=CONN_ID,
        idem_key=key,
        side="CALL",
        symbol=SYMBOL,
        qty=qty,
        price=100.0,
        action="EXIT",
        dry_run=False,
        trade_date="2026-06-12",
        strategy_version="test",
        bar_timestamp="2026-06-12T09:18:00+05:30",
        conn=conn,
    )


def test_live_exit_blocks_when_broker_is_flat():
    conn = _conn()
    adapter = FakeAdapter(Position(symbol=None, qty=0, side=None))

    result = _place_exit(conn, adapter)

    assert result.status == "NO_LONG_POSITION"
    assert adapter.exit_calls == 0
    assert svc.get_order_ledger("k1", conn)["status"] == "NO_LONG_POSITION"


def test_live_exit_blocks_when_broker_position_is_short():
    conn = _conn()
    adapter = FakeAdapter(Position(symbol=SYMBOL, qty=-65, side="CALL"))

    result = _place_exit(conn, adapter)

    assert result.status == "NO_LONG_POSITION"
    assert adapter.exit_calls == 0


def test_live_exit_blocks_when_qty_exceeds_broker_long():
    conn = _conn()
    adapter = FakeAdapter(Position(symbol=SYMBOL, qty=25, side="CALL"))

    result = _place_exit(conn, adapter)

    assert result.status == "EXIT_QTY_EXCEEDS_POSITION"
    assert adapter.exit_calls == 0


def test_live_exit_allows_matching_long_position():
    conn = _conn()
    adapter = FakeAdapter(Position(symbol=SYMBOL, qty=65, side="CALL"))

    result = _place_exit(conn, adapter)

    assert result.status == "PLACED"
    assert adapter.exit_calls == 1
    # GAP B: the caller's (marketable) exit price must reach the adapter so the
    # stop's SELL limit crosses the spread instead of re-fetching raw LTP.
    assert adapter.last_exit_price == 100.0


if __name__ == "__main__":
    failures = []
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            try:
                fn()
                print(f"PASS  {name}")
            except AssertionError as e:
                failures.append((name, str(e)))
                print(f"FAIL  {name}\n      {e}")
    if failures:
        raise SystemExit(1)
    print("\nAll live exit safety checks passed.")
