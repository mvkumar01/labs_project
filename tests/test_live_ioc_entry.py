"""Live IOC entries: priced after the gates from the ask, re-tried on no-fill, capped.

2026-09-29: three expiry-day entries (NIFTY26SEP22850PE) were cancelled unfilled. The
limit was the Kite LTP + 0.6%, read before gates that took 10-56 s, while the
premium rose Rs1-2 every 10 s.
"""

from __future__ import annotations

import sqlite3

import pytest

from live import live_executor as ex
from live import live_runner as lr
from live import live_service as svc
from live.brokers.base import OrderResult, Position
from storage.live_db import init_live_db

USER_ID, CONN_ID = "user-1", "user-1:angel"


@pytest.fixture(autouse=True)
def _static_order_proxy(monkeypatch):
    monkeypatch.setenv("LIVE_ORDER_PROXY_URL", "http://static.test:1234")


def _conn():
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    init_live_db(conn)
    svc.upsert_connection(USER_ID, CONN_ID, broker="angel", account_label="TEST",
                          account_ref="angel:TEST", status="connected", conn=conn)
    for key, value in (("mode", "LIVE_ARMED"), ("armed", "1"), ("kill_switch", "0"),
                       ("lots", "1"), ("daily_loss_cap", "50000")):
        svc.set_config(USER_ID, CONN_ID, key, value, conn)
    conn.commit()
    return conn


class Adapter:
    def __init__(self):
        self.prices = []

    def place_order(self, *, side, symbol, qty, price, idempotency_key):
        self.prices.append(price)
        return OrderResult(broker_order_id=f"OID{len(self.prices)}", status="cancelled",
                           avg_fill_price=None, raw={})

    def get_position(self):
        return Position(symbol=None, qty=0, side=None)


def _place(conn, adapter, price_fn, monkeypatch, key="en1"):
    monkeypatch.setattr(ex, "evaluate_all", lambda *a, **k: [ex.GateResult("all", True, "")])
    monkeypatch.setattr(ex, "_refresh_order_result", lambda adapter, result: result)
    return ex.place_idempotent(
        adapter, user_id=USER_ID, conn_id=CONN_ID, idem_key=key, side="PUT",
        symbol="NIFTY26SEP22850PE", qty=65, price=191.6, action="ENTER", dry_run=False,
        trade_date="2026-09-29", strategy_version="v2.14",
        bar_timestamp="2026-09-29T09:25:00+05:30", conn=conn, price_fn=price_fn)


# -- executor: price after the gates -------------------------------------------------
def test_executor_prices_the_entry_after_its_gates(monkeypatch):
    conn, adapter, order = _conn(), Adapter(), []
    monkeypatch.setattr(ex, "evaluate_all",
                        lambda *a, **k: order.append("gates") or [ex.GateResult("all", True, "")])
    monkeypatch.setattr(ex, "_refresh_order_result", lambda adapter, result: result)

    def fresh():
        order.append("price")
        return 199.9

    ex.place_idempotent(
        adapter, user_id=USER_ID, conn_id=CONN_ID, idem_key="en1", side="PUT",
        symbol="X", qty=65, price=191.6, action="ENTER", dry_run=False, conn=conn,
        price_fn=fresh)
    assert order == ["gates", "price"] and adapter.prices == [199.9]
    assert svc.get_order_ledger("en1", conn)["limit_price"] == 199.9


def test_executor_skips_the_entry_when_price_fn_declines(monkeypatch):
    conn, adapter = _conn(), Adapter()
    result = _place(conn, adapter, lambda: None, monkeypatch)
    assert result.status == "PRICE_SKIP" and adapter.prices == []
    assert svc.get_order_ledger("en1", conn)["status"] == "PRICE_SKIP"


# -- runner: the price callback -----------------------------------------------------
def test_limit_is_ask_plus_two_percent_capped(monkeypatch):
    monkeypatch.setattr(lr, "get_kite_ask", lambda s: 197.40)
    monkeypatch.setattr(lr, "get_kite_ltp", lambda s: 197.20)
    sent = {}
    assert lr.entry_price_fn("NIFTY26SEP22850PE", cap=250.0, sent=sent)() == 201.35
    assert sent == {"ask": 197.40, "limit": 201.35}
    # headroom is cut at the cap, never above it
    assert lr.entry_price_fn("NIFTY26SEP22850PE", cap=199.0)() == 199.0


def test_ask_past_the_chase_cap_skips(monkeypatch):
    monkeypatch.setattr(lr, "get_kite_ask", lambda s: 212.30)
    monkeypatch.setattr(lr, "get_kite_ltp", lambda s: 211.65)
    assert lr.entry_price_fn("NIFTY26SEP22850PE", cap=189.95)() is None


