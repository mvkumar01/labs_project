"""
Loads the stored Zerodha token and returns an authenticated KiteConnect instance.
Called by the collector and strategy runner at startup.
"""
import json
import logging
from pathlib import Path

BASE_DIR   = Path(__file__).resolve().parent.parent
TOKEN_PATH = BASE_DIR / "config" / "zerodha_token.json"

_kite_instance = None
_last_token_data = None
log = logging.getLogger(__name__)


def get_kite(force_refresh: bool = False):
    """Return a cached, authenticated KiteConnect instance."""
    global _kite_instance, _last_token_data

    if not TOKEN_PATH.exists():
        raise FileNotFoundError(
            f"Labs Zerodha token not found at {TOKEN_PATH}. "
            "Run auth/generate_token.py first."
        )

    token_data = json.loads(TOKEN_PATH.read_text())
    if (
        force_refresh
        or _kite_instance is None
        or token_data != _last_token_data
    ):
        if force_refresh:
            reason = "force_refresh"
        elif _kite_instance is None or _last_token_data is None:
            reason = "initial_load"
        else:
            reason = "token_changed"

        from kiteconnect import KiteConnect

        kite = KiteConnect(api_key=token_data["api_key"])
        kite.set_access_token(token_data["access_token"])
        _kite_instance = kite
        _last_token_data = token_data
        log.info(
            "Reloaded Labs KiteConnect token from %s (%s).",
            TOKEN_PATH,
            reason,
        )

    return _kite_instance


def reset():
    """Force re-load of token (call after token refresh)."""
    global _kite_instance, _last_token_data
    _kite_instance = None
    _last_token_data = None
