"""Sensex 2.5% V6 IV-half: the V6 paper book at half size on a day whose 09:20 SENSEX ATM IV is above 1.2 x the median
of the earlier sessions' (Vipin, 2026-11-09). Paper only."""
from __future__ import annotations

import sqlite3
import subprocess
import sys
from datetime import date, datetime, time
from pathlib import Path

import jinja2
import pandas as pd
import pytest

from labs.engine import proposer_px_tracker as tr
from labs.engine import tick_replay as tk
from live.engine import proposer_strike as ps
from tests.test_proposer_px import _synthetic_day

ROOT = Path(__file__).resolve().parents[1]
IVH = tr.V6_DT25_IVH
DAY = "2026-09-29"


def test_book_is_v6_apart_from_the_size():
    for day in ("2026-07-01", "2026-10-20", "2026-11-09"):
        assert tr.params_on(day, IVH) == tr.params_on(day, tr.V6_DT25)
    key = IVH.key
    assert key in tr.NO_INSIDE_CPR_BOOKS and key in tr.DELTA_STRIKE_BOOKS and key not in tr.NIFTY_BOOKS
    assert IVH.strategy_version == tr.V6_DT25.strategy_version and tr.SETTINGS_FROM[key] == "2026-07-01"
    assert tk.TICK_BOOKS[key] == "2026-10-27" and tk.index_of(key) == "SENSEX"
    assert tr.IV_HALF_BOOKS == {key: tr.IvHalf(1.2, 0.5, "2026-07-01", 10, time(9, 20))}


def test_iv_size_rule():
    rule = tr.IV_HALF_BOOKS[IVH.key]
    ten = [0.2, 0.18, 0.22, 0.2, 0.19, 0.21, 0.2, 0.25, 0.15, 0.2]           # median 0.2
    assert tr.iv_size(rule, 0.25, ten) == {"day_iv": 0.25, "iv_ref": 0.2, "iv_n": 10, "qty": 1000}
    assert tr.iv_size(rule, 0.24, ten)["qty"] == 2000                           # 1.2 x exactly: full
    assert tr.iv_size(rule, 0.5, ten[:9])["qty"] == 2000                        # fewer than 10 sessions
    assert tr.iv_size(rule, None, ten)["qty"] == 2000 and tr.iv_size(rule, None, ten)["day_iv"] is None
    assert tr.iv_size(rule, 0.3, ten + [None, float("nan")])["iv_n"] == 10


def test_atm_iv_is_the_mean_of_call_and_put_from_the_mid():
    at = datetime(2026, 11, 4, 9, 30)
    rows = [  # ATM 80000 (spot 80030 rounds down); the put has no valid quote, so its LTP is used
        dict(timestamp=at, spot=80030.0, strike=80000, option_type="CE", bid=400.0, ask=410.0, ltp=399.0),
        dict(timestamp=at, spot=80030.0, strike=80000, option_type="PE", bid=0.0, ask=0.0, ltp=380.0),
        dict(timestamp=at, spot=80030.0, strike=80100, option_type="CE", bid=350.0, ask=352.0, ltp=351.0),
        dict(timestamp=datetime(2026, 11, 4, 9, 31), spot=80030.0, strike=80000, option_type="CE", bid=1.0,
             ask=2.0, ltp=1.0)]
    near = pd.DataFrame(rows)
    years = (datetime(2026, 11, 5, 15, 30) - at).total_seconds() / (365 * 86400)
    want = (ps.implied_vol(405.0, 80030.0, 80000, years, "CE") + ps.implied_vol(380.0, 80030.0, 80000, years, "PE")) / 2
    assert ps.expiry_date("26N05") == date(2026, 11, 5)
    assert tr.atm_iv(near, "26N05", at) == pytest.approx(want)
    assert tr.atm_iv(near, "26N05", datetime(2026, 11, 4, 9, 29)) is None       # no 09:30 snapshot


@pytest.fixture
def paper(tmp_path, monkeypatch):
    data = {DAY: _synthetic_day(DAY)}
    monkeypatch.setattr(tr, "spot_ohlc", lambda day: data[day][0] if day in data else [])

    def frame(symbol, day, **_kw):
        if day not in data:
            raise FileNotFoundError(day)
        return data[day][1]

    monkeypatch.setattr(tr, "load_options_frame", frame)
    monkeypatch.setattr(tk, "TICK_BOOKS", {})                                   # the one-minute path here
    conn = sqlite3.connect(tmp_path / "labs.db")
    yield conn
    conn.close()


def _key(t):
    return t["side"], t["strike"], t["entry_ts"], t["exit_ts"], t["exit_rule"], t["exit_price"]


