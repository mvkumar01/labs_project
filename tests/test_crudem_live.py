"""CRUDEOILM combination on live: isolated SQLite and a fake broker only. No test reaches a network."""
import sqlite3
from datetime import datetime, timedelta
from types import SimpleNamespace

import pandas as pd
import pytest

from live import crudem_contract as cc
from live import crudem_live_runner as r
from live import live_executor as ex, live_service as svc
from live.brokers import crudem_angel as ca
from live.brokers.base import OrderResult, Position
from live.brokers.crudem_angel import CrudemAngelAdapter
from storage.live_db import init_live_db

USER, CID = "crude-user", "crude-user:angel"
SYMBOL, ANGEL, TOKEN = "CRUDEOILM26NOVFUT", "CRUDEOILM19NOV26FUT", "573423"
NOW = datetime(2026, 11, 3, 10, 15, 6)
RULE_LONG, RULE_SHORT = 593002, 2803          # 593002 is a percentage stop, 2803 an ATR stop


class Broker:
    """Records every order; fills are what the test says they are."""
    def __init__(self):
        self.qty, self.orders, self.cancels, self.status = 0, [], [], {}
        self.ltp, self.seq, self.fill_px, self.fill = 9800.0, 0, 9800.0, True
        self.stop_status = "TRIGGER PENDING"

    def get_position(self):
        return Position(SYMBOL, self.qty, "LONG" if self.qty > 0 else "SHORT") if self.qty else Position(None, 0, None)

    def holds(self, symbol, side, qty):
        held = self.qty if side == "LONG" else -self.qty
        return symbol == SYMBOL and 0 < qty <= held

    def _id(self):
        self.seq += 1
        return f"o{self.seq}"

    def place_order(self, *, side, symbol, qty, price, idempotency_key):
        oid = self._id()
        self.orders.append(("entry", side, qty, price))
        filled = qty if self.fill else 0
        self.qty += filled if side == "LONG" else -filled
        self.status[oid] = dict(status="complete" if self.fill else "cancelled", filledshares=filled,
                                averageprice=self.fill_px if self.fill else 0)
        return OrderResult(oid, "PLACED", None, {})

    def exit_all(self, *, symbol, qty, reason, idempotency_key, price=None):
        oid = self._id()
        self.orders.append(("exit", qty, price))
        filled = qty if self.fill else 0
        self.qty += -filled if self.qty > 0 else filled
        self.status[oid] = dict(status="complete" if self.fill else "cancelled", filledshares=filled,
                                averageprice=self.fill_px if self.fill else 0)
        return OrderResult(oid, "PLACED", None, {})

    def place_stop(self, *, side, symbol, qty, trigger, idempotency_key):
        oid = self._id()
        self.orders.append(("stop", side, qty, trigger))
        self.stop_id = oid
        self.status[oid] = dict(status=self.stop_status, filledshares=0, averageprice=0)
        return OrderResult(oid, "PLACED", None, {})

    def cancel_stop(self, order_id, key):
        self.cancels.append(key)
        self.status[order_id] = dict(status="cancelled", filledshares=0, averageprice=0)

    def fill_stop(self, qty, px):
        self.status[self.stop_id] = dict(status="complete", filledshares=qty, averageprice=px)
        self.qty += -qty if self.qty > 0 else qty

    def get_order_status(self, order_id):
        return self.status.get(order_id, {})

    def get_ltp(self, symbol):
        if self.ltp is None:
            raise RuntimeError("no price")
        return self.ltp

    def limit(self, symbol, buy=False):
        return self.ltp + 3 if buy else self.ltp - 3

    def margin(self, symbol, qty):
        return 30000.0

    def available_funds(self):
        return 100000.0


