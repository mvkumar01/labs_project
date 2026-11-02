"""Sensex 2.5% V6: the Sensex V5 2.5% paper book with no entry while SENSEX is inside the previous session's CPR."""
from __future__ import annotations

import sqlite3
from pathlib import Path

import jinja2
import pytest

from labs.engine import proposer_px_tracker as tr
from live.engine import entry_blocks as eb

ROOT = Path(__file__).resolve().parents[1]
CPR = {"S3": 73000.0, "S2": 73200.0, "S1": 73350.0, "BC": 73471.0, "P": 73500.0, "TC": 73556.0,
       "R1": 73700.0, "R2": 73850.0, "R3": 74000.0}


@pytest.mark.parametrize("spot,inside", [(73471.0, True), (73556.0, True), (73510.0, True),
                                         (73470.99, False), (73556.01, False), (74200.0, False)])
def test_inside_cpr_ends_included(spot, inside):
    assert eb.inside_cpr(spot, CPR) is inside


def test_inverted_cpr_and_no_cpr():
    assert eb.inside_cpr(73500.0, dict(CPR, TC=73471.0, BC=73556.0))
    assert not eb.inside_cpr(73500.0, None)


def test_v6_is_v5_dt25_plus_the_block():
    assert tr.NO_INSIDE_CPR_BOOKS == {tr.V6_DT25.key} and tr.V6_DT25.key not in tr.NIFTY_BOOKS
    assert tr.V6_DT25.strategy_version == tr.V5_DT25.strategy_version
    assert tr.V6_DT25.print_gap_s == tr.V5_DT25.print_gap_s and tr.V6_DT25.key in tr.DELTA_STRIKE_BOOKS
    for day in ("2026-07-01", "2026-10-20"):
        assert tr.params_on(day, tr.V6_DT25) == tr.params_on(day, tr.V5_DT25)


def test_tab_registered_and_renders(tmp_path):
    from labs.services.book_overview_more import MORE_BOOKS
    from labs.services.strategy_report import SOURCES
    from labs.ui.routes import LIVE_TABS
    key = tr.V6_DT25.key
    assert LIVE_TABS[key] == "Sensex 2.5% V6" and MORE_BOOKS[key]["instrument"] == "SENSEX options"
    assert key in SOURCES
    page = (ROOT / "templates" / "live_strategy.html").read_text(encoding="utf-8")
    assert "{% include '_" + key + ".html' %}" in page and f"/labs/api/{key}/backfill" in page
    assert f'("{key}", run_{key}_day)' in (ROOT / "pa_paper_tracker_loop.py").read_text(encoding="utf-8")
    module = __import__(f"labs.engine.{key}_book", fromlist=["BOOK"])
    assert module.BOOK is tr.V6_DT25 and module.DEFAULT_START == "2026-07-01"
    conn = sqlite3.connect(tmp_path / "labs.db")
    tr._ensure_tables(conn, tr.V6_DT25)
    env = jinja2.Environment(loader=jinja2.FileSystemLoader(str(ROOT / "templates")), autoescape=True)
    html = env.get_template(f"_{key}.html").render(**{f"{key}_rows": [], f"{key}_trades": [],
                                                      f"{key}_stats": {"daily_target_pct": 0.025}})
    assert "2.5% day target" in html and "inside the previous session" in html and "Paper only" in html
    assert 'id="proposer-v6-dt25-backfill"' in html and "proposer_v5_dt25" not in html


def test_ui_patch_is_idempotent():
    import subprocess
    import sys
    out = subprocess.run([sys.executable, str(ROOT / "scripts" / "patch_proposer_v6_dt25_ui.py"), str(ROOT)],
                         capture_output=True, text=True)
    assert out.returncode == 0 and out.stdout.count(": 0 edits applied") == 5, out.stdout + out.stderr
