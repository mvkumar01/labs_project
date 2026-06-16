"""
Broker adapter ABC + value objects (spec §5.3).

★ This module and its concrete siblings (angel.py, zerodha.py) are the ONLY
  files in the repo allowed to import a broker order SDK. ★

`BrokerAdapter` abstracts the broker surface so `live_executor` /
`live_runner` never touch an SDK directly. The guarded `place_order` /
`exit_all` MUST raise NotImplementedError until a deliberate Phase-1
enablement (spec §5.4 / §13).

MULTI-USER (spec §2, §5.3): one adapter instance per active connection.
The constructor takes the owning `user_id` + `conn_id` plus the in-memory
decrypted `creds` dict (NEVER stored, logged, or echoed). Adapter state is
therefore inherently per-connection and never shared between users.
"""
from abc import ABC, abstractmethod
from dataclasses import dataclass
from typing import Optional


@dataclass
class OrderResult:
    broker_order_id: Optional[str]   # None on DRY-RUN / failure
    status: str                      # "PLACED" | "REJECTED" | "DRY_RUN" | "FAILED"
    avg_fill_price: Optional[float]
    raw: Optional[dict]              # broker payload (NEVER contains creds)


@dataclass
class Position:
    symbol: Optional[str]
    qty: int                         # signed; 0 == flat
    side: Optional[str]              # "CALL" | "PUT" | None


class BrokerAdapter(ABC):
    """Abstract broker surface. Concrete adapters live in this package only.

    One instance per active (user_id, conn_id) connection.
    """

    broker_name: str = ""            # "angel" | "zerodha"

    def __init__(self, *, user_id: str, conn_id: str, creds: dict):
        # creds is the in-memory decrypted dict (crypto/live_service.decrypt).
        # NEVER stored to disk, logged, or echoed.
        self.user_id = user_id
        self.conn_id = conn_id
        self._creds = creds or {}

    @abstractmethod
    def connect(self) -> None:
        """Establish a session from the in-memory creds (held in-memory only)."""
        ...

    @abstractmethod
    def is_connected(self) -> bool:
        """Cheap auth ping for gate 3 (broker_connected)."""
        ...

    @abstractmethod
    def account_ref(self) -> str:
        """Stable account identifier for the isolation gate (spec §6 gate 4)."""
        ...

    @abstractmethod
    def available_funds(self) -> float | None:
        """Available cash/margin snapshot for display. Never returns secrets."""
        ...

    @abstractmethod
    def get_spot(self) -> float:
        """NIFTY index LTP."""
        ...

    @abstractmethod
    def get_ltp(self, symbol: str) -> float:
        ...

    @abstractmethod
    def quote(self, symbols: list) -> dict:
        """Quote batch (used for OI walls)."""
        ...

    @abstractmethod
    def get_position(self) -> Position:
        """Current broker NFO MIS position for this live connection."""
        ...

    def get_net_book(self) -> dict:
        """Full NIFTY net book {tradingsymbol: signed_qty} for ALL nonzero legs,
        used by the multi-source order manager so two books on one account
        reconcile per-symbol. Default {} — adapters that don't override cannot
        run order-manager mode (reconcile fails closed). zerodha/angel override."""
        return {}

    @abstractmethod
    def get_order_status(self, broker_order_id: str) -> dict:
        ...

    # ── THE GUARDED CALLS ────────────────────────────────────────────────
    @abstractmethod
    def place_order(self, *, side: str, symbol: str, qty: int,
                    price: float, idempotency_key: str) -> OrderResult:
        """MUST raise NotImplementedError until Phase-1 enablement (spec §5.4).

        Concrete adapters implement the real broker call ONLY behind the
        `_LIVE_ORDERS_ENABLED` guard. DRY_RUN never reaches the real branch:
        `live_executor` short-circuits to a simulated OrderResult and only
        calls this in LIVE_ARMED after all 6 gates pass.
        """
        ...

    @abstractmethod
    def exit_all(self, *, symbol: str, qty: int, reason: str,
                 idempotency_key: str) -> OrderResult:
        """Square off `qty` of `symbol`. Same guard as place_order in Phase 0."""
        ...