@pytest.fixture
def conn(monkeypatch):
    c = sqlite3.connect(":memory:")
    c.row_factory = sqlite3.Row
    init_live_db(c)
    r.ensure_schema(c)
    svc.upsert_connection(USER, CID, broker="angel", account_label="test", account_ref="test",
                          status="connected", conn=c)
    svc.set_config(USER, CID, "strategy_version", cc.VERSION, c)
    svc.set_config(USER, CID, "decision_engine", cc.ENGINE, c)
    monkeypatch.setattr(r, "notify_telegram", lambda text: True)
    # the executor's own gates are tested elsewhere; here the order path behind them is
    monkeypatch.setattr(ex, "evaluate_all", lambda *a, **k: [])
    monkeypatch.setattr(ex, "all_passed", lambda gates: True)
    monkeypatch.setattr(ex, "gate_mode_armed", lambda *a, **k: None)
    monkeypatch.setattr(ex, "gate_kill_switch_clear", lambda *a, **k: None)
    monkeypatch.setattr(ex, "gate_broker_connected", lambda *a, **k: None)
    monkeypatch.setattr(ex, "gate_account_isolation", lambda *a, **k: None)
    monkeypatch.setattr(ex, "gate_static_order_proxy", lambda *a, **k: None)
    monkeypatch.setattr(ex, "gate_daily_loss_ok", lambda *a, **k: None)
    monkeypatch.setattr(ex, "_refresh_order_result", lambda adapter, result, **k: result)
    yield c
    c.close()


def open_position(c, b, *, rule=RULE_LONG, dry=False, now=NOW, atr=None):
    """An entry through the real settle path, then the same cycle's manage, as process() runs them."""
    s = r.load(c, USER, CID)
    side = r.eng.MEMBER_BY_CID[rule].side
    r.queue(c, USER, CID, s, action="ENTER", symbol=SYMBOL, qty=10, side=side, price=b.ltp, dry=dry, now=now,
            rule=rule, atr=atr, signal_ts="2026-11-03 10:14:00")
    r.settle(c, USER, CID, s, b, now)
    s = r.load(c, USER, CID)
    r.manage(c, USER, CID, s, b, now)
    return r.load(c, USER, CID)


def cycle(c, b, now):
    s = r.load(c, USER, CID)
    if s.get("pending"):
        r.settle(c, USER, CID, s, b, now)
        s = r.load(c, USER, CID)
    if s.get("position"):
        r.manage(c, USER, CID, s, b, now)
    return r.load(c, USER, CID)


def later(seconds):
    return NOW + timedelta(seconds=seconds)


def test_entry_sets_levels_from_the_fill_and_rests_the_stop_at_once(conn):
    b = Broker()
    s = open_position(conn, b)
    p = s["position"]
    member = r.eng.MEMBER_BY_CID[RULE_LONG]
    _, stop, target = r.eng.levels(member, 9800.0, None)
    assert (p["stop"], p["target"]) == r.cr.tick_levels(1, stop, target)
    assert b.orders == [("entry", "LONG", 10, 9803.0), ("stop", "LONG", 10, p["stop"])]
    assert p["stop_order"]["state"] == "sent"
    assert svc.get_trade_state(USER, CID, conn)["qty"] == 10


def test_short_entry_rests_a_stop_above(conn):
    b = Broker()
    p = open_position(conn, b, rule=RULE_SHORT, atr=40.0)["position"]
    assert b.orders[0] == ("entry", "SHORT", 10, 9797.0)
    assert b.orders[1] == ("stop", "SHORT", 10, p["stop"]) and p["stop"] > 9800


def test_unfilled_ioc_entry_leaves_nothing(conn):
    b = Broker()
    b.fill = False
    s = open_position_no_manage(conn, b)
    assert s["position"] is None and s["pending"] is None
    assert [o[0] for o in b.orders] == ["entry"]


def open_position_no_manage(c, b):
    s = r.load(c, USER, CID)
    r.queue(c, USER, CID, s, action="ENTER", symbol=SYMBOL, qty=10, side=1, price=b.ltp, dry=False, now=NOW,
            rule=RULE_LONG, atr=None, signal_ts="x")
    r.settle(c, USER, CID, s, b, NOW)
    return r.load(c, USER, CID)


def test_target_cancels_the_stop_and_exits_only_once_it_is_dead(conn):
    b = Broker()
    p = open_position(conn, b)["position"]
    b.ltp = p["target"]
    s = cycle(conn, b, later(2))                  # sees the target: cancels, sends no exit yet
    assert len(b.cancels) == 1 and [o[0] for o in b.orders] == ["entry", "stop"]
    b.fill_px = p["target"]
    s = cycle(conn, b, later(4))                  # stop seen dead: exits
    assert s["position"] is None and [o[0] for o in b.orders] == ["entry", "stop", "exit"]
    trade = dict(conn.execute("SELECT * FROM live_trades").fetchone())
    assert trade["side"] == "LONG" and trade["gross_pnl"] == (p["target"] - 9800) * 10
    assert trade["reason"].startswith("target") and trade["strategy"] == cc.VERSION


