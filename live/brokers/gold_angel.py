"""Angel One MCX GOLD execution with shared Kite market data, never Angel data."""
from datetime import datetime
import math
import re

from live.brokers import angel
from live.brokers.base import OrderResult, Position
from live.brokers.gold_zerodha import GoldZerodhaAdapter
from live.brokers.order_transport import send_order


class GoldAngelAdapter(angel.AngelAdapter):
    gold_short = True
    _EXCHANGE, _segment, _underlying, _PRODUCT = "MCX", "MCX", "GOLD", "CARRYFORWARD"
    valid_symbol = staticmethod(GoldZerodhaAdapter.valid_symbol)

    @property
    def _kite(self):
        # The shared data session auto-reloads its daily token. Order auth remains Angel's.
        from auth.session_manager import get_kite
        return get_kite()

    def _gold_rows(self):
        rows = self._underlying_rows()
        rows = rows if rows is not None else self._ensure_instrument_master()
        return [r for r in rows if r.get("exch_seg") == "MCX" and r.get("name") == "GOLD"
                and r.get("instrumenttype") == "FUTCOM"]

    @staticmethod
    def _expiry(row):
        return datetime.strptime(str(row["expiry"]).upper(), "%d%b%Y").date().isoformat()

    def _bind(self, row):
        """Match the exact exchange expiry, never guess from the contract month."""
        symbol, expiry = row["tradingsymbol"], str(row["expiry"])[:10]
        matches = [r for r in self._gold_rows() if self._expiry(r) == expiry]
        if len(matches) != 1:
            raise RuntimeError("No unique Angel GOLD contract for the exact Kite expiry")
        r = matches[0]
        if (int(r["lotsize"]) != 1 or float(r["tick_size"]) / 100 != 1
                or not re.fullmatch(r"GOLD\d{2}[A-Z]{3}\d{2}FUT", str(r["symbol"])) or not r.get("token")):
            raise RuntimeError("Unexpected Angel GOLD contract specification")
        meta = dict(symbol=r["symbol"], token=str(r["token"]), lotsize=1, canonical=symbol, expiry=expiry)
        self._symbol_cache[symbol] = meta
        return meta

    def _resolve_symbol_meta(self, symbol):
        if not self.valid_symbol(symbol):
            raise ValueError("Gold V2 requires a canonical GOLD futures symbol")
        if symbol in self._symbol_cache:
            return self._symbol_cache[symbol]
        rows = [r for r in self._kite.instruments("MCX") if r.get("name") == "GOLD"
                and r.get("instrument_type") == "FUT" and r.get("tradingsymbol") == symbol]
        if len(rows) != 1:
            raise RuntimeError("No exact Kite GOLD contract for Angel mapping")
        return self._bind(rows[0])

    def contract(self, today):
        row = GoldZerodhaAdapter.contract(self, today)
        self._bind(row)  # warm instrument/token lookup before signal evaluation
        return row

    def get_ltp(self, symbol):
        return GoldZerodhaAdapter.get_ltp(self, symbol)

    def quote(self, symbols):
        return GoldZerodhaAdapter.quote(self, symbols)

    def limit(self, symbol, buy=False):
        return GoldZerodhaAdapter.limit(self, symbol, buy)

    def get_position(self):
        response = self._smart.position()
        if not isinstance(response, dict) or response.get("status") is not True:
            raise RuntimeError("Angel GOLD position read failed")
        rows = [r for r in response.get("data") or [] if r.get("exchange") == "MCX"
                and r.get("producttype") == "CARRYFORWARD"
                and re.fullmatch(r"GOLD\d{2}[A-Z]{3}\d{2}FUT", str(r.get("tradingsymbol")))
                and int(r.get("netqty") or 0)]
        if len(rows) > 1:
            raise RuntimeError("Multiple Angel GOLD positions require reconciliation")
        if not rows:
            return Position(None, 0, None)
        p = rows[0]
        metas = [m for m in self._symbol_cache.values() if m["symbol"] == p["tradingsymbol"]]
        if not metas:
            # A restarted bot may hold a contract which is no longer the nearest.
            angel_rows = [r for r in self._gold_rows() if r["symbol"] == p["tradingsymbol"]
                          and str(r["token"]) == str(p.get("symboltoken"))]
            if len(angel_rows) != 1:
                raise RuntimeError("Angel GOLD position is absent from the instrument master")
            expiry = self._expiry(angel_rows[0])
            kite_rows = [r for r in self._kite.instruments("MCX") if r.get("name") == "GOLD"
                         and r.get("instrument_type") == "FUT" and str(r["expiry"])[:10] == expiry]
            if len(kite_rows) == 1:
                metas = [self._bind(kite_rows[0])]
        if len(metas) != 1 or str(p.get("symboltoken")) != metas[0]["token"]:
            raise RuntimeError("Angel GOLD position token cannot be reconciled")
        return Position(metas[0]["canonical"], int(p["netqty"]), "SHORT")

    def margin(self, symbol, qty):
        meta = self._resolve_symbol_meta(symbol)
        response = self._smart.getMarginApi({"positions": [dict(exchange="MCX", qty=int(qty), price=0,
            productType="CARRYFORWARD", token=meta["token"], tradeType="SELL", orderType="MARKET")]})
        if not isinstance(response, dict) or response.get("status") is not True:
            raise RuntimeError("Angel GOLD margin quote failed")
        margin = float(response["data"]["totalMarginRequired"])
        if not math.isfinite(margin) or margin <= 0:
            raise RuntimeError("Invalid Angel GOLD margin quote")
        return margin

    def _send(self, operation, symbol, qty, price, key):
        if not angel._live_orders_enabled():
            raise NotImplementedError("Angel live order placement is disabled")
        if qty < 1 or int(qty) != qty or not math.isfinite(price) or price <= 0:
            raise ValueError("Invalid Angel GOLD futures order")
        meta = self._resolve_symbol_meta(symbol)
        result = send_order(self, operation, dict(variety="NORMAL", tradingsymbol=meta["symbol"],
            symboltoken=meta["token"], transactiontype="BUY" if operation == "exit" else "SELL",
            exchange="MCX", ordertype="LIMIT", producttype="CARRYFORWARD", duration="IOC",
            price=price, quantity=int(qty), squareoff="0", stoploss="0",
            ordertag=angel._angel_order_tag(key)), key)
        self._invalidate_reads()
        oid = (result.get("data") or {}).get("orderid") if isinstance(result, dict) else None
        if not oid:
            raise RuntimeError("Angel GOLD acknowledgement needs reconciliation")
        return OrderResult(str(oid), "PLACED", None, {"broker_symbol": meta["symbol"]})

    def place_order(self, *, side, symbol, qty, price, idempotency_key):
        if side != "SHORT" or self.get_position().qty:
            raise RuntimeError("Angel GOLD short entry requires a flat account position")
        return self._send("entry", symbol, qty, price, idempotency_key)

    def exit_all(self, *, symbol, qty, reason, idempotency_key, price=None):
        p = self.get_position()
        if p.symbol != symbol or p.qty >= 0 or qty <= 0 or qty > -p.qty:
            return OrderResult(None, "NO_SHORT_POSITION", None, {})
        return self._send("exit", symbol, qty, price or self.limit(symbol, buy=True), idempotency_key)
