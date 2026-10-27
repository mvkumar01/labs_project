"""NIFTY execution with shared SENSEX prints; all orders use fakes or DRY_RUN."""
from dataclasses import asdict
from datetime import date, datetime, timedelta

import pandas as pd
import pytest

from labs.engine import proposer_nifty_tracker as nt, proposer_px_tracker as tr
from live import live_executor as ex, live_service as svc, proposer_runner as pr
from live.engine import proposer_engine as pe, proposer_strike as ps
from tests.test_proposer_live import USER, CONN, FakeAdapter, FakeFeed, FakeMarket, _p, db  # noqa: F401
from tests.test_configure_strategy import client, CONN_ID, USER_ID, CSRF_TOKEN, _set_strategy  # noqa: F401

VERSION = pe.STRATEGY_VERSION_NIFTY_SCALPER_V1
NOW = datetime(2026, 10, 21, 9, 22, 30)


def test_params_equal_the_reference_paper_book_field_by_field():
    assert VERSION in pe.KNOWN_VERSIONS and VERSION in pe.DAY_TARGET_VERSIONS
    p = pe.params_for(VERSION)
    assert p.daily_target_pct == .015 and p.with_day_pts == 75
    assert p.confirm_bank_tolerance == .25
    assert pe.params_for(pe.STRATEGY_VERSION_V5).confirm_bank_tolerance == .10
    assert asdict(pe.scaled_for_nifty(p, .31)) == asdict(nt._params("2026-10-21", tr.NIFTY_V5, .31))
    assert pe.params_for(VERSION, .02).daily_target_pct == .02
    assert ex.PROPOSER_DECISION_ABI == "proposer-dt25-live-v13"


@pytest.mark.parametrize("ratio", [0, -1, float("inf"), float("nan")])
def test_invalid_ratio_fails_closed(ratio):
    with pytest.raises(ValueError):
        pe.scaled_for_nifty(pe.params_for(VERSION), ratio)


@pytest.mark.parametrize("day,codes,expected", [
    (date(2026, 10, 16), ["26O19", "26OCT"], "26O19"),
    (date(2026, 10, 19), ["26O19", "26OCT"], "26OCT"),
    (date(2026, 10, 20), ["26O19", "26OCT"], "26OCT"),
    (date(2026, 10, 27), ["26OCT", "26N03"], "26N03"),
    (date(2026, 10, 19), ["26O19"], None),
    (date(2026, 10, 28), ["26OCT"], None),
])
def test_nifty_own_expiry_and_monthly_codes(day, codes, expected):
    assert ps.nifty_expiry_code(codes, day) == expected


@pytest.mark.parametrize("side,typ,sign", [("CALL", "CE", -1), ("PUT", "PE", 1)])
def test_shared_strike_picker_preserves_reference_rule(side, typ, sign):
    chain = pd.DataFrame([{"strike": 23000 + sign * d, "option_type": typ, "ltp": px,
                           "tradingsymbol": f"NIFTY26OCT{23000 + sign * d}{typ}"}
                          for d, px in ((50, 190), (100, 225), (150, 262))])
    exp, today = date(2026, 10, 27), date(2026, 10, 21)
    symbol, strike, picked_type, info = ps.delta_itm_symbol_nifty(chain, side, 23010, today, exp)
    assert nt.pick_strike(chain, typ, 23010, exp, today) == (strike, symbol)
    candidates = [(23000 + sign * d, abs(ps.bs_delta(23010, 23000 + sign * d,
                   ps.years_to_expiry(exp, today), ps.implied_vol(px, 23010, 23000 + sign * d,
                   ps.years_to_expiry(exp, today), typ), typ)))
                  for d, px in ((50, 190), (100, 225), (150, 262))]
    want = next((k for k, delta in candidates if delta >= .60), max(candidates, key=lambda x: x[1])[0])
    assert strike == want and picked_type == typ
    assert info["depth"] in (50, 100, 150)


def test_ratio_uses_0915_opens_and_refreshes_per_session(monkeypatch):
    calls = []
    def candle(day, token):
        calls.append((day, token))
        return {"open": 74000 if token == 265 else (22940 if day == NOW.date() else 23680), "close": 99999}
    monkeypatch.setattr(pr, "broker_open_candle", candle)
    cache = {}
    assert pr.opening_ratio(NOW.replace(hour=9, minute=16, second=9), cache) is None
    assert pr.opening_ratio(NOW, cache) == .31
    assert pr.opening_ratio(NOW + timedelta(seconds=1), cache) == .31
    assert len(calls) == 2
    assert pr.opening_ratio(NOW + timedelta(days=1), cache) == .32
    assert len(calls) == 4