def test_stop_that_fills_while_being_cancelled_sends_no_exit(conn):
    b = Broker()
    p = open_position(conn, b)["position"]
    b.ltp = p["target"]
    b.cancel_stop = lambda order_id, key: (_ for _ in ()).throw(RuntimeError("already complete"))
    cycle(conn, b, later(2))
    b.fill_stop(10, p["stop"])
    s = cycle(conn, b, later(4))
    assert s["position"] is None and b.qty == 0
    assert [o[0] for o in b.orders] == ["entry", "stop"]            # no second closing order
    assert dict(conn.execute("SELECT reason FROM live_trades").fetchone())["reason"].startswith("stop")


def test_stop_filled_at_the_broker_is_booked_at_its_price(conn):
    b = Broker()
    p = open_position(conn, b)["position"]
    b.fill_stop(10, p["stop"] - 2)
    s = cycle(conn, b, later(30))
    assert s["position"] is None
    t = dict(conn.execute("SELECT * FROM live_trades").fetchone())
    assert t["exit_price"] == p["stop"] - 2 and t["reason"].startswith("stop")
    assert svc.get_day_pnl(USER, CID, "2026-11-03", conn=conn)["realized_pnl"] == t["net_pnl"]


def test_partial_stop_fill_books_the_part_and_keeps_the_rest(conn):
    b = Broker()
    s = r.load(conn, USER, CID)
    r.queue(conn, USER, CID, s, action="ENTER", symbol=SYMBOL, qty=20, side=1, price=b.ltp, dry=False, now=NOW,
            rule=RULE_LONG, atr=None, signal_ts="x")
    r.settle(conn, USER, CID, s, b, NOW)
    s = r.load(conn, USER, CID)
    r.manage(conn, USER, CID, s, b, NOW)
    p = r.load(conn, USER, CID)["position"]
    b.status[b.stop_id] = dict(status="open", filledshares=10, averageprice=p["stop"])
    b.qty = 10
    s = cycle(conn, b, later(5))
    assert s["position"]["qty"] == 10 and s["position"]["stop_booked"] == 10
    assert conn.execute("SELECT COUNT(*) FROM live_trades").fetchone()[0] == 1


def test_refused_stop_closes_the_position(conn, monkeypatch):
    b = Broker()

    def refuse(**k):
        raise RuntimeError("Broker rejected the order")
    b.place_stop = refuse
    s = open_position(conn, b)
    assert s["position"]["stop_failed"] == "FAILED"
    s = cycle(conn, b, later(2))
    assert s["position"] is None
    assert dict(conn.execute("SELECT reason FROM live_trades").fetchone())["reason"].startswith("no_broker_stop")


def test_stop_rejected_after_its_acknowledgement_closes_the_position(conn):
    b = Broker()
    b.stop_status = "rejected"
    open_position(conn, b)
    s = cycle(conn, b, later(2))
    assert s["position"] is None
    assert dict(conn.execute("SELECT reason FROM live_trades").fetchone())["reason"].startswith("no_broker_stop")


def test_uncertain_stop_blocks_for_a_person(conn):
    b = Broker()

    def unknown(**k):
        raise RuntimeError("timed out")
    b.place_stop = unknown
    conn.execute("INSERT INTO live_order_requests(request_id,route_id,revision,user_id,conn_id,operation,intent_key,"
                 "requested_at,outcome) SELECT 'x','r',1,?,?, 'exit', '', 0, 'uncertain'", (USER, CID))
    original = r.request_outcome
    r.request_outcome = lambda c, cid, key: "uncertain"
    try:
        s = open_position(conn, b)
    finally:
        r.request_outcome = original
    assert svc.get_config(USER, CID, "reconcile_blocked", conn) == "1"
    assert s["position"]["stop_order"] is None


def test_price_through_the_stop_limit_is_closed_by_the_bot(conn):
    b = Broker()
    p = open_position(conn, b)["position"]
    b.ltp = p["stop"] - ca.STOP_LIMIT_PTS - 1
    cycle(conn, b, later(2))
    s = cycle(conn, b, later(4))
    assert s["position"] is None
    assert dict(conn.execute("SELECT reason FROM live_trades").fetchone())["reason"].startswith("stop_missed")


