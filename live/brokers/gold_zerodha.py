"""GOLD NRML futures adapter. Order mutations still use the common transport."""
import math
import re

from live.brokers import zerodha as z
from live.brokers.base import OrderResult, Position
from live.brokers.order_transport import send_order


class GoldZerodhaAdapter(z.ZerodhaAdapter):
    gold_short = True
    _segment, _underlying = "MCX", "GOLD"

    def available_funds(self):
        data = self._kite.margins("commodity")
        value = float(data["net"])
        return value if math.isfinite(value) else None

    @staticmethod
    def valid_symbol(symbol):
        return bool(re.fullmatch(r"GOLD\d{2}[A-Z]{3}FUT", str(symbol)))

    def get_position(self):
        # Read failures propagate: never turn an unknown position into flat.
        rows = [r for r in self._kite.positions()["net"] if r.get("exchange") == "MCX"
                and r.get("product") == "NRML" and self.valid_symbol(r.get("tradingsymbol"))
                and int(r.get("quantity") or 0)]
        if len(rows) > 1:
            raise RuntimeError("Multiple GOLD positions require reconciliation")
        return (Position(rows[0]["tradingsymbol"], int(rows[0]["quantity"]), "SHORT")
                if rows else Position(None, 0, None))

    def contract(self, today):
        rows = [r for r in self._kite.instruments("MCX") if r.get("name") == "GOLD"
                and r.get("instrument_type") == "FUT" and str(r["expiry"])[:10] > str(today)]
        if not rows:
            raise RuntimeError("No unexpired GOLD futures contract")
        row = min(rows, key=lambda r: str(r["expiry"]))
        if not self.valid_symbol(row["tradingsymbol"]) or int(row["lot_size"]) != 1 or float(row["tick_size"]) != 1:
            raise RuntimeError("Unexpected GOLD contract specification")
        return row

    def margin(self, symbol, qty):
        data = self._kite.basket_order_margins([dict(exchange="MCX", tradingsymbol=symbol,
            transaction_type="SELL", variety="regular", product="NRML", order_type="MARKET",
            quantity=qty)], consider_positions=False)
        value = float(data["final"]["total"])
        if not math.isfinite(value) or value <= 0:
            raise RuntimeError("No valid GOLD margin quote")
        return value

    def limit(self, symbol, buy=False):
        row = self.quote([symbol])["MCX:" + symbol]
        levels = (row.get("depth") or {}).get("sell" if buy else "buy") or []
        if not levels or float(levels[0].get("price") or 0) <= 0:
            raise RuntimeError("No actionable GOLD order-book quote")
        # Cross the current touch by two ticks; IOC prevents a stale resting order.
        px = float(levels[0]["price"])
        return math.ceil(px + 2) if buy else math.floor(px - 2)

    def _send(self, operation, symbol, qty, price, key):
        if not z._live_orders_enabled():
            raise NotImplementedError("Live order placement is disabled")
        if not self.valid_symbol(symbol) or qty < 1 or int(qty) != qty or not math.isfinite(price) or price <= 0:
            raise ValueError("Invalid GOLD futures order")
        oid = send_order(self, operation, dict(variety="regular", exchange="MCX",
            tradingsymbol=symbol, transaction_type="BUY" if operation == "exit" else "SELL",
            quantity=int(qty), product="NRML", order_type="LIMIT", validity="IOC",
            price=price, tag=key[-20:]), key)
        return OrderResult(str(oid), "PLACED", None, {})

    def place_order(self, *, side, symbol, qty, price, idempotency_key):
        if side != "SHORT":
            raise ValueError("GOLD V2 only enters short")
        pos = self.get_position()
        if pos.qty:
            raise RuntimeError("Existing GOLD position blocks entry")
        return self._send("entry", symbol, qty, price, idempotency_key)

    def exit_all(self, *, symbol, qty, reason, idempotency_key, price=None):
        pos = self.get_position()
        if pos.symbol != symbol or pos.qty >= 0 or qty <= 0 or qty > -pos.qty:
            return OrderResult(None, "NO_SHORT_POSITION", None, {})
        return self._send("exit", symbol, qty, price or self.limit(symbol, buy=True), idempotency_key)
