"""Order-validity asymmetry (2026-07-14 phantom-lot incident).

Entries must be IOC so an unfilled entry limit never rests into a later bar and
fills stale — a stale entry fill creates a broker long the DB never recorded,
which both fabricates a second round-trip and (via live_runner current_open=
broker_open) makes the runner think it already holds a position, suppressing
re-entries. Exits stay DAY: a cancelled IOC exit is not retriable and would
strand a live long, so risk-off orders must be allowed to rest until filled.
"""
import contextlib

import pytest

from live.brokers import angel as angel_mod
from live.brokers import zerodha as zerodha_mod
from live.brokers.angel import AngelAdapter
from live.brokers.zerodha import ZerodhaAdapter
from live.brokers.base import Position


class _FakeSmart:
    """Captures the params dict handed to Angel SmartAPI.placeOrder."""

    def __init__(self):
        self.params = None

    def placeOrder(self, params):
        self.params = params
        return {"status": True, "data": {"orderid": "OID1"}}


class _FakeKite:
    VARIETY_REGULAR = "regular"
    EXCHANGE_NFO = "NFO"
    TRANSACTION_TYPE_BUY = "BUY"
    PRODUCT_MIS = "MIS"
    ORDER_TYPE_LIMIT = "LIMIT"
    VALIDITY_IOC = "IOC"

    def __init__(self):
        self.kwargs = None

    def place_order(self, **kwargs):
        self.kwargs = kwargs
        return "OID2"


@pytest.fixture
def _enable_angel(monkeypatch):
    monkeypatch.setenv("LIVE_ORDERS_ENABLED", "1")
    monkeypatch.setattr(angel_mod, "_LIVE_ORDERS_ENABLED", True, raising=False)
    monkeypatch.setattr(angel_mod, "order_proxy",
                        lambda smart: contextlib.nullcontext())


def _angel(monkeypatch):
    a = AngelAdapter(user_id="u", conn_id="u:angel", creds={"client_code": "X"})
    a._smart = _FakeSmart()
    monkeypatch.setattr(a, "_resolve_symbol_meta",
                        lambda sym: {"symbol": sym, "token": "111"})
    monkeypatch.setattr(a, "_invalidate_reads", lambda: None)
    return a


def test_angel_entry_is_ioc(monkeypatch, _enable_angel):
    a = _angel(monkeypatch)
    res = a.place_order(side="PUT", symbol="NIFTY2671424250PE", qty=65,
                        price=186.5, idempotency_key="k")
    assert a._smart.params["duration"] == "IOC"   # never rests → no stale fill
    assert a._smart.params["transactiontype"] == "BUY"
    assert res.broker_order_id == "OID1"


def test_angel_exit_is_day(monkeypatch, _enable_angel):
    a = _angel(monkeypatch)
    monkeypatch.setattr(
        a, "get_position",
        lambda: Position(symbol="NIFTY2671424250PE", qty=65, side="PUT"))
    a.exit_all(symbol="NIFTY2671424250PE", qty=65, reason="stop",
               idempotency_key="k", price=188.3)
    assert a._smart.params["duration"] == "DAY"    # risk-off must fill
    assert a._smart.params["transactiontype"] == "SELL"


def test_zerodha_entry_is_ioc(monkeypatch):
    monkeypatch.setenv("LIVE_ORDERS_ENABLED", "1")
    monkeypatch.setattr(zerodha_mod, "_LIVE_ORDERS_ENABLED", True, raising=False)
    monkeypatch.setattr(zerodha_mod, "order_proxy",
                        lambda kite: contextlib.nullcontext())
    z = ZerodhaAdapter(user_id="u", conn_id="u:zerodha", creds={})
    z._kite = _FakeKite()
    z.place_order(side="PUT", symbol="NIFTY2671424250PE", qty=65,
                  price=186.5, idempotency_key="k")
    assert z._kite.kwargs["validity"] == "IOC"