def test_price_between_trigger_and_limit_leaves_the_stop_to_work(conn):
    b = Broker()
    p = open_position(conn, b)["position"]
    b.ltp = p["stop"] - 5
    s = cycle(conn, b, later(2))
    assert s["position"] and not b.cancels


def test_session_close_exit_and_failed_price_reads(conn):
    b = Broker()
    open_position(conn, b)
    b.ltp = None
    s = cycle(conn, b, later(2))
    assert s["position"] and not b.cancels        # no price: the stop keeps working, nothing is sent
    at = NOW.replace(hour=23, minute=29, second=1)
    b.ltp = 9810.0
    cycle(conn, b, at)
    s = cycle(conn, b, at + timedelta(seconds=2))
    assert s["position"] is None
    assert dict(conn.execute("SELECT reason FROM live_trades").fetchone())["reason"].startswith("eod")


def test_unfilled_ioc_exit_is_sent_again(conn):
    b = Broker()
    p = open_position(conn, b)["position"]
    b.ltp = p["target"]
    cycle(conn, b, later(2))
    b.fill = False
    s = cycle(conn, b, later(4))
    assert s["position"]["exit_reason"] == "target"
    b.fill = True
    s = cycle(conn, b, later(6))
    assert s["position"] is None
    assert [o[0] for o in b.orders] == ["entry", "stop", "exit", "exit"]


def test_a_position_the_broker_does_not_show_blocks_and_sends_nothing(conn):
    b = Broker()
    open_position(conn, b)
    b.qty = 0                                     # flattened by hand, stop still pending
    s = cycle(conn, b, later(2))
    assert s["position"] and svc.get_config(USER, CID, "reconcile_blocked", conn) in ("", "0", None)   # grace
    s = cycle(conn, b, later(2 + r.POSITION_GRACE_S + 1))
    assert svc.get_config(USER, CID, "reconcile_blocked", conn) == "1"
    assert [o[0] for o in b.orders] == ["entry", "stop"]


def test_a_broker_position_in_other_units_says_no_stop_is_resting(conn, monkeypatch):
    alerts = []
    monkeypatch.setattr(r, "notify_telegram", lambda text: alerts.append(text) or True)
    b = Broker()
    open_position_no_manage(conn, b)
    b.qty = 1                                     # netqty in lots
    s = r.load(conn, USER, CID)
    r.manage(conn, USER, CID, s, b, NOW)
    cycle(conn, b, later(r.POSITION_GRACE_S + 1))
    assert any("NO STOP IS RESTING" in a for a in alerts)


def test_dry_position_is_managed_on_polled_prices_with_no_broker_order(conn):
    b = Broker()
    p = open_position(conn, b, dry=True)["position"]
    assert p["dry"] and not b.orders and p["stop_order"] is None
    b.ltp = p["stop"]
    s = cycle(conn, b, later(2))
    assert s["position"] is None and not b.orders
    t = dict(conn.execute("SELECT * FROM live_trades").fetchone())
    assert t["dry_run"] == 1 and t["reason"].startswith("stop")
    day = svc.get_day_pnl(USER, CID, "2026-11-03", conn=conn)
    assert day["realized_pnl"] == 0 and day["realized_pnl_dry"] == t["net_pnl"]


def test_a_restart_with_a_fill_recorded_only_as_pending_resumes_it(conn):
    b = Broker()
    s = r.load(conn, USER, CID)
    r.queue(conn, USER, CID, s, action="ENTER", symbol=SYMBOL, qty=10, side=1, price=b.ltp, dry=False, now=NOW,
            rule=RULE_LONG, atr=None, signal_ts="x")
    r.send(conn, USER, CID, b, s["pending"], NOW)     # the crash is after the send, before anything was booked
    assert len(b.orders) == 1
    s = cycle(conn, b, later(1))
    assert s["position"]["qty"] == 10 and len([o for o in b.orders if o[0] == "entry"]) == 1


