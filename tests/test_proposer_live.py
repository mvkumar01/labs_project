"""SENSEX Proposer live path: engine port, predictor rules, BFO adapters, runner hand-off."""
from __future__ import annotations

from datetime import datetime, timedelta

import pandas as pd
import pytest

from live import live_executor as ex
from live import live_runner as lr
from live import live_service as svc
from live import proposer_runner as pr
from live.brokers.base import OrderResult, Position
from live.engine import proposer_engine as pe
from live.engine import proposer_predictor as pp
from storage import live_db
from storage.live_db import init_live_db

T0 = datetime(2026, 9, 21, 9, 22, 0)


def _p(x5="strong_bull", conf=0.6, regime="neutral", asof=T0):
    return {"regime": regime, "x5": x5, "x5_conf": conf, "x5_asof": asof.isoformat()}


# ═══════════════════════════════════════════════════════════════════ engine ══
def test_regime_licences_one_entry_then_the_gated_5class_drives():
    e = pe.ProposerEngine()
    sig = e.evaluate(T0, _p("chop", 0.3, "bearish"), pe.Position(), option_ltp=None, spot=74000)
    assert sig.action == "ENTER" and sig.side == "PUT" and sig.reason == "proposer_bearish"
    e.mark_entry(_p("chop", 0.3, "bearish"))
    assert e.regime_entry_used and e.entry_decisive
    later = T0 + timedelta(minutes=20)
    flat = pe.Position()
    e.evaluate(later, _p("chop", 0.3, "bearish", later), pe.Position(side="PE", entry_price=300, qty=20,
               entry_spot=74000), option_ltp=300, spot=74000)
    t2 = later + timedelta(minutes=10)
    assert e.evaluate(t2, _p("chop", 0.3, "bearish", t2), flat, option_ltp=None, spot=74000).action == "HOLD"
    t3 = t2 + timedelta(minutes=5)
    sig = e.evaluate(t3, _p("mild_bear", 0.5, "bearish", t3), flat, option_ltp=None, spot=74000)
    assert sig.action == "ENTER" and sig.reason == "proposer_5class_mild_bear"
    e.mark_entry(_p("mild_bear", 0.5, "bearish", t3))
    assert not e.entry_decisive                     # a 5-class entry takes the neutral width


def test_confidence_gate_and_fresh_print_gate():
    e = pe.ProposerEngine()
    assert e.evaluate(T0, _p("strong_bull", 0.45), pe.Position(), option_ltp=None, spot=1).action == "HOLD"
    later = T0 + timedelta(seconds=30)               # same print again: already consumed
    assert e.evaluate(later, _p("strong_bull", 0.45), pe.Position(), option_ltp=None, spot=1).action == "HOLD"
    t2 = T0 + timedelta(minutes=5)
    assert e.evaluate(t2, _p("strong_bull", 0.46, asof=t2), pe.Position(), option_ltp=None, spot=1).action == "ENTER"


def test_cooldown_after_an_exit_but_not_after_a_flip():
    e = pe.ProposerEngine(pe.ProposerParams(daily_target_pct=1.0))
    held = pe.Position(side="CE", entry_price=400, qty=20, entry_spot=74000)
    e.evaluate(T0, _p(), held, option_ltp=400, spot=74000)
    sig = e.evaluate(T0, _p(), held, option_ltp=270, spot=73900)
    assert sig.reason == "loss_floor"
    t1 = T0 + timedelta(minutes=1)
    assert e.evaluate(t1, _p(asof=t1), pe.Position(), option_ltp=None, spot=1).reason == "cooldown"
    # a flip: exits, consumes the flipping print, no cooldown for the next print
    e2 = pe.ProposerEngine(pe.ProposerParams(daily_target_pct=1.0))
    e2.evaluate(T0, _p(), held, option_ltp=400, spot=74000)
    sig = e2.evaluate(T0, _p("strong_bear", 0.6), held, option_ltp=395, spot=73990)
    assert sig.reason == "signal_flip"
    assert e2.evaluate(T0 + timedelta(seconds=5), _p("strong_bear", 0.6), pe.Position(),
                       option_ltp=None, spot=1).action == "HOLD"
    t2 = T0 + timedelta(minutes=5)
    assert e2.evaluate(t2, _p("strong_bear", 0.6, asof=t2), pe.Position(), option_ltp=None,
                       spot=1).action == "ENTER"


