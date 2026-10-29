"""Angel MCX GOLD contract, quantity, margin and guarded order transport tests."""
from types import SimpleNamespace
from datetime import date

import pytest

from live.brokers import gold_angel as mod
from live.brokers.gold_angel import GoldAngelAdapter

CANONICAL, SYMBOL, TOKEN = "GOLD26DECFUT", "GOLD04DEC26FUT", "495213"


@pytest.fixture
def broker(monkeypatch):
    a = GoldAngelAdapter(user_id="u", conn_id="u:angel", creds={})
    rows = [dict(name="GOLD", exch_seg="MCX", instrumenttype="FUTCOM", symbol=SYMBOL,
                 expiry="04DEC2026", token=TOKEN, lotsize="1", tick_size="100.000000")]
    instruments = [dict(name="GOLD", tradingsymbol=CANONICAL, instrument_type="FUT",
                        expiry=date(2026, 12, 4), lot_size=1, tick_size=1)]
    data = SimpleNamespace(instruments=lambda e: instruments,
        ltp=lambda key: {key: {"last_price": 140000}},
        quote=lambda keys: {key: dict(last_price=140000, depth=dict(buy=[dict(price=139999)],
            sell=[dict(price=140001)])) for key in keys})
    monkeypatch.setattr(GoldAngelAdapter, "_kite", property(lambda self: data))
    monkeypatch.setattr(a, "_gold_rows", lambda: rows)
    a._smart = SimpleNamespace(position=lambda: dict(status=True, data=[]))
    return a, rows, instruments


def test_exact_expiry_symbol_and_token_mapping(broker):
    a, rows, instruments = broker
    c = a.contract(date(2026, 10, 29))
    assert c["tradingsymbol"] == CANONICAL
    assert a.broker_symbol(CANONICAL) == SYMBOL
    assert a._resolve_symbol_meta(CANONICAL)["token"] == TOKEN
    rows[0]["expiry"] = "05DEC2026"  # same month but different exchange expiry must fail
    a._symbol_cache.clear()
    with pytest.raises(RuntimeError, match="exact Kite expiry"):
        a.contract(date(2026, 10, 29))


@pytest.mark.parametrize("field,value", [("lotsize", "100"), ("tick_size", "10"), ("token", "")])
def test_changed_specification_blocks_orders(broker, field, value):
    a, rows, _ = broker
    rows[0][field] = value
    with pytest.raises(RuntimeError, match="specification"):
        a.contract(date(2026, 10, 29))


def test_angel_margin_uses_sell_carryforward_and_actual_contract_count(broker):
    a, _, _ = broker
    seen = []
    a._smart.getMarginApi = lambda p: seen.append(p) or dict(status=True, data={"totalMarginRequired": 800000})
    assert a.margin(CANONICAL, 2) == 800000
    assert seen == [{"positions": [dict(exchange="MCX", qty=2, price=0, productType="CARRYFORWARD",
        token=TOKEN, tradeType="SELL", orderType="MARKET")]}]
    a._smart.getMarginApi = lambda p: dict(status=False, data=None)
    with pytest.raises(RuntimeError, match="margin quote failed"):
        a.margin(CANONICAL, 1)


def test_angel_positions_use_exact_mcx_product_and_token_after_restart(broker):
    a, _, _ = broker
    a._smart.position = lambda: dict(status=True, data=[dict(exchange="MCX", producttype="CARRYFORWARD",
        tradingsymbol=SYMBOL, symboltoken=TOKEN, netqty="-2"), dict(exchange="MCX", producttype="INTRADAY",
        tradingsymbol="GOLDM05NOV26FUT", symboltoken="other", netqty="-9")])
    p = a.get_position()
    assert p.symbol == CANONICAL and p.qty == -2
    a._smart.position = lambda: dict(status=False)
    with pytest.raises(RuntimeError, match="position read failed"):
        a.get_position()


def test_angel_sell_entry_buy_exit_ioc_static_transport(broker, monkeypatch):
    a, _, _ = broker
    monkeypatch.setattr(mod.angel, "_live_orders_enabled", lambda: True)
    sent = []
    monkeypatch.setattr(mod, "send_order", lambda adapter, op, params, key:
                        sent.append((adapter.broker_name, op, params)) or dict(status=True, data={"orderid": "oid"}))
    a.place_order(side="SHORT", symbol=CANONICAL, qty=2, price=140000, idempotency_key="en")
    assert sent[0][0] == "angel"
    params = sent[0][2]
    assert params["transactiontype"] == "SELL" and params["quantity"] == 2
    assert params["tradingsymbol"] == SYMBOL and params["symboltoken"] == TOKEN
    assert params["exchange"] == "MCX" and params["producttype"] == "CARRYFORWARD" and params["duration"] == "IOC"
    a._smart.position = lambda: dict(status=True, data=[dict(exchange="MCX", producttype="CARRYFORWARD",
        tradingsymbol=SYMBOL, symboltoken=TOKEN, netqty="-2")])
    a.exit_all(symbol=CANONICAL, qty=2, reason="stop", price=140350, idempotency_key="exit")
    assert sent[-1][2]["transactiontype"] == "BUY"
    before = len(sent)
    result = a.exit_all(symbol=CANONICAL, qty=3, reason="stop", price=140350, idempotency_key="over")
    assert result.status == "NO_SHORT_POSITION" and len(sent) == before


def test_quotes_stay_on_shared_kite_and_angel_live_flag_remains_required(broker, monkeypatch):
    a, _, _ = broker
    assert a.get_ltp(CANONICAL) == 140000
    assert a.limit(CANONICAL) == 139997 and a.limit(CANONICAL, buy=True) == 140003
    monkeypatch.setattr(mod.angel, "_live_orders_enabled", lambda: False)
    with pytest.raises(NotImplementedError):
        a.place_order(side="SHORT", symbol=CANONICAL, qty=1, price=140000, idempotency_key="off")
