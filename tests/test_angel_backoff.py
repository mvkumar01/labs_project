"""Angel bounded backoff/retry on throttling (rate-limit defense, 2026-07-07).

Reads retry on throttle OR garbled JSON; orders retry ONLY on an explicit
throttle (rejected → nothing placed), never on ambiguous parse errors that
could mask a placed order and cause a double-fill on retry.
"""
import time

import pytest

from live.brokers.angel import (
    AngelAdapter, _is_rate_limited, _is_transient_read, _RETRY_TRIES,
)

# Real shape of the 2026-07-07 throttle: parse-wrapper around the rate message.
THROTTLE = RuntimeError(
    "Couldn't parse the JSON response received from the server: "
    "b'Access denied because of exceeding access rate'")
# A garbled body with NO throttle marker (e.g. an HTML 500).
PARSE_ONLY = RuntimeError(
    "Couldn't parse the JSON response received from the server: b'<html>500</html>'")


def _adapter():
    return AngelAdapter(user_id="u", conn_id="c", creds={"client_code": "X"})


def test_predicates():
    assert _is_rate_limited(THROTTLE)          # throttle marker present
    assert _is_transient_read(THROTTLE)
    assert not _is_rate_limited(PARSE_ONLY)    # parse-only is NOT a throttle
    assert _is_transient_read(PARSE_ONLY)      # but reads may retry it
    assert not _is_transient_read(ValueError("boom"))


def test_read_retries_then_succeeds(monkeypatch):
    monkeypatch.setattr(time, "sleep", lambda s: None)
    a = _adapter()
    n = {"c": 0}

    def fn():
        n["c"] += 1
        if n["c"] < 3:
            raise THROTTLE
        return "ok"

    assert a._with_backoff(fn, _is_transient_read) == "ok"
    assert n["c"] == 3


def test_gives_up_after_max_tries(monkeypatch):
    monkeypatch.setattr(time, "sleep", lambda s: None)
    a = _adapter()
    n = {"c": 0}

    def fn():
        n["c"] += 1
        raise THROTTLE

    with pytest.raises(RuntimeError):
        a._with_backoff(fn, _is_transient_read)
    assert n["c"] == _RETRY_TRIES


def test_non_retryable_raises_immediately(monkeypatch):
    monkeypatch.setattr(time, "sleep", lambda s: None)
    a = _adapter()
    n = {"c": 0}

    def fn():
        n["c"] += 1
        raise ValueError("nope")

    with pytest.raises(ValueError):
        a._with_backoff(fn, _is_transient_read)
    assert n["c"] == 1


def test_order_predicate_never_retries_parse_only(monkeypatch):
    """Order path uses _is_rate_limited: a parse-only error must NOT retry, so a
    possibly-placed order is never double-sent."""
    monkeypatch.setattr(time, "sleep", lambda s: None)
    a = _adapter()
    n = {"c": 0}

    def fn():
        n["c"] += 1
        raise PARSE_ONLY

    with pytest.raises(RuntimeError):
        a._with_backoff(fn, _is_rate_limited)
    assert n["c"] == 1