def test_strong_reversal_latches_the_regime_stale():
    e = pe.ProposerEngine()
    e.evaluate(T0, _p("strong_bear", 0.3, "bullish"), pe.Position(), option_ltp=None, spot=1)
    assert e.regime_stale


def test_spot_target_widths_and_day_target():
    e = pe.ProposerEngine()
    held = pe.Position(side="CE", entry_price=400, qty=20, entry_spot=74000)
    e.entry_decisive = False
    assert e.evaluate(T0, _p(), held, option_ltp=401, spot=74039).reason == "spot_target"
    e = pe.ProposerEngine()
    e.entry_decisive = True
    assert e.evaluate(T0, _p(), held, option_ltp=401, spot=74060).action == "HOLD"
    # day target: 2.5% of 400*20 = 200 rupees -> +10 premium points
    e = pe.ProposerEngine()
    assert e.evaluate(T0, _p(), held, option_ltp=410, spot=74010).reason == "daily_target"
    assert e.banked()
    e.evaluate(T0 + timedelta(seconds=2), _p(), pe.Position(), option_ltp=None, spot=1)  # next cycle: flat
    t1 = T0 + timedelta(minutes=10)
    assert e.evaluate(t1, _p(asof=t1), pe.Position(), option_ltp=None, spot=1).reason == "day_target_banked"


def test_bank_is_cleared_when_the_fill_lands_well_short():
    e = pe.ProposerEngine()
    held = pe.Position(side="CE", entry_price=400, qty=20, entry_spot=74000)
    e.evaluate(T0, _p(), held, option_ltp=410, spot=74010)
    e.set_book(day_realized=60, book_net=60)         # fill booked only Rs 60 of a Rs 200 target
    e.evaluate(T0 + timedelta(seconds=2), _p(), pe.Position(), option_ltp=None, spot=1)  # next cycle: flat
    t1 = T0 + timedelta(minutes=10)
    assert e.evaluate(t1, _p(asof=t1), pe.Position(), option_ltp=None, spot=1).action == "ENTER"


def test_itm_strike():
    assert pe.itm_strike(74226, "CALL") == (74000, "CE")
    assert pe.itm_strike(74226, "PUT") == (74400, "PE")


# ════════════════════════════════════════════════════════════════ predictor ══
def test_regime_from_gap():
    assert pp.regime_from_gap(74000, 74300).label == "bullish"
    assert pp.regime_from_gap(74000, 73700).label == "bearish"
    assert pp.regime_from_gap(74000, 74100).label == "neutral"
    assert pp.regime_from_gap(None, 74100).label == "neutral"


def test_x5_warmup_and_a_rising_tape():
    base = datetime(2026, 9, 21, 9, 15)
    prior = [(datetime(2026, 9, 18, 14, 0) + timedelta(minutes=i), 74000.0) for i in range(80)]
    today = [(base + timedelta(minutes=i), 74000.0 + 12 * i) for i in range(30)]
    assert pp.predict_x5(prior, today[:6], None, None) is None
    x = pp.predict_x5(prior, today, {"atm_pcr": 1.2}, pp.regime_from_gap(74000, 74000))
    assert x["x5"].endswith("bull") and x["components"]["p_up"] > 0.5
    assert pp.drift_state(x["micro"]) == "drift_up"


# ═════════════════════════════════════════════════════════════════ adapters ══
class FakeKite:
    EXCHANGE_NFO, EXCHANGE_BFO = "NFO", "BFO"
    VARIETY_REGULAR, TRANSACTION_TYPE_BUY, TRANSACTION_TYPE_SELL = "regular", "BUY", "SELL"
    PRODUCT_MIS, ORDER_TYPE_LIMIT, VALIDITY_IOC = "MIS", "LIMIT", "IOC"

    def ltp(self, key):
        return {key: {"last_price": 10.0}}

    def positions(self):
        return {"net": [
            {"tradingsymbol": "NIFTY26O0624400CE", "product": "MIS", "quantity": 65, "exchange": "NFO"},
            {"tradingsymbol": "SENSEX26O0872300PE", "product": "MIS", "quantity": 20, "exchange": "BFO"},
        ]}


