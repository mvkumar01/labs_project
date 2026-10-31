"""OI Unwind paper books: walls and the unwinding gate, the 5-minute break, both sides, stop and target, candles by
the clock, the ledger and the wiring. Parity with the research is checked outside the suite on the research's own
inputs (alphaIMB research/experiments/2026-09-09_proposer_v1_reverse_engineering/source/oi_unwind_parity.py)."""
from __future__ import annotations

import shutil
import sqlite3
import subprocess
import sys
from datetime import datetime, timedelta
from pathlib import Path

import pandas as pd
import pytest

from labs.engine import oi_unwind_tracker as U

ROOT = Path(__file__).resolve().parents[1]
DAY = "2026-10-26"
OPEN = datetime(2026, 10, 26, 9, 15)
CPR = U.cpr_levels(25600.0, 25400.0, 25500.0)


def _bars(closes):
    return [(OPEN + timedelta(minutes=i), c, c + 1, c - 1, c) for i, c in enumerate(closes)]


def _marks(res, unwinding=True, sup=(25300.0, False)):
    return [(OPEN + timedelta(minutes=5 * i), sup, (res, unwinding)) for i in range(40)]


def _frame(rows):
    return pd.DataFrame(rows, columns=["timestamp", "strike", "option_type", "expiry", "oi", "spot"])


def test_books_are_frozen():
    assert (U.NIFTY.lot_size, U.NIFTY.oi_pct, U.NIFTY.oi_raw, U.NIFTY.band) == (65, 4.0, 132000.0, 200.0)
    assert (U.SENSEX.lot_size, U.SENSEX.oi_pct, U.SENSEX.oi_raw, U.SENSEX.band) == (20, 6.2, 24300.0, 600.0)
    assert U.SENSEX.scale == 3.24 and U.STOP_PTS == 30.0


def test_cpr_pins_tc_above_bc():
    c = U.cpr_levels(100.0, 80.0, 82.0)                    # a close below the midpoint inverts the raw pivots
    assert c["TC"] > c["P"] > c["BC"]


def test_wall_is_largest_oi_in_band_and_a_fall_is_unwinding():
    t0, t1 = f"{DAY} 09:15:00", f"{DAY} 09:20:00"
    rows = [(t0, 25500, "CE", "26OCT", 500000, 25480), (t1, 25500, "CE", "26OCT", 300000, 25480),
            (t0, 25550, "CE", "26OCT", 100000, 25480), (t1, 25550, "CE", "26OCT", 100000, 25480),
            (t0, 25450, "PE", "26OCT", 400000, 25480), (t1, 25450, "PE", "26OCT", 450000, 25480)]
    m = U.wall_marks(_frame(rows), U.NIFTY)
    assert m[0][2] == (25500.0, False)                     # the first mark has no earlier OI
    assert m[1][2] == (25500.0, True) and m[1][1] == (25450.0, False)


def test_a_call_needs_a_close_through_an_unwinding_wall():
    closes = [25490.0] * 10 + [25505.0] * 5                # 09:25 candle (index 14) closes above 25500
    t = U.simulate(_bars(closes), _marks(25500.0), CPR, U.NIFTY, session_done=False)
    assert len(t) == 1 and t[0]["side"] == "CALL" and t[0]["entry_t"] == OPEN + timedelta(minutes=14)
    assert t[0]["stop"] == 25475.0 and t[0]["target"] == CPR["R1"] and t[0]["exit_t"] is None
    assert not U.simulate(_bars(closes), _marks(25500.0, unwinding=False), CPR, U.NIFTY, session_done=False)


def test_put_is_the_mirror():
    closes = [25310.0] * 10 + [25295.0] * 5
    marks = _marks(25700.0, unwinding=False, sup=(25300.0, True))
    t = U.simulate(_bars(closes), marks, CPR, U.NIFTY, session_done=False)
    assert len(t) == 1 and t[0]["side"] == "PUT" and t[0]["stop"] == 25325.0 and t[0]["target"] < 25295.0


def test_stop_first_when_a_bar_holds_both():
    bars = _bars([25490.0] * 10 + [25505.0] * 5)
    target = U.simulate(bars, _marks(25500.0), CPR, U.NIFTY, session_done=False)[0]["target"]
    bars.append((OPEN + timedelta(minutes=15), 25500.0, target + 1, 25470.0, 25500.0))
    t = U.simulate(bars, _marks(25500.0), CPR, U.NIFTY)
    assert t[0]["reason"] == "STOP" and t[0]["exit_spot"] == 25475.0


def test_candles_follow_the_clock_when_a_bar_is_missing():
    bars = [b for b in _bars([25490.0] * 10 + [25505.0] * 5) if b[0] != OPEN + timedelta(minutes=7)]
    t = U.simulate(bars, _marks(25500.0), CPR, U.NIFTY, session_done=False)
    assert t[0]["entry_t"] == OPEN + timedelta(minutes=14)


