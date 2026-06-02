"""
Zerodha adapter (secondary) — wraps kiteconnect.KiteConnect.

★ Permitted to import the broker SDK (lives under live/brokers/). ★

CRITICAL DRY-RUN GUARD (spec §5.4, §13): identical to angel.py —
`_LIVE_ORDERS_ENABLED = False` makes `place_order` / `exit_all` raise
NotImplementedError("LIVE_ARMED not enabled — Phase 1 gated") so NO real
order can fire in this build.

ACCOUNT ISOLATION (spec §12.2): Gate 4 (`gate_account_isolation` in
live_executor) blocks arming if this account is already claimed by another
user's connection. This adapter uses NFO MIS LIMIT orders at LTP.

MULTI-USER: one instance per (user_id, conn_id). Each user's Zerodha session
comes from that user's OWN stored encrypted token (creds).
auth.session_manager.get_kite() is available as shared neutral infra and may
be used ONLY here inside live/brokers/.
"""
import os

from .base import BrokerAdapter, OrderResult, Position
from live.proxy import configure_outbound_proxy


def build_login_url(api_key: str) -> str:
    return f"https://kite.trade/connect/login?v=3&api_key={api_key}"


def exchange_request_token(*, api_key: str, api_secret: str, request_token: str) -> dict:
    configure_outbound_proxy()
    from kiteconnect import KiteConnect

    kite = KiteConnect(api_key=api_key)
    data = kite.generate_session(request_token, api_secret=api_secret)
    kite.set_access_token(data["access_token"])
    profile = kite.profile()
    return {
        "access_token": data.get("access_token"),
        "public_token": data.get("public_token"),
        "user_id": profile.get("user_id") or data.get("user_id"),
    }


def _live_orders_enabled() -> bool:
    env_enabled = os.environ.get("LIVE_ORDERS_ENABLED", "0").strip().lower()
    return _LIVE_ORDERS_ENABLED and env_enabled in {"1", "true", "yes"}

# ── Phase-1 enablement flag (reviewed commit only). ──────────────────────
_LIVE_ORDERS_ENABLED = False


class ZerodhaAdapter(BrokerAdapter):
    broker_name = "zerodha"

    def __init__(self, *, user_id: str, conn_id: str, creds: dict):
        super().__init__(user_id=user_id, conn_id=conn_id, creds=creds)
        self._kite = None

    # ── session ─────────────────────────────────────────────────────────
    def connect(self) -> None:
        """Build a KiteConnect session from THIS user's own encrypted creds.

        Falls back to the shared labs session (auth.session_manager) only when
        no per-user api_key/access_token were supplied — both paths keep the
        SDK import inside live/brokers/.
        """
        configure_outbound_proxy()
        api_key = self._creds.get("api_key")
        access_token = self._creds.get("access_token")
        if not api_key or not access_token:
            raise RuntimeError("missing Zerodha api_key/access_token for this user connection")
        from kiteconnect import KiteConnect  # SDK isolated to this pkg

        self._kite = KiteConnect(api_key=api_key)
        self._kite.set_access_token(access_token)

    def is_connected(self) -> bool:
        if self._kite is None:
            return False
        try:
            self._kite.profile()  # cheap authenticated ping
            return True
        except Exception:
            return False

    def account_ref(self) -> str:
        user_id = (self._creds.get("user_id") or "").strip()
        return f"zerodha:{user_id}" if user_id else f"zerodha:{self._creds.get('api_key', '')}"

    # ── market reads ─────────────────────────────────────────────────────
    def available_funds(self) -> float | None:
        if self._kite is None:
            return None
        data = self._kite.margins() or {}
        available = ((data.get("equity") or {}).get("available") or {})
        for key in ("live_balance", "cash", "opening_balance"):
            value = available.get(key)
            if value is None:
                continue
            try:
                return float(value)
            except (TypeError, ValueError):
                continue
        return None

    def get_spot(self) -> float:
        data = self._kite.ltp("NSE:NIFTY 50")
        return float(data["NSE:NIFTY 50"]["last_price"])

    def get_ltp(self, symbol: str) -> float:
        key = f"NFO:{symbol}"
        return float(self._kite.ltp(key)[key]["last_price"])

    def quote(self, symbols: list) -> dict:
        keys = [f"NFO:{s}" for s in symbols]
        return self._kite.quote(keys)

    def get_position(self) -> Position:
        # First NIFTY MIS non-zero net leg for this live account.
        try:
            net = self._kite.positions()["net"]
        except Exception:
            return Position(symbol=None, qty=0, side=None)
        for p in net:
            if (p["tradingsymbol"].startswith("NIFTY")
                    and p["product"] == "MIS"
                    and p["quantity"] != 0):
                sym = p["tradingsymbol"]
                side = ("CALL" if sym.endswith("CE")
                        else ("PUT" if sym.endswith("PE") else None))
                return Position(symbol=sym, qty=p["quantity"], side=side)
        return Position(symbol=None, qty=0, side=None)

    def get_order_status(self, broker_order_id: str) -> dict:
        for o in self._kite.orders():
            if str(o.get("order_id")) == str(broker_order_id):
                return o
        return {}

    # ── THE GUARDED CALLS ─────────────────────────────────────────────────
    def place_order(self, *, side: str, symbol: str, qty: int,
                    price: float, idempotency_key: str) -> OrderResult:
        if not _live_orders_enabled():
            raise NotImplementedError(
                "LIVE_ARMED not enabled — Phase 1 gated. Zerodha live order "
                "placement is disabled (Phase-0 dry-run). Enable only via the "
                "Phase-1 enablement commit after a clean dry-run session."
            )
        # ── real branch — reached only in Phase 1 (LIVE_ARMED + 6 gates) ──
        order_id = self._kite.place_order(
            variety=self._kite.VARIETY_REGULAR,
            exchange=self._kite.EXCHANGE_NFO,
            tradingsymbol=symbol,
            transaction_type=self._kite.TRANSACTION_TYPE_BUY,
            quantity=qty,
            product=self._kite.PRODUCT_MIS,
            order_type=self._kite.ORDER_TYPE_LIMIT,
            price=price,
            tag=idempotency_key[:20],  # Zerodha tag max 20 chars
        )
        return OrderResult(
            broker_order_id=str(order_id),
            status="PLACED",
            avg_fill_price=None,
            raw={"order_id": order_id},
        )

    def exit_all(self, *, symbol: str, qty: int, reason: str,
                 idempotency_key: str) -> OrderResult:
        if not _live_orders_enabled():
            raise NotImplementedError(
                "LIVE_ARMED not enabled — Phase 1 gated. Zerodha live exit "
                "placement is disabled (Phase-0 dry-run)."
            )
        order_id = self._kite.place_order(
            variety=self._kite.VARIETY_REGULAR,
            exchange=self._kite.EXCHANGE_NFO,
            tradingsymbol=symbol,
            transaction_type=self._kite.TRANSACTION_TYPE_SELL,
            quantity=qty,
            product=self._kite.PRODUCT_MIS,
            order_type=self._kite.ORDER_TYPE_LIMIT,
            price=self.get_ltp(symbol),
            tag=idempotency_key[:20],
        )
        return OrderResult(
            broker_order_id=str(order_id),
            status="PLACED",
            avg_fill_price=None,
            raw={"order_id": order_id, "reason": reason},
        )
