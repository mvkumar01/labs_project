"""Nifty New Alpha V1 paper book: the 50-point range, the standard alpha, the weekly baseline, the NIFTY trail,
the ledger and the wiring. Parity with the research (reference_book.py) is checked outside the suite on the
research's own inputs (alphaIMB research/experiments/2026-10-24_nifty_bare_range/labs_book_parity.py:
95 sessions, 108/108 trades, +1,055.2 pts)."""
from __future__ import annotations

import py_compile
import shutil
import sqlite3
import subprocess
import sys
from datetime import datetime, timedelta
from pathlib import Path

import pandas as pd
import pytest

from labs.engine import bn_alpha_tracker as core
from labs.engine import nna_tracker as nna

ROOT = Path(__file__).resolve().parents[1]
DAY = "2026-10-26"


def test_the_book():
    assert (nna.HALF_WIDTH, nna.TRAIL_ARM, nna.TRAIL_DIST, nna.STRIKE_STEP) == (350, 30.0, 15.0, 50)
    assert nna.LOT_SIZE == 65 and nna.FIRST_UNSEEN == "2026-10-26" and nna.KEY == "nna_v1"


def _frame(rows):
    return pd.DataFrame(rows, columns=["timestamp", "strike", "option_type", "expiry", "oi", "bid", "ask", "tradingsymbol"])


def test_range_rounds_to_50_and_alpha_is_standard():
    t0, t1 = pd.Timestamp(f"{DAY} 09:15"), pd.Timestamp(f"{DAY} 09:20")
    # dPE = +60, dCE = +20: standard 40 * 100 / 80 = 50
    rows = [(t0, 25550, "CE", "26OCT", 100, 1, 2, "x"), (t0, 25550, "PE", "26OCT", 100, 1, 2, "x"),
            (t1, 25550, "CE", "26OCT", 120, 1, 2, "x"), (t1, 25550, "PE", "26OCT", 160, 1, 2, "x")]
    base = pd.DataFrame({"strike": [25550, 25550], "type": ["ce", "pe"], "oi": [100, 100]})
    bars = {datetime(2026, 10, 26, 9, m): (25560, 25561, 25559, 25560) for m in range(15, 21)}
    inp = core.session_inputs(DAY, close=25560.0, frame=_frame(rows), bars=bars, base=base,
                              half_width=nna.HALF_WIDTH, step=nna.STRIKE_STEP)
    assert (inp["range_lo"], inp["range_hi"]) == (25200, 25900)      # 25560 -> 25550, not 25600
    assert inp["alpha"][1] == pytest.approx(50.0)


def test_weekly_baseline_is_matched_by_date_and_monthly_by_month(tmp_path, monkeypatch):
    monkeypatch.setattr(nna, "PREV_DAY_DIR", tmp_path)
    pd.DataFrame({"symbol": "NIFTY", "strike": [25500] * 4, "type": ["ce", "pe", "pe", "ce"], "oi": [10, 20, 999, 7],
                  "expiry": ["2026-10-27", "2026-10-27", "2026-11-03", "2026-11-03"]}).to_csv(
        tmp_path / "prev_day_oi_NIFTY_2026-10-23.csv", index=False)
    base, day = nna.baseline(DAY, "26OCT")
    assert day == "2026-10-23" and dict(zip(base["type"], base["oi"])) == {"ce": 10, "pe": 20}
    base, _ = nna.baseline(DAY, "26N03")
    assert dict(zip(base["type"], base["oi"])) == {"ce": 7, "pe": 999}
    with pytest.raises(core.BnAlphaInputError):
        nna.baseline(DAY, "26N10")


def test_a_baseline_without_expiry_is_refused_not_guessed(tmp_path, monkeypatch):
    # codeB's old one-expiry format: on 2 Jun 2026 it held the 9 Jun weekly, not the expiring 2 Jun one
    monkeypatch.setattr(nna, "PREV_DAY_DIR", tmp_path)
    pd.DataFrame({"symbol": "NIFTY", "strike": [25500, 25500], "type": ["ce", "pe"], "oi": [10, 20]}).to_csv(
        tmp_path / "prev_day_oi_NIFTY_2026-06-01.csv", index=False)
    pd.DataFrame({"symbol": "NIFTY", "strike": [25500], "type": ["ce"], "oi": [5], "expiry": ["2026-06-02"]}).to_csv(
        tmp_path / "prev_day_oi_NIFTY_2026-05-29.csv", index=False)
    with pytest.raises(core.BnAlphaInputError, match="no expiry column"):
        nna.baseline("2026-06-02", "26602")


