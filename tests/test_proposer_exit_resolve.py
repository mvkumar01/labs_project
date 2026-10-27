"""A working exit order is resolved before any new exit (27 Oct 2026: the 10:20 Nifty Scalper stop rested,
the runner stacked 12 fresh SELLs on it, and when it filled nothing read it back - the position stayed OPEN)."""
from __future__ import annotations

from datetime import datetime, timedelta

import pytest

from live import live_executor as ex
from live import live_runner as lr
from live import live_service as svc
from live import proposer_runner as pr
from live.brokers import order_transport
from live.brokers.base import OrderResult, Position
from tests.test_proposer_live import CHAIN, CONN, USER, FakeFeed, FakeMarket, _p, db  # noqa: F401

DAY = "2026-10-05"
SYM = "SENSEX26O0874000CE"


def _ledger(key, oid, status, *, qty=20, bar="2026-10-05T10:20|spot_floor"):
    svc.insert_order_ledger(key, user_id=USER, conn_id=CONN, trade_date=DAY, strategy_version="proposer_dt25",
                            bar_timestamp=bar, action="EXIT", side="CALL", entry_rule="none", intent_seq=1,
                            symbol=SYM, qty=qty, order_type="LIMIT", limit_price=280.0, dry_run=0)
    svc.update_order_ledger(key, status=status, broker_order_id=oid)


class StatusAdapter:
    broker_name = "zerodha"

    def __init__(self, script):
        self.script = script            # oid -> list of status dicts, consumed one per read
        self.reads = []

    def get_order_status(self, oid):
        self.reads.append(oid)
        seq = self.script[oid]
        return seq.pop(0) if len(seq) > 1 else seq[0]


@pytest.fixture
def cancels(monkeypatch):
    sent = []
    monkeypatch.setattr(order_transport, "send_order", lambda a, op, params, key: sent.append((op, params, key)))
    return sent


def _resolve(adapter):
    return ex.resolve_working_exit(adapter, user_id=USER, conn_id=CONN, symbol=SYM, since="",
                                   sleep=lambda s: None)


def test_nothing_working_returns_none(db, cancels):
    assert _resolve(StatusAdapter({})) is None
    _ledger("k1", "O1", "complete")
    _ledger("k2", "O2", "rejected")
    assert _resolve(StatusAdapter({})) is None and cancels == []


def test_a_resting_exit_is_cancelled_before_anything_new(db, cancels):
    _ledger("k1", "O1", "open")
    a = StatusAdapter({"O1": [{"status": "OPEN"}, {"status": "CANCEL PENDING"}, {"status": "CANCELLED"}]})
    w = _resolve(a)
    assert w == {"state": "cancelled", "filled_qty": 0, "avg": None, "reason": "spot_floor"}
    assert cancels == [("cancel", {"variety": "regular", "order_id": "O1"}, "k1|cancel")]
    assert svc.get_order_ledger("k1")["status"] == "cancelled"


def test_a_rested_exit_that_filled_is_recorded_not_resent(db, cancels):
    _ledger("k1", "O1", "open")
    w = _resolve(StatusAdapter({"O1": [{"status": "COMPLETE", "average_price": 280.15, "filled_quantity": 20}]}))
    assert w["state"] == "filled" and w["filled_qty"] == 20 and w["avg"] == pytest.approx(280.15)
    assert cancels == [] and svc.get_order_ledger("k1")["status"] == "complete"


def test_cancel_unconfirmed_or_status_unreadable_means_send_nothing(db, cancels):
    _ledger("k1", "O1", "open")
    assert _resolve(StatusAdapter({"O1": [{"status": "OPEN"}]}))["state"] == "working"
    assert _resolve(StatusAdapter({"O1": [{}]}))["state"] == "working"


def test_partial_fill_then_cancel(db, cancels):
    _ledger("k1", "O1", "open")
    a = StatusAdapter({"O1": [{"status": "OPEN", "filled_quantity": 0},
                              {"status": "CANCELLED", "filled_quantity": 8, "average_price": 290.0}]})
    w = _resolve(a)
    assert w["state"] == "partial" and w["filled_qty"] == 8 and w["avg"] == pytest.approx(290.0)


def test_angel_cancel_params(db, cancels):
    _ledger("k1", "O1", "open")
    a = StatusAdapter({"O1": [{"orderstatus": "open"}, {"orderstatus": "cancelled"}]})
    a.broker_name, a._VARIETY = "angel", "NORMAL"
    assert _resolve(a)["state"] == "cancelled"
    assert cancels[0][1] == {"variety": "NORMAL", "orderid": "O1"}


# ------------------------------------------------------------------ runner ---
class RestingAdapter:
    """Entries fill at once; the first exit rests (OPEN) until the test fills or cancels it."""
    broker_name = "zerodha"

    def __init__(self, **kw):
        self.orders, self.position, self.status, self.n = [], Position(None, 0, None), {}, 0

    def connect(self):
        pass

    def is_connected(self):
        return True

    def account_ref(self):
        return "zerodha:T"

    def available_funds(self):
        return 1e6

    def use_segment(self, segment, underlying):
        self.segment = segment

    def broker_symbol(self, s):
        return s

    def get_position(self):
        return self.position

    def get_order_status(self, oid):
        return dict(self.status.get(oid) or {})

    def place_order(self, *, side, symbol, qty, price, idempotency_key):
        self.n += 1
        oid = f"B{self.n}"
        self.orders.append({"action": "BUY", "qty": qty, "price": price, "oid": oid})
        self.position = Position(symbol=symbol, qty=qty, side=side)
        self.status[oid] = {"status": "COMPLETE", "average_price": price, "filled_quantity": qty}
        return OrderResult(oid, "PLACED", None, {})

    def exit_all(self, *, symbol, qty, reason, idempotency_key, price=None):
        assert not any(s.get("status") == "OPEN" for s in self.status.values()), "SELL sent on top of a working SELL"
        self.n += 1
        oid = f"S{self.n}"
        self.orders.append({"action": "SELL", "qty": qty, "price": price, "oid": oid, "key": idempotency_key})
        first = sum(o["action"] == "SELL" for o in self.orders) == 1
        if first:
            self.status[oid] = {"status": "OPEN", "filled_quantity": 0}
        else:
            self.status[oid] = {"status": "COMPLETE", "average_price": price, "filled_quantity": qty}
            self.position = Position(None, 0, None)
        return OrderResult(oid, "PLACED", None, {})


