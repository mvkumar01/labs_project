"""Deterministic replay execution, positions, account, and performance.

Execution assumptions for one-minute OHLC candles:

* Market orders fill at the latest visible close and never wait for a future bar.
* A gapped limit receives the candle open when it is more favourable; otherwise
  it fills at the limit.
* A gapped stop receives the candle open when it is less favourable; otherwise
  it fills at the trigger.
* If both stop-loss and target are touched in one candle, stop-loss executes
  first. This deliberately conservative rule avoids optimistic look-ahead.
* Volume-based queueing and partial fills are deferred until tick/order-book
  replay exists. User-requested partial exits are supported.
"""
from __future__ import annotations

from copy import deepcopy
from datetime import datetime
import uuid


FINAL_ORDER_STATUSES = {"FILLED", "CANCELLED", "REJECTED"}


def _id(prefix: str) -> str:
    return f"{prefix}_{uuid.uuid4().hex[:12]}"


def _round(value: float) -> float:
    return round(float(value), 2)


class EquityChargesModel:
    """Configurable approximation of Indian equity intraday charges."""

    def __init__(self, brokerage_rate=0.0003, brokerage_cap=20.0):
        self.brokerage_rate = float(brokerage_rate)
        self.brokerage_cap = float(brokerage_cap)

    def calculate(self, buy_turnover: float, sell_turnover: float,
                  order_count: int = 2) -> dict:
        buy_turnover = max(0.0, float(buy_turnover))
        sell_turnover = max(0.0, float(sell_turnover))
        turnover = buy_turnover + sell_turnover
        brokerage = sum(
            min(self.brokerage_cap, side * self.brokerage_rate)
            for side in (buy_turnover, sell_turnover)
            if side > 0
        )
        stt = sell_turnover * 0.00025
        exchange = turnover * 0.0000297
        sebi = turnover * 0.000001
        stamp = buy_turnover * 0.00003
        gst = 0.18 * (brokerage + exchange + sebi)
        total = brokerage + stt + exchange + sebi + stamp + gst
        return {
            "brokerage": _round(brokerage),
            "stt": _round(stt),
            "exchange": _round(exchange),
            "sebi": _round(sebi),
            "stamp": _round(stamp),
            "gst": _round(gst),
            "total": _round(total),
        }