def test_crash_before_the_outcome_is_known_blocks(conn):
    b = Broker()
    s = r.load(conn, USER, CID)
    r.queue(conn, USER, CID, s, action="ENTER", symbol=SYMBOL, qty=10, side=1, price=b.ltp, dry=False, now=NOW,
            rule=RULE_LONG, atr=None, signal_ts="x")
    svc.insert_order_ledger(s["pending"]["key"], user_id=USER, conn_id=CID, trade_date="2026-11-03",
                            strategy_version=cc.VERSION, bar_timestamp="b", action="ENTER", side="LONG",
                            entry_rule="e", intent_seq=0, symbol=SYMBOL, qty=10, order_type="LIMIT",
                            limit_price=9800, dry_run=0, conn=conn)
    cycle(conn, b, later(1))
    assert svc.get_config(USER, CID, "reconcile_blocked", conn) == "1" and not b.orders


def test_a_fill_in_other_units_reaches_a_person_and_sends_no_stop(conn, monkeypatch):
    alerts = []
    monkeypatch.setattr(r, "notify_telegram", lambda text: alerts.append(text) or True)
    b = Broker()
    place = b.place_order

    def in_lots(**k):
        result = place(**k)
        b.status[result.broker_order_id]["filledshares"] = 1
        return result
    b.place_order = in_lots
    s = open_position_no_manage(conn, b)
    assert s["pending"] and svc.get_config(USER, CID, "reconcile_blocked", conn) == "1"
    assert any("no stop was placed" in a for a in alerts)
    assert [o[0] for o in b.orders] == ["entry"]


def test_late_entry_is_not_priced(conn):
    b = Broker()
    intent = dict(created=r.stamp(NOW), symbol=SYMBOL, side=1)
    assert r.entry_price(b, intent, NOW.replace(second=r.ENTRY_WINDOW_S)) == b.ltp + 3
    assert r.entry_price(b, intent, NOW.replace(second=r.ENTRY_WINDOW_S + 1)) is None


# -- entries through enter(): the signal itself is the engine's, tested in test_crudem_combo.py ----------------
class FakeSeries:
    def __init__(self, now):
        self.frame_ = pd.DataFrame({"ts": [pd.Timestamp(now).floor("min") - pd.Timedelta(minutes=1)]})
        self.refreshed = 0

    def refresh(self, now):
        self.refreshed += 1

    def front(self, now):
        return {"tradingsymbol": SYMBOL}

    def frame(self, now):
        return self.frame_


def fires(monkeypatch, *cids):
    monkeypatch.setattr(r.eng, "pending_fires", lambda frame, start, cutoff: [
        {"cid": c, "atr_stop_dist": 40.0 if r.eng.MEMBER_BY_CID[c].stop_kind == "atr" else None,
         "signal_ts": pd.Timestamp(cutoff) - pd.Timedelta(minutes=1)} for c in cids])


def go(conn, b, now, dry=False, series=None):
    series = series or FakeSeries(now)
    svc.set_config(USER, CID, "lots", "2", conn)
    r._runtime.clear()
    s = r.load(conn, USER, CID)
    r.enter(conn, USER, CID, s, b, now, dry, series, False)
    return r.load(conn, USER, CID), series


def test_decision_waits_for_the_candle_to_settle(conn, monkeypatch):
    fires(monkeypatch, RULE_LONG)
    b = Broker()
    s, series = go(conn, b, NOW.replace(second=r.SETTLE_S - 1))
    assert not series.refreshed and not b.orders
    s, series = go(conn, b, NOW.replace(second=r.SETTLE_S))
    assert series.refreshed and b.orders[0] == ("entry", "LONG", 20, 9803.0)


def test_first_fire_trades_and_the_rest_are_recorded(conn, monkeypatch):
    fires(monkeypatch, RULE_LONG, RULE_SHORT)
    go(conn, Broker(), NOW)
    rows = {r_["cid"]: r_["outcome"] for r_ in conn.execute("SELECT cid, outcome FROM live_crudem_decisions")}
    assert rows == {RULE_LONG: "taken", RULE_SHORT: "position_held"}


def test_a_minute_is_decided_once(conn, monkeypatch):
    fires(monkeypatch, RULE_LONG)
    b = Broker()
    b.fill = False
    go(conn, b, NOW)
    go(conn, b, NOW + timedelta(seconds=2))
    assert len(b.orders) == 1


def test_no_entry_without_funds_or_with_a_position_in_the_account(conn, monkeypatch):
    fires(monkeypatch, RULE_LONG)
    b = Broker()
    b.available_funds = lambda: 100.0
    go(conn, b, NOW)
    assert not b.orders
    assert conn.execute("SELECT outcome FROM live_crudem_decisions").fetchone()[0] == "no_funds"
    b = Broker()
    b.qty = 10
    go(conn, b, NOW + timedelta(minutes=1))
    assert not b.orders


