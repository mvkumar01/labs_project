"""Application service coordinating persisted state and replay market data."""
from __future__ import annotations

from copy import deepcopy

from labs.simulation.config import INSTRUMENTS, STARTING_CAPITAL, instrument_list
from labs.simulation.engine import FINAL_ORDER_STATUSES, SimulationEngine
from labs.simulation.indicators import chart_payload, indicator_series, visible_bars
from labs.simulation.market_data import CompositeMarketDataProvider
from labs.simulation.storage import SimulationStore


class SimulationService:
    def __init__(self, store=None, provider=None):
        self.store = store or SimulationStore()
        self.provider = provider or CompositeMarketDataProvider()

    def create_session(self, starting_capital=STARTING_CAPITAL) -> dict:
        session_id, state = self.store.create(float(starting_capital))
        return {"session_id": session_id, "state": SimulationEngine(state).snapshot()}

    def get(self, session_id: str, *, include_chart=True) -> dict:
        engine = SimulationEngine(self.store.load(session_id))
        return self._response(session_id, engine, include_chart=include_chart)

    def configure(self, session_id: str, payload: dict) -> dict:
        engine = SimulationEngine(self.store.load(session_id))
        instrument = payload.get("instrument", engine.state["instrument"])
        if instrument not in INSTRUMENTS:
            raise ValueError("Unsupported instrument")
        if engine.state["orders"] or engine.state["positions"] or engine.state["trades"]:
            changing_day = payload.get("trade_date") and payload["trade_date"] != engine.state.get("trade_date")
            changing_instrument = instrument != engine.state.get("instrument")
            if changing_day or changing_instrument:
                raise ValueError("Reset the simulation before changing instrument or date")
        engine.configure(**payload)
        self.store.save(session_id, engine.state)
        return self._response(session_id, engine)

    def start(self, session_id: str) -> dict:
        engine = SimulationEngine(self.store.load(session_id))
        frame = self._frame(engine)
        if frame.empty:
            raise ValueError("No candles available for this session")
        if engine.state["replay_index"] < 0:
            engine.on_candle(self._candle(frame, 0), 0)
        engine.state["replay_status"] = "PLAYING"
        self.store.save(session_id, engine.state)
        return self._response(session_id, engine, frame=frame)

    def set_status(self, session_id: str, status: str) -> dict:
        engine = SimulationEngine(self.store.load(session_id))
        status = str(status).upper()
        if status not in {"PLAYING", "PAUSED"}:
            raise ValueError("Unsupported replay status")
        engine.state["replay_status"] = status
        self.store.save(session_id, engine.state)
        return self._response(session_id, engine)

    def step(self, session_id: str, count: int = 1) -> dict:
        engine = SimulationEngine(self.store.load(session_id))
        frame = self._frame(engine)
        count = max(1, min(60, int(count)))
        for _ in range(count):
            next_index = engine.state["replay_index"] + 1
            if next_index >= len(frame):
                self._complete(engine)
                break
            engine.on_candle(self._candle(frame, next_index), next_index)
        if engine.state["replay_index"] >= len(frame) - 1:
            self._complete(engine)
        self.store.save(session_id, engine.state)
        return self._response(session_id, engine, frame=frame)

    def submit_order(self, session_id: str, payload: dict) -> dict:
        return self._mutate(session_id, lambda e: e.submit_order(**payload))

    def modify_order(self, session_id: str, order_id: str, payload: dict) -> dict:
        return self._mutate(session_id, lambda e: e.modify_order(order_id, **payload))

    def cancel_order(self, session_id: str, order_id: str) -> dict:
        return self._mutate(session_id, lambda e: e.cancel_order(order_id))

    def exit_position(self, session_id: str, symbol: str, qty=None) -> dict:
        return self._mutate(session_id, lambda e: e.exit_position(symbol, qty))

    def modify_position(self, session_id: str, symbol: str, payload: dict) -> dict:
        return self._mutate(session_id, lambda e: e.modify_position(symbol, **payload))

    def reset(self, session_id: str, payload: dict | None = None) -> dict:
        engine = SimulationEngine(self.store.load(session_id))
        payload = payload or {}
        engine.reset(
            instrument=payload.get("instrument") or engine.state.get("instrument"),
            trade_date=payload.get("trade_date") or engine.state.get("trade_date"),
            starting_capital=payload.get("starting_capital") or engine.state.get("starting_capital"),
        )
        self.store.save(session_id, engine.state)
        return self._response(session_id, engine)

    def dates(self, instrument: str) -> list[str]:
        if instrument not in INSTRUMENTS:
            raise ValueError("Unsupported instrument")
        return self.provider.available_dates(instrument)

    def fetch_day(self, instrument: str, trade_date: str) -> dict:
        frame = self.provider.load_day(instrument, trade_date)
        return {"instrument": instrument, "trade_date": trade_date, "candles": len(frame)}

    def bootstrap(self) -> dict:
        return {"instruments": instrument_list(), "starting_capital": STARTING_CAPITAL}

    def _mutate(self, session_id: str, mutation) -> dict:
        engine = SimulationEngine(self.store.load(session_id))
        mutation(engine)
        self.store.save(session_id, engine.state)
        return self._response(session_id, engine)

    def _frame(self, engine: SimulationEngine):
        if not engine.state.get("trade_date"):
            raise ValueError("Select a historical date")
        return self.provider.load_day(engine.state["instrument"], engine.state["trade_date"])

    @staticmethod
    def _candle(frame, index: int) -> dict:
        row = frame.iloc[index]
        return {
            "timestamp": frame.index[index].isoformat(),
            "open": float(row.open), "high": float(row.high),
            "low": float(row.low), "close": float(row.close),
            "volume": int(row.volume or 0),
        }

    @staticmethod
    def _complete(engine: SimulationEngine) -> None:
        for order in engine.state["orders"]:
            if order["status"] not in FINAL_ORDER_STATUSES:
                order["status"] = "CANCELLED"
        for symbol in list(engine.state["positions"]):
            engine.exit_position(symbol, reason="EOD")
        engine.state["replay_status"] = "COMPLETE"

    def _response(self, session_id: str, engine: SimulationEngine,
                  *, include_chart=True, frame=None) -> dict:
        response = {"session_id": session_id, "state": engine.snapshot()}
        if not include_chart or not engine.state.get("trade_date"):
            response["chart"] = {"candles": [], "indicators": {}}
            return response
        try:
            frame = frame if frame is not None else self._frame(engine)
            visible = visible_bars(
                frame, engine.state["replay_index"], engine.state["timeframe"]
            )
            response["chart"] = {
                "candles": chart_payload(visible, engine.state["chart_type"]),
                "indicators": indicator_series(visible, engine.state["indicators"]),
                "visible_count_1m": max(0, engine.state["replay_index"] + 1),
                "total_count_1m": len(frame),
            }
        except (FileNotFoundError, RuntimeError, ValueError) as exc:
            response["chart"] = {"candles": [], "indicators": {}, "error": str(exc)}
        return response
