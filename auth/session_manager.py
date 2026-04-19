"""
Loads the stored Zerodha token and returns an authenticated KiteConnect instance.
Called by the collector and strategy runner at startup.
"""
import json
from pathlib import Path

BASE_DIR   = Path(__file__).resolve().parent.parent
TOKEN_PATH = BASE_DIR / "config" / "zerodha_token.json"

_kite_instance = None


def get_kite():
    """Return a cached, authenticated KiteConnect instance."""
    global _kite_instance
    if _kite_instance is not None:
        return _kite_instance

    if not TOKEN_PATH.exists():
        raise FileNotFoundError(
            f"Labs Zerodha token not found at {TOKEN_PATH}. "
            "Run auth/generate_token.py first."
        )

    from kiteconnect import KiteConnect
    data = json.loads(TOKEN_PATH.read_text())
    kite = KiteConnect(api_key=data["api_key"])
    kite.set_access_token(data["access_token"])
    _kite_instance = kite
    return kite


def reset():
    """Force re-load of token (call after token refresh)."""
    global _kite_instance
    _kite_instance = None
