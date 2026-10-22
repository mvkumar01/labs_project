"""Fixed/dynamic sizing without balance RPCs on the entry path."""
from datetime import timedelta
import threading

import pytest

from live import proposer_sizing as sz, proposer_runner as pr, live_executor as ex, live_service as svc
from live.engine import proposer_engine as pe
from tests.test_proposer_live import db, USER, CONN, FakeAdapter, _complete  # noqa: F401
from tests.test_nifty_scalper_v1 import setup_nifty, cycle, NOW
from tests.test_configure_strategy import client, USER_ID, CONN_ID, CSRF_TOKEN, _set_strategy  # noqa: F401


@pytest.mark.parametrize("funds,expected", [(35000, 3), (100000, 4), (10300, 1), (10299, 0), (0, 0),
                                             (-1, 0), (float("nan"), 0), (float("inf"), 0)])
def test_affordable_whole_lots_with_headroom(funds, expected):
    assert sz.affordable_lots(4, 20, 500, funds) == expected


def test_cache_age_invalid_funds_and_connection_isolation():
    now = [100.]
    cache = sz.FundsCache(clock=lambda: now[0], start_threads=False)
    class Adapter:
        value = 35000
        def available_funds(self):
            return self.value
    a = Adapter()
    cache.watch(("u", "a"), a)
    cache.refresh_once(("u", "a"))
    assert cache.get(("u", "a")) == 35000 and cache.get(("other", "a")) is None
    now[0] += sz.MAX_AGE_SECONDS
    assert cache.get(("u", "a")) is None
    for value in (None, -1, float("nan"), float("inf")):
        a.value = value
        cache.refresh_once(("u", "a"))
        assert cache.get(("u", "a")) is None
    cache.close()


def test_blocked_balance_request_never_blocks_cached_read_and_cannot_survive_order():
    started, release, finished = threading.Event(), threading.Event(), threading.Event()
    class Adapter:
        def available_funds(self):
            started.set()
            assert release.wait(2)
            return 35000
    cache = sz.FundsCache(start_threads=False)
    key = ("u", "a")
    cache.watch(key, Adapter())
    worker = threading.Thread(target=cache.refresh_once, args=(key,))
    worker.start()
    try:
        assert started.wait(1)
        # A separate reader completes while the broker RPC remains deliberately blocked.
        def read():
            assert cache.get(key) is None
            cache.invalidate(key)
            finished.set()
        reader = threading.Thread(target=read)
        reader.start()
        assert finished.wait(.5)
        reader.join(1)
    finally:
        release.set()
        worker.join(2)
    try:
        assert not worker.is_alive()
        assert cache.get(key) is None  # before close: the old in-flight result was discarded
    finally:
        cache.close()


def test_cache_rejects_slow_balance_as_stale_and_read_errors():
    now = [0.]
    cache = sz.FundsCache(clock=lambda: now[0], start_threads=False)
    class Slow:
        def available_funds(self):
            now[0] += 61
            return 99999
    cache.watch("key", Slow())
    cache.refresh_once("key")
    assert cache.get("key") is None
    class Failed:
        def available_funds(self):
            raise RuntimeError("offline")
    cache.watch("key", Failed())
    cache.refresh_once("key")
    assert cache.get("key") is None


def test_real_background_worker_starts_only_once_and_unwatch_discards_balance():
    done = threading.Event()
    class Adapter:
        calls = 0
        def available_funds(self):
            self.calls += 1
            return 35000
    adapter = Adapter()
    cache = sz.FundsCache(on_result=lambda *a: done.set())
    try:
        cache.watch("key", adapter)
        assert done.wait(1)
        cache.watch("key", adapter)
        assert cache.get("key") == 35000 and adapter.calls == 1
        cache.unwatch("key")
        assert cache.get("key") is None
    finally:
        cache.close()


def test_retain_stops_removed_accounts_without_touching_other_balances():
    class Adapter:
        def available_funds(self):
            return 35000
    cache = sz.FundsCache(start_threads=False)
    for key in ("a", "b"):
        cache.watch(key, Adapter())
        cache.refresh_once(key)
    cache.retain({"b"})
    assert cache.get("a") is None and cache.get("b") == 35000
    cache.close()


def seed(ctx, adapter, funds, *, key=(USER, CONN)):
    cache = ctx["funds_cache"] = sz.FundsCache(start_threads=False)
    cache.watch(key, adapter)
    adapter.available_funds = lambda: funds
    cache.refresh_once(key)
    adapter.available_funds = lambda: pytest.fail("Balance RPC attempted on the entry path")
    return cache


@pytest.mark.parametrize("strategy,lot_size", [(pe.STRATEGY_VERSION_NIFTY_SCALPER_V1, 65),
                                               (pe.STRATEGY_VERSION, 20)])
