"""Sensex V5 2.5% = the live preset proposer_dt35_v5 (Sensex V5: the 150-point floor inside the engine) with a
2.5% day target; the live Sensex V4 / V5 preset's per-connection day target."""
from __future__ import annotations

import dataclasses
import py_compile
import subprocess
import sys
from pathlib import Path

import pytest

from labs.engine import proposer_px_tracker as tr
from labs.engine import proposer_v5_dt25_book as book
from live import live_executor as ex
from live.engine import proposer_engine as pe

ROOT = Path(__file__).resolve().parents[1]


def test_the_book_is_v5_with_a_2_5_pct_day_target():
    assert book.BOOK is tr.V5_DT25 and book.BOOK.strategy_version == pe.STRATEGY_VERSION_V5
    assert book.DEFAULT_START == "2026-07-01" and "proposer_v5_dt25" in tr.DELTA_STRIKE_BOOKS
    for day in ("2026-07-01", "2026-10-20"):
        p, v5 = tr.params_on(day, tr.V5_DT25), tr.params_on(day, tr.V5)
        assert p.daily_target_pct == 0.025 and v5.daily_target_pct == 0.035
        # V5's paper book applies its floor outside the engine; this one inside - otherwise identical
        assert dataclasses.replace(p, daily_target_pct=0.035, spot_floor_pts=0.0) == v5
        assert p == pe.params_for(pe.STRATEGY_VERSION_V5, 0.025)       # = the live preset at 2.5%
    assert "proposer_v5_dt25" not in tr.SPOT_FLOOR_PTS                 # no second floor on top of the engine's


@pytest.mark.parametrize("text,want", [("2.5", 0.025), (" 3 % ", 0.03), ("10", 0.10), ("0.5", 0.005),
                                       ("", None), (None, None), ("abc", None), ("0.4", None), ("12", None),
                                       ("-2", None)])
def test_parse_day_target(text, want):
    got = pe.parse_day_target(text)
    assert got == pytest.approx(want) if want is not None else got is None


def test_day_target_applies_only_to_sensex_v4_and_v5():
    for v in (pe.STRATEGY_VERSION_V3_DT35, pe.STRATEGY_VERSION_V5):
        assert pe.params_for(v, 0.025).daily_target_pct == 0.025
        assert pe.params_for(v).daily_target_pct == 0.035 and pe.params_for(v, None).daily_target_pct == 0.035
    for v in (pe.STRATEGY_VERSION, pe.STRATEGY_VERSION_PX, pe.STRATEGY_VERSION_V3):
        assert pe.params_for(v, 0.05) == pe.params_for(v)
    assert ex.PROPOSER_DECISION_ABI == "proposer-dt25-live-v10"


def test_runner_rebuilds_the_engine_when_the_day_target_changes(monkeypatch):
    from live import proposer_runner as pr
    pc = pr.ProposerConnection("u", "c", strategy=pe.STRATEGY_VERSION_V5, day_target=0.025)
    assert pc.engine.params.daily_target_pct == 0.025 and pc.engine.params.spot_floor_pts == 150.0
    pc = pr.ProposerConnection("u", "c", strategy=pe.STRATEGY_VERSION_V5)
    assert pc.engine.params.daily_target_pct == 0.035
    src = (ROOT / "live" / "proposer_runner.py").read_text(encoding="utf-8")
    assert "pc.day_target != day_target" in src and '"proposer_day_target_pct"' in src


def test_registered_as_a_paper_tab_runner_and_overview_card():
    from labs.services.book_overview_more import MORE_BOOKS as BOOKS
    from labs.ui.routes import LIVE_TABS
    assert LIVE_TABS["proposer_v5_dt25"] == "Sensex V5 2.5%" and BOOKS["proposer_v5_dt25"]["label"] == "Sensex V5 2.5%"
    template = (ROOT / "templates" / "live_strategy.html").read_text(encoding="utf-8")
    assert "{% include '_proposer_v5_dt25.html' %}" in template and "/labs/api/proposer_v5_dt25/backfill" in template
    loop = (ROOT / "pa_paper_tracker_loop.py").read_text(encoding="utf-8")
    assert '("proposer_v5_dt25", run_proposer_v5_dt25_day)' in loop
    tab = (ROOT / "templates" / "_proposer_v5_dt25.html").read_text(encoding="utf-8")
    assert "2.5%" in tab and "150 points against its entry" in tab and "in-sample" in tab


def test_tab_renders_its_own_day_target(tmp_path):
    import sqlite3
    import jinja2
    conn = sqlite3.connect(tmp_path / "labs.db")
    tr._ensure_tables(conn, tr.V5_DT25)
    env = jinja2.Environment(loader=jinja2.FileSystemLoader(str(ROOT / "templates")), autoescape=True)
    html = env.get_template("_proposer_v5_dt25.html").render(proposer_v5_dt25_rows=[], proposer_v5_dt25_trades=[],
                                                             proposer_v5_dt25_stats={"daily_target_pct": 0.025})
    assert "2.5% day target" in html and "&amp;mdash;" not in html


def test_ui_patch_applies_next_to_the_v5_wiring_and_is_idempotent(tmp_path):
    files = ("labs/ui/routes.py", "templates/live_strategy.html", "pa_paper_tracker_loop.py",
             "labs/services/book_overview_more.py")
    for rel in files:
        target = tmp_path / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(subprocess.run(["git", "-C", str(ROOT), "show", f"b0839c2:{rel}"],
                                          capture_output=True, check=True).stdout)
    script = str(ROOT / "scripts" / "patch_proposer_v5_dt25_ui.py")
    first = subprocess.run([sys.executable, script, str(tmp_path)], capture_output=True, text=True)
    assert first.returncode == 0 and "0 edits applied" not in first.stdout, first.stdout + first.stderr
    second = subprocess.run([sys.executable, script, str(tmp_path)], capture_output=True, text=True)
    assert second.returncode == 0 and second.stdout.count("0 edits applied") == 4
    routes = (tmp_path / "labs/ui/routes.py").read_text(encoding="utf-8")
    assert routes.count('"proposer_v5_dt25": "Sensex V5 2.5%"') == 1 and routes.count('"proposer_v5": "Sensex V5"') == 1
    for rel in ("labs/ui/routes.py", "pa_paper_tracker_loop.py", "labs/services/book_overview_more.py"):
        py_compile.compile(str(tmp_path / rel), doraise=True)
    import jinja2
    jinja2.Environment().parse((tmp_path / "templates/live_strategy.html").read_text(encoding="utf-8"))
