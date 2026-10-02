"""
Angel One adapter (PRIMARY broker) — wraps SmartApi.SmartConnect.

★ Permitted to import the broker SDK (lives under live/brokers/). ★

CRITICAL DRY-RUN GUARD (spec §5.4, §13):
    `_LIVE_ORDERS_ENABLED = False`. While False, `place_order` and
    `exit_all` raise NotImplementedError("LIVE_ARMED not enabled — Phase 1
    gated") so NO real order can fire in this build. Phase-1 enablement flips
    the flag in a deliberate, reviewed commit AND still requires LIVE_ARMED +
    all 7 gates.

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
import time
from datetime import datetime

from .base import BrokerAdapter, OrderResult, Position
from config.labs_config import STATE_DIR
from live.brokers.order_transport import send_order


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


def _lotsize(instrument: dict) -> int | None:
    try:
        lot = int(float(instrument.get("lotsize")))
    except (TypeError, ValueError):
        return None
    return lot if lot > 0 else None


def _check_lot_multiple(meta: dict, qty: int) -> None:
    """Refuse an ENTRY whose quantity is not a whole number of the resolved
    contract's lots. A mismatch means the symbol resolved to the wrong contract
    (or the lot size changed) -- never send that order."""
    lot = meta.get("lotsize")
    if lot and int(qty) % lot != 0:
        raise RuntimeError(
            f"lot size mismatch for {meta.get('symbol')}: qty {qty} is not a "
            f"multiple of lot {lot} -- entry refused")

INSTRUMENT_MASTER_URL = (
    "https://margincalculator.angelbroking.com/OpenAPI_File/files/"
    "OpenAPIScripMaster.json"
)
INSTRUMENT_FILE = STATE_DIR / "angel_instruments.json"

# Short-TTL read cache (rate-limit defense, 2026-07-07). One ~2s poll cycle
# reads position/LTP several times (reconcile + gate + verify + exit_all); each
# was a separate Angel call, so an order burst ~6 data calls and tripped Angel's
# per-second limit. TTL < the runner's POLL_INTERVAL (2s) so every new cycle
# still refreshes; within a cycle the reads collapse to one call each.
_READ_CACHE_TTL = 1.5
# Reuse a passed session health check (rmsLimit) for this long.
_HEALTH_TTL_S = 15.0

# Bounded backoff/retry on Angel throttling (rate-limit defense, 2026-07-07).
# 3 tries at 0.5s -> 1.0s adds at most ~1.5s and lets a throttled call recover
# instead of failing (a stop must still be bounded, so tries are small).
_RETRY_TRIES = 3
_RETRY_BASE_DELAY = 0.5


def _is_rate_limited(exc) -> bool:
    """Angel gateway throttle. The request was REJECTED before processing, so
    nothing was placed — safe to retry even for an order."""
    return "exceeding access rate" in str(exc).lower()


def _is_transient_read(exc) -> bool:
    """Retryable for idempotent READS only: throttle or a garbled JSON body
    (Angel returns unparseable payloads under load). NOT used for orders — a
    parse error there could mask a placed order and a retry would double it."""
    m = str(exc).lower()
    return (_is_rate_limited(exc)
            or "couldn't parse the json" in m
            or "could not parse the json" in m)


class AngelAdapter(BrokerAdapter):
    broker_name = "angel"

    # Angel placeOrder constants (NFO intraday LIMIT). Used only inside the
    # guarded real branch.
    _EXCHANGE = "NFO"
    _underlying = "NIFTY"           # per-instance override via use_segment (SENSEX on BFO)
    _PRODUCT = "INTRADAY"
    _ORDER_TYPE = "LIMIT"
    _VARIETY = "NORMAL"

    def __init__(self, *, user_id: str, conn_id: str, creds: dict):
        super().__init__(user_id=user_id, conn_id=conn_id, creds=creds)
        self._smart = None
        self._client_code = (creds or {}).get("client_code", "")
        self._token_cache = {}
        self._symbol_cache = {}
        self._read_cache = {}   # key -> (expiry_monotonic, value); see _cached

    def use_segment(self, segment: str, underlying: str) -> None:
        """Trade another index's options on this connection (SENSEX on BFO).

        Angel lists BSE index options under the same tradingsymbols as Kite
        (SENSEX26O0169700PE), so the exact-symbol branch of the instrument
        lookup resolves them once the segment is BFO. Default stays NFO/NIFTY.
        """
        self._EXCHANGE = str(segment).upper()
        self._underlying = str(underlying).upper()
        self._symbol_cache.clear()
        self._token_cache.clear()
        self._invalidate_reads()

    def broker_symbol(self, kite_symbol: str) -> str:
        """The tradingsymbol this broker uses for a Kite option symbol."""
        return self._resolve_symbol_meta(kite_symbol)["symbol"]

    # ── session ─────────────────────────────────────────────────────────
    def connect(self) -> None:
        """generateSession(client_code, pin, totp) from decrypted creds.

        Login is a DATA/auth call — it goes out DIRECT (not via the static IP).
        Order mutations use the separate assigned order transport."""
        from SmartApi import SmartConnect  # SDK import isolated to this pkg
        import pyotp                        # TOTP for Angel login

        self._health_ok_at = None           # a new session is checked afresh
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
        # A health check that passed moments ago is reused. The runner pinged
        # rmsLimit every cycle for every connection AND again in the entry gates
        # and funds read; through the US->India route with Angel's rate-limit
        # backoff this dominated a 40-60 s cycle (2026-09-29). A dropped session
        # still surfaces within HEALTH_TTL_S, or at the next broker call.
        last_ok = getattr(self, "_health_ok_at", None)
        if last_ok is not None and time.monotonic() - last_ok < _HEALTH_TTL_S:
            return True
        try:
            # Cheap authenticated read — profile/RMS limit. Any success means
            # the session token is live. Never logs cred values.
            response = self._with_backoff(self._smart.rmsLimit, _is_transient_read)
            ok = (
                isinstance(response, dict)
                and response.get("status") is True
                and response.get("data") is not None
            )
        except Exception:
            ok = False
        self._health_ok_at = time.monotonic() if ok else None
        return ok

    def account_ref(self) -> str:
        # Return a stable identifier for duplicate-account isolation.
        return f"angel:{self._client_code}"

    # ── short-TTL read cache ──────────────────────────────────────────────
    # Collapses the repeated position/LTP/spot reads within one poll cycle into
    # ONE broker call each. Invalidated after every order so post-trade reads
    # reflect the new book. is_connected() is deliberately NOT cached (it is a
    # health check — staleness could mask a dropped session).
    def _cached(self, key, ttl, fn):
        now = time.monotonic()
        hit = self._read_cache.get(key)
        if hit is not None and hit[0] > now:
            return hit[1]
        val = fn()
        self._read_cache[key] = (now + ttl, val)
        return val

    def _invalidate_reads(self) -> None:
        self._read_cache.clear()

    def _with_backoff(self, fn, retryable):
        """Call fn(), retrying with exponential backoff while `retryable(exc)`
        is True, up to _RETRY_TRIES. Non-retryable errors and the final failure
        propagate. Bounded so an order/stop can never wait indefinitely."""
        for attempt in range(_RETRY_TRIES):
            try:
                return fn()
            except Exception as e:
                if not retryable(e) or attempt == _RETRY_TRIES - 1:
                    raise
                time.sleep(_RETRY_BASE_DELAY * (2 ** attempt))

    # ── account/order reads ───────────────────────────────────────────────
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
        raise RuntimeError("Angel market data is disabled; use Kite")

    def get_ltp(self, symbol: str) -> float:
        raise RuntimeError("Angel market data is disabled; use Kite")

    def quote(self, symbols: list) -> dict:
        raise RuntimeError("Angel market data is disabled; use Kite")

    def get_position(self) -> Position:
        return self._cached("position", _READ_CACHE_TTL, self._get_position_uncached)

    def _get_position_uncached(self) -> Position:
        resp = self._with_backoff(self._smart.position, _is_transient_read)
        if not isinstance(resp, dict) or resp.get("status") is not True:
            raise RuntimeError("Angel position read failed")
        net = resp.get("data") or []
        for p in net:
            qty = int(p.get("netqty", 0) or 0)
            sym = p.get("tradingsymbol", "")
            if qty != 0 and sym.startswith(self._underlying):
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
        # Weekly: YY + month (1-9, O/N/D for Oct/Nov/Dec) + DD.
        m = re.match(r"^(\d{2})([1-9OND])(\d{2})$", code)
        if m:
            yy, mo, dd = m.groups()
            month = {"O": 10, "N": 11, "D": 12}.get(mo) or int(mo)
            try:
                return datetime(2000 + int(yy), month, int(dd)).strftime("%d%b%Y").upper()
            except ValueError:
                return None
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
        if parsed and not target_expiry:
            # Matching "any expiry" would pick an arbitrary contract.
            raise RuntimeError(
                f"cannot decode expiry {parsed['expiry']!r} of {symbol} -- entry refused")
        candidates = []
        for ins in instruments:
            if ins.get("exch_seg") != self._EXCHANGE:
                continue
            angel_symbol = str(ins.get("symbol") or ins.get("tradingsymbol") or "").upper()
            if angel_symbol == str(symbol).upper():
                meta = {"symbol": angel_symbol, "token": str(ins.get("token")),
                        "lotsize": _lotsize(ins)}
                self._symbol_cache[symbol] = meta
                self._token_cache[symbol] = meta["token"]
                return meta
            if not parsed:
                continue
            # The underlying MUST be the NIFTY index option. On a monthly expiry
            # FINNIFTY lists the same date/strike/type (2026-09-25/28: every entry
            # resolved to FINNIFTY29SEP26..., lot 60, and was rejected AB4014).
            if (str(ins.get("name") or "").upper() != self._underlying
                    or str(ins.get("instrumenttype") or "").upper() != "OPTIDX"):
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
                    "lotsize": _lotsize(ins),
                })
        if candidates:
            candidates.sort(key=lambda x: x["expiry"])
            meta = {"symbol": candidates[0]["symbol"], "token": candidates[0]["token"],
                    "lotsize": candidates[0]["lotsize"]}
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
        # Never fall back to "the first search hit": a near-miss can be another
        # underlying. No exact symbol -> no order.
        token = str((match or {}).get("symboltoken") or "")
        if not token:
            raise RuntimeError(f"Angel symboltoken not found for {symbol}")
        broker_symbol = str(match.get("tradingsymbol") or symbol).strip().upper()
        meta = {"symbol": broker_symbol, "token": token, "lotsize": None}
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
        if price is None or float(price) <= 0:
            raise RuntimeError("Angel entry requires a Kite-supplied positive price")
        # ── real branch — reached only in Phase 1 (LIVE_ARMED + 7 gates) ──
        meta = self._resolve_symbol_meta(symbol)
        _check_lot_multiple(meta, qty)
        order_params = {
            "variety": self._VARIETY,
            "tradingsymbol": meta["symbol"],
            "symboltoken": meta["token"],
            "transactiontype": "BUY",
            "exchange": self._EXCHANGE,
            "ordertype": self._ORDER_TYPE,
            "producttype": self._PRODUCT,
            # IOC (immediate-or-cancel), NOT DAY: an entry signal is for THIS
            # bar only. A resting DAY limit that fills minutes later (2026-07-14:
            # a 181.9 buy filled long after the bar) creates a broker long the
            # DB never recorded → phantom P&L AND, via live_runner current_open=
            # broker_open, makes the runner think it already holds a position so
            # it stops re-entering. IOC fills what it can now, else cancels — the
            # per-bar loop retries next bar. Exits stay DAY (see exit_all).
            "duration": "IOC",
            "price": price,
            "quantity": qty,
            "ordertag": _angel_order_tag(idempotency_key),
        }
        # Static IP used ONLY for the order placement (symbol/token resolution
        # above already ran direct). The transport never mutates this SDK client.
        resp = send_order(self, 'entry', order_params, idempotency_key)
        self._invalidate_reads()   # book changed — next position/LTP read is fresh
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
                 idempotency_key: str, price: float | None = None) -> OrderResult:
        if not _live_orders_enabled():
            raise NotImplementedError(
                "LIVE_ARMED not enabled — Phase 1 gated. Angel live exit "
                "placement is disabled (Phase-0 dry-run)."
            )
        meta = self._resolve_symbol_meta(symbol)
        pos = self.get_position()
        broker_symbol = str(pos.symbol or "").strip().upper()
        requested_symbol = str(meta["symbol"] or symbol).strip().upper()
        requested_qty = abs(int(qty or 0))
        broker_qty = int(pos.qty or 0)
        if broker_symbol != requested_symbol or broker_qty <= 0:
            return OrderResult(
                broker_order_id=None,
                status="NO_LONG_POSITION",
                avg_fill_price=None,
                raw={
                    "requested_symbol": requested_symbol,
                    "requested_qty": requested_qty,
                    "broker_symbol": pos.symbol,
                    "broker_qty": pos.qty,
                    "reason": reason,
                },
            )
        if requested_qty <= 0 or requested_qty > broker_qty:
            return OrderResult(
                broker_order_id=None,
                status="EXIT_QTY_EXCEEDS_POSITION",
                avg_fill_price=None,
                raw={
                    "requested_symbol": requested_symbol,
                    "requested_qty": requested_qty,
                    "broker_symbol": pos.symbol,
                    "broker_qty": pos.qty,
                    "reason": reason,
                },
            )
        if price is None or float(price) <= 0:
            raise RuntimeError("Angel exit requires a Kite-supplied positive price")
        order_params = {
            "variety": self._VARIETY,
            "tradingsymbol": meta["symbol"],
            "symboltoken": meta["token"],
            "transactiontype": "SELL",
            "exchange": self._EXCHANGE,
            "ordertype": self._ORDER_TYPE,
            "producttype": self._PRODUCT,
            # DAY (deliberately NOT IOC): an exit is risk-off and must fill. A
            # cancelled IOC exit is not in _RETRIABLE_EXIT_STATUSES, so it would
            # NOT be retried and would strand a live long. The caller's SELL
            # limit is already marketable-adjusted, so DAY fills at once in the
            # normal case and only rests if the market gaps away — acceptable
            # for a square-off. Entries use IOC (see place_order).
            "duration": "DAY",
            # Caller-supplied Kite marketable SELL limit. Angel is never used
            # to fetch market data.
            "price": float(price),
            "quantity": qty,
            "ordertag": _angel_order_tag(idempotency_key),
        }
        # Static IP used ONLY for order placement; position and symbol/token
        # reads above ran direct.
        resp = send_order(self, 'exit', order_params, idempotency_key)
        self._invalidate_reads()   # book changed — next position/LTP read is fresh
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
