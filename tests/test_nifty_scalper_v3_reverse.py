"""Exercise the reverse preset through the broker-free DRY_RUN path."""

from datetime import timedelta
import json

import pandas as pd
import pytest

from labs.ui import live_routes
from live import live_service as svc, proposer_runner as runner
from live.engine import proposer_engine as engine
from tests.test_nifty_scalper_v1 import NOW, cycle, setup_nifty
from tests.test_proposer_live import CONN, USER, _p, db  # noqa: F401


VERSION = engine.STRATEGY_VERSION_NIFTY_SCALPER_V3_REVERSE
CALL, PUT = "NIFTY26OCT22950CE", "NIFTY26OCT23050PE"


def setup_reverse(monkeypatch, signal_side="CALL"):
    context, market, feed = setup_nifty(monkeypatch)
    svc.set_config(USER, CONN, "strategy_version", VERSION)
    chain = pd.DataFrame([
        {"tradingsymbol": symbol, "strike": strike, "option_type": symbol[-2:],
         "expiry": "26OCT", "ltp": 190.0}
        for symbol, strike in ((CALL, 22950), (PUT, 23050))
    ])
    monkeypatch.setattr(runner, "option_chain", lambda *args: chain)
    prices = {CALL: 400.0, PUT: 400.0}

    def quote(symbol, profile):
        mid = prices[symbol]
        return {"ltp": mid, "bid": mid - 0.5, "ask": mid + 0.5}

    market.quote = quote
    if signal_side == "PUT":
        feed.snap = _p(x5="strong_bear", asof=NOW.replace(second=0))
        context["bars"] = runner.SessionBars(bars_fn=lambda day: [
            (NOW.replace(minute=15, second=0), 74000),
            (NOW.replace(minute=21, second=0), 73800),
        ])
    return context, market, feed, prices


def trade_reason():
    with runner.get_live_conn() as conn:
        return conn.execute("SELECT reason FROM live_trades WHERE strategy=?", (VERSION,)).fetchone()[0]


def test_preset_offered_and_uses_v3_clock():
    assert live_routes.offered_presets(None)["nifty_scalper_v3_reverse"].startswith("Nifty Scalper V3 Reverse")
    assert runner.instrument_for(VERSION) == runner.NIFTY_I
    assert runner.print_clock_for(VERSION) == runner.PRINT_CLOCK_3MIN
    assert engine.params_for(VERSION) == engine.params_for(engine.STRATEGY_VERSION_NIFTY_SCALPER_V3)


@pytest.mark.parametrize("signal,held,symbol,move,reason", [
    ("CALL", "PUT", PUT, -47, "spot_floor"),
    ("PUT", "CALL", CALL, 47, "spot_floor"),
    ("CALL", "PUT", PUT, 19, "spot_target"),
])
def test_opposite_contract_preserves_original_spot_exit(db, monkeypatch, signal, held, symbol, move, reason):
    ctx, market, feed, _ = setup_reverse(monkeypatch, signal)
    assert cycle(ctx, market, feed)
    state = svc.get_trade_state(USER, CONN)
    assert (state["side"], state["symbol"]) == (held, symbol)
    ctx["conns"].clear()
    market.nifty += move
    assert not cycle(ctx, market, feed, NOW + timedelta(seconds=2))
    assert trade_reason() == reason
    assert next(iter(ctx["adapters"].values())).orders == []


def test_original_premium_controls_day_target(db, monkeypatch):
    ctx, market, feed, prices = setup_reverse(monkeypatch)
    assert cycle(ctx, market, feed)
    prices[PUT] = 420
    assert cycle(ctx, market, feed, NOW + timedelta(seconds=1))
    prices[CALL] = 410
    assert not cycle(ctx, market, feed, NOW + timedelta(seconds=2))
    assert trade_reason() == "daily_target"


def test_reverse_loss_and_retired_stop_latch_do_not_drive_exits(db, monkeypatch):
    ctx, market, feed, prices = setup_reverse(monkeypatch)
    assert cycle(ctx, market, feed)
    key = "proposer_v3_reverse_shadow_dry"
    saved = json.loads(svc.get_config(USER, CONN, key))
    saved["stopped"] = True
    svc.set_config(USER, CONN, key, json.dumps(saved))
    prices[PUT] = 100
    feed.snap = {}
    ctx["conns"].clear()
    assert cycle(ctx, market, feed, NOW + timedelta(seconds=2))
    assert "stopped" not in json.loads(svc.get_config(USER, CONN, key))
    market.nifty -= 47
    feed.snap = _p(asof=NOW.replace(second=0))
    assert not cycle(ctx, market, feed, NOW + timedelta(seconds=3))
    assert trade_reason() == "spot_floor"
    assert svc.get_trade_state(USER, CONN)["position"] == "NONE"
    assert next(iter(ctx["adapters"].values())).orders == []