def _day_inputs(monkeypatch, spots):
    """Marks every 5 minutes; alpha crosses +30 at 09:20 and stays below the target; `spots` = 1-min closes from 09:15."""
    t = [pd.Timestamp(f"{DAY} 09:15") + pd.Timedelta(minutes=5 * i) for i in range(len(spots) // 5 + 1)]
    rows = []
    for k, ts in enumerate(t):
        ce, pe = (100, 100) if k == 0 else (150, 200)                  # alpha 0, then 33.3
        rows += [(ts, 25500, "CE", "26OCT", ce, 100.0, 101.0, "NIFTY26OCT25500CE"),
                 (ts, 25500, "PE", "26OCT", pe, 90.0, 91.0, "NIFTY26OCT25500PE")]
    bars = {datetime(2026, 10, 26, 9, 15) + timedelta(minutes=k): (c, c + 0.5, c - 0.5, c) for k, c in enumerate(spots)}
    monkeypatch.setattr(nna, "options_frame", lambda d: _frame(rows))
    monkeypatch.setattr(nna, "spot_bars", lambda d: bars)
    monkeypatch.setattr(nna, "baseline", lambda d, code: (pd.DataFrame({"strike": [25500, 25500], "type": ["ce", "pe"],
                                                                          "oi": [100, 100]}), "2026-10-23"))
    monkeypatch.setattr(nna, "prev_close", lambda d, kite=None: (25510.0, "2026-10-23", "kite_daily"))


def test_trail_arms_at_30_and_trails_15(monkeypatch):
    # enter 09:20 at 25500; high reaches 25540.5 by 09:25 (armed: best - entry >= 30); stop = 25525.5
    spots = [25490] * 5 + [25500, 25510, 25520, 25530, 25540, 25540, 25520] + [25520] * 9
    _day_inputs(monkeypatch, spots)
    r = nna.replay_day(DAY)
    closed = [t for t in r["trades"] if t["status"] == "closed"]
    assert closed[0]["side"] == "CALL" and closed[0]["entry_spot"] == 25500
    assert closed[0]["exit_reason"] == "TRAIL" and closed[0]["exit_spot"] == pytest.approx(25525.5)
    assert closed[0]["strike"] == 25500 and closed[0]["qty"] == 65


def test_saved_and_rendered(monkeypatch, tmp_path):
    _day_inputs(monkeypatch, [25490] * 5 + [25500] * 16)
    conn = sqlite3.connect(tmp_path / "labs.db")
    conn.row_factory = sqlite3.Row
    out = nna.run_day(DAY, now=datetime(2026, 10, 27, 9, 0, tzinfo=core.IST), connection=conn)
    assert out["range"] == "25150-25850"
    import jinja2
    env = jinja2.Environment(loader=jinja2.FileSystemLoader(str(ROOT / "templates")), undefined=jinja2.StrictUndefined)
    rows, trades, stats = nna.tab_data(conn)
    assert rows[0]["trade_date"] == DAY and stats["lot_size"] == 65 and stats["label"] == nna.LABEL
    html = env.get_template("_nna_alpha.html").render(nna_rows=rows, nna_trades=trades, nna_stats=stats)
    assert "Nifty New Alpha V1" in html and "SENSEX" not in html


def test_registered_everywhere():
    from labs.services.book_overview_more import MORE_BOOKS
    from labs.services.strategy_report import SOURCES
    from labs.ui.routes import LIVE_TABS
    assert "nna_v1" in LIVE_TABS and "nna_v1" in MORE_BOOKS and SOURCES["nna_v1"].table == "nna_v1_trades"
    assert '("nna_v1", run_nna_day)' in (ROOT / "pa_paper_tracker_loop.py").read_text(encoding="utf-8")


def test_ui_patch_is_idempotent_and_compiles(tmp_path):
    files = ("labs/ui/routes.py", "templates/live_strategy.html", "pa_paper_tracker_loop.py",
             "labs/services/book_overview_more.py", "labs/services/strategy_report.py", "templates/_nna_alpha.html")
    for rel in files:
        target = tmp_path / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy(ROOT / rel, target)
    run = subprocess.run([sys.executable, str(ROOT / "scripts" / "patch_nna_ui.py"), str(tmp_path)],
                         capture_output=True, text=True)
    assert run.returncode == 0 and run.stdout.count("0 edits applied") == 5, run.stdout + run.stderr
    for rel in ("labs/ui/routes.py", "pa_paper_tracker_loop.py", "labs/services/book_overview_more.py",
                "labs/services/strategy_report.py"):
        py_compile.compile(str(tmp_path / rel), doraise=True)