def test_a_high_iv_day_trades_v6_at_half_size(paper):
    full = tr.simulate_day(DAY, book=tr.V6_DT25)
    iv = tr.simulate_day(DAY, book=IVH, iv_hist=[])["day_iv"]
    assert iv and full["trades"] and full["qty"] == 2000 and full.get("day_iv") is None
    low = tr.simulate_day(DAY, book=IVH, iv_hist=[iv] * 10)
    high = tr.simulate_day(DAY, book=IVH, iv_hist=[iv / 1.3] * 10)
    assert low["qty"] == 2000 and high["qty"] == 1000 and high["iv_ref"] == pytest.approx(iv / 1.3, abs=1e-6)
    assert [_key(t) for t in low["trades"]] == [_key(t) for t in full["trades"]] == [_key(t) for t in high["trades"]]
    assert low["gross_rs"] == full["gross_rs"] and high["gross_rs"] == pytest.approx(full["gross_rs"] / 2, abs=0.05)
    assert all(t["qty"] == 1000 for t in high["trades"])
    assert all(t["entry_ts"] >= DAY + "T09:20" for t in high["trades"])


def test_run_day_reads_the_earlier_sessions_and_stores_the_size(paper):
    conn = paper
    tr._ensure_tables(conn, IVH)
    iv = tr.simulate_day(DAY, book=IVH, iv_hist=[])["day_iv"]
    insert = (f"INSERT INTO {IVH.key}_daily (trade_date,status,n_trades,lots,qty,strategy_version,updated_at,day_iv) "
              "VALUES (?,'no_trade',0,100,2000,'x','x',?)")
    for i in range(10):                         # ten earlier sessions at a low IV, one before 1 Jul that is ignored
        conn.execute(insert, (f"2026-07-{i + 1:02d}", iv / 1.5))
    conn.execute(insert, ("2026-06-30", 9.9))
    conn.commit()
    assert tr.iv_history(conn, IVH, DAY) == pytest.approx([iv / 1.5] * 10)
    tr.run_day(DAY, connection=conn, book=IVH)
    row = conn.execute(f"SELECT qty, lots, day_iv, iv_ref, iv_n FROM {IVH.key}_daily WHERE trade_date=?",
                       (DAY,)).fetchone()
    assert row[:2] == (1000, 50) and row[2] == pytest.approx(iv, abs=1e-6) and row[4] == 10
    from labs.engine import proposer_px_view as view
    rows, trades, stats = view.tab_data(conn, book=IVH)
    today = next(r for r in rows if r["trade_date"] == DAY)
    assert today["half"] and today["iv_ratio"] == 1.5 and stats["iv_half"]["days"] == 1
    env = jinja2.Environment(loader=jinja2.FileSystemLoader(str(ROOT / "templates")), autoescape=True)
    html = env.get_template(f"_{IVH.key}.html").render(**{f"{IVH.key}_rows": rows, f"{IVH.key}_trades": trades,
                                                         f"{IVH.key}_stats": stats})
    assert "1,000 (half)" in html and "Half-size days" in html and "(1.5×)" in html


def test_tab_registered_and_renders():
    from labs.services.book_overview_more import MORE_BOOKS
    from labs.services.strategy_report import SOURCES
    from labs.ui.routes import LIVE_TABS
    key = IVH.key
    assert LIVE_TABS[key] == "Sensex 2.5% V6 · IV half" and MORE_BOOKS[key]["instrument"] == "SENSEX options"
    assert MORE_BOOKS[key]["size"] == "100 or 50 lots" and key in SOURCES
    page = (ROOT / "templates" / "live_strategy.html").read_text(encoding="utf-8")
    assert "{% include '_" + key + ".html' %}" in page and f"/labs/api/{key}/backfill" in page
    assert f'("{key}", run_{key}_day)' in (ROOT / "pa_paper_tracker_loop.py").read_text(encoding="utf-8")
    module = __import__(f"labs.engine.{key}_book", fromlist=["BOOK"])
    assert module.BOOK is IVH and module.DEFAULT_START == "2026-07-01"
    env = jinja2.Environment(loader=jinja2.FileSystemLoader(str(ROOT / "templates")), autoescape=True)
    html = env.get_template(f"_{key}.html").render(**{f"{key}_rows": [], f"{key}_trades": [],
                                                      f"{key}_stats": {"daily_target_pct": 0.025}})
    assert "1,000 qty (50 lots)" in html and "Paper only" in html and "26 Oct" in html
    assert 'id="proposer-v6-dt25-ivh-backfill"' in html and "proposer_v6_dt25_stats" not in html


def test_ui_patch_is_idempotent():
    out = subprocess.run([sys.executable, str(ROOT / "scripts" / "patch_proposer_v6_dt25_ivh_ui.py"), str(ROOT)],
                         capture_output=True, text=True)
    assert out.returncode == 0 and out.stdout.count(": 0 edits applied") == 5, out.stdout + out.stderr


def test_transfer_carries_the_iv_columns(tmp_path, paper):
    from labs.engine import paper_day_transfer as xfer
    r = tr.simulate_day(DAY, book=IVH, iv_hist=[0.01] * 10)
    payload = xfer.export_days(IVH, [r])
    daily = payload["sessions"][DAY]["daily"][0]
    assert daily["qty"] == 1000 and daily["iv_n"] == 10 and daily["day_iv"] == r["day_iv"]
    conn = sqlite3.connect(tmp_path / "pa.db")
    xfer.import_days(conn, [payload])
    assert conn.execute(f"SELECT qty, day_iv FROM {IVH.key}_daily").fetchone() == (1000, r["day_iv"])
