"""The 3.5% Proposer is an independent paper bot, not a v3 comparison sidecar."""
from __future__ import annotations

import shutil
import sqlite3
import subprocess
import sys
from pathlib import Path

import jinja2

from labs.engine import proposer_px_tracker as tracker
from labs.engine import proposer_v3_dt35_book as dt35

ROOT = Path(__file__).resolve().parents[1]


def test_run_and_backfill_touch_only_the_dt35_book(monkeypatch):
    seen = []
    monkeypatch.setattr(dt35._tracker, "run_day",
                        lambda trade_date=None, *, book, **kw: seen.append(("day", book.key, trade_date)) or {})
    monkeypatch.setattr(dt35._backfill, "run_backfill",
                        lambda **kw: seen.append(("backfill", kw["book"].key, kw["start_date"])) or {})
    dt35.run_day("2026-10-14")
    dt35.run_backfill(start_date="2026-06-01", limit=5)
    assert seen == [("day", "proposer_v3_dt35", "2026-10-14"),
                    ("backfill", "proposer_v3_dt35", "2026-06-01")]
    assert dt35.BOOK == tracker.V3_DT35


def test_tab_reads_the_existing_independent_ledger(tmp_path):
    conn = sqlite3.connect(tmp_path / "labs.db")
    conn.row_factory = sqlite3.Row
    tracker._ensure_tables(conn, tracker.V3_DT35)
    conn.execute("INSERT INTO proposer_v3_dt35_daily (trade_date,status,n_trades,gross_rs,charges_rs,net_rs,"
                 "lots,qty,strategy_version,updated_at) VALUES ('2026-10-14','closed',1,1000,100,900,100,2000,"
                 "'proposer_dt35_v3','x')")
    conn.execute("INSERT INTO proposer_v3_dt35_trades (trade_date,seq,signal,side,strike,entry_ts,exit_ts,"
                 "entry_price,exit_price,gross_rs,charges_rs,net_rs) VALUES ('2026-10-14',1,'s','CE',73000,"
                 "'2026-10-14T10:00:00','2026-10-14T10:05:00',100,101,1000,100,900)")
    rows, trades, stats = dt35.tab_data(conn)
    assert len(rows) == len(trades) == 1
    assert stats["strategy_version"] == "proposer_dt35_v3"
    assert stats["daily_target_pct"] == 0.035 and stats["net_total"] == 900


def test_separate_template_renders_its_name_and_ledger():
    page = jinja2.Environment(loader=jinja2.FileSystemLoader(str(ROOT / "templates"))).get_template(
        "_proposer_v3_dt35.html")
    html = page.render(proposer_v3_dt35_rows=[], proposer_v3_dt35_trades=[], proposer_v3_dt35_stats={})
    assert "Sensex V3 3.5%" in html and "proposer_v3_dt35" in html
    assert "proposer-v3-dt35-backfill" in html


def test_ui_patch_is_idempotent_and_wires_a_separate_tab(tmp_path):
    for rel in ("labs/ui/routes.py", "templates/live_strategy.html", "pa_paper_tracker_loop.py"):
        target = tmp_path / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy(ROOT / rel, target)
    script = ROOT / "scripts" / "patch_proposer_v3_dt35_ui.py"
    first = subprocess.run([sys.executable, str(script), str(tmp_path)], capture_output=True, text=True)
    second = subprocess.run([sys.executable, str(script), str(tmp_path)], capture_output=True, text=True)
    assert first.returncode == second.returncode == 0
    assert second.stdout.count("0 edits applied") == 3
    routes = (tmp_path / "labs/ui/routes.py").read_text(encoding="utf-8")
    page = (tmp_path / "templates/live_strategy.html").read_text(encoding="utf-8")
    loop = (tmp_path / "pa_paper_tracker_loop.py").read_text(encoding="utf-8")
    assert routes.count('"proposer_v3_dt35": "Sensex V3 3.5%"') == 1
    assert routes.count('@labs_bp.route("/api/proposer_v3_dt35/backfill"') == 1
    assert page.count("{% include '_proposer_v3_dt35.html' %}") == 1
    assert loop.count('(\"proposer_v3_dt35\", run_proposer_v3_dt35_day)') == 1
