import os

import pytest

from live.brokers.angel import AngelAdapter
from live import live_executor, live_runner
from live.proxy import (
    StaticOrderProxyRequired,
    configure_outbound_proxy,
    order_proxy,
)


_ORDER_PROXY_KEYS = (
    "LIVE_ORDER_PROXY_URL",
    "LIVE_OUTBOUND_PROXY_URL",
    "QUOTAGUARDSTATIC_URL",
)


def test_runner_never_falls_back_to_execution_broker_ltp(monkeypatch) -> None:
    class AngelLike:
        @staticmethod
        def get_ltp(_symbol):
            raise AssertionError("Angel market data must not be called")

    monkeypatch.setattr(live_runner, "get_kite_ltp", lambda _symbol: None)

    with pytest.raises(RuntimeError, match="no Kite LTP"):
        live_runner._fast_ltp(AngelLike(), "NIFTY2671424400PE")


def test_angel_market_data_surface_is_disabled() -> None:
    adapter = AngelAdapter(user_id="u", conn_id="c", creds={})

    for call in (
        adapter.get_spot,
        lambda: adapter.get_ltp("NIFTY2671424400PE"),
        lambda: adapter.quote(["NIFTY2671424400PE"]),
    ):
        with pytest.raises(RuntimeError, match="use Kite"):
            call()


def test_order_proxy_fails_closed_when_static_route_missing(monkeypatch) -> None:
    for key in _ORDER_PROXY_KEYS:
        monkeypatch.delenv(key, raising=False)

    with pytest.raises(StaticOrderProxyRequired):
        with order_proxy():
            raise AssertionError("order body must never run")

    gate = live_executor.gate_static_order_proxy()
    assert gate.name == "static_order_proxy"
    assert gate.passed is False


def test_order_proxy_is_transient_and_order_only(monkeypatch) -> None:
    url = "http://static.example:1234"
    monkeypatch.setenv("LIVE_ORDER_PROXY_URL", url)
    monkeypatch.setenv("HTTP_PROXY", "http://normal-data-egress")

    class Client:
        proxies = {"before": "value"}

    client = Client()
    with order_proxy(client):
        assert os.environ["HTTP_PROXY"] == url
        assert client.proxies == {"http": url, "https": url}

    assert os.environ["HTTP_PROXY"] == "http://normal-data-egress"
    assert client.proxies == {"before": "value"}
    assert live_executor.gate_static_order_proxy().passed is True


def test_legacy_global_proxy_switch_cannot_route_data(monkeypatch) -> None:
    monkeypatch.setenv("LIVE_PROXY_ALL", "1")
    monkeypatch.setenv("LIVE_OUTBOUND_PROXY_URL", "http://static.example:1234")
    monkeypatch.setenv("HTTP_PROXY", "http://normal-data-egress")

    assert configure_outbound_proxy() == ""
    assert os.environ["HTTP_PROXY"] == "http://normal-data-egress"
