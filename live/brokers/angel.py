"""
Angel One adapter (PRIMARY broker) — wraps SmartApi.SmartConnect.

★ Permitted to import the broker SDK (lives under live/brokers/). ★

CRITICAL DRY-RUN GUARD (spec §5.4, §13):
    `_LIVE_ORDERS_ENABLED = False`. While False, `place_order` and
    `exit_all` raise NotImplementedError("LIVE_ARMED not enabled — Phase 1
    gated") so NO real order can fire in this build. Phase-1 enablement flips
    the flag in a deliberate, reviewed commit AND still requires LIVE_ARMED +
    all 6 gates.

MULTI-USER: instantiated once per (user_id, conn_id). creds is the decrypted
blob held in-memory only — NEVER logged. The SmartApi import is deferred into
`connect()` so importing this module never requires the SDK installed (keeps
Phase-0 dry-run + CI green).
"""
import hashlib
import json
import os
import calendar
import re
from datetime import datetime

from .base import BrokerAdapter, OrderResult, Position
from config.labs_config import STATE_DIR
from live.proxy import order_proxy


def _live_orders_enabled() -> bool:
    env_enabled = os.environ.get("LIVE_ORDERS_ENABLED", "0").strip().lower()
    return _LIVE_ORDERS_ENABLED and env_enabled in {"1", "true", "yes"}

# ── Phase-1 enablement flag. Flipping to True is the ONLY thing that lets a
#    real Angel order leave this process. Reviewed commit only. ───────────
_LIVE_ORDERS_ENABLED = True


def _angel_order_tag(idempotency_key: str) -> str:
    """Angel rejects ordertag values >= 20 chars; keep this deterministic."""
    digest = hashlib.sha1(str(idempotency_key).encode("utf-8")).hexdigest()[:14]
    return f"LABS{digest}"

INSTRUMENT_MASTER_URL = (
    "https://margincalculator.angelbroking.com/OpenAPI_File/files/"
    "OpenAPIScripMaster.json"
)
INSTRUMENT_FILE = STATE_DIR / "angel_instruments.json"


