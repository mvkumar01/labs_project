"""
Helpers for outbound proxy configuration for the live stack.

Static-IP proxy policy (2026-06): the QuotaGuard static IP has a LIMITED
request quota, so it is used ONLY for ORDER PLACEMENT (place_order / exit_all)
— where a broker may require a whitelisted source IP. ALL data fetches
(spot / LTP / option chain / funds / login / instrument master / position /
order-status reads) go out DIRECT and never touch the static IP. Order calls
are wrapped transiently by order_proxy(); everything else is unproxied.

This keeps proxy credentials out of tracked code and lets PythonAnywhere/local
processes supply the order-only proxy via environment variables.
"""
from __future__ import annotations

import contextlib
import os

_PROXY_ENV_KEYS = ("HTTP_PROXY", "HTTPS_PROXY", "http_proxy", "https_proxy")


class StaticOrderProxyRequired(RuntimeError):
    """A live order was attempted without the mandatory static-IP route."""


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
    LIVE_ORDER_PROXY_URL can override just the order path. An empty string is a
    hard configuration error for order placement; orders never go out direct.
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

    Both the env vars and the SDK attribute are restored on exit. Missing proxy
    configuration fails closed before the order call is reached.
    """
    url = order_proxy_url()
    if not url:
        raise StaticOrderProxyRequired(
            "LIVE_ORDER_PROXY_URL/LIVE_OUTBOUND_PROXY_URL/"
            "QUOTAGUARDSTATIC_URL is required for every live order"
        )

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
    """Legacy launcher hook retained as an intentional no-op.

    Data/auth/position/order-status traffic must use normal direct egress. The
    static-IP proxy is applied only inside :func:`order_proxy`; global proxying
    is no longer supported, even if an old ``LIVE_PROXY_ALL`` variable remains.
    """
    return ""