def test_collector_ratio_fallback_and_missing_open(monkeypatch, tmp_path):
    monkeypatch.setattr(pr, "DATA_DIR", tmp_path)
    monkeypatch.setattr(pr, "broker_open_candle", lambda *a: None)
    for p, value in ((pr.NIFTY_I, 22940), (pr.SENSEX_I, 74000)):
        pd.DataFrame([{"timestamp": "2026-10-21T09:15:00", "open": value},
                      {"timestamp": "2026-10-21T09:16:00", "open": value + 1000}]).to_csv(
                          tmp_path / f"2026-10-21_{p.underlying}_spot_1min.csv", index=False)
    assert pr.opening_ratio(NOW, {}) == .31
    assert pr.opening_ratio(NOW + timedelta(days=1), {}) is None


def test_kite_market_batches_spots_and_prices_nfo(monkeypatch):
    seen = []
    class Kite:
        def ltp(self, keys):
            seen.append(keys)
            return {"BSE:SENSEX": {"last_price": 74000}, "NSE:NIFTY 50": {"last_price": 23000}}
        def quote(self, key):
            seen.append(key)
            return {key: {"last_price": 200, "depth": {"buy": [{"price": 199}], "sell": [{"price": 201}]}}}
    m = pr.KiteMarket()
    monkeypatch.setattr(m, "_kite", lambda: Kite())
    assert m.spots() == {"SENSEX": 74000, "NIFTY": 23000}
    assert m.quote("NIFTY26OCT22950CE", pr.NIFTY_I)["bid"] == 199
    assert seen == [["BSE:SENSEX", "NSE:NIFTY 50"], "NFO:NIFTY26OCT22950CE"]


@pytest.mark.parametrize("kite_symbol,angel_symbol,expiry", [
    ("NIFTY26OCT23000CE", "NIFTY27OCT2623000CE", "27OCT2026"),
    ("NIFTY26N0323000CE", "NIFTY03NOV2623000CE", "03NOV2026"),
    ("NIFTY26O1923000CE", "NIFTY19OCT2623000CE", "19OCT2026"),
])
def test_angel_nfo_resolves_monthly_weekly_and_shifted_expiry(monkeypatch, kite_symbol, angel_symbol, expiry):
    from live.brokers.angel import AngelAdapter
    adapter = AngelAdapter(user_id="test", conn_id="test:angel", creds={})
    adapter._keeps_rows = False
    adapter.use_segment("NFO", "NIFTY")
    monkeypatch.setattr(adapter, "_ensure_instrument_master", lambda: [
        {"symbol": angel_symbol, "name": "NIFTY", "token": "123", "expiry": expiry,
         "strike": "2300000", "lotsize": "65", "instrumenttype": "OPTIDX", "exch_seg": "NFO"}])
    assert adapter.broker_symbol(kite_symbol) == angel_symbol


def test_chain_chooses_next_expiry_on_holiday_shift(monkeypatch, tmp_path):
    monkeypatch.setattr(pr, "SHARED_LIVE_DIR", tmp_path)
    day = date(2026, 10, 19)
    folder = tmp_path / day.isoformat()
    folder.mkdir()
    rows = [{"timestamp": "2026-10-19 09:22:00", "tradingsymbol": f"NIFTY{code}22950CE",
             "strike": 22950, "option_type": "CE", "expiry": code, "ltp": 190, "oi": 10, "spot": 23000}
            for code in ("26O19", "26OCT")]
    pd.DataFrame(rows).to_csv(folder / "NIFTY_options_1min.csv", index=False)
    assert pr.option_chain(day, pr.NIFTY_I).expiry.unique().tolist() == ["26OCT"]


class NiftyMarket(FakeMarket):
    def __init__(self):
        super().__init__()
        self.nifty = 23010.
        self.quoted = []
    def spots(self):
        return {"SENSEX": self.sp, "NIFTY": self.nifty}
    def quote(self, symbol, profile=pr.SENSEX_I):
        self.quoted.append((symbol, profile.segment))
        return super().quote(symbol)


def setup_nifty(monkeypatch, lots="1", ratio=.31):
    svc.set_config(USER, CONN, "strategy_version", VERSION)
    svc.set_config(USER, CONN, "lots", lots)
    ex.set_mode(USER, CONN, ex.Mode.DRY_RUN)
    monkeypatch.setattr(pr, "opening_ratio", lambda *a: ratio)
    chain = pd.DataFrame([{"tradingsymbol": "NIFTY26OCT22950CE", "strike": 22950,
                            "option_type": "CE", "expiry": "26OCT", "ltp": 190.}])
    monkeypatch.setattr(pr, "option_chain", lambda *a: chain)
    ctx = {"task_id": "scalper-test", "adapters": {}, "reconciled": set(), "conns": {},
           "bars": pr.SessionBars(bars_fn=lambda day: [(NOW.replace(minute=15, second=0), 74000),
                                                       (NOW.replace(minute=21, second=0), 74200)])}
    market, feed = NiftyMarket(), FakeFeed(_p(asof=NOW.replace(second=0)))
    return ctx, market, feed