def test_saved_and_rendered(monkeypatch, tmp_path):
    bars = {OPEN + timedelta(minutes=i): (c, c + 1, c - 1, c) for i, c in enumerate([25490.0] * 10 + [25505.0] * 5)}
    prev = {OPEN - timedelta(days=3): (25500.0, 25600.0, 25400.0, 25500.0)}
    t0, t1 = f"{DAY} 09:15:00", f"{DAY} 09:20:00"
    frame = pd.DataFrame(
        [(t0, 25500, "CE", "26N03", 500000, 25480, 10, 9, 11, "NIFTY26N0325500CE"),
         (t1, 25500, "CE", "26N03", 300000, 25480, 10, 9, 11, "NIFTY26N0325500CE"),
         (f"{DAY} 09:30:00", 25500, "CE", "26N03", 300000, 25480, 12, 11, 13, "NIFTY26N0325500CE")],
        columns=["timestamp", "strike", "option_type", "expiry", "oi", "spot", "ltp", "bid", "ask", "tradingsymbol"])
    monkeypatch.setattr(U, "options_frame", lambda day, book: frame)
    monkeypatch.setattr(U, "spot_bars", lambda day, book: bars if day == DAY else prev)
    conn = sqlite3.connect(tmp_path / "labs.db")
    conn.row_factory = sqlite3.Row
    out = U.run_day(DAY, U.NIFTY, now=datetime(2026, 10, 26, 9, 31, tzinfo=U.IST), connection=conn)
    assert out["trades"] == 1 and out["open"] == 1
    import jinja2
    env = jinja2.Environment(loader=jinja2.FileSystemLoader(str(ROOT / "templates")), undefined=jinja2.StrictUndefined)
    rows, trades, stats = U.tab_data(conn, "oi_unwind_nifty")
    assert rows[0]["status"] == "live" and stats["label"] == "OI Unwind NIFTY" and stats["open_trades"]
    html = env.get_template("_oi_unwind.html").render(oiu_rows=rows, oiu_trades=trades, oiu_stats=stats)
    assert "OI Unwind NIFTY" in html and "SENSEX" not in html.split("Evidence status")[0]


def _render(rows, trades, stats):
    import jinja2
    env = jinja2.Environment(loader=jinja2.FileSystemLoader(str(ROOT / "templates")))
    return env.get_template("_oi_unwind.html").render(oiu_rows=rows, oiu_trades=trades, oiu_stats=stats)


def test_tab_renders_before_any_session_is_recorded(tmp_path):
    conn = sqlite3.connect(tmp_path / "labs.db")
    html = _render(*U.tab_data(conn, "oi_unwind_sensex"))                # no tables yet
    assert "OI Unwind SENSEX" in html and "No sessions recorded" in html
    U._ensure_tables(conn, "oi_unwind_sensex")
    assert "No sessions recorded" in _render(*U.tab_data(conn, "oi_unwind_sensex"))   # no rows in the dates
    assert "book unavailable" in _render([], [], {"error": "boom"})      # what the route passes on a failed read


def test_backfill_runs_in_batches_and_skips_recorded_days(monkeypatch, tmp_path):
    days = ["2026-10-20", "2026-10-21", "2026-10-22"]
    db = tmp_path / "labs.db"

    def fake_run_day(day, book):
        conn = sqlite3.connect(db)
        U._save(conn, day, book, {}, error="no quotes")      # a recorded day, even when unavailable
        conn.close()
        return {"trade_date": day}

    monkeypatch.setattr(U, "get_conn", lambda: sqlite3.connect(db))
    monkeypatch.setattr(U, "sessions_with_quotes", lambda start, end, book: days)
    monkeypatch.setattr(U, "run_day", fake_run_day)
    run = lambda **kw: U.run_backfill(end="2026-10-30", books=[U.NIFTY], limit=2, **kw)["oi_unwind_nifty"]   # noqa: E731
    assert run() == {"done": 2, "failed": {}, "remaining": 1}
    assert run() == {"done": 1, "failed": {}, "remaining": 0}
    assert run()["done"] == 0
    assert run(rebuild=True)["done"] == 2


def test_ui_patch_is_idempotent_on_its_own_output(tmp_path):
    files = ("labs/ui/routes.py", "templates/live_strategy.html", "pa_paper_tracker_loop.py",
             "labs/services/book_overview_more.py", "labs/services/strategy_report.py")
    for rel in files:
        target = tmp_path / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy(ROOT / rel, target)
    for _ in range(2):
        run = subprocess.run([sys.executable, str(ROOT / "scripts" / "patch_oi_unwind_ui.py"), str(tmp_path)],
                             capture_output=True, text=True)
        assert run.returncode == 0, run.stdout + run.stderr
    assert run.stdout.count(": 0 edits applied") == 5, run.stdout
    routes = (tmp_path / "labs/ui/routes.py").read_text(encoding="utf-8")
    assert 'oi_unwind_sensex' in routes and "def oi_unwind_backfill(key):" in routes


@pytest.mark.parametrize("name", ["place_order", "modify_order", "cancel_order"])
def test_paper_only(name):
    assert name not in (ROOT / "labs" / "engine" / "oi_unwind_tracker.py").read_text(encoding="utf-8")
