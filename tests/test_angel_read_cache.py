"""Angel adapter short-TTL read cache (rate-limit defense, 2026-07-07).

Repeated position/LTP reads within one ~2s poll cycle must collapse to a single
broker call, refresh after the TTL, and refresh immediately after an order (so
post-trade reconcile sees the new book). is_connected() stays uncached.
"""
import time

from live.brokers.angel import AngelAdapter, _READ_CACHE_TTL


class _Smart:
    def __init__(self):
        self.position_calls = 0

    def position(self):
        self.position_calls += 1
        return {"status": True,
                "data": [{"netqty": 65, "tradingsymbol": "NIFTY07JUL2624250CE"}]}


def _adapter():
    a = AngelAdapter(user_id="u", conn_id="c", creds={"client_code": "X"})
    a._smart = _Smart()
    return a


def test_repeated_reads_collapse_to_one_call():
    a = _adapter()
    p1, p2, p3 = a.get_position(), a.get_position(), a.get_position()
    assert a._smart.position_calls == 1          # 3 reads -> 1 broker call
    assert p1.qty == p2.qty == p3.qty == 65


def test_invalidate_after_order_forces_fresh_read():
    a = _adapter()
    a.get_position()
    a._invalidate_reads()                        # what place_order/exit_all do
    a.get_position()
    assert a._smart.position_calls == 2


def test_cache_expires_after_ttl(monkeypatch):
    a = _adapter()
    clock = [1000.0]
    monkeypatch.setattr(time, "monotonic", lambda: clock[0])
    a.get_position()
    clock[0] += _READ_CACHE_TTL + 0.1            # past the TTL -> next read is live
    a.get_position()
    assert a._smart.position_calls == 2
