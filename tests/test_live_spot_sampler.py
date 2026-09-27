"""Live runner spot sampler (own 2-second thread) and broker rejection reasons.

2026-09-25: the boundary clock was sampled once per strategy cycle (~60 s), so
closes were rejected as stale; and two Angel entry rejections logged no reason.
"""

from __future__ import annotations

import threading
from datetime import datetime, timedelta

import pytest

from live import live_runner as lr
from live.brokers import order_transport as ot
from live.engine import minute_ticks

IST = lr.IST


def at(day: str, hms: str) -> datetime:
    return datetime.fromisoformat(f"{day}T{hms}").replace(tzinfo=IST)


@pytest.mark.parametrize("stamp, expected", [
    (at("2026-09-28", "09:14:59"), False),       # Monday, pre-open
    (at("2026-09-28", "09:15:00"), True),
    (at("2026-09-28", "15:30:58"), True),        # freezes the 15:29 minute's successor
    (at("2026-09-28", "15:31:00"), False),
    (at("2026-09-28", "19:00:00"), False),       # after hours: stale prints
    (at("2026-09-27", "10:00:00"), False),       # Sunday
])
def test_sampling_window(stamp, expected):
    assert lr.spot_sampling_window(stamp) is expected


def test_no_kite_call_outside_market_hours(monkeypatch):
    calls = []
    monkeypatch.setattr(lr, "_MINUTE_TICKS", minute_ticks.MinuteTickAggregator())
    lr.poll_global_spot_tick(at("2026-09-27", "07:42:00"), fetch=lambda: calls.append(1))
    assert calls == []


def test_sampler_thread_keeps_minutes_fresh_while_the_strategy_loop_is_busy(monkeypatch):
    agg = minute_ticks.MinuteTickAggregator()
    monkeypatch.setattr(lr, "_MINUTE_TICKS", agg)
    monkeypatch.setattr(lr, "_log_spot_sample", lambda *a, **k: None)
    clock = {"t": at("2026-09-28", "10:00:00")}
    stop = threading.Event()
    n = {"calls": 0}

    def now():
        clock["t"] += timedelta(seconds=2)        # every sample is 2 s later
        return clock["t"]

    def fetch():
        n["calls"] += 1
        if n["calls"] >= 70:                      # a bit over two minutes
            stop.set()
        return 24000.0 + n["calls"]

    monkeypatch.setattr(lr, "_now_ist", now)
    monkeypatch.setattr(lr, "_sampler_kite_spot", fetch)
    lr._spot_sampler_loop(stop, interval_s=0.0)
    minutes = agg.minutes_for("2026-09-28")
    assert set(minutes) >= {"10:00", "10:01"}
    assert agg.rejected("2026-09-28") == []
    # sample k lands at 10:00:00 + 2k s, so 10:01's last sample is k=59 (10:01:58)
    assert minutes["10:01"][3] == 24000.0 + 59


def test_aggregator_is_safe_across_threads():
    agg = minute_ticks.MinuteTickAggregator()
    base = at("2026-09-28", "10:00:00")
    errors = []

    def writer():
        try:
            for i in range(3000):
                agg.add(base + timedelta(seconds=i * 0.5), 24000 + i)
        except Exception as exc:                  # pragma: no cover - failure path
            errors.append(exc)

    def reader():
        try:
            for _ in range(3000):
                agg.minutes_for("2026-09-28")
                agg.boundary_key("2026-09-28")
        except Exception as exc:                  # pragma: no cover - failure path
            errors.append(exc)

    threads = [threading.Thread(target=writer), threading.Thread(target=reader)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert errors == [] and len(agg.minutes_for("2026-09-28")) >= 20


def test_sampler_rebuilds_its_client_after_a_failed_read(monkeypatch):
    from auth import session_manager as sm

    built = []

    class Client:
        def __init__(self, fail):
            self.fail = fail

        def ltp(self, key):
            if self.fail:
                raise RuntimeError("TokenException")
            return {key: {"last_price": 24123.5}}

    def new_client():
        built.append(1)
        return Client(fail=len(built) == 1), 7

    monkeypatch.setattr(sm, "new_kite_client", new_client)
    monkeypatch.setattr(sm, "token_mtime_ns", lambda: 7)
    monkeypatch.setattr(lr, "_sampler_client", {"mtime": None, "kite": None})
    assert lr._sampler_kite_spot() is None       # first client fails -> dropped
    assert lr._sampler_kite_spot() == 24123.5    # rebuilt on the next sample
    assert len(built) == 2


# -- broker rejection reason ------------------------------------------------------
@pytest.mark.parametrize("broker, body, expected", [
    ("angel", {"status": False, "errorcode": "AB4008",
               "message": "Order price is out of range"},
     "AB4008 / Order price is out of range"),
    ("zerodha", {"status": "error", "error_type": "InputException",
                 "message": "Invalid\n price"}, "InputException / Invalid price"),
    ("angel", {"status": False}, "no reason given"),
    ("angel", "not json", "no reason given"),
])
def test_broker_reason(broker, body, expected):
    assert ot._broker_reason(broker, body) == expected


def test_broker_reason_is_truncated():
    assert len(ot._broker_reason("angel", {"message": "x" * 500})) == 160


def test_rejected_order_carries_the_broker_reason(monkeypatch):
    class Resp:
        status_code = 200

        @staticmethod
        def json():
            return {"status": False, "errorcode": "AB1007", "message": "Insufficient funds"}

    class Session:
        trust_env = True
        proxies = None

        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

        def request(self, *a, **k):
            return Resp()

    class Smart:
        access_token = "tok"

        @staticmethod
        def requestHeaders():
            return {}

    class Adapter:
        broker_name = "angel"
        user_id, conn_id = "u", "u:angel"
        _creds = {}
        _smart = Smart()

    outcomes = []
    monkeypatch.setattr(ot.requests, "Session", Session)
    monkeypatch.setattr(ot.cp, "reserve", lambda *a, **k: ("rid", "http://proxy"))
    monkeypatch.setattr(ot.cp, "finish_request", lambda rid, outcome: outcomes.append(outcome))
    with pytest.raises(ot.OrderTransportError) as err:
        ot.send_order(Adapter(), "entry", {"tradingsymbol": "X"}, "key")
    assert "AB1007 / Insufficient funds" in str(err.value)
    assert "proxy" not in str(err.value) and outcomes == ["rejected"]