def test_dynamic_chooses_three_of_four_without_balance_rpc(db, monkeypatch, strategy, lot_size):  # noqa: F811
    ctx, market, feed = setup_nifty(monkeypatch, lots="4")
    svc.set_config(USER, CONN, "strategy_version", strategy)
    if lot_size == 20:
        from tests.test_proposer_live import CHAIN
        monkeypatch.setattr(pr, "option_chain", lambda *a: CHAIN)
    svc.set_config(USER, CONN, "proposer_sizing_mode", sz.DYNAMIC)
    adapter = pr._adapter(USER, CONN, ctx["adapters"], FakeAdapter)
    # 3.5 lots at the maximum chased price + headroom, below the four-lot requirement.
    funds = 3.5 * 400.5 * (1 + pr.lr.ENTRY_CHASE_CAP_PCT) * lot_size * sz.FUNDING_BUFFER
    cache = seed(ctx, adapter, funds)
    assert cycle(ctx, market, feed)
    assert svc.get_trade_state(USER, CONN)["qty"] == 3 * lot_size
    assert svc.get_lots(USER, CONN) == 4 and adapter.orders == []
    assert cache.get((USER, CONN)) is None  # balances collected around an order cannot be reused


@pytest.mark.parametrize("funds", [0, 1])
def test_dynamic_insufficient_balance_skips_without_waiting(db, monkeypatch, funds):  # noqa: F811
    ctx, market, feed = setup_nifty(monkeypatch, lots="4")
    svc.set_config(USER, CONN, "proposer_sizing_mode", sz.DYNAMIC)
    adapter = pr._adapter(USER, CONN, ctx["adapters"], FakeAdapter)
    seed(ctx, adapter, funds)
    assert not cycle(ctx, market, feed)
    assert svc.get_trade_state(USER, CONN)["position"] == "NONE" and adapter.orders == []


@pytest.mark.parametrize("stale", [False, True])
def test_missing_or_stale_balance_falls_back_to_fixed_for_this_entry(db, monkeypatch, stale):  # noqa: F811
    ctx, market, feed = setup_nifty(monkeypatch, lots="4")
    svc.set_config(USER, CONN, "proposer_sizing_mode", sz.DYNAMIC)
    adapter = pr._adapter(USER, CONN, ctx["adapters"], FakeAdapter)
    cache = seed(ctx, adapter, 1 if stale else None)
    if stale:
        original_clock = cache.clock
        cache.clock = lambda: original_clock() + sz.MAX_AGE_SECONDS
    assert cycle(ctx, market, feed)
    assert svc.get_trade_state(USER, CONN)["qty"] == 4 * 65
    assert svc.get_config(USER, CONN, "proposer_sizing_mode") == sz.DYNAMIC
    assert svc.get_lots(USER, CONN) == 4
    adapter.available_funds = lambda: 40000
    cache.refresh_once((USER, CONN))
    adapter.available_funds = lambda: pytest.fail("No funds RPC at entry")
    assert cycle(ctx, market, feed, NOW + timedelta(seconds=2))
    # Newly arrived funds do not add another order or resize the already-open position.
    assert svc.get_trade_state(USER, CONN)["qty"] == 4 * 65 and adapter.orders == []


@pytest.mark.parametrize("mode", ["disarmed", "fixed", "after_close"])
def test_unused_funds_reader_is_stopped(db, monkeypatch, mode):  # noqa: F811
    ctx, market, feed = setup_nifty(monkeypatch, lots="4")
    svc.set_config(USER, CONN, "proposer_sizing_mode", sz.DYNAMIC)
    adapter = pr._adapter(USER, CONN, ctx["adapters"], FakeAdapter)
    cache = seed(ctx, adapter, 100000)
    now = NOW
    if mode == "disarmed":
        ex.set_mode(USER, CONN, ex.Mode.DISARMED)
    elif mode == "fixed":
        svc.set_config(USER, CONN, "proposer_sizing_mode", sz.FIXED)
    else:
        now = NOW.replace(hour=16)
    cycle(ctx, market, feed, now=now)
    assert cache.get((USER, CONN)) is None


def test_fixed_default_does_not_create_balance_cache_or_read_balance(db, monkeypatch):  # noqa: F811
    ctx, market, feed = setup_nifty(monkeypatch, lots="4")
    adapter = pr._adapter(USER, CONN, ctx["adapters"], FakeAdapter)
    adapter.available_funds = lambda: pytest.fail("Fixed mode must not read funds at entry")
    assert cycle(ctx, market, feed)
    assert svc.get_trade_state(USER, CONN)["qty"] == 4 * 65
    assert "funds_cache" not in ctx


