"""PythonAnywhere-safe launcher for the MCX real-time DRY runners (phase 0): the CRUDEOILM
combination and the gold CCI short, stepped in one process (live/mcx_dry_loop.py).

One always-on PA task. Phase 0 has no broker code path: the runners read Kite market data and
write their decisions to live.db (live_crudem_* and live_gold_* tables). See live/crudem_runner.py
and live/gold_runner.py.
"""
from pathlib import Path
import runpy
import sys

BASE_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(BASE_DIR))

from live.env_loader import load_private_env


if __name__ == "__main__":
    load_private_env(BASE_DIR)
    runpy.run_module("live.mcx_dry_loop", run_name="__main__")