class SimulationEngine:
    def __init__(self, state: dict | None = None):
        self.state = deepcopy(state) if state else self.new_state()
        self.state.setdefault("mode", "HISTORICAL")
        self.state.setdefault("live_last_poll_at", None)
        self.charges_model = EquityChargesModel()

    @staticmethod
    def new_state(starting_capital: float = 1_000_000.0) -> dict:
        return {
            "version": 1,
            "instrument": "NIFTY",
            "mode": "HISTORICAL",
            "trade_date": None,
            "replay_index": -1,
            "replay_status": "READY",
            "speed": 1,
            "timeframe": "1m",
            "chart_type": "candlestick",
            "current_timestamp": None,
            "current_price": None,
            "live_last_poll_at": None,
            "starting_capital": float(starting_capital),
            "realized_pnl": 0.0,
            "charges": 0.0,
            "orders": [],
            "positions": {},
            "trades": [],
            "notifications": [],
            "indicators": [],
            "slippage": {"mode": "points", "value": 0.0},
        }

    def reset(self, *, instrument=None, trade_date=None, mode=None,
              starting_capital=None) -> dict:
        capital = (
            float(starting_capital)
            if starting_capital is not None
            else float(self.state.get("starting_capital") or 1_000_000.0)
        )
        self.state = self.new_state(capital)
        if instrument:
            self.state["instrument"] = instrument
        if mode:
            self.state["mode"] = mode
        if trade_date:
            self.state["trade_date"] = trade_date
        return self.snapshot()

    def configure(self, **values) -> dict:
        allowed = {
            "instrument", "mode", "trade_date", "speed", "timeframe", "chart_type",
            "indicators", "slippage",
        }
        for key, value in values.items():
            if key in allowed and value is not None:
                self.state[key] = deepcopy(value)
        return self.snapshot()

    def notify(self, message: str, level: str = "info") -> None:
        self.state["notifications"].append({
            "id": _id("note"),
            "timestamp": self.state.get("current_timestamp"),
            "level": level,
            "message": message,
        })
        self.state["notifications"] = self.state["notifications"][-30:]

    def _slipped(self, side: str, price: float) -> float:
        cfg = self.state.get("slippage") or {}
        value = max(0.0, float(cfg.get("value") or 0.0))
        if cfg.get("mode") == "bps":
            value = float(price) * value / 10_000.0
        return _round(price + value if side == "BUY" else price - value)

    def account(self) -> dict:
        unrealized = 0.0
        used = 0.0
        current = self.state.get("current_price")
        for position in self.state["positions"].values():
            mark = float(current if current is not None else position["avg_price"])
            sign = 1 if position["side"] == "LONG" else -1
            unrealized += (mark - position["avg_price"]) * position["qty"] * sign
            margin_rate = 1.0 if position["side"] == "LONG" else 0.20
            used += abs(mark * position["qty"]) * margin_rate
        net_value = (
            self.state["starting_capital"]
            + self.state["realized_pnl"]
            - self.state["charges"]
            + unrealized
        )
        return {
            "starting_capital": _round(self.state["starting_capital"]),
            "available_cash": _round(max(0.0, net_value - used)),
            "used_capital": _round(used),
            "realized_pnl": _round(self.state["realized_pnl"]),
            "unrealized_pnl": _round(unrealized),
            "charges": _round(self.state["charges"]),
            "net_account_value": _round(net_value),
        }

    def submit_order(self, *, side: str, qty: int, order_type: str = "MARKET",
                    limit_price=None, trigger_price=None, stop_loss=None,
                    target=None) -> dict:
        side = str(side).upper()
        order_type = str(order_type).upper()
        qty = int(qty)
        if side not in {"BUY", "SELL"} or order_type not in {"MARKET", "LIMIT", "STOP"}:
            raise ValueError("Unsupported order side or type")
        if qty <= 0:
            raise ValueError("Quantity must be positive")
        if self.state.get("replay_status") == "COMPLETE":
            raise ValueError("Simulation is complete; reset before placing an order")
        if self.state.get("current_price") is None:
            raise ValueError("Start replay before placing an order")
        if order_type == "LIMIT" and limit_price is None:
            raise ValueError("Limit price is required")
        if order_type == "STOP" and trigger_price is None:
            raise ValueError("Trigger price is required")
        now = self.state.get("current_timestamp")
        status = "OPEN" if order_type != "STOP" else "TRIGGER_PENDING"
        order = {
            "order_id": _id("ord"), "timestamp": now,
            "instrument": self.state["instrument"], "side": side, "qty": qty,
            "order_type": order_type, "limit_price": _num(limit_price),
            "trigger_price": _num(trigger_price), "stop_loss": _num(stop_loss),
            "target": _num(target), "status": status, "filled_price": None,
            "filled_qty": 0, "updated_at": now,
        }
        self.state["orders"].append(order)
        if order_type == "MARKET":
            self._fill_order(order, self._slipped(side, self.state["current_price"]))
        else:
            self.notify(f"{side} {order_type} order placed for {qty}")
        return deepcopy(order)

    def modify_order(self, order_id: str, **changes) -> dict:
        order = self._find_order(order_id)
        if order["status"] in FINAL_ORDER_STATUSES:
            raise ValueError("Only pending orders can be modified")
        for key in ("qty", "limit_price", "trigger_price", "stop_loss", "target"):
            if key in changes and changes[key] is not None:
                value = int(changes[key]) if key == "qty" else float(changes[key])
                if value <= 0:
                    raise ValueError(f"{key.replace('_', ' ').title()} must be positive")
                order[key] = value
        order["updated_at"] = self.state.get("current_timestamp")
        return deepcopy(order)

    def cancel_order(self, order_id: str) -> dict:
        order = self._find_order(order_id)
        if order["status"] in FINAL_ORDER_STATUSES:
            raise ValueError("Order is no longer pending")
        order["status"] = "CANCELLED"
        order["updated_at"] = self.state.get("current_timestamp")
        self.notify(f"Order {order_id} cancelled")
        return deepcopy(order)

    def _find_order(self, order_id: str) -> dict:
        for order in self.state["orders"]:
            if order["order_id"] == order_id:
                return order
        raise KeyError("Order not found")

    def on_candle(self, candle: dict, replay_index: int) -> dict:
        """Advance state with exactly one newly visible one-minute candle."""
        self.state["replay_index"] = int(replay_index)
        self.state["current_timestamp"] = str(candle["timestamp"])
        self.state["current_price"] = float(candle["close"])
        self._match_pending(candle)
        self._match_protective_exits(candle)
        return self.snapshot()

    def _match_pending(self, candle: dict) -> None:
        for order in self.state["orders"]:
            if order["status"] not in {"OPEN", "TRIGGER_PENDING"}:
                continue
            side = order["side"]
            fill = None
            if order["order_type"] == "LIMIT":
                limit = order["limit_price"]
                if side == "BUY" and candle["low"] <= limit:
                    fill = min(float(candle["open"]), limit)
                elif side == "SELL" and candle["high"] >= limit:
                    fill = max(float(candle["open"]), limit)
            elif order["order_type"] == "STOP":
                trigger = order["trigger_price"]
                if side == "BUY" and candle["high"] >= trigger:
                    fill = max(float(candle["open"]), trigger)
                elif side == "SELL" and candle["low"] <= trigger:
                    fill = min(float(candle["open"]), trigger)
            if fill is not None:
                self._fill_order(order, self._slipped(side, fill))

    def _required_capital(self, side: str, qty: int, price: float) -> float:
        position = self.state["positions"].get(self.state["instrument"])
        reduces = position and (
            (position["side"] == "LONG" and side == "SELL")
            or (position["side"] == "SHORT" and side == "BUY")
        )
        opening_qty = max(0, qty - int(position["qty"])) if reduces else qty
        return opening_qty * price * (1.0 if side == "BUY" else 0.20)

    def _fill_order(self, order: dict, price: float) -> None:
        required = self._required_capital(order["side"], order["qty"], price)
        if required > self.account()["available_cash"]:
            order["status"] = "REJECTED"
            order["updated_at"] = self.state.get("current_timestamp")
            self.notify("Order rejected: insufficient simulated capital", "error")
            return
        order["status"] = "FILLED"
        order["filled_price"] = _round(price)
        order["filled_qty"] = order["qty"]
        order["updated_at"] = self.state.get("current_timestamp")
        self._apply_fill(order)
        self.notify(
            f"{order['side']} {order['qty']} {order['instrument']} filled @ {price:.2f}",
            "success",
        )

    def _apply_fill(self, order: dict) -> None:
        symbol = order["instrument"]
        side = order["side"]
        qty = int(order["filled_qty"])
        price = float(order["filled_price"])
        position = self.state["positions"].get(symbol)
        incoming_direction = "LONG" if side == "BUY" else "SHORT"
        if position is None:
            self.state["positions"][symbol] = {
                "instrument": symbol, "side": incoming_direction, "qty": qty,
                "avg_price": price, "entry_time": order["updated_at"],
                "realized_pnl": 0.0, "stop_loss": order.get("stop_loss"),
                "target": order.get("target"), "status": "OPEN",
            }
            return
        if position["side"] == incoming_direction:
            total = position["qty"] + qty
            position["avg_price"] = _round(
                (position["avg_price"] * position["qty"] + price * qty) / total
            )
            position["qty"] = total
            position["stop_loss"] = order.get("stop_loss") or position.get("stop_loss")
            position["target"] = order.get("target") or position.get("target")
            return
        closing_qty = min(position["qty"], qty)
        self._record_exit(position, closing_qty, price, "ORDER")
        position["qty"] -= closing_qty
        remainder = qty - closing_qty
        if position["qty"] == 0:
            del self.state["positions"][symbol]
        if remainder:
            self.state["positions"][symbol] = {
                "instrument": symbol, "side": incoming_direction, "qty": remainder,
                "avg_price": price, "entry_time": order["updated_at"],
                "realized_pnl": 0.0, "stop_loss": order.get("stop_loss"),
                "target": order.get("target"), "status": "OPEN",
            }

    def _match_protective_exits(self, candle: dict) -> None:
        for symbol, position in list(self.state["positions"].items()):
            stop = position.get("stop_loss")
            target = position.get("target")
            reason = price = None
            if position["side"] == "LONG":
                stop_hit = stop is not None and candle["low"] <= stop
                target_hit = target is not None and candle["high"] >= target
                if stop_hit:
                    reason, price = "STOP_LOSS", min(float(candle["open"]), stop)
                elif target_hit:
                    reason, price = "TARGET", max(float(candle["open"]), target)
            else:
                stop_hit = stop is not None and candle["high"] >= stop
                target_hit = target is not None and candle["low"] <= target
                if stop_hit:
                    reason, price = "STOP_LOSS", max(float(candle["open"]), stop)
                elif target_hit:
                    reason, price = "TARGET", min(float(candle["open"]), target)
            if reason:
                exit_side = "SELL" if position["side"] == "LONG" else "BUY"
                price = self._slipped(exit_side, price)
                self._record_exit(position, position["qty"], price, reason)
                del self.state["positions"][symbol]
                self.notify(f"{symbol} {reason.replace('_', ' ').lower()} @ {price}")

    def exit_position(self, symbol: str, qty: int | None = None,
                      reason: str = "MANUAL") -> dict:
        position = self.state["positions"].get(symbol)
        if not position:
            raise KeyError("Open position not found")
        qty = position["qty"] if qty is None else int(qty)
        if qty <= 0 or qty > position["qty"]:
            raise ValueError("Invalid exit quantity")
        exit_side = "SELL" if position["side"] == "LONG" else "BUY"
        price = self._slipped(exit_side, self.state["current_price"])
        trade = self._record_exit(position, qty, price, reason)
        position["qty"] -= qty
        if position["qty"] == 0:
            del self.state["positions"][symbol]
        return deepcopy(trade)

    def modify_position(self, symbol: str, *, stop_loss=None, target=None) -> dict:
        position = self.state["positions"].get(symbol)
        if not position:
            raise KeyError("Open position not found")
        if stop_loss is not None:
            stop_loss = float(stop_loss)
            if stop_loss <= 0:
                raise ValueError("Stop loss must be positive")
            position["stop_loss"] = stop_loss
        if target is not None:
            target = float(target)
            if target <= 0:
                raise ValueError("Target must be positive")
            position["target"] = target
        return deepcopy(position)

    def _record_exit(self, position: dict, qty: int, exit_price: float,
                     reason: str) -> dict:
        sign = 1 if position["side"] == "LONG" else -1
        gross = (exit_price - position["avg_price"]) * qty * sign
        buy_turnover = (
            position["avg_price"] * qty if position["side"] == "LONG"
            else exit_price * qty
        )
        sell_turnover = (
            exit_price * qty if position["side"] == "LONG"
            else position["avg_price"] * qty
        )
        charges = self.charges_model.calculate(buy_turnover, sell_turnover)
        trade = {
            "trade_id": _id("trade"), "instrument": position["instrument"],
            "direction": position["side"], "qty": qty,
            "entry_time": position["entry_time"],
            "exit_time": self.state.get("current_timestamp"),
            "entry_price": _round(position["avg_price"]),
            "exit_price": _round(exit_price), "gross_pnl": _round(gross),
            "charges": charges["total"], "net_pnl": _round(gross - charges["total"]),
            "exit_reason": reason,
        }
        self.state["trades"].append(trade)
        self.state["realized_pnl"] = _round(self.state["realized_pnl"] + gross)
        self.state["charges"] = _round(self.state["charges"] + charges["total"])
        position["realized_pnl"] = _round(position.get("realized_pnl", 0) + gross)
        return trade

    def performance(self) -> dict:
        trades = self.state["trades"]
        net = [float(t["net_pnl"]) for t in trades]
        wins = [v for v in net if v > 0]
        losses = [v for v in net if v < 0]
        equity = peak = drawdown = 0.0
        for value in net:
            equity += value
            peak = max(peak, equity)
            drawdown = max(drawdown, peak - equity)
        gross_profit = sum(wins)
        gross_loss = abs(sum(losses))
        count = len(trades)
        return {
            "trades": count, "winning_trades": len(wins),
            "losing_trades": len(losses),
            "win_rate": _round(100 * len(wins) / count) if count else 0.0,
            "gross_pnl": _round(sum(float(t["gross_pnl"]) for t in trades)),
            "net_pnl": _round(sum(net)),
            "charges": _round(sum(float(t["charges"]) for t in trades)),
            "largest_winner": _round(max(wins)) if wins else 0.0,
            "largest_loser": _round(min(losses)) if losses else 0.0,
            "max_realized_drawdown": _round(drawdown),
            "average_trade": _round(sum(net) / count) if count else 0.0,
            "average_winner": _round(sum(wins) / len(wins)) if wins else 0.0,
            "average_loser": _round(sum(losses) / len(losses)) if losses else 0.0,
            "profit_factor": _round(gross_profit / gross_loss) if gross_loss else None,
        }

    def snapshot(self) -> dict:
        out = deepcopy(self.state)
        mark = self.state.get("current_price")
        for position in out["positions"].values():
            position["current_price"] = mark
            sign = 1 if position["side"] == "LONG" else -1
            position["unrealized_pnl"] = _round(
                ((mark or position["avg_price"]) - position["avg_price"])
                * position["qty"] * sign
            )
        out["positions"] = list(out["positions"].values())
        out["account"] = self.account()
        out["performance"] = self.performance()
        return out


def _num(value):
    if value is None or value == "":
        return None
    return float(value)