def test_blocked_connection_takes_no_entry(conn, monkeypatch):
    fires(monkeypatch, RULE_LONG)
    svc.set_config(USER, CID, "reconcile_blocked", "1", conn)
    b = Broker()
    go(conn, b, NOW)
    assert not b.orders


@pytest.mark.parametrize("now", [NOW.replace(hour=9, minute=0, second=6), NOW.replace(hour=23, minute=29, second=6),
                                 datetime(2026, 11, 7, 10, 15, 6)])
def test_no_entry_outside_hours(conn, monkeypatch, now):
    fires(monkeypatch, RULE_LONG)
    b = Broker()
    go(conn, b, now)
    assert not b.orders


# -- contract and selection -------------------------------------------------------------------------------------
def test_contract_abi_is_what_the_gate_expects():
    assert ex.expected_decision_abi(cc.VERSION) == cc.ABI


def test_arming_refuses_an_open_dry_position(conn):
    svc.set_config(USER, CID, "mode", "DRY_RUN", conn)
    svc.set_config(USER, CID, "strategy_version", cc.VERSION, conn)
    open_position(conn, Broker(), dry=True)
    with pytest.raises(ex.InvalidTransition, match="crude"):
        ex.arm_live(USER, CID, conn)


def test_broker_exit_gate_refuses_an_order_that_would_reverse(conn):
    b = Broker()
    result = ex.place_idempotent(b, user_id=USER, conn_id=CID, idem_key="k1", side="LONG", symbol=SYMBOL, qty=10,
                                 price=9800, action="EXIT", dry_run=False, trade_date="2026-11-03",
                                 strategy_version=cc.VERSION, bar_timestamp="b", conn=conn)
    assert result.status == "NO_POSITION" and not b.orders


def test_flat_refresh_clears_the_crude_state(conn):
    from live.configuration_reconcile import clear_stale_state
    open_position(conn, Broker(), dry=True)
    clear_stale_state(conn, USER, CID)
    s = r.load(conn, USER, CID)
    assert s["position"] is None and s["pending"] is None


# -- adapter ------------------------------------------------------------------------------------------------------
@pytest.fixture
def angel(monkeypatch):
    a = CrudemAngelAdapter(user_id="u", conn_id="u:angel", creds={})
    rows = [dict(name="CRUDEOILM", exch_seg="MCX", instrumenttype="FUTCOM", symbol=ANGEL, expiry="19NOV2026",
                 token=TOKEN, lotsize="10", tick_size="100.000000")]
    instruments = [dict(name="CRUDEOILM", tradingsymbol=SYMBOL, instrument_type="FUT", expiry="2026-11-19")]
    data = SimpleNamespace(instruments=lambda e: instruments, ltp=lambda key: {key: {"last_price": 9800}},
                           quote=lambda keys: {k: dict(depth=dict(buy=[dict(price=9799)], sell=[dict(price=9801)]))
                                               for k in keys})
    monkeypatch.setattr(CrudemAngelAdapter, "_kite", property(lambda self: data))
    monkeypatch.setattr(a, "_contract_rows", lambda: rows)
    monkeypatch.setattr(ca.angel, "_live_orders_enabled", lambda: True)
    sent = []
    monkeypatch.setattr(ca, "send_order", lambda adapter, op, params, key: sent.append((op, params)) or
                        dict(status=True, data=dict(orderid="1")))
    a._position = []
    a._smart = SimpleNamespace(position=lambda: dict(status=True, data=a._position))
    return a, rows, sent


def held(a, qty):
    a._position = [dict(exchange="MCX", producttype="CARRYFORWARD", tradingsymbol=ANGEL, symboltoken=TOKEN, netqty=str(qty))]


def test_exact_expiry_mapping_and_a_changed_lot_size_stops_orders(angel):
    a, rows, _ = angel
    assert a._resolve_symbol_meta(SYMBOL)["token"] == TOKEN
    rows[0]["lotsize"] = "100"
    a._symbol_cache.clear()
    with pytest.raises(RuntimeError, match="specification"):
        a._resolve_symbol_meta(SYMBOL)


