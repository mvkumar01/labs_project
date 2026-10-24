"""SENSEX Alpha paper books SX_PC400 / SX_OPEN1400: the two ranges, the absolute alpha, the weekly baseline,
the stale-open flag, the ledger and the wiring. Parity with the research (reference_books.py) is checked outside
the suite on the research's own inputs (alphaIMB research/experiments/2026-10-24_sensex_alpha/labs_book_parity.py:
96 sessions, PC400 125/125 trades +3,617.3 pts, OPEN1400 104/104 +3,593.7 pts)."""
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
from labs.engine import sx_alpha_tracker as sx

ROOT = Path(__file__).resolve().parents[1]
DAY = "2026-10-26"


def test_the_two_books():
    assert (sx.PC400.anchor, sx.PC400.half_width, sx.PC400.arm, sx.PC400.dist) == ("prev_close", 400, 100.0, 50.0)
    assert (sx.OPEN1400.anchor, sx.OPEN1400.half_width, sx.OPEN1400.arm, sx.OPEN1400.dist) == ("open", 1400, 150.0, 75.0)
    assert sx.LOT_SIZE == 20 and sx.FIRST_UNSEEN == "2026-10-26"


def _frame(rows):
    return pd.DataFrame(rows, columns=["timestamp", "strike", "option_type", "expiry", "oi", "bid", "ask", "tradingsymbol"])


def test_absolute_denominator():
    t0, t1 = pd.Timestamp(f"{DAY} 09:15"), pd.Timestamp(f"{DAY} 09:20")
    # dPE = +60, dCE = -100 (call OI unwinding): standard formula 160 * 100 / -40; absolute 160 * 100 / 160
    rows = [(t0, 74000, "CE", "26O29", 200, 1, 2, "x"), (t0, 74000, "PE", "26O29", 100, 1, 2, "x"),
            (t1, 74000, "CE", "26O29", 100, 1, 2, "x"), (t1, 74000, "PE", "26O29", 160, 1, 2, "x")]
    base = pd.DataFrame({"strike": [74000, 74000], "type": ["ce", "pe"], "oi": [200, 100]})
    bars = {datetime(2026, 10, 26, 9, m): (74000, 74001, 73999, 74000) for m in range(15, 21)}
    kw = dict(close=74000.0, frame=_frame(rows), bars=bars, base=base)
    assert core.session_inputs(DAY, **kw, abs_den=True)["alpha"][1] == pytest.approx(100.0)
    assert core.session_inputs(DAY, **kw)["alpha"][1] == pytest.approx(-400.0)
    wide = core.session_inputs(DAY, **kw, half_width=1400, abs_den=True)
    assert (wide["range_lo"], wide["range_hi"]) == (72600, 75400)


def test_weekly_baseline_is_matched_by_date_not_month(tmp_path, monkeypatch):
    monkeypatch.setattr(sx, "PREV_DAY_DIR", tmp_path)
    pd.DataFrame({"symbol": "SENSEX", "strike": [74000, 74000, 74000], "type": ["ce", "pe", "pe"], "oi": [10, 20, 999],
                  "expiry": ["2026-10-29", "2026-10-29", "2026-11-05"]}).to_csv(tmp_path / "prev_day_oi_SENSEX_2026-10-23.csv", index=False)
    base, day = sx.baseline(DAY, "26O29")
    assert day == "2026-10-23" and dict(zip(base["type"], base["oi"])) == {"ce": 10, "pe": 20}
    with pytest.raises(core.BnAlphaInputError):
        sx.baseline(DAY, "26N12")


def _day_inputs(monkeypatch, *, open_px, close_px=74010.0):
    t = [pd.Timestamp(f"{DAY} 09:15") + pd.Timedelta(minutes=5 * i) for i in range(4)]
    alphas_oi = [(100, 100), (100, 160), (100, 200), (100, 240)]                # PE OI builds: alpha rises
    rows = []
    for ts, (ce, pe) in zip(t, alphas_oi):
        rows += [(ts, 74000, "CE", "26O29", ce, 300.0, 302.0, "SENSEX26O2974000CE"),
                 (ts, 74000, "PE", "26O29", pe, 280.0, 282.0, "SENSEX26O2974000PE")]
    bars = {datetime(2026, 10, 26, 9, 15) + timedelta(minutes=k): (open_px if k == 0 else 74000.0, 74001.0, 73999.0, 74000.0)
            for k in range(20)}
    monkeypatch.setattr(sx, "options_frame", lambda d: _frame(rows))
    monkeypatch.setattr(sx, "spot_bars", lambda d: bars)
    monkeypatch.setattr(sx, "baseline", lambda d, code: (pd.DataFrame({"strike": [74000, 74000], "type": ["ce", "pe"],
                                                                         "oi": [100, 100]}), "2026-10-23"))
    monkeypatch.setattr(sx, "prev_close", lambda d, kite=None: (close_px, "2026-10-23", "kite_daily"))


