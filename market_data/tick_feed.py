"""Kite websocket price cache for the Proposer runner.

The broker pushes index and held-option prices the moment they change, instead of the runner reading
them over REST once a second. Read-only market data from the labs Kite data session (never an order
path). A price older than STALE_S, or one never received, reads as None so the caller falls back to
its REST read - the feed can only make prices fresher, never block a decision."""
from __future__ import annotations

import logging
import threading
import time

log = logging.getLogger(__name__)

STALE_S = 2.5                 # an older tick is not trusted: the REST read is used instead
IDLE_S = 600.0                # an option nobody asked about for this long is unsubscribed
FORBIDDEN_BACKOFF_S = 300.0   # a rejected session is not retried until then (or a new token)


def _best(levels, pick):
    px = [float(x.get("price") or 0) for x in levels or [] if float(x.get("price") or 0) > 0]
    return pick(px) if px else None


def _kite_ticker(api_key: str, access_token: str):
    from kiteconnect import KiteTicker
    return KiteTicker(api_key, access_token, reconnect=True, reconnect_max_tries=50, reconnect_max_delay=60)


def _in_loop(fn, *args) -> None:
    """Run a websocket call on the reactor thread that owns the socket (direct when it isn't running)."""
    try:
        from twisted.internet import reactor
        if reactor.running:
            reactor.callFromThread(fn, *args)
            return
    except ImportError:
        pass
    fn(*args)


class TickFeed:
    def __init__(self, credentials, ticker_factory=None, clock=time.monotonic):
        self._credentials = credentials          # () -> (api_key, access_token)
        self._factory = ticker_factory or _kite_ticker
        self._clock = clock
        self._lock = threading.Lock()
        self._prices: dict[int, tuple[float, float | None, float | None, float | None]] = {}
        self._wanted: dict[int, float | None] = {}   # token -> last asked (None = always)
        self._ticker = None
        self._session = None
        self._connected = False
        self._retry_at = 0.0
        self.ticked = threading.Event()

    # ── reads ────────────────────────────────────────────────────────────────
    def price(self, token: int, max_age: float = STALE_S) -> dict | None:
        with self._lock:
            p = self._prices.get(int(token))
        if p is None or self._clock() - p[0] > max_age:
            return None
        return {"ltp": p[1], "bid": p[2], "ask": p[3]}

    def live(self) -> bool:
        return self._ticker is not None and self._connected

    def wait(self, timeout: float) -> bool:
        """Block until the next tick or `timeout` seconds; True when a tick arrived."""
        fired = self.ticked.wait(timeout)
        self.ticked.clear()
        return fired

    # ── subscriptions ────────────────────────────────────────────────────────
    def watch(self, token: int, always: bool = False) -> None:
        token = int(token)
        with self._lock:
            new = token not in self._wanted
            self._wanted[token] = None if always else self._clock()
        self._ensure()
        if new and self.live():
            self._subscribe([token])
        self._prune()

    def _subscribe(self, tokens: list[int]) -> None:
        t = self._ticker
        if t is None or not tokens:
            return
        try:
            _in_loop(t.subscribe, tokens)
            _in_loop(t.set_mode, t.MODE_FULL, tokens)
        except Exception as e:
            log.warning("tick subscribe failed: %s", type(e).__name__)

    def _prune(self) -> None:
        now = self._clock()
        with self._lock:
            idle = [k for k, at in self._wanted.items() if at is not None and now - at > IDLE_S]
            for k in idle:
                del self._wanted[k]
                self._prices.pop(k, None)
        if idle and self.live():
            try:
                _in_loop(self._ticker.unsubscribe, idle)
            except Exception as e:
                log.warning("tick unsubscribe failed: %s", type(e).__name__)

    # ── connection ───────────────────────────────────────────────────────────
    def _ensure(self) -> None:
        try:
            key = tuple(self._credentials())
        except Exception as e:
            log.warning("tick feed has no Kite session: %s", type(e).__name__)
            return
        if key == self._session and (self._ticker is not None or self._clock() < self._retry_at):
            return
        self.close()
        self._session = key
        try:
            t = self._factory(*key)
            t.on_connect = self._on_connect
            t.on_ticks = self._on_ticks
            t.on_error = self._on_error
            t.on_close = self._on_close
            t.on_noreconnect = self._on_noreconnect
            self._ticker = t
            t.connect(threaded=True)
            log.info("tick feed connecting (%d tokens)", len(self._wanted))
        except Exception as e:
            log.warning("tick feed connect failed: %s", type(e).__name__)
            self._ticker, self._retry_at = None, self._clock() + FORBIDDEN_BACKOFF_S

    def close(self) -> None:
        t, self._ticker, self._connected = self._ticker, None, False
        if t is not None:
            try:
                t.close()
            except Exception:
                pass

    def _on_connect(self, ws, response) -> None:
        self._connected = True
        with self._lock:
            tokens = list(self._wanted)
        log.info("tick feed connected; FULL mode on %d tokens", len(tokens))
        if tokens:
            ws.subscribe(tokens)
            ws.set_mode(ws.MODE_FULL, tokens)

    def _on_ticks(self, ws, ticks) -> None:
        now = self._clock()
        with self._lock:
            for t in ticks or []:
                token = t.get("instrument_token")
                if token is None:
                    continue
                depth = t.get("depth") or {}
                self._prices[int(token)] = (now, float(t.get("last_price") or 0) or None,
                                            _best(depth.get("buy"), max), _best(depth.get("sell"), min))
        self.ticked.set()

    def _on_error(self, ws, code, reason) -> None:
        log.warning("tick feed error code=%s reason=%s", code, reason)
        if "403" in str(reason) and not self._connected:
            # a rejected token never recovers by retrying; wait for a new one or the backoff
            self._retry_at = self._clock() + FORBIDDEN_BACKOFF_S
            self.close()

    def _on_close(self, ws, code, reason) -> None:
        self._connected = False

    def _on_noreconnect(self, ws) -> None:
        log.error("tick feed gave up reconnecting; REST reads only until the next session")
        self._ticker, self._connected = None, False
        self._retry_at = self._clock() + FORBIDDEN_BACKOFF_S
