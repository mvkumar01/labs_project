"""PythonAnywhere-safe launcher for Labs token generation."""
from pathlib import Path
import runpy
import sys

BASE_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(BASE_DIR))

if __name__ == "__main__":
    runpy.run_module("auth.generate_token", run_name="__main__")
