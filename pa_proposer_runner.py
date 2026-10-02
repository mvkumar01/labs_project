"""PythonAnywhere-safe launcher for the SENSEX Proposer live runner.

Its own always-on PA task, separate from pa_live_runner.py (NIFTY). It only drives
connections whose strategy is the Proposer; live_runner never claims those.
"""
from pathlib import Path
import runpy
import sys

BASE_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(BASE_DIR))

from live.proxy import configure_outbound_proxy
from live.env_loader import load_private_env


if __name__ == "__main__":
    load_private_env(BASE_DIR)
    configure_outbound_proxy()
    runpy.run_module("live.proposer_runner", run_name="__main__")
