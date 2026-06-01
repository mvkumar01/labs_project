"""
Helpers for outbound proxy configuration for the live stack.

This keeps proxy credentials out of tracked code and lets PythonAnywhere/local
processes opt in via environment variables.
"""
from __future__ import annotations

import os


def _env_first(*names: str) -> str:
    for name in names:
        value = (os.environ.get(name) or "").strip()
        if value:
            return value
    return ""


def configure_outbound_proxy() -> str:
    """Apply outbound proxy settings from environment variables.

    Priority:
      1. LIVE_OUTBOUND_PROXY_URL
      2. QUOTAGUARDSTATIC_URL
      3. existing HTTPS_PROXY / HTTP_PROXY

    Returns the proxy URL that was applied, or an empty string if no proxy is
    configured.
    """
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

    os.environ["HTTP_PROXY"] = proxy_url
    os.environ["HTTPS_PROXY"] = proxy_url
    os.environ["http_proxy"] = proxy_url
    os.environ["https_proxy"] = proxy_url
    os.environ.setdefault("NO_PROXY", "127.0.0.1,localhost")
    os.environ.setdefault("no_proxy", "127.0.0.1,localhost")
    return proxy_url