@pytest.mark.parametrize("snapshot", ["fresh", "missing", "stale"])
def test_dynamic_broker_rejection_still_steps_down_and_preserves_setting(db, monkeypatch, snapshot):  # noqa: F811
    ctx, market, feed = setup_nifty(monkeypatch, lots="4")
    svc.set_config(USER, CONN, "proposer_sizing_mode", sz.DYNAMIC)
    ex.set_mode(USER, CONN, ex.Mode.LIVE_ARMED)
    class Limited(FakeAdapter):
        def place_order(self, **kw):
            if kw["qty"] > 65:
                self.orders.append({"qty": kw["qty"], "price": kw["price"]})
                raise RuntimeError("Broker rejected the order (Insufficient funds)")
            return super().place_order(**kw)
    adapter = pr._adapter(USER, CONN, ctx["adapters"], Limited)
    funds = 3.5 * 400.5 * (1 + pr.lr.ENTRY_CHASE_CAP_PCT) * 65 * sz.FUNDING_BUFFER
    cache = seed(ctx, adapter, funds if snapshot == "fresh" else None)
    if snapshot == "stale":
        adapter.available_funds = lambda: 1
        cache.refresh_once((USER, CONN))
        adapter.available_funds = lambda: pytest.fail("No funds RPC at entry")
        original_clock = cache.clock
        cache.clock = lambda: original_clock() + sz.MAX_AGE_SECONDS
    monkeypatch.setattr(ex, "evaluate_all", lambda *a, **k: [ex.GateResult("all", True, "")])
    monkeypatch.setattr(ex, "gate_static_order_proxy", lambda *a, **k: ex.GateResult("proxy", True, ""))
    monkeypatch.setattr(ex, "_refresh_order_result", _complete)
    assert cycle(ctx, market, feed, factory=Limited)
    assert [o["qty"] for o in adapter.orders] == ([195, 65] if snapshot == "fresh" else [260, 130, 65])
    assert svc.get_lots(USER, CONN) == 4


def test_stale_funds_never_prevent_position_exit(db, monkeypatch):  # noqa: F811
    ctx, market, feed = setup_nifty(monkeypatch)
    assert cycle(ctx, market, feed)
    svc.set_config(USER, CONN, "proposer_sizing_mode", sz.DYNAMIC)
    cache = ctx["funds_cache"] = sz.FundsCache(start_threads=False)
    market.nifty -= 47
    assert not cycle(ctx, market, feed, NOW + timedelta(seconds=2))
    assert svc.get_trade_state(USER, CONN)["position"] == "NONE"
    cache.close()


def post(client, mode=None, **kwargs):
    data = {"csrf_token": CSRF_TOKEN, "lots": "4", "daily_loss_cap": "3000", "strategy": "nifty_scalper_v1", **kwargs}
    if mode is not None:
        data["proposer_sizing_mode"] = mode
    return client.post("/live/configure", data=data)


def test_configure_modes_default_and_dynamic_round_trip(client):  # noqa: F811
    _set_strategy("proposer", pe.STRATEGY_VERSION_NIFTY_SCALPER_V1)
    page = client.get("/live/configure").get_data(as_text=True)
    assert 'value="fixed" selected' in page and 'value="dynamic"' in page
    svc.set_config(USER_ID, CONN_ID, "runner_decision_abi", ex.PROPOSER_DECISION_ABI)
    assert post(client, sz.DYNAMIC).status_code == 302
    assert svc.get_config(USER_ID, CONN_ID, "proposer_sizing_mode") == sz.DYNAMIC
    assert 'id="lots-label">Maximum lots' in client.get("/live/configure").get_data(as_text=True)
    assert post(client).status_code == 302  # older form preserves saved mode
    assert svc.get_config(USER_ID, CONN_ID, "proposer_sizing_mode") == sz.DYNAMIC


@pytest.mark.parametrize("loaded", ["", "proposer-dt25-live-v11"])
def test_configure_rejects_dynamic_until_runner_loaded_v12(client, loaded):  # noqa: F811
    _set_strategy("proposer", pe.STRATEGY_VERSION_NIFTY_SCALPER_V1)
    svc.set_config(USER_ID, CONN_ID, "runner_decision_abi", loaded)
    assert post(client, sz.DYNAMIC).status_code == 200
    assert svc.get_config(USER_ID, CONN_ID, "proposer_sizing_mode") in (None, "", sz.FIXED)
    assert svc.get_lots(USER_ID, CONN_ID) == 1  # rejected form made no partial writes


def test_configure_invalid_and_open_position_make_no_changes(client):  # noqa: F811
    _set_strategy("proposer", pe.STRATEGY_VERSION_NIFTY_SCALPER_V1)
    assert "Select Fixed or Dynamic" in post(client, "bogus").get_data(as_text=True)
    state = svc.get_trade_state(USER_ID, CONN_ID)
    state.update(position="OPEN", symbol="NIFTY26OCT23000CE", qty=65)
    svc.save_trade_state(USER_ID, CONN_ID, state)
    assert "open position" in post(client, sz.DYNAMIC).get_data(as_text=True)
    assert svc.get_lots(USER_ID, CONN_ID) == 1
