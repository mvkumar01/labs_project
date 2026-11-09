"""Nifty Scalper V4 1.5% / 2.5% on a fixed clock (Vipin, 2026-11-09): the V4 books with every print acted on 30 s
into its minute and no new entry after the day's 2nd losing trade. Paper only."""
from __future__ import annotations

import dataclasses
import sqlite3
from datetime import datetime
from pathlib import Path

import jinja2
import pytest

from labs.engine import proposer_px_tracker as tr
from labs.engine import tick_replay
from live.engine import proposer_engine as pe

ROOT = Path(__file__).resolve().parents[1]
PAIRS = ((tr.NIFTY_V4_FC, tr.NIFTY_V4, "Nifty Scalper V4 1.5% · fixed clock"),
         (tr.NIFTY_V4_DT25_FC, tr.NIFTY_V4_DT25, "Nifty Scalper V4 2.5% · fixed clock"))


@pytest.mark.parametrize("book,base,label", PAIRS)
def test_book_is_its_v4_book_plus_the_loss_limit(book, base, label):
    a, b = tr.params_on("2026-10-20", base), tr.params_on("2026-10-20", book)
    assert b.max_losses_per_day == 2 and a.max_losses_per_day == 0
    assert dataclasses.replace(b, max_losses_per_day=0) == a
    assert book.print_lag_s == 30.0 and base.print_lag_s is None and book.print_gap_s == base.print_gap_s == 180.0
    for s in (tr.NIFTY_BOOKS, tr.ENTRY_BLOCK_BOOKS, tr.BANK_REGARDLESS_BOOKS, tr.DELTA_STRIKE_BOOKS):
        assert book.key in s
    assert tr.early_adverse_on("2026-07-01", book) == tr.early_adverse_on("2026-07-01", base)
    assert tick_replay.TICK_BOOKS[book.key] == "2026-10-27"


def test_engine_holds_after_the_second_loss():
    eng = pe.ProposerEngine(tr.params_on("2026-10-20", tr.NIFTY_V4_FC))
    eng.set_book(day_realized=-2000.0, book_net=-2000.0, day_losses=2)
    p = {"regime": "bull", "x5": "strong_bull", "x5_conf": 0.9, "x5_asof": "2026-10-20T10:00:30",
         "drift_state": "up"}
    sig = eng.evaluate(datetime(2026, 10, 20, 10, 0, 30), p, pe.Position(), option_ltp=None, spot=25000.0,
                       day_move=500.0)
    assert sig.action == "HOLD"
    eng.set_book(day_realized=-1000.0, book_net=-1000.0, day_losses=1)
    assert eng.evaluate(datetime(2026, 10, 20, 10, 0, 30), p, pe.Position(), option_ltp=None, spot=25000.0,
                        day_move=500.0).reason != "day_loss_limit"


def test_prints_land_on_the_book_second(monkeypatch):
    monkeypatch.setattr(tr.pp, "predict_x5", lambda prior, rows, chain, regime: {
        "x5": "neutral", "x5_conf": 0.5, "micro": {}} if len(rows) >= 7 else None)
    monkeypatch.setattr(tr.pp, "drift_state", lambda micro: "flat")
    t0 = datetime(2026, 10, 20, 9, 15)
    bars = [(t0.replace(minute=15 + i), 25000.0) for i in range(30)]
    through = datetime(2026, 10, 20, 9, 44)
    for lag, want in ((None, tr.PRINT_LAG_S), (30.0, 30.0)):
        out, _ = tr._prints("2026-10-20", [], bars, None, {}, through, gap_s=180.0, print_lag_s=lag)
        assert out and {p["sec"] for p in out} == {float(want)}
        assert [p["t"].strftime("%H:%M") for p in out[:3]] == ["09:22", "09:25", "09:28"]


@pytest.mark.parametrize("book,base,label", PAIRS)
def test_tab_registered_and_renders(tmp_path, book, base, label):
    from labs.services.book_overview_more import MORE_BOOKS
    from labs.services.strategy_report import NIFTY_SCALPER_KEYS
    from labs.ui.routes import LIVE_TABS
    key = book.key
    assert LIVE_TABS[key] == label and MORE_BOOKS[key]["instrument"] == "NIFTY options" and key in NIFTY_SCALPER_KEYS
    page = (ROOT / "templates" / "live_strategy.html").read_text(encoding="utf-8")
    assert "{% include '_" + key + ".html' %}" in page and f"/labs/api/{key}/backfill" in page
    assert f'("{key}", run_{key}_day)' in (ROOT / "pa_paper_tracker_loop.py").read_text(encoding="utf-8")
    module = __import__(f"labs.engine.{key}_book", fromlist=["BOOK"])
    assert module.BOOK is book and module.DEFAULT_START == "2026-07-01"
    conn = sqlite3.connect(tmp_path / "labs.db")
    tr._ensure_tables(conn, book)
    env = jinja2.Environment(loader=jinja2.FileSystemLoader(str(ROOT / "templates")), autoescape=True)
    html = env.get_template(f"_{key}.html").render(**{f"{key}_rows": [], f"{key}_trades": [], f"{key}_stats": {}})
    assert "30 s into its minute" in html and "2nd losing trade" in html and "Paper only" in html
    assert f'id="{key.replace("_", "-")}-backfill"' in html and f"{base.key}_stats" not in html