def test_open_centres_the_wide_book_and_a_stale_open_is_flagged_not_changed(monkeypatch):
    _day_inputs(monkeypatch, open_px=74010.0, close_px=74010.0)
    r = sx.replay_day(DAY)
    assert r["open"] == 74010.0 and r["note"] and "stale" in r["note"]
    pc, op = r["books"]["sx_pc400"], r["books"]["sx_open1400"]
    assert (pc["inp"]["range_lo"], pc["inp"]["range_hi"]) == (73600, 74400)
    assert (op["inp"]["range_lo"], op["inp"]["range_hi"]) == (72600, 75400)
    assert op["prev_close_source"] == "open_0915" and pc["prev_close_source"] == "kite_daily"
    _day_inputs(monkeypatch, open_px=74250.0)
    r = sx.replay_day(DAY)
    assert r["note"] is None and r["books"]["sx_open1400"]["inp"]["range_lo"] == 72800


def test_both_books_are_saved_and_rendered(monkeypatch, tmp_path):
    _day_inputs(monkeypatch, open_px=74250.0)
    conn = sqlite3.connect(tmp_path / "labs.db")
    conn.row_factory = sqlite3.Row
    out = sx.run_day(DAY, now=datetime(2026, 10, 27, 9, 0, tzinfo=core.IST), connection=conn)
    assert set(out) >= {"sx_pc400", "sx_open1400"}
    import jinja2
    env = jinja2.Environment(loader=jinja2.FileSystemLoader(str(ROOT / "templates")), undefined=jinja2.StrictUndefined)
    for key in ("sx_pc400", "sx_open1400"):
        rows, trades, stats = sx.tab_data(conn, key)
        assert rows[0]["trade_date"] == DAY and stats["book_key"] == key and stats["lot_size"] == 20
        html = env.get_template("_sx_alpha.html").render(sx_rows=rows, sx_trades=trades, sx_stats=stats)
        assert sx.BOOKS[key].label in html


def test_missing_0915_bar_makes_the_day_unavailable(monkeypatch):
    _day_inputs(monkeypatch, open_px=74250.0)
    bars = {k: v for k, v in sx.spot_bars(DAY).items() if k.minute != 15}
    monkeypatch.setattr(sx, "spot_bars", lambda d: bars)
    with pytest.raises(core.BnAlphaInputError):
        sx.replay_day(DAY)


def test_registered_everywhere():
    from labs.services.book_overview_more import MORE_BOOKS
    from labs.services.strategy_report import SOURCES
    from labs.ui.routes import LIVE_TABS
    for key in ("sx_pc400", "sx_open1400"):
        assert key in LIVE_TABS and key in MORE_BOOKS and SOURCES[key].table == f"{key}_trades"
    assert '("sx_alpha", run_sx_alpha_day)' in (ROOT / "pa_paper_tracker_loop.py").read_text(encoding="utf-8")


def test_ui_patch_is_idempotent_and_compiles(tmp_path):
    files = ("labs/ui/routes.py", "templates/live_strategy.html", "pa_paper_tracker_loop.py",
             "labs/services/book_overview_more.py", "labs/services/strategy_report.py", "templates/_sx_alpha.html")
    for rel in files:
        target = tmp_path / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy(ROOT / rel, target)
    run = subprocess.run([sys.executable, str(ROOT / "scripts" / "patch_sx_alpha_ui.py"), str(tmp_path)],
                         capture_output=True, text=True)
    assert run.returncode == 0 and run.stdout.count("0 edits applied") == 5, run.stdout + run.stderr
    for rel in ("labs/ui/routes.py", "pa_paper_tracker_loop.py", "labs/services/book_overview_more.py",
                "labs/services/strategy_report.py"):
        py_compile.compile(str(tmp_path / rel), doraise=True)
