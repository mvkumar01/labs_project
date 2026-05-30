"""PythonAnywhere-safe launcher for the Labs LIVE runner.

Mirrors pa_strategy_runner.py: fixes sys.path so absolute package imports
resolve, then hands off to the live runner module's __main__.

Runs as its OWN always-on PA task, fully separate from the paper-trading
strategy runner. DRY-RUN ONLY in Phase 0 — no broker order can fire.
"""
from pathlib import Path
import runpy
import sys

BASE_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(BASE_DIR))

if __name__ == "__main__":
    runpy.run_module("live.live_runner", run_name="__main__")
