"""Nifty Scalper V4 at a chosen day target (Vipin, 2026-11-05): the 2.5% paper book, the 1.5 / 2 / 2.5 / 3% choice on the
configure page, banking whatever the book at any of them, and the day-target card on the live page."""
from __future__ import annotations

import dataclasses
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

import jinja2
import pytest

from labs.engine import proposer_px_tracker as tr
from labs.ui import live_routes as lr
from live import live_executor as ex, live_service as svc
from live.engine import proposer_engine as pe
from storage.live_db import get_live_conn
from tests.test_configure_strategy import CONN_ID, CSRF_TOKEN, USER_ID, _set_strategy, _strategy_pair  # noqa: F401
from tests.test_configure_strategy import client  # noqa: F401

ROOT = Path(__file__).resolve().parents[1]
V3, V4 = pe.STRATEGY_VERSION_NIFTY_SCALPER_V3, pe.STRATEGY_VERSION_NIFTY_SCALPER_V4


def test_book_is_v4_at_2_5_percent():
    a, b = tr.params_on("2026-10-20", tr.NIFTY_V4), tr.params_on("2026-10-20", tr.NIFTY_V4_DT25)
    assert b.daily_target_pct == 0.025 and a.daily_target_pct == 0.015
    assert dataclasses.replace(b, daily_target_pct=0.015) == a
    assert b.bank_regardless_of_book and (b.early_adverse_pts, b.early_adverse_secs) == (35.0, 600.0)
    key = tr.NIFTY_V4_DT25.key
    assert key in tr.NIFTY_BOOKS and key in tr.ENTRY_BLOCK_BOOKS and key in tr.BANK_REGARDLESS_BOOKS
    assert key in tr.DELTA_STRIKE_BOOKS and tr.NIFTY_V4_DT25.print_gap_s == tr.NIFTY_V4.print_gap_s


@pytest.mark.parametrize("pct", [0.015, 0.02, 0.025, 0.03])
def test_live_v4_banks_whatever_the_book_at_every_target(pct):
    p = pe.params_for(V4, pct)
    assert p.daily_target_pct == pct and p.bank_regardless_of_book
    assert not pe.params_for(V3, pct).bank_regardless_of_book
    eng = pe.ProposerEngine(p)
    eng.last_entry_capital = 1_000_000.0
    eng.set_book(day_realized=pct * 1_000_000.0, book_net=-500_000.0)
    assert eng.banked()


def test_tab_registered_and_renders(tmp_path):
    from labs.services.book_overview_more import MORE_BOOKS
    from labs.services.strategy_report import NIFTY_SCALPER_KEYS
    from labs.ui.routes import LIVE_TABS
    key = tr.NIFTY_V4_DT25.key
    assert LIVE_TABS[key] == "Nifty Scalper V4 2.5%" and MORE_BOOKS[key]["instrument"] == "NIFTY options"
    assert key in NIFTY_SCALPER_KEYS
    page = (ROOT / "templates" / "live_strategy.html").read_text(encoding="utf-8")
    assert "{% include '_" + key + ".html' %}" in page and f"/labs/api/{key}/backfill" in page
    assert f'("{key}", run_{key}_day)' in (ROOT / "pa_paper_tracker_loop.py").read_text(encoding="utf-8")
    module = __import__(f"labs.engine.{key}_book", fromlist=["BOOK"])
    assert module.BOOK is tr.NIFTY_V4_DT25 and module.DEFAULT_START == "2026-07-01"
    conn = sqlite3.connect(tmp_path / "labs.db")
    tr._ensure_tables(conn, tr.NIFTY_V4_DT25)
    env = jinja2.Environment(loader=jinja2.FileSystemLoader(str(ROOT / "templates")), autoescape=True)
    html = env.get_template(f"_{key}.html").render(**{f"{key}_rows": [], f"{key}_trades": [],
                                                      f"{key}_stats": {"daily_target_pct": 0.025}})
    assert "2.5% day target" in html and "whatever the running book" in html and "Paper only" in html
    assert 'id="proposer-nifty-v4-dt25-backfill"' in html and "proposer_nifty_v4_stats" not in html


def _post(client, strategy, day_target):                               # noqa: F811
    return client.post("/live/configure", data={"csrf_token": CSRF_TOKEN, "lots": "1", "daily_loss_cap": "3000",
                                                "strategy": strategy, "day_target": day_target})


@pytest.mark.parametrize("choice", ["1.5", "2", "2.5", "3"])
def test_configure_v4_day_target_choices(client, choice):  # noqa: F811
    _set_strategy("proposer", V3)
    assert _post(client, "nifty_scalper_v4", choice).status_code == 302
    assert _strategy_pair() == ("proposer", V4)
    assert svc.get_config(USER_ID, CONN_ID, "proposer_day_target_pct") == choice
    page = client.get("/live/configure").get_data(as_text=True)
    select = page.split('id="day_target"')[1].split("</select>")[0]
    assert f'<option value="{choice}" selected' in select and ">2%</option>" in select and ">3%</option>" in select
    assert "3.5% (Sensex preset)" not in select


def test_v4_defaults_to_1_5_and_other_presets_keep_their_list(client):  # noqa: F811
    _set_strategy("proposer", V3)
    assert _post(client, "nifty_scalper_v4", "").status_code == 302
    assert svc.get_config(USER_ID, CONN_ID, "proposer_day_target_pct") == "1.5"
    _set_strategy("proposer", V3)
    select = client.get("/live/configure").get_data(as_text=True).split('id="day_target"')[1].split("</select>")[0]
    assert "3.5% (Sensex preset)" in select and ">2%</option>" not in select


def _trade(entry_price, qty, gross, reason, dry_run=0):
    now = datetime.now(timezone.utc).isoformat()
    with get_live_conn() as c:
        c.execute("INSERT INTO live_trades (trade_id, user_id, conn_id, side, symbol, entry_price, exit_price, qty, pnl, "
                  "gross_pnl, net_pnl, entry_time, exit_time, reason, strategy, dry_run) VALUES "
                  "(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                  (f"t{reason}{gross}", USER_ID, CONN_ID, "CALL", "NIFTY09NOV2624300CE", entry_price, entry_price, qty,
                   gross, gross, gross, now, now, reason, V4, dry_run))


def test_status_shows_the_day_target_and_banked_state(client):  # noqa: F811
    _set_strategy("proposer", V4)
    svc.set_config(USER_ID, CONN_ID, "proposer_day_target_pct", "2.5")
    t = client.get("/live/status").get_json()["day_target"]
    assert t["pct"] == 2.5 and t["target_rs"] is None and not t["banked"] and t["bank_regardless_of_book"]
    assert t["strategy"] == "Nifty Scalper V4" and t["mode"] == "LIVE"
    _trade(200.0, 325, -1000.0, "spot_floor")
    _trade(250.0, 260, 1625.0, "daily_target")
    t = client.get("/live/status").get_json()["day_target"]
    assert t["capital_rs"] == 65_000.0 and t["target_rs"] == pytest.approx(1625.0) and t["banked"]
    assert t["realized_rs"] == 625.0 and t["trades"] == 2


def test_no_day_target_card_for_other_strategies(client):  # noqa: F811
    _set_strategy("champion_replay", "v2.12")
    assert client.get("/live/status").get_json()["day_target"] is None
    page = (ROOT / "templates" / "live.html").read_text(encoding="utf-8")
    assert 'id="day-target-card"' in page and "applyDayTarget(s)" in (ROOT / "static" / "live.js").read_text(encoding="utf-8")
    assert ex.PROPOSER_DECISION_ABI                                         # unchanged contract; no runner restart needed
