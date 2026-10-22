"""Proposer lot sizing. Balance RPCs run only in background workers, never in an entry."""
from __future__ import annotations

from dataclasses import dataclass, field
from decimal import Decimal, ROUND_FLOOR
import logging
import math
import threading
import time

FIXED, DYNAMIC = "fixed", "dynamic"
MODES = {FIXED: "Fixed lots (4 → 2 → 1 on a funds rejection)",
         DYNAMIC: "Dynamic lots (based on available funds)"}
REFRESH_SECONDS = 15.0
MAX_AGE_SECONDS = 60.0
FUNDING_BUFFER = 1.03
log = logging.getLogger("live.proposer_sizing")


def affordable_lots(max_lots: int, lot_size: int, price_cap: float, funds: float) -> int:
    """Whole lots within the configured/freeze ceiling, budgeting at the maximum entry price.

    A 3% headroom covers charges and small balance changes; broker RMS remains authoritative.
    """
    if (max_lots < 1 or lot_size < 1 or not math.isfinite(price_cap) or price_cap <= 0
            or not math.isfinite(funds) or funds <= 0):
        return 0
    cost = Decimal(str(price_cap)) * lot_size * Decimal(str(FUNDING_BUFFER))
    fits = int((Decimal(str(funds)) / cost).to_integral_value(rounding=ROUND_FLOOR))
    return min(max_lots, fits)


@dataclass
class _Watch:
    adapter: object
    epoch: int = 0
    snapshot: tuple[float, float] | None = None  # funds, monotonic read-start time
    stop: threading.Event = field(default_factory=threading.Event)


class FundsCache:
    """Independent background reader per connection; short memory-only locks on the order path.

    Invalidating before an order also discards any balance request already in flight, so its
    pre-order answer cannot be republished as a fresh post-order balance.
    """

    def __init__(self, *, clock=time.monotonic, on_result=None, start_threads=True):
        self.clock, self.on_result, self.start_threads = clock, on_result, start_threads
        self._lock = threading.Lock()
        self._watches = {}

    def watch(self, key, adapter):
        with self._lock:
            old = self._watches.get(key)
            if old is not None and old.adapter is adapter:
                return
            if old is not None:
                old.stop.set()
            record = self._watches[key] = _Watch(adapter)
        if self.start_threads:
            threading.Thread(target=self._loop, args=(key, record), daemon=True,
                             name="proposer-funds").start()

    def _loop(self, key, record):
        while not record.stop.is_set():
            self.refresh_once(key, record)
            record.stop.wait(REFRESH_SECONDS)

    def refresh_once(self, key, record=None):
        with self._lock:
            current = self._watches.get(key)
            if current is None or (record is not None and current is not record):
                return
            record, epoch = current, current.epoch
        started = self.clock()
        error = ""
        try:
            value = record.adapter.available_funds()  # outside the lock, on a background thread
            funds = float(value) if value is not None else None
            if funds is None or not math.isfinite(funds) or funds < 0:
                funds, error = None, "funds_unavailable"
        except Exception as exc:
            funds, error = None, type(exc).__name__
        with self._lock:
            if self._watches.get(key) is not record or record.epoch != epoch:
                return
            record.snapshot = (funds, started) if funds is not None else None
        if self.on_result is not None:
            try:
                self.on_result(key, funds, error)  # dashboard persistence is also off the order path
            except Exception as exc:
                log.warning("background funds persistence failed: %s", type(exc).__name__)

    def get(self, key):
        now = self.clock()
        with self._lock:
            record = self._watches.get(key)
            snapshot = record.snapshot if record is not None else None
        if snapshot is None:
            return None
        funds, started = snapshot
        return funds if 0 <= now - started < MAX_AGE_SECONDS else None

    def invalidate(self, key):
        with self._lock:
            record = self._watches.get(key)
            if record is not None:
                record.epoch += 1
                record.snapshot = None

    def unwatch(self, key):
        with self._lock:
            record = self._watches.pop(key, None)
            if record is not None:
                record.stop.set()

    def retain(self, keys):
        """Stop readers for deleted connections or connections moved to a different runner."""
        with self._lock:
            for key in set(self._watches) - set(keys):
                self._watches.pop(key).stop.set()

    def close(self):
        with self._lock:
            for record in self._watches.values():
                record.stop.set()
            self._watches.clear()