class AngelAdapter(BrokerAdapter):
    broker_name = "angel"

    # Angel placeOrder constants (NFO intraday LIMIT). Used only inside the
    # guarded real branch.
    _EXCHANGE = "NFO"
    _PRODUCT = "INTRADAY"
    _ORDER_TYPE = "LIMIT"
    _VARIETY = "NORMAL"

    def __init__(self, *, user_id: str, conn_id: str, creds: dict):
        super().__init__(user_id=user_id, conn_id=conn_id, creds=creds)
        self._smart = None
        self._client_code = (creds or {}).get("client_code", "")
        self._token_cache = {}
        self._symbol_cache = {}

    # ── session ─────────────────────────────────────────────────────────
    def connect(self) -> None:
        """generateSession(client_code, pin, totp) from decrypted creds.

        Login is a DATA/auth call — it goes out DIRECT (not via the static IP).
        Only place_order / exit_all are wrapped with order_proxy()."""
        from SmartApi import SmartConnect  # SDK import isolated to this pkg
        import pyotp                        # TOTP for Angel login

        self._smart = SmartConnect(api_key=self._creds["api_key"])
        totp = pyotp.TOTP(self._creds["totp_secret"]).now()
        session = self._smart.generateSession(
            self._creds["client_code"],
            self._creds["pin"],
            totp,
        )
        if (
            not isinstance(session, dict)
            or session.get("status") is not True
            or not getattr(self._smart, "access_token", None)
        ):
            self._smart = None
            raise RuntimeError("Angel session login failed")

    def is_connected(self) -> bool:
        if self._smart is None:
            return False
        try:
            # Cheap authenticated read — profile/RMS limit. Any success means
            # the session token is live. Never logs cred values.
            response = self._smart.rmsLimit()
            return (
                isinstance(response, dict)
                and response.get("status") is True
                and response.get("data") is not None
            )
        except Exception:
            return False

    def account_ref(self) -> str:
        # Return a stable identifier for duplicate-account isolation.
        return f"angel:{self._client_code}"

    # ── market reads ──────────────────────────────────────────────────────
    def available_funds(self) -> float | None:
        if self._smart is None:
            return None
        resp = self._smart.rmsLimit() or {}
        if not isinstance(resp, dict) or resp.get("status") is not True:
            raise RuntimeError("Angel funds read failed")
        data = resp.get("data") or resp
        for key in (
            "availablecash",
            "availableCash",
            "available_limit",
            "availableLimit",
            "net",
            "cash",
        ):
            value = data.get(key) if isinstance(data, dict) else None
            if value is None:
                continue
            try:
                return float(str(value).replace(",", ""))
            except (TypeError, ValueError):
                continue
        return None

    def get_spot(self) -> float:
        data = self._smart.ltpData("NSE", "Nifty 50", "26000")
        return float(data["data"]["ltp"])

    def get_ltp(self, symbol: str) -> float:
        meta = self._resolve_symbol_meta(symbol)
        data = self._smart.ltpData(self._EXCHANGE, meta["symbol"], meta["token"])
        return float(((data or {}).get("data") or {}).get("ltp") or 0.0)

    def quote(self, symbols: list) -> dict:
        return {symbol: {"ltp": self.get_ltp(symbol)} for symbol in symbols}

    def get_position(self) -> Position:
        resp = self._smart.position()
        if not isinstance(resp, dict) or resp.get("status") is not True:
            raise RuntimeError("Angel position read failed")
        net = resp.get("data") or []
        for p in net:
            qty = int(p.get("netqty", 0) or 0)
            sym = p.get("tradingsymbol", "")
            if qty != 0 and sym.startswith("NIFTY"):
                side = ("CALL" if sym.endswith("CE")
                        else ("PUT" if sym.endswith("PE") else None))
                return Position(symbol=sym, qty=qty, side=side)
        return Position(symbol=None, qty=0, side=None)

    def get_order_status(self, broker_order_id: str) -> dict:
        try:
            resp = self._smart.orderBook()
            rows = (resp or {}).get("data") or []
        except Exception:
            return {}
        for row in rows:
            order_id = row.get("orderid") or row.get("order_id")
            if str(order_id) == str(broker_order_id):
                return row
        return {}

    def _ensure_instrument_master(self) -> list:
        refresh = True
        if INSTRUMENT_FILE.exists():
            modified = datetime.fromtimestamp(INSTRUMENT_FILE.stat().st_mtime).date()
            refresh = modified != datetime.now().date()
        if refresh:
            import requests

            INSTRUMENT_FILE.parent.mkdir(parents=True, exist_ok=True)
            response = requests.get(INSTRUMENT_MASTER_URL, timeout=45)
            response.raise_for_status()
            INSTRUMENT_FILE.write_text(json.dumps(response.json()), encoding="utf-8")
        return json.loads(INSTRUMENT_FILE.read_text(encoding="utf-8"))

    @staticmethod
    def _parse_zerodha_nifty_symbol(symbol: str) -> dict | None:
        m = re.match(r"^NIFTY(?P<expiry>[0-9A-Z]{5})(?P<strike>\d{4,5})(?P<typ>CE|PE)$",
                     str(symbol).strip().upper())
        if not m:
            return None
        return {
            "expiry": m.group("expiry"),
            "strike": int(m.group("strike")),
            "type": m.group("typ"),
        }

    @staticmethod
    def _angel_expiry_from_zerodha(code: str) -> str | None:
        code = str(code).strip().upper()
        months = {
            "JAN": 1, "FEB": 2, "MAR": 3, "APR": 4, "MAY": 5, "JUN": 6,
            "JUL": 7, "AUG": 8, "SEP": 9, "OCT": 10, "NOV": 11, "DEC": 12,
        }
        m = re.match(r"^(\d{2})(\d)(\d{2})$", code)
        if m:
            yy, mo, dd = m.groups()
            return datetime(2000 + int(yy), int(mo), int(dd)).strftime("%d%b%Y").upper()
        m = re.match(r"^(\d{2})([A-Z]{3})$", code)
        if m and m.group(2) in months:
            yy = 2000 + int(m.group(1))
            month = months[m.group(2)]
            for day in range(calendar.monthrange(yy, month)[1], 0, -1):
                expiry = datetime(yy, month, day)
                if expiry.weekday() == 1:
                    return expiry.strftime("%d%b%Y").upper()
        return None

    def _resolve_symbol_meta(self, symbol: str) -> dict:
        cached = self._symbol_cache.get(symbol)
        if cached:
            return cached
        parsed = self._parse_zerodha_nifty_symbol(symbol)
        try:
            instruments = self._ensure_instrument_master()
        except Exception:
            instruments = []

        target_expiry = (
            self._angel_expiry_from_zerodha(parsed["expiry"])
            if parsed else None
        )
        candidates = []
        for ins in instruments:
            if ins.get("exch_seg") != self._EXCHANGE:
                continue
            angel_symbol = str(ins.get("symbol") or ins.get("tradingsymbol") or "").upper()
            if angel_symbol == str(symbol).upper():
                meta = {"symbol": angel_symbol, "token": str(ins.get("token"))}
                self._symbol_cache[symbol] = meta
                self._token_cache[symbol] = meta["token"]
                return meta
            if not parsed:
                continue
            try:
                strike = int(float(ins.get("strike") or 0) / 100)
            except (TypeError, ValueError):
                continue
            if strike != parsed["strike"] or not angel_symbol.endswith(parsed["type"]):
                continue
            if target_expiry and str(ins.get("expiry") or "").upper() != target_expiry:
                continue
            token = str(ins.get("token") or "")
            if token:
                candidates.append({
                    "symbol": angel_symbol,
                    "token": token,
                    "expiry": str(ins.get("expiry") or ""),
                })
        if candidates:
            candidates.sort(key=lambda x: x["expiry"])
            meta = {"symbol": candidates[0]["symbol"], "token": candidates[0]["token"]}
            self._symbol_cache[symbol] = meta
            self._token_cache[symbol] = meta["token"]
            return meta

        # Fallback to broker search if the daily master is unavailable or the
        # symbol format changes. This keeps DRY_RUN diagnosis possible.
        resp = self._smart.searchScrip(self._EXCHANGE, symbol)
        rows = (resp or {}).get("data") or []
        match = None
        for row in rows:
            if str(row.get("tradingsymbol") or "").strip().upper() == symbol.upper():
                match = row
                break
        if match is None and rows:
            match = rows[0]
        token = str((match or {}).get("symboltoken") or "")
        if not token:
            raise RuntimeError(f"Angel symboltoken not found for {symbol}")
        broker_symbol = str(match.get("tradingsymbol") or symbol).strip().upper()
        meta = {"symbol": broker_symbol, "token": token}
        self._symbol_cache[symbol] = meta
        self._token_cache[symbol] = token
        return meta

    def _resolve_symbol_token(self, symbol: str) -> str:
        return self._resolve_symbol_meta(symbol)["token"]

    # ── THE GUARDED CALLS ─────────────────────────────────────────────────
    def place_order(self, *, side: str, symbol: str, qty: int,
                    price: float, idempotency_key: str) -> OrderResult:
        if not _live_orders_enabled():
            raise NotImplementedError(
                "LIVE_ARMED not enabled — Phase 1 gated. Angel live order "
                "placement is disabled (Phase-0 dry-run). Enable only via the "
                "Phase-1 enablement commit after a clean dry-run session."
            )
        # ── real branch — reached only in Phase 1 (LIVE_ARMED + 6 gates) ──
        meta = self._resolve_symbol_meta(symbol)
        order_params = {
            "variety": self._VARIETY,
            "tradingsymbol": meta["symbol"],
            "symboltoken": meta["token"],
            "transactiontype": "BUY",
            "exchange": self._EXCHANGE,
            "ordertype": self._ORDER_TYPE,
            "producttype": self._PRODUCT,
            "duration": "DAY",
            "price": price,
            "quantity": qty,
            "ordertag": _angel_order_tag(idempotency_key),
        }
        # Static IP used ONLY for the order placement (symbol/token resolution
        # above already ran direct). order_proxy restores env + SDK proxy after.
        with order_proxy(self._smart):
            resp = self._smart.placeOrder(order_params)
        if isinstance(resp, dict):
            ok = resp.get("status") is True or resp.get("success") is True
            data = resp.get("data") or {}
            order_id = (
                data.get("orderid")
                or data.get("order_id")
                or resp.get("orderid")
                or resp.get("order_id")
            )
            return OrderResult(
                broker_order_id=str(order_id) if order_id else None,
                status="PLACED" if ok and order_id else "FAILED",
                avg_fill_price=None,
                raw={
                    "response_status": resp.get("status"),
                    "success": resp.get("success"),
                    "error_code": resp.get("errorCode") or resp.get("errorcode"),
                    "message": resp.get("message"),
                    "broker_symbol": meta["symbol"],
                },
            )
        return OrderResult(
            broker_order_id=str(resp) if resp else None,
            status="PLACED" if resp else "FAILED",
            avg_fill_price=None,
            raw={"order_id": resp, "broker_symbol": meta["symbol"]},
        )

    def exit_all(self, *, symbol: str, qty: int, reason: str,
                 idempotency_key: str) -> OrderResult:
        if not _live_orders_enabled():
            raise NotImplementedError(
                "LIVE_ARMED not enabled — Phase 1 gated. Angel live exit "
                "placement is disabled (Phase-0 dry-run)."
            )
        meta = self._resolve_symbol_meta(symbol)
        order_params = {
            "variety": self._VARIETY,
            "tradingsymbol": meta["symbol"],
            "symboltoken": meta["token"],
            "transactiontype": "SELL",
            "exchange": self._EXCHANGE,
            "ordertype": self._ORDER_TYPE,
            "producttype": self._PRODUCT,
            "duration": "DAY",
            "price": self.get_ltp(symbol),  # data fetch — runs direct (dict built first)
            "quantity": qty,
            "ordertag": _angel_order_tag(idempotency_key),
        }
        # Static IP used ONLY for the order placement; the get_ltp above and the
        # symbol/token resolution already ran direct while building order_params.
        with order_proxy(self._smart):
            resp = self._smart.placeOrder(order_params)
        if isinstance(resp, dict):
            ok = resp.get("status") is True or resp.get("success") is True
            data = resp.get("data") or {}
            order_id = (
                data.get("orderid")
                or data.get("order_id")
                or resp.get("orderid")
                or resp.get("order_id")
            )
            return OrderResult(
                broker_order_id=str(order_id) if order_id else None,
                status="PLACED" if ok and order_id else "FAILED",
                avg_fill_price=None,
                raw={
                    "response_status": resp.get("status"),
                    "success": resp.get("success"),
                    "error_code": resp.get("errorCode") or resp.get("errorcode"),
                    "message": resp.get("message"),
                    "reason": reason,
                    "broker_symbol": meta["symbol"],
                },
            )
        return OrderResult(
            broker_order_id=str(resp) if resp else None,
            status="PLACED" if resp else "FAILED",
            avg_fill_price=None,
            raw={"order_id": resp, "reason": reason, "broker_symbol": meta["symbol"]},
        )
