"""Nifty Scalper V3 Reverse paper book: V3 as an unseen shadow, the book holding the opposite option."""
import sqlite3
from pathlib import Path

import jinja2

from labs.engine import proposer_nifty_v3_reverse_book as rev
from labs.engine import proposer_px_tracker as tr
from labs.engine import proposer_px_view as view
from labs.engine import tick_replay as tk

ROOT = Path(__file__).resolve().parents[1]
DAY = "2026-11-12"


def test_the_shadow_trades_exactly_as_v3():
    book = rev.BOOK
    assert book.reverse and not tr.NIFTY_V3.reverse
    assert book.key in tr.NIFTY_BOOKS and book.key in tr.DELTA_STRIKE_BOOKS
    assert book.print_gap_s == tr.NIFTY_V3.print_gap_s and book.print_lag_s == tr.NIFTY_V3.print_lag_s
    for day in ("2026-07-01", "2026-10-20", DAY):
        assert tr.params_on(day, book) == tr.params_on(day, tr.NIFTY_V3)
    assert rev.DEFAULT_START == "2026-07-01" and tk.TICK_BOOKS[book.key] == tk.TICK_BOOKS[tr.NIFTY_V3.key]


def _day(**kw):
    r = {"trade_date": DAY, "status": "closed", "expiry_code": "26NOV", "regime": "bullish", "gap_pct": 0.1,
         "n_trades": 1, "n_losses": 1, "day_banked": True, "gross_rs": -25_000.0, "charges_rs": 500.0,
         "net_rs": -25_500.0, "book_gross_before": 0.0, "through_ts": DAY + "T15:25", "qty": 650,
         "shadow_gross_rs": 16_000.0, "replay": None, "prints": [],
         "trades": [{"signal": "regime_entry", "side": "PE", "strike": 26_100, "tradingsymbol": "NIFTY26NOV26100PE",
                     "expiry_code": "26NOV", "entry_ts": DAY + "T09:31:25", "exit_ts": DAY + "T09:52:10",
                     "entry_spot": 26_000.0, "exit_spot": 26_060.0, "entry_price": 180.0, "exit_price": 141.54,
                     "gross_rs": -25_000.0, "charges_rs": 500.0, "net_rs": -25_500.0, "exit_rule": "daily_target"}]}
    r.update(kw)
    return r


def test_the_recovery_book_is_the_shadows_and_the_tab_shows_both(tmp_path):
    conn = sqlite3.connect(tmp_path / "paper.db")
    conn.row_factory = sqlite3.Row
    tr._ensure_tables(conn, rev.BOOK)
    tr._persist(conn, _day(), rev.BOOK)
    assert tr._book_before(conn, "2026-11-13", rev.BOOK) == 16_000.0      # the shadow's gross, not the book's -25,000

    rows, trades, stats = view.tab_data(conn, book=rev.BOOK)
    assert rows[0]["shadow_gross_rs"] == 16_000.0 and stats["shadow_gross"] == 16_000.0
    assert stats["net_total"] == -25_500.0 and trades[0]["side"] == "PE"
    env = jinja2.Environment(loader=jinja2.FileSystemLoader(str(ROOT / "templates")), autoescape=True)
    html = env.get_template("_proposer_nifty_v3_reverse.html").render(
        proposer_nifty_v3_reverse_rows=rows, proposer_nifty_v3_reverse_trades=trades,
        proposer_nifty_v3_reverse_stats=stats)
    assert "V3 shadow gross" in html and "16,000" in html and "NIFTY26NOV26100PE" in html


def test_registered_as_a_tab_a_loop_runner_and_an_overview_card():
    from labs.services.book_overview_more import MORE_BOOKS
    from labs.services.strategy_report import SOURCES
    from labs.ui.routes import LIVE_TABS
    key = rev.BOOK.key
    assert LIVE_TABS[key] == "Nifty Scalper V3 Reverse" and MORE_BOOKS[key]["instrument"] == "NIFTY options"
    assert SOURCES[key].table == key + "_trades"
    page = (ROOT / "templates" / "live_strategy.html").read_text(encoding="utf-8")
    assert f"{{% include '_{key}.html' %}}" in page and f"/labs/api/{key}/backfill" in page
    loop = (ROOT / "pa_paper_tracker_loop.py").read_text(encoding="utf-8")
    assert f'("{key}", run_{key}_day)' in loop