def test_zerodha_bfo_segment(monkeypatch):
    from live.brokers import zerodha
    a = zerodha.ZerodhaAdapter(user_id="u", conn_id="u:zerodha", creds={})
    a._kite = FakeKite()
    assert a.get_position().symbol.startswith("NIFTY")          # default unchanged
    a.use_segment("BFO", "SENSEX")
    assert a.get_position().symbol == "SENSEX26O0872300PE"
    assert a.get_spot() == 10.0 and a.broker_symbol("SENSEX26O0872300PE") == "SENSEX26O0872300PE"
    sent = {}
    monkeypatch.setattr(zerodha, "_live_orders_enabled", lambda: True)
    monkeypatch.setattr(zerodha, "send_order", lambda adapter, op, params, key: sent.update(params) or "OID")
    a.place_order(side="PUT", symbol="SENSEX26O0872300PE", qty=20, price=300, idempotency_key="k")
    assert sent["exchange"] == "BFO" and sent["validity"] == "IOC"


def test_angel_bfo_segment_resolves_the_kite_symbol(monkeypatch):
    from live.brokers import angel
    a = angel.AngelAdapter(user_id="u", conn_id="u:angel", creds={"client_code": "C"})
    master = [
        {"token": "1", "symbol": "SENSEX26O0872300PE", "name": "SENSEX", "expiry": "08OCT2026",
         "strike": "7230000.0", "lotsize": "20", "instrumenttype": "OPTIDX", "exch_seg": "BFO"},
        {"token": "2", "symbol": "NIFTY06OCT2624400CE", "name": "NIFTY", "expiry": "06OCT2026",
         "strike": "2440000.0", "lotsize": "65", "instrumenttype": "OPTIDX", "exch_seg": "NFO"},
    ]
    monkeypatch.setattr(a, "_ensure_instrument_master", lambda: master)
    a.use_segment("BFO", "SENSEX")
    meta = a._resolve_symbol_meta("SENSEX26O0872300PE")
    assert meta == {"symbol": "SENSEX26O0872300PE", "token": "1", "lotsize": 20}
    assert a._EXCHANGE == "BFO" and angel.AngelAdapter._EXCHANGE == "NFO"


# ════════════════════════════════════════════════════════ gates and hand-off ══
USER, CONN = "user-1", "user-1:zerodha"


@pytest.fixture
def db(tmp_path, monkeypatch):
    monkeypatch.setenv("LIVE_ORDER_PROXY_URL", "http://static.test:1234")
    monkeypatch.setattr(live_db, "LIVE_DB_PATH", tmp_path / "live.db")
    init_live_db()
    pr.ensure_schema()
    pr.ensure_regime_schema()
    svc.upsert_connection(USER, CONN, broker="zerodha", account_label="T", account_ref="zerodha:T",
                          status="connected")
    for k, v in (("lots", "1"), ("daily_loss_cap", "50000"), ("strategy_version", "proposer_dt25"),
                 ("decision_engine", "proposer"), ("kill_switch", "0")):
        svc.set_config(USER, CONN, k, v)
    monkeypatch.setattr(pr, "notify_telegram", lambda *a, **k: None)
    monkeypatch.setattr(lr, "notify_telegram", lambda *a, **k: None)
    return tmp_path


def test_nifty_runner_never_claims_a_proposer_connection(db):
    assert lr.publish_runner_heartbeat(USER, CONN, "nifty-task") is False
    assert svc.get_config(USER, CONN, "runner_owner") in ("", None)
    assert pr.publish_heartbeat(USER, CONN, "prop-task") is True
    assert svc.get_config(USER, CONN, "runner_decision_abi") == ex.PROPOSER_DECISION_ABI
    gate = ex.gate_runner_decision_abi(USER, CONN)
    assert gate.passed, gate.detail
    svc.set_config(USER, CONN, "strategy_version", "v2.14")      # NIFTY strategy expects its own ABI
    assert not ex.gate_runner_decision_abi(USER, CONN).passed


class FakeAdapter:
    def __init__(self, *, user_id, conn_id, creds):
        self.orders, self.position = [], Position(symbol=None, qty=0, side=None)

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
        return {"status": "COMPLETE", "average_price": self.orders[-1]["price"]}

    def place_order(self, *, side, symbol, qty, price, idempotency_key):
        self.orders.append({"action": "BUY", "symbol": symbol, "qty": qty, "price": price})
        self.position = Position(symbol=symbol, qty=qty, side=side)
        return OrderResult("OID1", "PLACED", None, {})

    def exit_all(self, *, symbol, qty, reason, idempotency_key, price=None):
        self.orders.append({"action": "SELL", "symbol": symbol, "qty": qty, "price": price})
        self.position = Position(symbol=None, qty=0, side=None)
        return OrderResult("OID2", "PLACED", None, {})


