"""Nifty Scalper V3 paper books on their own tabs: proposer_nifty_v3 (prints every 3 min, the live preset) and
proposer_nifty_v3_pclock (Pramanaa's timing, paper only)."""
from __future__ import annotations

import py_compile
import sqlite3
import subprocess
import sys
from pathlib import Path

import jinja2
import pytest

from labs.engine import proposer_px_tracker as tr

ROOT = Path(__file__).resolve().parents[1]
BOOKS = {"proposer_nifty_v3": ("Nifty Scalper V3", tr.NIFTY_V3, "every 3 minutes"),
         "proposer_nifty_v3_pclock": ("Nifty Scalper V3 (Pramanaa timing)", tr.NIFTY_V3_PCLOCK, "310.5 s")}


@pytest.mark.parametrize("key", BOOKS)
def test_registered_as_its_own_tab_runner_and_overview_card(key):
    from labs.services.book_overview_more import MORE_BOOKS
    from labs.ui.routes import LIVE_TABS
    label, book, _ = BOOKS[key]
    assert LIVE_TABS[key] == label and MORE_BOOKS[key]["instrument"] == "NIFTY options"
    template = (ROOT / "templates" / "live_strategy.html").read_text(encoding="utf-8")
    assert "{% include '_" + key + ".html' %}" in template and f"/labs/api/{key}/backfill" in template
    loop = (ROOT / "pa_paper_tracker_loop.py").read_text(encoding="utf-8")
    assert f'("{key}", run_{key}_day)' in loop
    module = __import__(f"labs.engine.{key}_book", fromlist=["BOOK"])
    assert module.BOOK is book and module.DEFAULT_START == "2026-07-01"


@pytest.mark.parametrize("key", BOOKS)
def test_tab_renders(tmp_path, key):
    _, book, phrase = BOOKS[key]
    conn = sqlite3.connect(tmp_path / "labs.db")
    tr._ensure_tables(conn, book)
    env = jinja2.Environment(loader=jinja2.FileSystemLoader(str(ROOT / "templates")), autoescape=True)
    html = env.get_template(f"_{key}.html").render(**{f"{key}_rows": [], f"{key}_trades": [],
                                                      f"{key}_stats": {"daily_target_pct": 0.015}})
    assert "1.5% day target" in html and phrase in html and "Paper only" in html
    assert f'id="{key.replace("_", "-")}-backfill"' in html and "proposer_nifty_v5" not in html
    assert "&amp;mdash;" not in html and "&amp;#8377;" not in html


def test_v1_tab_no_longer_nests_the_v3_books():
    page = (ROOT / "templates" / "_proposer_nifty_v5.html").read_text(encoding="utf-8")
    assert "Nifty Scalper V3" not in page and "twins" not in page


def test_ui_patch_applies_after_the_v1_wiring_and_is_idempotent(tmp_path):
    files = ("labs/ui/routes.py", "templates/live_strategy.html", "pa_paper_tracker_loop.py",
             "labs/services/book_overview_more.py")
    for rel in files:
        target = tmp_path / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(subprocess.run(["git", "-C", str(ROOT), "show", f"a3c5ae5:{rel}"],
                                          capture_output=True, check=True).stdout)
    script = str(ROOT / "scripts" / "patch_proposer_nifty_v3_ui.py")
    first = subprocess.run([sys.executable, script, str(tmp_path)], capture_output=True, text=True)
    assert first.returncode == 0 and ": 0 edits applied" not in first.stdout, first.stdout + first.stderr
    second = subprocess.run([sys.executable, script, str(tmp_path)], capture_output=True, text=True)
    assert second.returncode == 0 and second.stdout.count(": 0 edits applied") == 4
    routes = (tmp_path / "labs/ui/routes.py").read_text(encoding="utf-8")
    v1, v3, pc = (routes.index(f'"{k}": "') for k in ("proposer_nifty_v5", "proposer_nifty_v3", "proposer_nifty_v3_pclock"))
    assert v1 < v3 < pc                                                   # tabs in order after V1
    for rel in ("labs/ui/routes.py", "pa_paper_tracker_loop.py", "labs/services/book_overview_more.py"):
        py_compile.compile(str(tmp_path / rel), doraise=True)
    jinja2.Environment().parse((tmp_path / "templates/live_strategy.html").read_text(encoding="utf-8"))
