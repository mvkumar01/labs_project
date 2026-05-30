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
from .base import BrokerAdapter, OrderResult, Position

# ── Phase-1 enablement flag. Flipping to True is the ONLY thing that lets a
#    real Angel order leave this process. Reviewed commit only. ───────────
_LIVE_ORDERS_ENABLED = False


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

    # ── session ─────────────────────────────────────────────────────────
    def connect(self) -> None:
        """generateSession(client_code, pin, totp) from decrypted creds."""
        from SmartApi import SmartConnect  # SDK import isolated to this pkg
        import pyotp                        # TOTP for Angel login

        self._smart = SmartConnect(api_key=self._creds["api_key"])
        totp = pyotp.TOTP(self._creds["totp_secret"]).now()
        self._smart.generateSession(
            self._creds["client_code"],
            self._creds["pin"],
            totp,
        )

    def is_connected(self) -> bool:
        if self._smart is None:
            return False
        try:
            # Cheap authenticated read — profile/RMS limit. Any success means
            # the session token is live. Never logs cred values.
            self._smart.rmsLimit()
            return True
        except Exception:
            return False

    def account_ref(self) -> str:
        # Angel is a different broker from Bot A's Zerodha book — isolation
        # gate 4 passes trivially, but we still return a stable identifier.
        return f"angel:{self._client_code}"

    # ── market reads ──────────────────────────────────────────────────────
    def get_spot(self) -> float:
        data = self._smart.ltpData("NSE", "Nifty 50", "26000")
        return float(data["data"]["ltp"])

    def get_ltp(self, symbol: str) -> float:
        raise NotImplementedError(
            "Angel get_ltp requires symbol-token resolution — wired in Phase 1."
        )

    def quote(self, symbols: list) -> dict:
        raise NotImplementedError("Angel quote() wired in Phase 1.")

    def get_position(self) -> Position:
        try:
            resp = self._smart.position()
            net = (resp or {}).get("data") or []
        except Exception:
            return Position(symbol=None, qty=0, side=None)
        for p in net:
            qty = int(p.get("netqty", 0) or 0)
            sym = p.get("tradingsymbol", "")
            if qty != 0 and sym.startswith("NIFTY"):
                side = ("CALL" if sym.endswith("CE")
                        else ("PUT" if sym.endswith("PE") else None))
                return Position(symbol=sym, qty=qty, side=side)
        return Position(symbol=None, qty=0, side=None)

    def get_order_status(self, broker_order_id: str) -> dict:
        raise NotImplementedError("Angel get_order_status wired in Phase 1.")

    # ── THE GUARDED CALLS ─────────────────────────────────────────────────
    def place_order(self, *, side: str, symbol: str, qty: int,
                    price: float, idempotency_key: str) -> OrderResult:
        if not _LIVE_ORDERS_ENABLED:
            raise NotImplementedError(
                "LIVE_ARMED not enabled — Phase 1 gated. Angel live order "
                "placement is disabled (Phase-0 dry-run). Enable only via the "
                "Phase-1 enablement commit after a clean dry-run session."
            )
        # ── real branch — reached only in Phase 1 (LIVE_ARMED + 6 gates) ──
        order_params = {
            "variety": self._VARIETY,
            "tradingsymbol": symbol,
            "transactiontype": "BUY",
            "exchange": self._EXCHANGE,
            "ordertype": self._ORDER_TYPE,
            "producttype": self._PRODUCT,
            "duration": "DAY",
            "price": price,
            "quantity": qty,
            "ordertag": idempotency_key,
        }
        resp = self._smart.placeOrder(order_params)
        return OrderResult(
            broker_order_id=str(resp) if resp else None,
            status="PLACED" if resp else "FAILED",
            avg_fill_price=None,
            raw={"order_id": resp},
        )

    def exit_all(self, *, symbol: str, qty: int, reason: str,
                 idempotency_key: str) -> OrderResult:
        if not _LIVE_ORDERS_ENABLED:
            raise NotImplementedError(
                "LIVE_ARMED not enabled — Phase 1 gated. Angel live exit "
                "placement is disabled (Phase-0 dry-run)."
            )
        order_params = {
            "variety": self._VARIETY,
            "tradingsymbol": symbol,
            "transactiontype": "SELL",
            "exchange": self._EXCHANGE,
            "ordertype": self._ORDER_TYPE,
            "producttype": self._PRODUCT,
            "duration": "DAY",
            "price": self.get_ltp(symbol),
            "quantity": qty,
            "ordertag": idempotency_key,
        }
        resp = self._smart.placeOrder(order_params)
        return OrderResult(
            broker_order_id=str(resp) if resp else None,
            status="PLACED" if resp else "FAILED",
            avg_fill_price=None,
            raw={"order_id": resp, "reason": reason},
        )
