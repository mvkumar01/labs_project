"""PythonAnywhere-safe launcher for the CRUDEOILM combination real-time runner (phase 0: dry run).

Its own always-on PA task. Phase 0 has no broker code path: it reads Kite market data and writes
its decisions to live.db (live_crudem_* tables). See live/crudem_runner.py.
"""
from pathlib import Path
import runpy
import sys

BASE_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(BASE_DIR))

from live.env_loader import load_private_env


if __name__ == "__main__":
    load_private_env(BASE_DIR)
    runpy.run_module("live.crudem_runner", run_name="__main__")
