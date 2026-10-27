"""The Angel SDK's calls go through a kept-alive session (live/brokers/angel.py _KeepAliveRequests):
one session per thread, no cookies, a dropped connection retried once for GET reads only, never for
anything on Angel's order routes, and installed once into SmartApi.smartConnect."""
from __future__ import annotations

import sys
import threading
import types

import pytest
import requests

from live.brokers import angel


class FakeSession:
    made = 0

    def __init__(self, fail_first=0):
        FakeSession.made += 1
        self.calls, self.fail_first = [], fail_first
        self.cookies = requests.cookies.RequestsCookieJar()

    def request(self, method, url, **kw):
        self.calls.append((method, url, kw))
        if self.fail_first:
            self.fail_first -= 1
            raise requests.exceptions.ConnectionError("connection reset by peer")
        return f"{method} {url}"


def _shim(monkeypatch, fail_first=0):
    sessions = []
    def make():
        sessions.append(FakeSession(fail_first if not sessions else 0))   # only the first pool is dead
        return sessions[-1]
    module = types.SimpleNamespace(exceptions=requests.exceptions, packages=requests.packages, Session=make)
    return angel._KeepAliveRequests(module), sessions


def test_calls_reuse_one_session_per_thread_with_cookies_refused(monkeypatch):
    shim, sessions = _shim(monkeypatch)
    shim.request("GET", "https://apiconnect.angelone.in/rest/secure/angelbroking/order/v1/getPosition", timeout=7)
    shim.get("https://apiconnect.angelone.in/x", headers={"Authorization": "Bearer a"})
    assert len(sessions) == 1 and len(sessions[0].calls) == 2
    assert sessions[0].calls[0][2] == {"timeout": 7}
    policy = sessions[0].cookies._policy                    # cookies a response sets are refused
    assert policy.allowed_domains() == () and not policy.set_ok_domain(
        types.SimpleNamespace(domain="apiconnect.angelone.in"), None)
    t = threading.Thread(target=lambda: shim.get("https://apiconnect.angelone.in/y"))
    t.start(); t.join()
    assert len(sessions) == 2                                # another thread gets its own session


def test_a_dropped_connection_is_retried_once_for_a_read():
    shim, sessions = _shim(None, fail_first=1)
    assert shim.request("GET", "https://apiconnect.angelone.in/rest/secure/angelbroking/user/v1/getRMS") \
        .startswith("GET")
    assert len(sessions) == 2                                # the dead pool was dropped


@pytest.mark.parametrize("method,path", [
    ("POST", "rest/auth/angelbroking/user/v1/loginByPassword"),
    ("POST", "rest/secure/angelbroking/order/v1/placeOrder"),
    ("GET", "rest/secure/angelbroking/order/v1/details/123"),
])
def test_nothing_but_plain_reads_is_retried(method, path):
    shim, sessions = _shim(None, fail_first=1)
    with pytest.raises(requests.exceptions.ConnectionError):
        shim.request(method, "https://apiconnect.angelone.in/" + path)
    assert len(sessions) == 1


def test_other_module_attributes_pass_through():
    shim = angel._KeepAliveRequests(requests)
    assert shim.exceptions is requests.exceptions and shim.packages is requests.packages
    assert shim.adapters is requests.adapters


def test_installed_once_into_the_sdk_module(monkeypatch):
    pkg = types.ModuleType("SmartApi")
    sdk = types.ModuleType("SmartApi.smartConnect")
    sdk.requests = requests
    pkg.smartConnect = sdk
    monkeypatch.setitem(sys.modules, "SmartApi", pkg)
    monkeypatch.setitem(sys.modules, "SmartApi.smartConnect", sdk)
    angel._install_sdk_keep_alive()
    first = sdk.requests
    angel._install_sdk_keep_alive()
    assert isinstance(first, angel._KeepAliveRequests) and sdk.requests is first
    assert first._module is requests