def test_entries_are_ioc_limits_in_barrels_both_ways(angel):
    a, _, sent = angel
    a.place_order(side="LONG", symbol=SYMBOL, qty=10, price=9803, idempotency_key="k")
    a.place_order(side="SHORT", symbol=SYMBOL, qty=20, price=9797, idempotency_key="k2")
    assert sent[0][1]["transactiontype"] == "BUY" and sent[1][1]["transactiontype"] == "SELL"
    assert sent[0][1]["quantity"] == 10 and sent[1][1]["quantity"] == 20
    assert all(s[1]["duration"] == "IOC" and s[1]["ordertype"] == "LIMIT" and s[1]["exchange"] == "MCX"
               and s[1]["producttype"] == "CARRYFORWARD" for s in sent)


@pytest.mark.parametrize("qty", [1, 5, 15])
def test_quantity_must_be_whole_lots(angel, qty):
    a, _, sent = angel
    with pytest.raises(ValueError, match="whole number of lots"):
        a.place_order(side="LONG", symbol=SYMBOL, qty=qty, price=9803, idempotency_key="k")
    assert not sent


def test_entry_refuses_an_existing_position(angel):
    a, _, sent = angel
    a._resolve_symbol_meta(SYMBOL)
    held(a, 10)
    with pytest.raises(RuntimeError, match="flat"):
        a.place_order(side="SHORT", symbol=SYMBOL, qty=10, price=9797, idempotency_key="k")
    assert not sent


def test_exit_closes_the_held_side_and_never_more(angel):
    a, _, sent = angel
    a._resolve_symbol_meta(SYMBOL)
    held(a, -10)
    a.exit_all(symbol=SYMBOL, qty=10, reason="target", idempotency_key="k", price=9803)
    assert sent[0][1]["transactiontype"] == "BUY"
    assert a.exit_all(symbol=SYMBOL, qty=20, reason="target", idempotency_key="k2", price=9803).status == "NO_POSITION"
    a._position = []
    assert a.exit_all(symbol=SYMBOL, qty=10, reason="target", idempotency_key="k3", price=9803).status == "NO_POSITION"
    assert len(sent) == 1


def test_stop_is_a_resting_stop_limit_on_the_right_side(angel):
    a, _, sent = angel
    a._resolve_symbol_meta(SYMBOL)
    held(a, 10)
    a.place_stop(side="LONG", symbol=SYMBOL, qty=10, trigger=9700, idempotency_key="k")
    o = sent[0][1]
    assert (sent[0][0], o["variety"], o["ordertype"], o["duration"]) == ("exit", "STOPLOSS", "STOPLOSS_LIMIT", "DAY")
    assert (o["transactiontype"], o["triggerprice"], o["price"]) == ("SELL", 9700, 9700 - ca.STOP_LIMIT_PTS)
    held(a, -10)
    a.place_stop(side="SHORT", symbol=SYMBOL, qty=10, trigger=9900, idempotency_key="k2")
    o = sent[1][1]
    assert (o["transactiontype"], o["triggerprice"], o["price"]) == ("BUY", 9900, 9900 + ca.STOP_LIMIT_PTS)


def test_stop_needs_the_position_it_protects(angel):
    a, _, sent = angel
    assert a.place_stop(side="LONG", symbol=SYMBOL, qty=10, trigger=9700, idempotency_key="k").status == "NO_POSITION"
    assert not sent


def test_position_read_failure_is_not_flat(angel):
    a, _, _ = angel
    a._smart = SimpleNamespace(position=lambda: dict(status=False, data=None))
    with pytest.raises(RuntimeError, match="position read failed"):
        a.get_position()
    assert a.holds(SYMBOL, "LONG", 10) is False


def test_margin_is_quoted_in_barrels(angel):
    a, _, _ = angel
    seen = []
    a._smart.getMarginApi = lambda p: seen.append(p) or dict(status=True, data={"totalMarginRequired": 31000})
    assert a.margin(SYMBOL, 10) == 31000
    assert seen[0]["positions"][0]["qty"] == 10 and seen[0]["positions"][0]["token"] == TOKEN


def test_market_data_is_kite_and_limits_cross_the_touch(angel):
    a, _, _ = angel
    assert a.get_ltp(SYMBOL) == 9800
    assert a.limit(SYMBOL, buy=True) == 9803 and a.limit(SYMBOL, buy=False) == 9797
