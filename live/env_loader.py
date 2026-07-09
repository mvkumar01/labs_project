"""Load private live-trading env keys from config/live_env.json.

Both the always-on runner (pa_live_runner) AND the Flask web app must load
these: the static_order_proxy gate is evaluated in the WEB process when the
user arms LIVE (2026-07-09 incident: arming failed with configured=0 because
only the runner loaded the file). Values never override an already-set env.
"""
from __future__ import annotations

import json
import os
from pathlib import Path

_ENV_KEYS = (
    "LABS_CRED_KEY",
    "LIVE_ORDERS_ENABLED",
    "LIVE_ORDER_PROXY_URL",
    "LIVE_OUTBOUND_PROXY_URL",
    "QUOTAGUARDSTATIC_URL",
)


def load_private_env(base_dir: Path | None = None) -> None:
    base = base_dir or Path(__file__).resolve().parent.parent
    env_path = base / "config" / "live_env.json"
    if not env_path.is_file():
        return
    try:
        # PowerShell may write JSON with a UTF-8 BOM; accept it so a process
        # restart cannot silently lose the credential key and live settings.
        data = json.loads(env_path.read_text(encoding="utf-8-sig"))
    except Exception:
        return
    for key in _ENV_KEYS:
        value = data.get(key)
        if value is not None and not os.environ.get(key):
            os.environ[key] = str(value)
