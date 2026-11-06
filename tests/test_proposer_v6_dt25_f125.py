"""Sensex 2.5% V6 floor 125: the V6 paper book with the SENSEX loss floor at 125 points (Vipin, 2026-11-06). Paper only."""
from __future__ import annotations

import dataclasses
import sqlite3
import subprocess
import sys
from pathlib import Path

import jinja2

from labs.engine import proposer_px_tracker as tr

ROOT = Path(__file__).resolve().parents[1]


def test_book_is_v6_with_a_125_point_floor():
    for day in ("2026-07-01", "2026-10-20"):
        a, b = tr.params_on(day, tr.V6_DT25), tr.params_on(day, tr.V6_DT25_F125)
        assert a.spot_floor_pts == 150.0 and b.spot_floor_pts == 125.0
        assert dataclasses.replace(b, spot_floor_pts=150.0) == a
    key = tr.V6_DT25_F125.key
    assert key in tr.NO_INSIDE_CPR_BOOKS and key in tr.DELTA_STRIKE_BOOKS and key not in tr.NIFTY_BOOKS
    assert tr.V6_DT25_F125.strategy_version == tr.V6_DT25.strategy_version


def test_tab_registered_and_renders(tmp_path):
    from labs.services.book_overview_more import MORE_BOOKS
    from labs.services.strategy_report import SOURCES
    from labs.ui.routes import LIVE_TABS
    key = tr.V6_DT25_F125.key
    assert LIVE_TABS[key] == "Sensex 2.5% V6 · floor 125" and MORE_BOOKS[key]["instrument"] == "SENSEX options"
    assert key in SOURCES
    page = (ROOT / "templates" / "live_strategy.html").read_text(encoding="utf-8")
    assert "{% include '_" + key + ".html' %}" in page and f"/labs/api/{key}/backfill" in page
    assert f'("{key}", run_{key}_day)' in (ROOT / "pa_paper_tracker_loop.py").read_text(encoding="utf-8")
    module = __import__(f"labs.engine.{key}_book", fromlist=["BOOK"])
    assert module.BOOK is tr.V6_DT25_F125 and module.DEFAULT_START == "2026-07-01"
    conn = sqlite3.connect(tmp_path / "labs.db")
    tr._ensure_tables(conn, tr.V6_DT25_F125)
    env = jinja2.Environment(loader=jinja2.FileSystemLoader(str(ROOT / "templates")), autoescape=True)
    html = env.get_template(f"_{key}.html").render(**{f"{key}_rows": [], f"{key}_trades": [],
                                                      f"{key}_stats": {"daily_target_pct": 0.025}})
    assert "125 points against its entry" in html and "Paper only" in html
    assert 'id="proposer-v6-dt25-f125-backfill"' in html and "proposer_v6_dt25_stats" not in html


def test_ui_patch_is_idempotent():
    out = subprocess.run([sys.executable, str(ROOT / "scripts" / "patch_proposer_v6_dt25_f125_ui.py"), str(ROOT)],
                         capture_output=True, text=True)
    assert out.returncode == 0 and out.stdout.count(": 0 edits applied") == 5, out.stdout + out.stderr