def _setup(db, monkeypatch, cancel_effect):
    ex.set_mode(USER, CONN, ex.Mode.DRY_RUN)
    ex.set_mode(USER, CONN, ex.Mode.LIVE_ARMED)
    svc.set_config(USER, CONN, "armed", "1")
    monkeypatch.setattr(pr, "option_chain", lambda *a, **k: CHAIN)
    monkeypatch.setattr(ex, "evaluate_all", lambda *a, **k: [ex.GateResult("all", True, "")])
    monkeypatch.setattr(ex, "gate_static_order_proxy", lambda *a, **k: ex.GateResult("p", True, ""))

    def refreshed(adapter, result):
        s = adapter.get_order_status(result.broker_order_id)
        return OrderResult(result.broker_order_id, s.get("status", "PLACED"), s.get("average_price"), result.raw)
    monkeypatch.setattr(ex, "_refresh_order_result", refreshed)
    box = {}
    monkeypatch.setattr(order_transport, "send_order", lambda a, op, params, key: cancel_effect(box["a"], params))
    monkeypatch.setattr(ex, "_CANCEL_CONFIRM_DELAY_S", 0.0)
    return box


def _run(box, feed, market, now, ctx):
    return pr.process_connection(USER, CONN, ctx=ctx, feed=feed, market=market, now=now,
                                 adapter_factory=lambda **kw: box.setdefault("a", RestingAdapter(**kw)))


def _enter_then_floor(box, monkeypatch):
    now = datetime(2026, 10, 5, 9, 22, 30)
    feed, market = FakeFeed(_p("strong_bull", 0.6, "neutral", datetime(2026, 10, 5, 9, 22))), FakeMarket()
    ctx = {"task_id": "prop-task", "adapters": {}, "reconciled": set(), "conns": {}}
    _run(box, feed, market, now, ctx)
    assert svc.get_trade_state(USER, CONN)["position"] == "OPEN"
    market.px = 270.0                                            # -32%: below the -30% loss floor
    assert _run(box, feed, market, now + timedelta(minutes=1), ctx) is True
    sells = [o for o in box["a"].orders if o["action"] == "SELL"]
    assert len(sells) == 1 and box["a"].status[sells[0]["oid"]]["status"] == "OPEN"   # the stop rests
    return feed, market, ctx, now


def test_runner_cancels_a_resting_stop_and_resends_at_the_bid(db, monkeypatch):
    def cancel(adapter, params):
        adapter.status[params["order_id"]] = {"status": "CANCELLED", "filled_quantity": 0}
    box = _setup(db, monkeypatch, cancel)
    feed, market, ctx, now = _enter_then_floor(box, monkeypatch)
    market.px = 380.0                                            # recovered: the engine alone would not exit now
    assert _run(box, feed, market, now + timedelta(minutes=1, seconds=3), ctx) is False
    a = box["a"]
    sells = [o for o in a.orders if o["action"] == "SELL"]
    assert len(sells) == 2 and "|loss_floor|r1:" in sells[1]["key"]   # a new key in the same minute
    assert sells[1]["price"] <= 379.5                            # priced off the current bid
    assert svc.get_trade_state(USER, CONN)["position"] == "NONE"
    assert svc.get_day_pnl(USER, CONN)["trade_count"] == 1


def test_runner_records_a_rested_stop_that_filled_and_sends_nothing(db, monkeypatch):
    box = _setup(db, monkeypatch, lambda adapter, params: None)
    feed, market, ctx, now = _enter_then_floor(box, monkeypatch)
    a = box["a"]
    oid = [o for o in a.orders if o["action"] == "SELL"][0]["oid"]
    a.status[oid] = {"status": "COMPLETE", "average_price": 280.15, "filled_quantity": 20}
    a.position = Position(None, 0, None)
    market.px = 275.0
    assert _run(box, feed, market, now + timedelta(minutes=12), ctx) is False
    assert sum(o["action"] == "SELL" for o in a.orders) == 1
    assert svc.get_trade_state(USER, CONN)["position"] == "NONE"
    day = svc.get_day_pnl(USER, CONN)
    assert day["trade_count"] == 1


def test_runner_waits_while_the_cancel_is_unconfirmed(db, monkeypatch):
    box = _setup(db, monkeypatch, lambda adapter, params: None)   # cancel accepted, order stays OPEN
    feed, market, ctx, now = _enter_then_floor(box, monkeypatch)
    for k in range(3):
        assert _run(box, feed, market, now + timedelta(minutes=1, seconds=3 + k), ctx) is True
    assert sum(o["action"] == "SELL" for o in box["a"].orders) == 1   # never stacked
    assert svc.get_trade_state(USER, CONN)["position"] == "OPEN"


def test_contract_bumped():
    assert ex.PROPOSER_DECISION_ABI == "proposer-dt25-live-v14"
