"""NIFTY on Sensex V5 signals: the Sensex V5 2.5% book's SENSEX signals traded on NIFTY options with NIFTY's
own exits. Parity with the research simulator (alphaIMB source/nifty_book_parity.py: 76/76 sessions identical,
+Rs 6,28,848, 1 Jul - 16 Oct) is checked outside the suite; these pin the rules and the wiring."""
from __future__ import annotations

import dataclasses
import py_compile
import subprocess
import sys
from datetime import date
from pathlib import Path

import pandas as pd
import pytest

from labs.engine import proposer_nifty_tracker as nt
from labs.engine import proposer_nifty_v5_book as book
from labs.engine import proposer_px_tracker as tr
from live.engine import proposer_engine as pe

ROOT = Path(__file__).resolve().parents[1]


def test_book_rules():
    assert book.BOOK is tr.NIFTY_V5 and book.BOOK.strategy_version == pe.STRATEGY_VERSION_V5
    assert book.DEFAULT_START == "2026-07-01" and tr.NIFTY_V5.key in tr.NIFTY_BOOKS
    p = tr.params_on("2026-10-20", tr.NIFTY_V5)
    assert p.daily_target_pct == 0.015 and p.loss_floor_pct == -0.999 and p.reentry_cooldown_secs == 270.0
    assert p.with_day_pts == 75.0                         # the SENSEX filter, unscaled
    q = nt._params("2026-10-20", tr.NIFTY_V5, 0.31)
    assert q.spot_target_decisive_pts == pytest.approx(100 * 0.31 * 1.5)
    assert q.spot_target_neutral_pts == pytest.approx(40 * 0.31 * 1.5)
    assert q.spot_floor_pts == pytest.approx(150 * 0.31) and q.with_day_pts == 75.0
    assert dataclasses.replace(q, spot_target_decisive_pts=p.spot_target_decisive_pts,
                               spot_target_neutral_pts=p.spot_target_neutral_pts,
                               spot_target_tolerance_pts=p.spot_target_tolerance_pts,
                               spot_floor_pts=p.spot_floor_pts) == p
    assert nt.CAPITAL_RS == 1_200_000.0 and nt.LOT_SIZE > 0


def test_simulate_day_dispatches_nifty_books(monkeypatch):
    seen = {}
    monkeypatch.setattr(nt, "simulate_day", lambda day, **kw: seen.setdefault("call", (day, kw["book"])) and {"x": 1})
    assert tr.simulate_day("2026-10-20", book=tr.NIFTY_V5) == {"x": 1}
    assert seen["call"] == ("2026-10-20", tr.NIFTY_V5)


def _snap(spot, ltps):
    return pd.DataFrame([{"strike": k, "option_type": typ, "ltp": v, "tradingsymbol": f"N{k}{typ}"}
                         for (k, typ), v in ltps.items()])


def test_strike_is_the_shallowest_itm_with_delta_at_least_0_6():
    day, exp = date(2026, 10, 20), date(2026, 10, 27)
    # ITM calls below spot 23,010 (ATM 23,000): 22,950 / 22,900 / 22,850, priced so deeper = higher delta
    snap = _snap(23010, {(22950, "CE"): 190.0, (22900, "CE"): 225.0, (22850, "CE"): 262.0})
    k, sym = nt.pick_strike(snap, "CE", 23010.0, exp, day)
    assert k in (22950, 22900, 22850) and sym == f"N{k}CE"
    deltas = {s: abs(nt.ps.bs_delta(23010.0, s, nt.ps.years_to_expiry(exp, day),
                                    nt.ps.implied_vol(p, 23010.0, s, nt.ps.years_to_expiry(exp, day), "CE"), "CE"))
              for s, p in ((22950, 190.0), (22900, 225.0), (22850, 262.0))}
    first = next((s for s in (22950, 22900, 22850) if deltas[s] >= 0.60), max(deltas, key=deltas.get))
    assert k == first
    assert nt.pick_strike(snap.iloc[0:0], "CE", 23010.0, exp, day) == (None, None)


def test_registered_as_a_paper_tab_runner_and_overview_card():
    from labs.services.book_overview_more import MORE_BOOKS as BOOKS
    from labs.ui.routes import LIVE_TABS
    assert LIVE_TABS["proposer_nifty_v5"] == "Nifty · Sensex V5 signals"
    assert BOOKS["proposer_nifty_v5"]["instrument"] == "NIFTY options"
    template = (ROOT / "templates" / "live_strategy.html").read_text(encoding="utf-8")
    assert "{% include '_proposer_nifty_v5.html' %}" in template and "/labs/api/proposer_nifty_v5/backfill" in template
    loop = (ROOT / "pa_paper_tracker_loop.py").read_text(encoding="utf-8")
    assert '("proposer_nifty_v5", run_proposer_nifty_v5_day)' in loop


def test_tab_renders(tmp_path):
    import sqlite3
    import jinja2
    conn = sqlite3.connect(tmp_path / "labs.db")
    tr._ensure_tables(conn, tr.NIFTY_V5)
    env = jinja2.Environment(loader=jinja2.FileSystemLoader(str(ROOT / "templates")), autoescape=True)
    html = env.get_template("_proposer_nifty_v5.html").render(
        proposer_nifty_v5_rows=[], proposer_nifty_v5_trades=[], proposer_nifty_v5_stats={"daily_target_pct": 0.015})
    assert "1.5% day target" in html and "NIFTY options" in html and "in-sample" in html
    assert "&amp;mdash;" not in html and "&amp;#8377;" not in html


def test_ui_patch_applies_next_to_the_v5_dt25_wiring_and_is_idempotent(tmp_path):
    files = ("labs/ui/routes.py", "templates/live_strategy.html", "pa_paper_tracker_loop.py",
             "labs/services/book_overview_more.py")
    for rel in files:
        target = tmp_path / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(subprocess.run(["git", "-C", str(ROOT), "show", f"45fc676:{rel}"],
                                          capture_output=True, check=True).stdout)
    script = str(ROOT / "scripts" / "patch_proposer_nifty_v5_ui.py")
    first = subprocess.run([sys.executable, script, str(tmp_path)], capture_output=True, text=True)
    assert first.returncode == 0 and "0 edits applied" not in first.stdout, first.stdout + first.stderr
    second = subprocess.run([sys.executable, script, str(tmp_path)], capture_output=True, text=True)
    assert second.returncode == 0 and second.stdout.count("0 edits applied") == 4
    routes = (tmp_path / "labs/ui/routes.py").read_text(encoding="utf-8")
    assert routes.count('"proposer_nifty_v5": "Nifty · Sensex V5 signals"') == 1
    assert routes.count('"proposer_v5_dt25": "Sensex V5 2.5%"') == 1
    for rel in ("labs/ui/routes.py", "pa_paper_tracker_loop.py", "labs/services/book_overview_more.py"):
        py_compile.compile(str(tmp_path / rel), doraise=True)
    import jinja2
    jinja2.Environment().parse((tmp_path / "templates/live_strategy.html").read_text(encoding="utf-8"))
