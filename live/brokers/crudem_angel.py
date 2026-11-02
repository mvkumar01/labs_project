"""Angel One MCX CRUDEOILM futures, long and short, with shared Kite market data, never Angel data.

Order quantity is barrels: lots x 10, as the 2026-10-09 margin probe found. The contract's master row is
checked when a symbol is bound, so a changed lot size or tick stops orders instead of resizing them.
"""
from datetime import datetime
import math
import re

from live.brokers import angel
from live.brokers.base import OrderResult, Position
from live.brokers.order_transport import send_order

LOT_QTY = 10
TICK = 1
STOP_LIMIT_PTS = 20      # a stop-limit's limit sits this far past its trigger; the runner backstops a jump past it
_KITE_SYMBOL = r"CRUDEOILM\d{2}[A-Z]{3}FUT"
_ANGEL_SYMBOL = r"CRUDEOILM\d{2}[A-Z]{3}\d{2}FUT"


class CrudemAngelAdapter(angel.AngelAdapter):
    _EXCHANGE, _segment, _underlying, _PRODUCT = "MCX", "MCX", "CRUDEOILM", "CARRYFORWARD"

    @staticmethod
    def valid_symbol(symbol):
        return bool(re.fullmatch(_KITE_SYMBOL, str(symbol)))

    @property
    def _kite(self):
        # The shared data session auto-reloads its daily token. Order auth remains Angel's.
        from auth.session_manager import get_kite
        return get_kite()

    def _contract_rows(self):
        rows = self._underlying_rows()
        rows = rows if rows is not None else self._ensure_instrument_master()
        return [r for r in rows if r.get("exch_seg") == "MCX" and r.get("name") == "CRUDEOILM"
                and r.get("instrumenttype") == "FUTCOM"]

    @staticmethod
    def _expiry(row):
        return datetime.strptime(str(row["expiry"]).upper(), "%d%b%Y").date().isoformat()

    def _kite_rows(self):
        return [r for r in self._kite.instruments("MCX") if r.get("name") == "CRUDEOILM"
                and r.get("instrument_type") == "FUT"]

    def _bind(self, row):
        """Match the exact exchange expiry, never guess from the contract month."""
        symbol, expiry = row["tradingsymbol"], str(row["expiry"])[:10]
        matches = [r for r in self._contract_rows() if self._expiry(r) == expiry]
        if len(matches) != 1:
            raise RuntimeError("No unique Angel CRUDEOILM contract for the exact Kite expiry")
        r = matches[0]
        if (int(r["lotsize"]) != LOT_QTY or float(r["tick_size"]) / 100 != TICK
                or not re.fullmatch(_ANGEL_SYMBOL, str(r["symbol"])) or not r.get("token")):
            raise RuntimeError("Unexpected Angel CRUDEOILM contract specification")
        meta = dict(symbol=r["symbol"], token=str(r["token"]), lotsize=LOT_QTY, canonical=symbol, expiry=expiry)
        self._symbol_cache[symbol] = meta
        return meta

    def _resolve_symbol_meta(self, symbol):
        if not self.valid_symbol(symbol):
            raise ValueError("The crude combination requires a canonical CRUDEOILM futures symbol")
        if symbol in self._symbol_cache:
            return self._symbol_cache[symbol]
        rows = [r for r in self._kite_rows() if r.get("tradingsymbol") == symbol]
        if len(rows) != 1:
            raise RuntimeError("No exact Kite CRUDEOILM contract for Angel mapping")
        return self._bind(rows[0])

    # -- market data, always Kite ------------------------------------------------
    def get_ltp(self, symbol):
        key = "MCX:" + symbol
        value = float(self._kite.ltp(key)[key]["last_price"])
        if not math.isfinite(value) or value <= 0:
            raise RuntimeError("No valid CRUDEOILM last price")
        return value

    def limit(self, symbol, buy=False):
        key = "MCX:" + symbol
        levels = (self._kite.quote([key])[key].get("depth") or {}).get("sell" if buy else "buy") or []
        if not levels or float(levels[0].get("price") or 0) <= 0:
            raise RuntimeError("No actionable CRUDEOILM order-book quote")
        # Cross the touch by two ticks; IOC prevents a stale resting order.
        px = float(levels[0]["price"])
        return math.ceil(px + 2 * TICK) if buy else math.floor(px - 2 * TICK)

    # -- account reads -----------------------------------------------------------
    def get_position(self):
        """Signed barrels. Read failures propagate: an unknown position is never flat."""
        response = self._with_backoff(self._smart.position, angel._is_transient_read)
        if not isinstance(response, dict) or response.get("status") is not True:
            raise RuntimeError("Angel CRUDEOILM position read failed")
        rows = [r for r in response.get("data") or [] if r.get("exchange") == "MCX"
                and r.get("producttype") == "CARRYFORWARD"
                and re.fullmatch(_ANGEL_SYMBOL, str(r.get("tradingsymbol"))) and int(r.get("netqty") or 0)]
        if len(rows) > 1:
            raise RuntimeError("Multiple Angel CRUDEOILM positions require reconciliation")
        if not rows:
            return Position(None, 0, None)
        p = rows[0]
        metas = [m for m in self._symbol_cache.values() if m["symbol"] == p["tradingsymbol"]]
        if not metas:
            # A restarted bot may hold a contract which is no longer the nearest.
            found = [r for r in self._contract_rows() if r["symbol"] == p["tradingsymbol"]
                     and str(r["token"]) == str(p.get("symboltoken"))]
            if len(found) != 1:
                raise RuntimeError("Angel CRUDEOILM position is absent from the instrument master")
            expiry = self._expiry(found[0])
            kite_rows = [r for r in self._kite_rows() if str(r["expiry"])[:10] == expiry]
            if len(kite_rows) == 1:
                metas = [self._bind(kite_rows[0])]
        if len(metas) != 1 or str(p.get("symboltoken")) != metas[0]["token"]:
            raise RuntimeError("Angel CRUDEOILM position token cannot be reconciled")
        qty = int(p["netqty"])
        return Position(metas[0]["canonical"], qty, "LONG" if qty > 0 else "SHORT")

    def holds(self, symbol, side, qty):
        """Does the account hold at least qty of this contract on this side? A failed read means no."""
        try:
            pos = self.get_position()
        except Exception:
            return False
        held = pos.qty if side == "LONG" else -pos.qty
        return pos.symbol == symbol and 0 < qty <= held

    def margin(self, symbol, qty):
        meta = self._resolve_symbol_meta(symbol)
        response = self._smart.getMarginApi({"positions": [dict(exchange="MCX", qty=int(qty), price=0,
            productType="CARRYFORWARD", token=meta["token"], tradeType="SELL", orderType="MARKET")]})
        if not isinstance(response, dict) or response.get("status") is not True:
            raise RuntimeError("Angel CRUDEOILM margin quote failed")
        margin = float(response["data"]["totalMarginRequired"])
        if not math.isfinite(margin) or margin <= 0:
            raise RuntimeError("Invalid Angel CRUDEOILM margin quote")
        return margin

    # -- orders ------------------------------------------------------------------
    def _send(self, operation, meta, qty, buy, key, **order):
        if not angel._live_orders_enabled():
            raise NotImplementedError("Angel live order placement is disabled")
        if qty < LOT_QTY or int(qty) != qty or qty % LOT_QTY:
            raise ValueError("CRUDEOILM order quantity must be a whole number of lots")
        result = send_order(self, operation, dict(tradingsymbol=meta["symbol"], symboltoken=meta["token"],
            transactiontype="BUY" if buy else "SELL", exchange="MCX", producttype="CARRYFORWARD",
            quantity=int(qty), squareoff="0", stoploss="0", ordertag=angel._angel_order_tag(key), **order), key)
        self._invalidate_reads()
        oid = (result.get("data") or {}).get("orderid") if isinstance(result, dict) else None
        if not oid:
            raise RuntimeError("Angel CRUDEOILM acknowledgement needs reconciliation")
        return OrderResult(str(oid), "PLACED", None, {"broker_symbol": meta["symbol"]})

    @staticmethod
    def _checked_price(price):
        if not math.isfinite(price) or price <= 0:
            raise ValueError("Invalid Angel CRUDEOILM order price")
        return price

    def place_order(self, *, side, symbol, qty, price, idempotency_key):
        if side not in ("LONG", "SHORT") or self.get_position().qty:
            raise RuntimeError("Angel CRUDEOILM entry requires a flat account position")
        return self._send("entry", self._resolve_symbol_meta(symbol), qty, side == "LONG", idempotency_key,
                          variety="NORMAL", ordertype="LIMIT", duration="IOC", price=self._checked_price(price))

    def exit_all(self, *, symbol, qty, reason, idempotency_key, price=None):
        """Closes the side the account actually holds, and never more than it holds."""
        pos = self.get_position()
        if pos.symbol != symbol or not pos.qty or qty <= 0 or qty > abs(pos.qty):
            return OrderResult(None, "NO_POSITION", None, {})
        buy = pos.qty < 0
        return self._send("exit", self._resolve_symbol_meta(symbol), qty, buy, idempotency_key,
                          variety="NORMAL", ordertype="LIMIT", duration="IOC",
                          price=self._checked_price(price or self.limit(symbol, buy=buy)))

    def place_stop(self, *, side, symbol, qty, trigger, idempotency_key):
        """A resting stop-limit for a held position. It keeps working through a runner or network failure."""
        if not self.holds(symbol, side, qty):
            return OrderResult(None, "NO_POSITION", None, {})
        buy = side == "SHORT"
        limit = trigger + STOP_LIMIT_PTS if buy else trigger - STOP_LIMIT_PTS
        return self._send("exit", self._resolve_symbol_meta(symbol), qty, buy, idempotency_key,
                          variety="STOPLOSS", ordertype="STOPLOSS_LIMIT", duration="DAY",
                          triggerprice=self._checked_price(trigger), price=self._checked_price(limit))

    def cancel_stop(self, order_id, key):
        send_order(self, "cancel", dict(variety="STOPLOSS", orderid=str(order_id)), key)
        self._invalidate_reads()