class FakeMarket:
    def __init__(self):
        self.px, self.sp = 400.0, 74226.0

    def spot(self):
        return self.sp

    def quote(self, symbol):
        return {"ltp": self.px, "bid": self.px - 0.5, "ask": self.px + 0.5}


class FakeFeed:
    def __init__(self, snap):
        self.snap = snap

    def snapshot(self):
        return dict(self.snap)

    def step(self, now):
        return None


CHAIN = pd.DataFrame([{"timestamp": "x", "tradingsymbol": "SENSEX26O0874000CE", "strike": 74000,
                       "option_type": "CE", "expiry": "26O08", "oi": 1, "spot": 74226}])


def _cycle(feed, market, now, adapters_box, ctx):
    return pr.process_connection(USER, CONN, ctx=ctx, feed=feed, market=market, now=now,
                                 adapter_factory=lambda **kw: adapters_box.setdefault("a", FakeAdapter(**kw)))


def test_dry_run_enters_banks_the_day_target_and_stops(db, monkeypatch):
    ex.set_mode(USER, CONN, ex.Mode.DRY_RUN)
    monkeypatch.setattr(pr, "option_chain", lambda day: CHAIN)
    now = datetime(2026, 10, 5, 9, 22, 30)
    feed, market, box = FakeFeed(_p("strong_bull", 0.6, "neutral", datetime(2026, 10, 5, 9, 22))), FakeMarket(), {}
    ctx = {"task_id": "prop-task", "adapters": {}, "reconciled": set(), "conns": {}}
    assert _cycle(feed, market, now, box, ctx) is True
    st = svc.get_trade_state(USER, CONN)
    assert st["position"] == "OPEN" and st["symbol"] == "SENSEX26O0874000CE"
    assert st["qty"] == 20 and int(st["virtual"]) == 1 and st["side"] == "CALL"
    assert box["a"].orders == []                                  # DRY_RUN never calls the broker
    market.px = 411.0                                             # +11 x 20 = 220 > 2.5% of ~8,010
    assert _cycle(feed, market, now + timedelta(seconds=5), box, ctx) is False
    assert svc.get_trade_state(USER, CONN)["position"] == "NONE"
    day = svc.get_day_pnl(USER, CONN)
    assert day["trade_count_dry"] == 1 and day["trade_count"] == 0
    assert _cycle(feed, market, now + timedelta(seconds=7), box, ctx) is False   # flat cycle
    feed.snap = _p("strong_bull", 0.7, "neutral", datetime(2026, 10, 5, 9, 32))
    eng = ctx["conns"][CONN].engine
    assert eng.banked()
    assert eng.cooldown_until < now + timedelta(minutes=10)
    assert _cycle(feed, market, now + timedelta(minutes=10), box, ctx) is False
    assert svc.get_trade_state(USER, CONN)["position"] == "NONE"   # banked: no more entries


def test_live_entry_goes_through_the_gates_to_a_bfo_order(db, monkeypatch):
    ex.set_mode(USER, CONN, ex.Mode.DRY_RUN)
    ex.set_mode(USER, CONN, ex.Mode.LIVE_ARMED)
    svc.set_config(USER, CONN, "armed", "1")
    monkeypatch.setattr(pr, "option_chain", lambda day: CHAIN)
    monkeypatch.setattr(ex, "evaluate_all", lambda *a, **k: [ex.GateResult("all", True, "")])
    monkeypatch.setattr(ex, "_refresh_order_result", lambda adapter, result: OrderResult(
        result.broker_order_id, "COMPLETE", adapter.orders[-1]["price"], result.raw))
    monkeypatch.setattr(ex, "gate_static_order_proxy", lambda *a, **k: ex.GateResult("p", True, ""))
    now = datetime(2026, 10, 5, 9, 22, 30)
    feed, market, box = FakeFeed(_p("strong_bull", 0.6, "neutral", datetime(2026, 10, 5, 9, 22))), FakeMarket(), {}
    ctx = {"task_id": "prop-task", "adapters": {}, "reconciled": set(), "conns": {}}
    _cycle(feed, market, now, box, ctx)
    a = box["a"]
    assert a.segment == "BFO"
    assert a.orders[0]["action"] == "BUY" and a.orders[0]["symbol"] == "SENSEX26O0874000CE"
    assert a.orders[0]["qty"] == 20 and a.orders[0]["price"] >= 400.5
    st = svc.get_trade_state(USER, CONN)
    assert st["position"] == "OPEN" and int(st["virtual"]) == 0
    # 15:25 flat
    _cycle(feed, market, datetime(2026, 10, 5, 15, 25, 1), box, ctx)
    assert a.orders[-1]["action"] == "SELL" and svc.get_trade_state(USER, CONN)["position"] == "NONE"
    assert svc.get_day_pnl(USER, CONN)["trade_count"] == 1