def cycle(ctx, market, feed, now=NOW, factory=FakeAdapter):
    return pr.process_connection(USER, CONN, ctx=ctx, market=market, feed=feed, now=now,
                                 adapter_factory=factory)


@pytest.mark.parametrize("lots,qty", [("1", 65), ("2", 130), ("100", 1755)])
def test_nifty_entry_size_spot_and_no_broker_order(db, monkeypatch, lots, qty):  # noqa: F811
    ctx, market, feed = setup_nifty(monkeypatch, lots)
    assert cycle(ctx, market, feed)
    state = svc.get_trade_state(USER, CONN)
    assert state["qty"] == qty and state["entry_spot"] == market.nifty
    adapter = next(iter(ctx["adapters"].values()))
    assert adapter.segment == "NFO" and adapter.orders == []
    assert market.quoted[-1][1] == "NFO"


@pytest.mark.parametrize("move,reason", [(-47, "spot_floor"), (19, "spot_target")])
def test_exit_uses_nifty_spot_while_sensex_stays_still(db, monkeypatch, move, reason):  # noqa: F811
    ctx, market, feed = setup_nifty(monkeypatch)
    assert cycle(ctx, market, feed)
    market.nifty += move
    assert not cycle(ctx, market, feed, NOW + timedelta(seconds=2))
    assert svc.get_trade_state(USER, CONN)["position"] == "NONE"
    with pr.get_live_conn() as conn:
        row = conn.execute("SELECT reason, strategy FROM live_trades").fetchone()
    assert tuple(row) == (reason, VERSION)


def test_sensex_filter_is_unscaled_and_missing_ratio_blocks_entry(db, monkeypatch):  # noqa: F811
    ctx, market, feed = setup_nifty(monkeypatch, ratio=None)
    assert not cycle(ctx, market, feed)
    assert svc.get_trade_state(USER, CONN)["position"] == "NONE"
    monkeypatch.setattr(pr, "opening_ratio", lambda *a: .31)
    ctx["bars"] = pr.SessionBars(bars_fn=lambda day: [(NOW.replace(minute=15, second=0), 74000),
                                                      (NOW.replace(minute=21, second=0), 74050)])
    market.nifty += 500
    assert not cycle(ctx, market, feed, NOW + timedelta(seconds=2))
    assert svc.get_trade_state(USER, CONN)["position"] == "NONE"


def test_restart_restores_ratio_and_angel_quote_symbol(db, monkeypatch):  # noqa: F811
    ctx, market, feed = setup_nifty(monkeypatch)
    class AngelStyle(FakeAdapter):
        def broker_symbol(self, s):
            return "NIFTY27OCT2622950CE"
    assert cycle(ctx, market, feed, factory=AngelStyle)
    assert svc.get_trade_state(USER, CONN)["symbol"] == "NIFTY27OCT2622950CE"
    ctx["conns"].clear()
    monkeypatch.setattr(pr, "opening_ratio", lambda *a: None)
    market.nifty -= 47
    assert not cycle(ctx, market, feed, NOW + timedelta(seconds=2), factory=AngelStyle)
    assert market.quoted[-1] == ("NIFTY26OCT22950CE", "NFO")


def test_connection_rebuilds_scaled_exits_on_new_session(db, monkeypatch):  # noqa: F811
    ctx, market, feed = setup_nifty(monkeypatch)
    assert cycle(ctx, market, feed)
    old = ctx["conns"][CONN]
    svc.reset_trade_state(USER, CONN)
    tomorrow = NOW + timedelta(days=1)
    monkeypatch.setattr(pr, "opening_ratio", lambda *a: .32)
    feed.snap = _p(asof=tomorrow.replace(second=0))
    cycle(ctx, market, feed, tomorrow)
    new = ctx["conns"][CONN]
    assert new is not old and new.day == tomorrow.date().isoformat()
    assert new.engine.params.spot_floor_pts == pytest.approx(150 * .32)


