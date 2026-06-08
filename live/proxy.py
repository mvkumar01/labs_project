"""
Helpers for outbound proxy configuration for the live stack.

Static-IP proxy policy (2026-06): the QuotaGuard static IP has a LIMITED
request quota, so it is used ONLY for ORDER PLACEMENT (place_order / exit_all)
— where a broker may require a whitelisted source IP. ALL data fetches
(spot / LTP / option chain / funds / login / instrument master / position /
order-status reads) go out DIRECT and never touch the static IP. Order calls
are wrapped transiently by order_proxy(); everything else is unproxied.

This keeps proxy credentials out of tracked code and lets PythonAnywhere/local
processes opt in via environment variables.
"""
from __future__ import annotations

import contextlib
import os

_PROXY_ENV_KEYS = ("HTTP_PROXY", "HTTPS_PROXY", "http_proxy", "https_proxy")


def _env_first(*names: str) -> str:
    for name in names:
        value = (os.environ.get(name) or "").strip()
        if value:
            return value
    return ""


def order_proxy_url() -> str:
    """Static-IP proxy URL reserved for ORDER PLACEMENT only.

    Reads the same env vars the runner already provides (so no PA env change
    is needed — they are simply re-scoped to orders). An optional
    LIVE_ORDER_PROXY_URL can override just the order path. An empty string
    means 'no proxy' → orders go out direct, exactly like data.
    """
    return _env_first(
        "LIVE_ORDER_PROXY_URL",      # explicit order-only override (optional)
        "LIVE_OUTBOUND_PROXY_URL",
        "QUOTAGUARDSTATIC_URL",
    )


@contextlib.contextmanager
def order_proxy(sdk_client=None):
    """Route ONLY the wrapped order call through the static-IP proxy.

    Data fetches must NOT consume the static IP's limited quota, so the proxy
    is applied transiently around a single order placement and torn down
    immediately afterwards. Applied two ways for robustness across broker SDKs:

      1. temporarily set HTTP(S)_PROXY env vars (requests honours these
         per-request when the session trusts the environment), and
      2. set ``sdk_client.proxies`` when the SDK exposes it — both SmartConnect
         (Angel) and KiteConnect (Zerodha) pass ``self.proxies`` on each
         request, so this guarantees the order egresses via the static IP even
         if env-trust is disabled.

    Both the env vars and the SDK attribute are restored on exit. No-op when no
    order proxy is configured, so orders then go out direct exactly like data.
    """
    url = order_proxy_url()
    if not url:
        yield
        return

    proxies = {"http": url, "https": url}
    saved_env = {k: os.environ.get(k) for k in _PROXY_ENV_KEYS}
    has_attr = sdk_client is not None and hasattr(sdk_client, "proxies")
    saved_attr = getattr(sdk_client, "proxies", None) if has_attr else None
    try:
        for k in _PROXY_ENV_KEYS:
            os.environ[k] = url
        if has_attr:
            sdk_client.proxies = proxies
        yield
    finally:
        for k in _PROXY_ENV_KEYS:
            prev = saved_env.get(k)
            if prev is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = prev
        if has_attr:
            sdk_client.proxies = saved_attr


def configure_outbound_proxy() -> str:
    """Opt-in GLOBAL proxy — disabled by default.

    Historically this routed ALL outbound traffic through the static IP, which
    exhausted the QuotaGuard quota on data fetches. The static IP is now
    reserved for order placement via order_proxy(). This function is therefore
    a NO-OP unless LIVE_PROXY_ALL is truthy, in which case it restores the old
    global-proxy behaviour (route everything through the static IP) for the
    rare environment where even data egress must use the whitelisted IP.

    Returns the proxy URL applied, or "" when no global proxy is set.
    """
    if (os.environ.get("LIVE_PROXY_ALL") or "").strip().lower() not in {"1", "true", "yes"}:
        return ""
    proxy_url = _env_first(
        "LIVE_OUTBOUND_PROXY_URL",
        "QUOTAGUARDSTATIC_URL",
        "HTTPS_PROXY",
        "HTTP_PROXY",
        "https_proxy",
        "http_proxy",
    )
    if not proxy_url:
        return ""

    for k in _PROXY_ENV_KEYS:
        os.environ[k] = proxy_url
    os.environ.setdefault("NO_PROXY", "127.0.0.1,localhost")
    os.environ.setdefault("no_proxy", "127.0.0.1,localhost")
    return proxy_url
