"""Sensex V5 = Sensex V4 with the 150 rule: no premium floor; out once SENSEX is 150 points against the
entry (alphaIMB REBUILD sec. 33, P150). Parity with that research run (76/76 sessions, +Rs 10,75,241) is
checked outside the suite (alphaIMB source/v5_book_parity.py); these pin the rule and the wiring."""
from __future__ import annotations

import dataclasses
import py_compile
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

from labs.engine import proposer_px_tracker as tr
from labs.engine import proposer_v5_book as v5
from live.engine import proposer_engine as pe

ROOT = Path(__file__).resolve().parents[1]


def test_v5_is_v4_with_the_premium_floor_off():
    assert v5.BOOK is tr.V5 and v5.BOOK.strategy_version == pe.STRATEGY_VERSION_V3_DT35
    assert v5.DEFAULT_START == "2026-07-01" and tr.SPOT_FLOOR_PTS == {"proposer_v5": 150.0}
    for day in ("2026-07-01", "2026-09-15", "2026-10-20"):
        p5, p4 = tr.params_on(day, tr.V5), tr.params_on(day, tr.V4)
        assert p5.loss_floor_pct == -0.999 and p4.loss_floor_pct == pe.params_for(pe.STRATEGY_VERSION_V3_DT35).loss_floor_pct
        assert dataclasses.replace(p5, loss_floor_pct=p4.loss_floor_pct) == p4           # nothing else differs
    assert "proposer_v5" in tr.DELTA_STRIKE_BOOKS and tr.SETTINGS_FROM["proposer_v5"] == "2026-07-01"


class _Eng:
    skip_cd_arm = True


@pytest.mark.parametrize("side,espot,spot,fires", [
    ("CE", 74000.0, 73851.0, False),      # 149 against: hold
    ("CE", 74000.0, 73850.0, True),       # 150 against: out
    ("CE", 74000.0, 74300.0, False),      # in favour
    ("PE", 74000.0, 74149.5, False),
    ("PE", 74000.0, 74150.0, True),
    ("PE", 74000.0, 73700.0, False),
])
def test_spot_floor_fires_at_150_points_against(side, espot, spot, fires):
    eng = _Eng()
    sig = tr.spot_floor_exit(eng, pe.Signal("HOLD"), {"side": side, "espot": espot}, spot, tr.V5)
    if fires:
        assert (sig.action, sig.side, sig.reason) == ("EXIT", "CALL" if side == "CE" else "PUT", "spot_floor")
        assert eng.skip_cd_arm is False                       # the re-entry wait arms as after any exit
    else:
        assert sig.action == "HOLD" and eng.skip_cd_arm is True


def test_spot_floor_leaves_other_books_and_engine_exits_alone():
    eng = _Eng()
    pos = {"side": "CE", "espot": 74000.0}
    for book in (tr.V4, tr.V3_DT35, tr.V3, tr.PX):
        assert tr.spot_floor_exit(eng, pe.Signal("HOLD"), pos, 73000.0, book).action == "HOLD"
    target = pe.Signal("EXIT", "CALL", "daily_target")
    assert tr.spot_floor_exit(eng, target, pos, 73000.0, tr.V5) is target                # the engine's exit wins
    assert tr.spot_floor_exit(eng, pe.Signal("HOLD"), pos, None, tr.V5).action == "HOLD"   # no spot, no stop


def test_registered_as_a_paper_tab_runner_and_overview_card():
    from labs.services.book_overview_more import MORE_BOOKS as BOOKS
    from labs.ui.routes import LIVE_TABS
    assert LIVE_TABS["proposer_v5"] == "Sensex V5" and BOOKS["proposer_v5"]["label"] == "Sensex V5"
    template = (ROOT / "templates" / "live_strategy.html").read_text(encoding="utf-8")
    assert "{% include '_proposer_v5.html' %}" in template and "/labs/api/proposer_v5/backfill" in template
    loop = (ROOT / "pa_paper_tracker_loop.py").read_text(encoding="utf-8")
    assert '("proposer_v5", run_proposer_v5_day)' in loop
    tab = (ROOT / "templates" / "_proposer_v5.html").read_text(encoding="utf-8")
    assert "150 points against its entry" in tab and "in-sample" in tab and "tick data" in tab


def test_ui_patch_applies_next_to_the_v4_wiring_and_is_idempotent(tmp_path):
    files = ("labs/ui/routes.py", "templates/live_strategy.html", "pa_paper_tracker_loop.py",
             "labs/services/book_overview_more.py", "templates/_proposer_v5.html")
    for rel in files:
        target = tmp_path / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy(ROOT / rel, target)
    run = subprocess.run([sys.executable, str(ROOT / "scripts" / "patch_proposer_v5_ui.py"), str(tmp_path)],
                         capture_output=True, text=True)
    assert run.returncode == 0 and run.stdout.count("0 edits applied") == 4, run.stdout + run.stderr
    routes = (tmp_path / "labs/ui/routes.py").read_text(encoding="utf-8")
    assert routes.count('"proposer_v5": "Sensex V5"') == 1
    assert routes.count('@labs_bp.route("/api/proposer_v5/backfill"') == 1
    py_compile.compile(str(tmp_path / "labs/ui/routes.py"), doraise=True)
    py_compile.compile(str(tmp_path / "pa_paper_tracker_loop.py"), doraise=True)
    import jinja2
    for rel in ("templates/live_strategy.html", "templates/_proposer_v5.html"):
        jinja2.Environment().parse((tmp_path / rel).read_text(encoding="utf-8"))
