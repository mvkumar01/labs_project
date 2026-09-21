from __future__ import annotations

import re
import sqlite3
from pathlib import Path

from labs.engine import crude_macd_st_tracker, theta_straddle_tracker
from labs.services.book_overview import BOOKS, build_overview

ROOT = Path(__file__).resolve().parents[1]
TODAY = "2026-09-22"


def _conn() -> sqlite3.Connection:
    conn = sqlite3.connect(":memory:")
    crude_macd_st_tracker._ensure_tables(conn)
    theta_straddle_tracker._ensure_tables(conn)
    return conn


def test_crude_card_uses_margin_as_capital_and_shows_open_position():
    conn = _conn()
    for day, net in (("2026-09-18", 1000.0), (TODAY, 500.0)):
        conn.execute(
            "INSERT INTO crude_macd_st_daily (trade_date,status,n_trades,net_rs,qty,"
            "strategy_version,updated_at) VALUES (?,?,1,?,100,'v','u')",
            (day, "final" if day != TODAY else "live", net))
    conn.execute(
        "INSERT INTO crude_macd_st_trades (trade_date,seq,tradingsymbol,signal_ts,entry_ts,"
        "entry_price,stop_price,target_price,stop_dist,qty,net_rs,status,margin_rs) VALUES "
        "(?,1,'X','s','e',9000,8960,9020,40,100,500,'open',290000)", (TODAY,))
    conn.execute(
        "INSERT INTO crude_macd_st_trades (trade_date,seq,tradingsymbol,signal_ts,entry_ts,"
        "entry_price,stop_price,target_price,stop_dist,qty,net_rs,status,margin_rs) VALUES "
        "('2026-09-18',1,'X','s','e',8000,7960,8020,40,100,1000,'closed',250000)")
    cards = {c["key"]: c for c in build_overview(conn, TODAY)}
    crude = cards["crude_macd_st"]
    assert crude["today_net"] == 500.0 and crude["total_net"] == 1500.0
    assert crude["capital"] == 290000.0                     # peak single commitment
    assert crude["return_pct"] == round(100 * 1500 / 290000, 2)
    assert crude["position"] == "Long 1 lot" and crude["open_net"] == 500.0


def test_short_premium_card_uses_recorded_capital():
    conn = _conn()
    conn.execute(
        "INSERT INTO theta_straddle_daily (trade_date,status,lot_size,lots,qty,n_legs,"
        "priced_legs,capital_required_rs,gross_rs,charges_rs,net_rs,margin_method,"
        "strategy_version,updated_at) VALUES (?,'closed',65,1,65,2,2,160000,900,100,800,'m','v','u')",
        ("2026-09-21",))
    card = {c["key"]: c for c in build_overview(conn, TODAY)}["theta_straddle"]
    assert card["capital"] == 160000.0 and card["total_net"] == 800.0
    assert card["position"] == "Flat" and card["win_pct"] == 100


def test_missing_book_tables_degrade_to_waiting_cards():
    cards = build_overview(sqlite3.connect(":memory:"), TODAY)
    assert [c["key"] for c in cards] == list(BOOKS)
    assert all(c["status"] == "Waiting" for c in cards)


def test_overview_is_the_default_alpha_labs_view():
    from labs.ui.routes import LIVE_TABS
    assert next(iter(LIVE_TABS)) == "overview"
    for retired in ("alpha_v211a", "alpha_v213", "proposer_sensex", "sensex_alpha_inverted",
                    "sensex_v211", "sensex_v211_inverted"):
        assert retired not in LIVE_TABS


def test_retired_books_are_not_run():
    for script in ("pa_paper_tracker_loop.py", "pa_paper_tracker.py"):
        src = (ROOT / script).read_text(encoding="utf-8")
        for module in ("alpha_v211a_tracker", "alpha_v213_tracker", "proposer_sensex_tracker",
                       "sensex_alpha_tracker import", "sensex_v211_tracker",
                       "sensex_v211_inverted_tracker"):
            assert module not in src, (script, module)
        # the book shown as "Sensex_alpha" reads the inverted-execution tables
        assert "sensex_alpha_inverted_tracker" in src


def test_simulation_uses_the_labs_kite_token():
    from labs.simulation.config import LABS_KITE_TOKEN
    assert LABS_KITE_TOKEN == ROOT / "config" / "zerodha_token.json"
    for path in (ROOT / "labs").rglob("*.py"):
        assert "zerodha_access_token" not in path.read_text(encoding="utf-8"), path


def test_every_page_uses_the_shared_chrome_and_theme():
    for page in (ROOT / "templates").glob("*.html"):
        if page.name.startswith("_"):
            continue
        src = page.read_text(encoding="utf-8")
        assert "{{ theme_head() }}" in src, page.name


def test_every_colour_token_is_defined_for_both_themes():
    theme = (ROOT / "static" / "theme.css").read_text(encoding="utf-8")
    dark, light = theme.split('html[data-theme="light"] {', 1)
    refs = set()
    for path in list((ROOT / "static").glob("*.*")) + list((ROOT / "templates").glob("*.html")):
        if path.suffix in (".css", ".js", ".html") and path.name != "theme.css":
            text = path.read_text(encoding="utf-8")
            refs |= set(re.findall(r"var\(--c-([0-9a-f]{6})\)", text))
            refs |= set(re.findall(r'labsColor\("#([0-9a-f]{6})"\)', text))
    assert refs
    for token in refs:
        assert f"--c-{token}:" in dark and f"--c-{token}:" in light, token


def test_no_page_says_paper():
    from app import app
    client = app.test_client()
    for url in ("/labs/", "/labs/live", "/labs/live?tab=nifty", "/labs/live?tab=crude_macd_st",
                "/labs/backtest", "/labs/simulation/", "/live/login"):
        html = client.get(url).get_data(as_text=True)
        visible = re.sub(r"<script.*?</script>|<style.*?</style>|<[^>]+>", " ", html, flags=re.S)
        assert not re.search(r"\bpaper\b", visible, re.I), url