def test_no_quote_skips(monkeypatch):
    monkeypatch.setattr(lr, "get_kite_ask", lambda s: None)
    monkeypatch.setattr(lr, "get_kite_ltp", lambda s: None)
    monkeypatch.setattr(lr, "kite_symbol_for", lambda s: s)
    assert lr.entry_price_fn("X", cap=500.0)() is None


def test_ltp_above_a_stale_ask_is_respected(monkeypatch):
    monkeypatch.setattr(lr, "get_kite_ask", lambda s: 180.90)
    monkeypatch.setattr(lr, "get_kite_ltp", lambda s: 186.40)
    assert lr.entry_price_fn("X", cap=500.0)() == pytest.approx(190.15)


# -- runner: chase anchor and per-attempt keys ---------------------------------------
def test_chase_anchor_holds_for_the_same_signal_and_resets_for_a_new_one(monkeypatch):
    store = {}
    monkeypatch.setattr(svc, "get_config", lambda u, c, k, conn=None: store.get(k))
    monkeypatch.setattr(svc, "set_config", lambda u, c, k, v, conn=None: store.__setitem__(k, v))
    assert lr.entry_chase_anchor("u", "c", "2026-09-29|PUT|RULE3|0|22666.8", 181.3) == 181.3
    assert lr.entry_chase_anchor("u", "c", "2026-09-29|PUT|RULE3|0|22666.8", 211.6) == 181.3
    assert lr.entry_chase_anchor("u", "c", "2026-09-29|PUT|RULE1|1|22600.0", 150.0) == 150.0


def test_ioc_retries_get_their_own_idempotency_keys(monkeypatch):
    keys = []
    monkeypatch.setattr(lr.ex, "place_idempotent",
                        lambda *a, **k: keys.append(k["idem_key"]) or OrderResult(None, "cancelled", None, {}))
    monkeypatch.setattr(lr, "next_intent_seq", lambda *a, **k: 1)
    monkeypatch.setattr(lr.svc, "get_config", lambda *a, **k: "v2.14")
    for attempt in range(3):
        lr._route_order(None, USER_ID, CONN_ID, action="ENTER", side="PUT", symbol="S",
                        qty=65, price=190.0, dry_run=False, entry_rule="RULE3",
                        attempt=attempt, price_fn=lambda: 191.0)
    assert keys[1] == keys[0] + ":ioc2" and keys[2] == keys[0] + ":ioc3"
    assert len(set(keys)) == 3


def _drive_entry(monkeypatch, tmp_path, statuses, lots=1):
    """One process_connection cycle with a CALL entry signal; _route_order returns
    `statuses` in turn. Returns (routed kwargs, telegram messages, trade state)."""
    from datetime import datetime, timedelta, timezone
    import storage.live_db as live_db

    monkeypatch.setattr(live_db, "LIVE_DB_PATH", tmp_path / "live.db")
    init_live_db()
    conn = live_db.get_live_conn()
    svc.upsert_connection(USER_ID, CONN_ID, broker="angel", account_label="T",
                          account_ref="angel:T", status="connected", conn=conn)
    for k, v in (("mode", "LIVE_ARMED"), ("armed", "1"), ("kill_switch", "0"),
                 ("lots", str(lots)), ("daily_loss_cap", "50000"),
                 ("decision_engine", "champion_replay"), ("strategy_version", "v2.12")):
        svc.set_config(USER_ID, CONN_ID, k, v, conn)
    conn.close()

    class FlatAdapter:
        def __init__(self, **_kwargs):
            pass

        def connect(self):
            return None

        def is_connected(self):
            return True

        def account_ref(self):
            return "angel:T"

        def get_position(self):
            return Position(symbol=None, qty=0, side=None)

    IST = timezone(timedelta(hours=5, minutes=30))
    monkeypatch.setattr(lr, "_now_ist", lambda: datetime(2026, 9, 29, 9, 26, tzinfo=IST))
    monkeypatch.setattr(lr, "_today_ist_iso", lambda: "2026-09-29")
    monkeypatch.setattr(lr, "market_session_available", lambda _now: True)
    monkeypatch.setattr(lr, "eod_watchdog", lambda _now_t: False)
    messages = []
    monkeypatch.setattr(lr, "notify_telegram", messages.append)
    monkeypatch.setattr(lr, "_fast_spot", lambda: 22666.8)
    monkeypatch.setattr(lr, "get_latest_alpha", lambda: {
        "timestamp": "2026-09-29T09:25:00+05:30", "alpha": -40.0, "spot": 22666.8})
    monkeypatch.setattr(lr.champion_inputs, "latest_completed_ohlc_minute",
                        lambda _d: "2026-09-29T09:25")
    monkeypatch.setattr(lr.champion_decider, "champion_target", lambda *_a, **_k: {
        "position": "PUT", "entry_spot": 22666.8, "entry_rule": "RULE3",
        "n_closed": 0, "last_closed_event_id": None})
    monkeypatch.setattr(lr, "resolve_affordable_option",
                        lambda *_a, **_k: ("NIFTY26SEP22850PE", 181.3))
    monkeypatch.setattr(lr, "get_kite_ask", lambda s: 186.75)
    monkeypatch.setattr(lr, "get_kite_ltp", lambda s: 186.40)
    routed, queue = [], list(statuses)

    def fake_route(*_args, **kwargs):
        routed.append(kwargs)
        kwargs["price_fn"]()                       # the executor would call it post-gates
        status = queue.pop(0)
        status, raw = status if isinstance(status, tuple) else (status, {})
        return OrderResult(broker_order_id=f"OID{len(routed)}", status=status,
                           avg_fill_price=187.0 if status == "COMPLETE" else None, raw=raw)

    monkeypatch.setattr(lr, "_route_order", fake_route)
    lr.process_connection(USER_ID, CONN_ID, adapters={}, reconciled=set(),
                          task_id="test-runner", signal_engines={}, alpha_seen={},
                          adapter_factory=FlatAdapter)
    conn = live_db.get_live_conn()
    try:
        return routed, messages, svc.get_trade_state(USER_ID, CONN_ID, conn=conn)
    finally:
        conn.close()


