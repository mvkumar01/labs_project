"""PythonAnywhere-safe launcher for the Labs LIVE runner.

Mirrors pa_strategy_runner.py: fixes sys.path so absolute package imports
resolve, then hands off to the live runner module's __main__.

Runs as its OWN always-on PA task, fully separate from the paper-trading
strategy runner. DRY-RUN ONLY in Phase 0 — no broker order can fire.
"""
from pathlib import Path
import json
import os
import runpy
import sys

BASE_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(BASE_DIR))

from live.proxy import configure_outbound_proxy


def _load_private_env() -> None:
    """Load PA-only live secrets without putting them in Git or task command."""
    env_path = BASE_DIR / "config" / "live_env.json"
    if not env_path.exists():
        return
    try:
        data = json.loads(env_path.read_text(encoding="utf-8"))
    except Exception:
        return
    for key in (
        "LABS_CRED_KEY",
        "LIVE_ORDERS_ENABLED",
        "LIVE_OUTBOUND_PROXY_URL",
        "QUOTAGUARDSTATIC_URL",
    ):
        value = data.get(key)
        if value is not None and not os.environ.get(key):
            os.environ[key] = str(value)


if __name__ == "__main__":
    _load_private_env()
    configure_outbound_proxy()
    runpy.run_module("live.live_runner", run_name="__main__")
