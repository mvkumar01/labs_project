from datetime import datetime, timedelta
from pathlib import Path
import sys

import pandas as pd
import pytest


ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from labs.simulation.engine import EquityChargesModel, SimulationEngine
from labs.simulation.indicators import indicator_series, visible_bars
from labs.simulation.service import SimulationService


def candle(ts="2026-08-10T09:16:00", open_=100, high=105, low=95, close=102):
    return {"timestamp": ts, "open": open_, "high": high, "low": low, "close": close, "volume": 1000}


def ready_engine(price=100):
    engine = SimulationEngine()
    engine.configure(instrument="HDFCBANK", trade_date="2026-08-10")
    engine.on_candle(candle(close=price), 0)
    return engine


def test_market_order_executes_at_visible_price():
    engine = ready_engine(102)
    order = engine.submit_order(side="BUY", qty=10)
    assert order["status"] == "FILLED"
    assert order["filled_price"] == 102
    assert engine.snapshot()["positions"][0]["qty"] == 10


def test_buy_limit_executes_only_when_low_crosses():
    engine = ready_engine()
    order = engine.submit_order(side="BUY", qty=2, order_type="LIMIT", limit_price=96)
    engine.on_candle(candle(low=97), 1)
    assert engine._find_order(order["order_id"])["status"] == "OPEN"
    engine.on_candle(candle(open_=98, low=95), 2)
    assert engine._find_order(order["order_id"])["filled_price"] == 96


def test_sell_limit_executes_only_when_high_crosses():
    engine = ready_engine()
    order = engine.submit_order(side="SELL", qty=2, order_type="LIMIT", limit_price=104)
    engine.on_candle(candle(high=103), 1)
    assert engine._find_order(order["order_id"])["status"] == "OPEN"
    engine.on_candle(candle(open_=102, high=105), 2)
    assert engine._find_order(order["order_id"])["filled_price"] == 104


@pytest.mark.parametrize(
    "side,trigger,bar,expected",
    [
        ("BUY", 104, candle(open_=102, high=105), 104),
        ("SELL", 96, candle(open_=98, low=95), 96),
    ],
)
def test_stop_order_triggers(side, trigger, bar, expected):
    engine = ready_engine()
    order = engine.submit_order(side=side, qty=1, order_type="STOP", trigger_price=trigger)
    engine.on_candle(bar, 1)
    assert engine._find_order(order["order_id"])["filled_price"] == expected


def test_long_stop_loss_executes_and_closes_position():
    engine = ready_engine(100)
    engine.submit_order(side="BUY", qty=5, stop_loss=95, target=110)
    engine.on_candle(candle(open_=99, high=101, low=94, close=96), 1)
    assert engine.snapshot()["positions"] == []
    assert engine.state["trades"][-1]["exit_reason"] == "STOP_LOSS"


def test_target_executes_and_closes_position():
    engine = ready_engine(100)
    engine.submit_order(side="BUY", qty=5, stop_loss=95, target=110)
    engine.on_candle(candle(open_=102, high=111, low=99, close=109), 1)
    assert engine.state["trades"][-1]["exit_reason"] == "TARGET"


def test_oco_is_conservative_when_stop_and_target_share_candle():
    engine = ready_engine(100)
    engine.submit_order(side="BUY", qty=5, stop_loss=95, target=110)
    engine.on_candle(candle(open_=100, high=112, low=94, close=108), 1)
    assert len(engine.state["trades"]) == 1
    assert engine.state["trades"][0]["exit_reason"] == "STOP_LOSS"


def test_partial_exit_preserves_remaining_quantity_and_pnl():
    engine = ready_engine(100)
    engine.submit_order(side="BUY", qty=10)
    engine.state["current_price"] = 110
    trade = engine.exit_position("HDFCBANK", qty=4)
    assert trade["gross_pnl"] == 40
    assert engine.snapshot()["positions"][0]["qty"] == 6


def test_pending_orders_and_position_risk_can_be_modified():
    engine = ready_engine(100)
    pending = engine.submit_order(side="BUY", qty=2, order_type="LIMIT", limit_price=95)
    changed = engine.modify_order(pending["order_id"], qty=3, limit_price=96)
    assert changed["qty"] == 3
    assert changed["limit_price"] == 96
    with pytest.raises(ValueError):
        engine.modify_order(pending["order_id"], qty=0)

    engine.submit_order(side="BUY", qty=2)
    position = engine.modify_position("HDFCBANK", stop_loss=98, target=110)
    assert position["stop_loss"] == 98
    assert position["target"] == 110


def test_equity_charges_are_separated_and_positive():
    charges = EquityChargesModel().calculate(10_000, 11_000)
    assert charges["brokerage"] > 0
    assert charges["stt"] > 0
    assert charges["total"] == round(sum(charges[k] for k in ("brokerage", "stt", "exchange", "sebi", "stamp", "gst")), 2)


def sample_frame(count=75):
    index = pd.date_range("2026-08-10 09:15", periods=count, freq="1min")
    values = pd.Series(range(100, 100 + count), index=index, dtype=float)
    return pd.DataFrame({"open": values, "high": values + 2, "low": values - 2, "close": values + 1, "volume": 1000}, index=index)


def test_replay_progression_never_exposes_future_candles():
    frame = sample_frame(10)
    visible = visible_bars(frame, replay_index=2, timeframe="1m")
    assert len(visible) == 3
    assert visible.index.max() == frame.index[2]


def test_resampled_bars_are_completed_and_one_hour_is_market_anchored():
    frame = sample_frame(75)
    before_close = visible_bars(frame, replay_index=58, timeframe="1h")
    assert before_close.empty
    after_close = visible_bars(frame, replay_index=59, timeframe="1h")
    assert list(after_close.index) == [pd.Timestamp("2026-08-10 09:15")]


def test_adx_di_indicators_use_visible_data_only():
    frame = sample_frame(40)
    visible = visible_bars(frame, replay_index=19, timeframe="1m")
    series = indicator_series(visible, [{"name": "ADX", "params": {"period": 14}}])
    assert set(series) == {"0:ADX 14", "0:DI+", "0:DI-"}
    assert all(len(values) <= 20 for values in series.values())


def test_reset_clears_session_trading_state():
    engine = ready_engine(100)
    engine.submit_order(side="BUY", qty=1)
    engine.reset(instrument="HDFCBANK", trade_date="2026-08-10")
    assert engine.state["orders"] == []
    assert engine.state["positions"] == {}
    assert engine.state["trades"] == []
    assert engine.state["replay_index"] == -1


class MemoryStore:
    def __init__(self): self.states = {}
    def create(self, starting_capital):
        self.states["session"] = SimulationEngine.new_state(starting_capital)
        return "session", self.states["session"]
    def load(self, session_id): return self.states[session_id]
    def save(self, session_id, state): self.states[session_id] = state


class FrameProvider:
    def __init__(self, frame): self.frame = frame
    def load_day(self, instrument, trade_date): return self.frame
    def available_dates(self, instrument): return ["2026-08-10"]


def test_service_steps_one_candle_and_persists_cursor():
    store = MemoryStore(); service = SimulationService(store=store, provider=FrameProvider(sample_frame(5)))
    created = service.create_session(); sid = created["session_id"]
    service.configure(sid, {"instrument": "HDFCBANK", "trade_date": "2026-08-10"})
    started = service.start(sid)
    assert started["state"]["replay_index"] == 0
    stepped = service.step(sid, 1)
    assert stepped["state"]["replay_index"] == 1
    assert stepped["chart"]["visible_count_1m"] == 2