# ═══════════════════════════════════════════════════════ LLM daily regime ══
from live.engine import proposer_regime_llm as rl


def test_llm_regime_parses_the_model_json_and_maps_to_a_regime():
    items = [{"title": "Rupee slides", "category": "INDIA_MARKETS", "published": None}]
    seen = {}

    def call(system, user):
        seen.update(system=system, user=user)
        return 'text {"regime":"Bearish","p_bull":0.2,"p_bear":0.5,"p_chop":0.3,"confidence":0.6,"trigger":"rupee"} end'
    out = rl.daily_regime(items, {"n": 60, "bull": 0.25, "bear": 0.43, "chop": 0.32}, call=call)
    assert out["regime"] == "bearish"
    assert "REALIZED BASE RATES" in seen["user"] and "Rupee slides" in seen["user"]
    r = rl.to_regime(out)
    assert (r.label, r.p_bear, r.source) == ("bearish", 0.5, "deepseek")
    with pytest.raises(ValueError):
        rl.daily_regime(items, None, call=lambda s, u: '{"regime":"moon"}')
    br = rl.base_rates_from_days([(100, 101), (100, 99), (100, 100.05)])
    assert br["n"] == 3 and br["bull"] == br["bear"] == br["chop"] == pytest.approx(1 / 3)


def test_regime_job_runs_once_in_the_morning_window(db, monkeypatch):
    monkeypatch.setenv("DEEPSEEK_API_KEY", "test")
    calls = []
    job = pr.LlmRegimeJob(fetch=lambda now: ([{"title": "x"}], []),
                          score=lambda items, br: calls.append(1) or {"regime": "bullish", "p_bull": 0.5,
                                                                      "p_bear": 0.2, "p_chop": 0.3,
                                                                      "confidence": 0.6},
                          bars_fn=lambda d: [], spawn=False)
    job.step(datetime(2026, 10, 5, 8, 59))
    assert calls == []
    job.step(datetime(2026, 10, 5, 9, 0, 5))
    job.step(datetime(2026, 10, 5, 9, 5))
    assert calls == [1] and pr.get_regime("2026-10-05", "deepseek")["regime"] == "bullish"
    job2 = pr.LlmRegimeJob(fetch=lambda now: (_ for _ in ()).throw(OSError("down")),
                           score=lambda i, b: {}, bars_fn=lambda d: [], spawn=False)
    job2.step(datetime(2026, 10, 6, 9, 0))                     # failure: logged, nothing stored
    assert pr.get_regime("2026-10-06", "deepseek") is None and not job2.running


def test_feed_regime_source_gap_by_default_deepseek_only_when_selected(db, monkeypatch):
    base = datetime(2026, 10, 5, 9, 15)
    today = [(base + timedelta(minutes=i), 74000.0 + i) for i in range(10)]
    prior = [(datetime(2026, 10, 1, 14, 0) + timedelta(minutes=i), 74000.0) for i in range(80)]

    def bars(day):
        return today if day == base.date() else (prior if day.isoformat() == "2026-10-01" else [])
    pr.save_regime("2026-10-05", "deepseek", {"regime": "bearish", "p_bull": 0.2, "p_bear": 0.5,
                                              "p_chop": 0.3, "confidence": 0.6})
    now = datetime(2026, 10, 5, 9, 25, 3)
    monkeypatch.delenv("PROPOSER_REGIME_SOURCE", raising=False)
    feed = pr.PredictorFeed(bars_fn=bars, chain_fn=lambda d: pd.DataFrame())
    assert feed.step(now)["regime"] == "neutral"              # gap rule (flat open) drives
    assert pr.get_regime("2026-10-05", "gap_rule")["regime"] == "neutral"
    monkeypatch.setenv("PROPOSER_REGIME_SOURCE", "deepseek")
    feed = pr.PredictorFeed(bars_fn=bars, chain_fn=lambda d: pd.DataFrame())
    feed.day = None
    p = feed.step(now + timedelta(minutes=10))
    assert p["regime"] == "bearish" and feed.regime.source == "deepseek"