def test_adapter_switch_repoints_only_this_connection(db, monkeypatch):  # noqa: F811
    adapters = {}
    first = pr._adapter(USER, CONN, adapters, FakeAdapter)
    assert first.segment == "BFO"
    svc.set_config(USER, CONN, "strategy_version", VERSION)
    again = pr._adapter(USER, CONN, adapters, FakeAdapter)
    assert again is first and again.segment == "NFO"
    svc.set_config(USER, CONN, "strategy_version", pe.STRATEGY_VERSION_V5)
    assert pr._adapter(USER, CONN, adapters, FakeAdapter).segment == "BFO"


def test_sensex_and_nifty_connections_do_not_cross_talk(db, monkeypatch):  # noqa: F811
    ctx, market, feed = setup_nifty(monkeypatch)
    assert cycle(ctx, market, feed)
    other = USER + ":angel"
    svc.upsert_connection(USER, other, broker="angel", account_label="Other", account_ref="angel:other",
                          status="connected")
    for k, v in (("strategy_version", pe.STRATEGY_VERSION), ("decision_engine", "proposer"),
                 ("lots", "1"), ("kill_switch", "0"), ("daily_loss_cap", "50000")):
        svc.set_config(USER, other, k, v)
    ex.set_mode(USER, other, ex.Mode.DRY_RUN)
    from tests.test_proposer_live import CHAIN
    monkeypatch.setattr(pr, "option_chain", lambda *a: CHAIN)
    class OtherAdapter(FakeAdapter):
        def account_ref(self):
            return "angel:other"
    assert pr.process_connection(USER, other, ctx=ctx, market=market, feed=feed, now=NOW,
                                  adapter_factory=OtherAdapter)
    assert svc.get_trade_state(USER, other)["qty"] == 20
    assert svc.get_trade_state(USER, other)["entry_spot"] == market.sp
    assert svc.get_trade_state(USER, CONN)["qty"] == 65
    assert {a.segment for a in ctx["adapters"].values()} == {"NFO", "BFO"}


def test_approved_bank_tolerance_reduces_reentry_only_for_nifty():
    pos = pe.Position("CE", 400, 23010, 65)
    for version, banked in ((VERSION, True), (pe.STRATEGY_VERSION_V5, False)):
        eng = pe.ProposerEngine(pe.params_for(version))
        snap = _p(asof=NOW)
        target_ltp = 400 * (1 + eng.params.daily_target_pct)
        assert eng.evaluate(NOW, snap, pos, option_ltp=target_ltp + .01,
                            spot=23010).reason == "daily_target"
        # Fill books 80% of the threshold: accepted by 25% tolerance, rejected by 10%.
        gross = .8 * 400 * 65 * eng.params.daily_target_pct
        eng.set_book(day_realized=gross, book_net=gross)
        eng.evaluate(NOW + timedelta(seconds=2), snap, pe.Position(), option_ltp=None, spot=23010)
        assert eng.banked() == banked


def post_config(client, **extra):
    return client.post("/live/configure", data={"csrf_token": CSRF_TOKEN, "lots": "1",
        "daily_loss_cap": "3000", "strategy": "nifty_scalper_v1", **extra})


def test_configure_default_custom_target_and_old_abi(client):  # noqa: F811
    _set_strategy("proposer", pe.STRATEGY_VERSION_V5)
    svc.set_config(USER_ID, CONN_ID, "runner_decision_abi", "proposer-dt25-live-v10")
    assert "earlier release" in post_config(client).get_data(as_text=True)
    assert svc.get_config(USER_ID, CONN_ID, "strategy_version") == pe.STRATEGY_VERSION_V5
    svc.set_config(USER_ID, CONN_ID, "runner_decision_abi", ex.PROPOSER_DECISION_ABI)
    assert post_config(client).status_code == 302
    assert svc.get_config(USER_ID, CONN_ID, "strategy_version") == VERSION
    assert svc.get_config(USER_ID, CONN_ID, "proposer_day_target_pct") == "1.5"
    page = client.get("/live/configure").get_data(as_text=True)
    assert 'value="nifty_scalper_v1" selected' in page and "only 1.5% has been backtested on NIFTY" in page
    assert post_config(client, day_target="other", day_target_custom="2.1").status_code == 302
    assert svc.get_config(USER_ID, CONN_ID, "proposer_day_target_pct") == "2.1"


def test_configure_refuses_open_position(client):  # noqa: F811
    _set_strategy("proposer", pe.STRATEGY_VERSION_V5)
    state = svc.get_trade_state(USER_ID, CONN_ID)
    state.update(position="OPEN", symbol="SENSEX26O2274000CE", side="CALL", qty=20)
    svc.save_trade_state(USER_ID, CONN_ID, state)
    assert "open position" in post_config(client).get_data(as_text=True)
    assert svc.get_config(USER_ID, CONN_ID, "strategy_version") == pe.STRATEGY_VERSION_V5