def test_no_fill_is_requoted_and_resent_until_it_fills(monkeypatch, tmp_path):
    routed, messages, state = _drive_entry(monkeypatch, tmp_path,
                                           ["cancelled", "cancelled", "COMPLETE"])
    assert [r["attempt"] for r in routed] == [0, 1, 2]
    assert all(r["action"] == "ENTER" and r["price_fn"] for r in routed)
    assert state["position"] == "OPEN" and state["entry_price"] == 187.0
    assert not any("not filled" in m for m in messages)


def test_three_no_fills_stop_the_burst_and_alert(monkeypatch, tmp_path):
    routed, messages, state = _drive_entry(monkeypatch, tmp_path, ["cancelled"] * 3)
    assert len(routed) == 3 and state["position"] != "OPEN"
    # ask 186.75 + 2% = 190.50, but the cap is 181.30 * 1.05 -> 190.35 (tick-rounded)
    assert any("not filled after 3 IOC tries" in m and "last limit 190.35" in m
               for m in messages)


def test_exits_keep_the_marketable_limit_and_no_price_fn(monkeypatch):
    seen = {}
    monkeypatch.setattr(lr.ex, "place_idempotent",
                        lambda *a, **k: seen.update(k) or OrderResult(None, "PLACED", None, {}))
    monkeypatch.setattr(lr, "next_intent_seq", lambda *a, **k: 1)
    monkeypatch.setattr(lr.svc, "get_config", lambda *a, **k: "v2.14")
    lr._route_order(None, USER_ID, CONN_ID, action="EXIT", side="PUT", symbol="S",
                    qty=65, price=200.0, dry_run=False)
    assert seen["price_fn"] is None and seen["price"] == 198.8     # 200 - 0.6%


# -- runner: size step-down and part fills (2026-10-05) ------------------------------
def _rejected(message):
    return ("REJECTED", {"status_snapshot": {"status": "REJECTED", "status_message": message}})


def test_a_size_rejection_is_resent_at_half_the_lots(monkeypatch, tmp_path):
    routed, messages, state = _drive_entry(
        monkeypatch, tmp_path,
        [_rejected("Insufficient funds. Required margin is 2,40,000 but available is 1,30,000"),
         "COMPLETE"], lots=10)
    assert [r["qty"] for r in routed] == [650, 325]
    assert len({r["attempt"] for r in routed}) == 2               # distinct idempotency keys
    assert state["position"] == "OPEN" and state["qty"] == 325
    assert any("retrying with 5 lots" in m for m in messages)


def test_lots_above_the_freeze_quantity_are_sent_at_the_freeze_size(monkeypatch, tmp_path):
    routed, messages, state = _drive_entry(monkeypatch, tmp_path, ["COMPLETE"], lots=40)
    assert [r["qty"] for r in routed] == [27 * 65]                # NIFTY freeze 1,800 qty
    assert state["qty"] == 27 * 65 and any("freeze" in m for m in messages)


def test_a_non_size_rejection_does_not_step_down(monkeypatch, tmp_path):
    routed, messages, state = _drive_entry(
        monkeypatch, tmp_path, [_rejected("Price exceeds the circuit limit")], lots=10)
    assert len(routed) == 1 and state["position"] != "OPEN"
    assert any("REJECTED at broker" in m for m in messages)


def test_an_ioc_part_fill_is_held_and_not_resent(monkeypatch, tmp_path):
    routed, messages, state = _drive_entry(
        monkeypatch, tmp_path,
        [("cancelled", {"status_snapshot": {"status": "CANCELLED", "filled_quantity": 130}})], lots=4)
    assert len(routed) == 1
    assert state["position"] == "OPEN" and state["qty"] == 130
    assert any("part-filled" in m for m in messages)
