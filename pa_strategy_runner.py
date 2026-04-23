"""PythonAnywhere-safe launcher for the Labs strategy runner."""
from pathlib import Path
import runpy
import sys

BASE_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(BASE_DIR))

if __name__ == "__main__":
    runpy.run_module("labs.engine.strategy_runner", run_name="__main__")
