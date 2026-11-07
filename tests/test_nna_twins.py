"""Nifty New Alpha V1 paper twins (C1 win rate, C2 balanced, C3 points): the minute-stop engine, lot A and the entry
stop, the VIX lock, max one trade a day, the tick path, the option leg, the ledger and the wiring. Parity with the
research (nna_candidates_reference.py) is checked outside the suite on the research's own inputs (alphaIMB
research/experiments/2026-10-24_nifty_bare_range/nna_twins_labs_parity.py) and the tick rebuild against the raw
ticks (nna_twins_tick_check.py)."""
from __future__ import annotations

import sqlite3
from datetime import datetime, timedelta
from pathlib import Path

import numpy as np
import pandas as pd
import pytest

from labs.engine import nna_twins_tracker as tw
from labs.engine import tick_replay

ROOT = Path(__file__).resolve().parents[1]
DAY = "2026-11-09"
C1, C2, C3, REF = tw.BOOKS["nna_c1"], tw.BOOKS["nna_c2"], tw.BOOKS["nna_c3"], tw.REF_MAX1


def at(hm: str) -> datetime:
    return datetime.fromisoformat(f"{DAY}T{hm}")


def session(alpha_by_mark: dict, bars: dict, done: bool = True) -> tuple[dict, dict]:
    """inp as bn_alpha.session_inputs builds it: marks with alpha and the mark minute's close."""
    marks = sorted(at(k) for k in alpha_by_mark)
    inp = {"marks": marks, "alpha": np.array([alpha_by_mark[m.strftime("%H:%M")] for m in marks], float),
           "spot": np.array([bars[m][3] for m in marks], float), "session_done": done, "expiry": "26N10"}
    return inp, bars


def flat_bars(start="09:15", end="15:29", px=100.0) -> dict:
    t, out = at(start), {}
    while t <= at(end):
        out[t] = (px, px, px, px)
        t += timedelta(minutes=1)
    return out


def test_the_books():
    assert (C1.lots, C1.book, C1.alpha_stop, C1.lock) == (2, 10.0, -15.0, False)
    assert (C2.lots, C2.book, C2.alpha_stop, C2.lock) == (2, 15.0, -30.0, False)
    assert (C3.lots, C3.book, C3.alpha_stop, C3.lock) == (1, None, -30.0, True)
    assert set(tw.BOOKS) <= set(tick_replay.TICK_BOOKS) and tick_replay.TICK_BOOKS["nna_c1"] == "2026-10-27"
    assert {C1.reference["points"], C2.reference["points"], C3.reference["points"]} == {1913.2, 2333.6, 4733.3}


def call_day(over=None):
    """CALL at 09:20 (alpha 20 -> 40) at 100; the alpha stays 40 at every later mark."""
    bars = flat_bars()
    bars.update(over or {})
    alpha = {f"{9 + (15 + 5 * j) // 60:02d}:{(15 + 5 * j) % 60:02d}": (20.0 if j == 0 else 40.0) for j in range(75)}
    return session(alpha, bars)


def test_lot_a_then_entry_stop_on_one_minute_bars():
    inp, bars = call_day({at("09:22"): (100, 111, 100, 105), at("09:23"): (105, 106, 99, 100)})
    closed, open_, _ = tw.simulate(inp, bars, C1, None)
    t, = closed
    assert t["lotA_ts"] == at("09:22") and t["lotA_pts"] == 10.0
    assert t["exit_reason"] == "ENTRY_STOP" and t["exit_ts"] == at("09:23") and t["exit_spot"] == 100.0
    assert t["spot_pts"] == pytest.approx(5.0)          # 0.5 x 10 + 0.5 x 0


def test_the_entry_stop_starts_the_minute_after_lot_a_on_bars_but_at_once_on_ticks():
    # 09:22 reaches +10 and falls back to 99 inside the same minute
    later = {m: (101, 101, 101, 101) for m in flat_bars("09:23")}         # never back to the entry spot
    inp, bars = call_day({at("09:22"): (100, 111, 99, 100), **later})
    closed, _, _ = tw.simulate(inp, bars, C1, None)
    assert closed[0]["exit_reason"] == "EOD"            # on bars the entry stop is only in force from 09:23

    class Ticks:
        lat = 2.0
        idx = ([at("09:22") + timedelta(seconds=s) for s in (1, 10, 20, 40)], [100.0, 110.5, 105.0, 99.5], [], [])
    closed, _, info = tw.simulate(inp, bars, C1, None, Ticks())
    t, = closed
    assert t["exit_reason"] == "ENTRY_STOP" and t["exit_spot"] == 99.5
    assert t["lotA_fill_at"] == at("09:22") + timedelta(seconds=10)
    assert t["exit_fill_at"] == at("09:22") + timedelta(seconds=40)
    assert info["fb_min"] > 0                           # the other minutes had no ticks: one-minute rule, counted


def test_c1_alpha_stop_is_minus_15_and_c2_minus_30():
    bars = flat_bars()
    alpha = {"09:15": 20.0, "09:20": 40.0, "09:25": -20.0, "09:30": -35.0}
    inp, bars = session(alpha, bars)
    assert tw.simulate(inp, bars, C1, None)[0][0]["exit_ts"] == at("09:25")
    t = tw.simulate(inp, bars, C2, None)[0][0]
    assert (t["exit_ts"], t["exit_reason"]) == (at("09:30"), "ALPHA_SL")


def test_max_one_trade_a_day():
    bars = flat_bars()
    alpha = {"09:15": 20.0, "09:20": 40.0, "09:25": -40.0, "09:30": 20.0, "09:35": 40.0, "09:40": 45.0}
    inp, bars = session(alpha, bars)
    closed, open_, _ = tw.simulate(inp, bars, REF, None)
    assert len(closed) == 1 and open_ is None


def test_c3_lock_depends_on_vix_at_the_open():
    over = {at("09:21"): (100, 135, 100, 134), at("09:22"): (134, 141, 133, 140), at("09:23"): (140, 140, 110, 112)}
    inp, bars = call_day(over)
    lo, = tw.simulate(inp, bars, C3, 14.0)[0]
    hi, = tw.simulate(inp, bars, C3, 16.0)[0]
    assert (lo["exit_reason"], lo["exit_spot"], lo["exit_ts"]) == ("LOCK", 115.0, at("09:23"))   # +30 -> entry +15
    assert (hi["exit_reason"], hi["exit_spot"], hi["exit_ts"]) == ("LOCK", 120.0, at("09:23"))   # +40 -> entry +20
    gap = dict(over)
    gap[at("09:23")] = (105, 106, 104, 105)              # opens through the level: fill at the open
    inp, bars = call_day(gap)
    assert tw.simulate(inp, bars, C3, 14.0)[0][0]["exit_spot"] == 105.0


def test_trail_arms_at_30_and_trails_15():
    over = {at("09:21"): (100, 131, 100, 130), at("09:22"): (130, 140, 129, 139), at("09:23"): (139, 139, 120, 121)}
    inp, bars = call_day(over)
    t, = tw.simulate(inp, bars, REF, None)[0]
    assert (t["exit_reason"], t["exit_spot"]) == ("TRAIL", 125.0)   # best 140 known before 09:23 -> 125


def test_an_open_trade_before_the_close_is_marked_not_closed():
    inp, bars = call_day()
    inp["session_done"] = False
    closed, open_, _ = tw.simulate(inp, bars, C2, None)
    assert closed == [] and open_["exit_reason"] == "open"


def _frame(minutes, strike=100, typ="CE", bid=10.0, ask=11.0):
    rows = []
    for j, m in enumerate(minutes):
        rows.append((m, strike, typ, "26N10", 1, bid + j, ask + j, "NIFTY26N10100CE"))
    return pd.DataFrame(rows, columns=["timestamp", "strike", "option_type", "expiry", "oi", "bid", "ask",
                                       "tradingsymbol"])


def test_option_leg_one_minute_and_ticks():
    minutes = [at("09:20"), at("09:21"), at("09:22"), at("09:23"), at("09:24")]
    frame = _frame(minutes)                              # bid 10,11,12,13,14 / ask 11..15 by minute
    t = dict(side="CALL", entry_ts=at("09:20"), entry_fill_at=at("09:21"), entry_spot=100.0, lotA_ts=at("09:21"),
             lotA_fill_at=at("09:21") + timedelta(seconds=30), lotA_pts=10.0, exit_ts=at("09:22"),
             exit_fill_at=at("09:22") + timedelta(seconds=5), exit_reason="ENTRY_STOP", spot_pts=5.0)
    row = tw.price_trade(t, frame, "26N10", C1)
    # entry at the mark minute's ask (11); lot A and the stop at the first quote after their minute (12, 13)
    assert (row["entry_ask"], row["lotA_bid"], row["exit_bid"]) == (11.0, 12.0, 13.0)
    assert row["qty"] == 130 and row["gross_rs"] == pytest.approx((12 - 11) * 65 + (13 - 11) * 65)

    class Ticks:
        lat = 2.0

        def quote(self, sym, when):
            return {at("09:21:02"): (20.5, 0.5), at("09:21:32"): (24.0, 1.0), at("09:22:07"): (21.0, 1.0)}.get(when)
    row = tw.price_trade(t, frame, "26N10", C1, Ticks())
    assert (row["entry_ask"], row["lotA_bid"], row["exit_bid"]) == (21.0, 23.0, 20.0)


def test_ledger_and_tab(tmp_path):
    conn = sqlite3.connect(tmp_path / "t.db")
    for key, twin in tw.BOOKS.items():
        tw._ensure_tables(conn, key)
        inp, bars = call_day({at("09:22"): (100, 111, 100, 105), at("09:23"): (105, 106, 99, 100)})
        inp["range_lo"], inp["range_hi"] = 24000, 24700
        sess = {"frame": _frame(sorted(bars)), "inp": inp, "bars": bars, "prev_close": 24360.0,
                "prev_close_date": "2026-11-06", "prev_close_source": "kite_daily", "baseline_date": "2026-11-06",
                "vix_open": 16.2, "vix_source": "alpha_v214e_daily.context"}
        r = tw.replay_twin(sess, twin)
        tw._save(conn, DAY, twin, r)
        rows, trades, stats = tw.tab_data(conn, key)
        assert rows[0]["status"] == "traded" and rows[0]["replay"] == "1min" and rows[0]["vix_open"] == 16.2
        assert stats["trades"] == 1 and stats["label"] == twin.label and stats["lots"] == twin.lots
        assert trades[0]["qty"] == 65 * twin.lots
    assert tick_replay.replays(conn, "nna_c2")[DAY]["replay"] == "1min"


def test_a_traded_day_counts_as_done_for_the_tick_marker(tmp_path):
    conn = sqlite3.connect(tmp_path / "t.db")
    assert "traded" in tick_replay.DONE_STATUSES and "partial_quotes" in tick_replay.DONE_STATUSES
    assert tick_replay.day_done(conn, "2026-11-09") is False        # no tables yet


def test_run_live_reads_the_session_once_for_all_three(monkeypatch, tmp_path):
    calls = []
    conn_path = tmp_path / "t.db"
    monkeypatch.setattr(tw, "get_conn", lambda: sqlite3.connect(conn_path))
    monkeypatch.setattr(tick_replay, "ticks_for", lambda *a, **k: None)

    def fake_session(day, **kw):
        calls.append(day)
        inp, bars = call_day()
        inp.update(range_lo=1, range_hi=2, session_done=False)
        return {"frame": _frame(sorted(bars)), "inp": inp, "bars": bars, "prev_close": 1.0, "prev_close_date": "x",
                "prev_close_source": "kite_daily", "baseline_date": "x", "vix_open": None, "vix_source": "missing"}
    monkeypatch.setattr(tw, "session", fake_session)
    out = tw.run_live(datetime.fromisoformat(f"{DAY}T10:31:30").replace(tzinfo=tw.IST))
    assert calls == [DAY] and set(out) == set(tw.BOOKS)


def test_wiring():
    import importlib
    for key in tw.BOOKS:
        mod = importlib.import_module(f"labs.engine.{key}_book")
        assert mod.KEY == key and mod.BOOK is tw.BOOKS[key]
    routes = (ROOT / "labs/ui/routes.py").read_text(encoding="utf-8")
    page = (ROOT / "templates/live_strategy.html").read_text(encoding="utf-8")
    loop = (ROOT / "pa_paper_tracker_loop.py").read_text(encoding="utf-8")
    assert all(f'"{k}": "NNA V1' in routes for k in tw.BOOKS) and "_nna_twins.html" in page
    assert "run_nna_twins_day" in loop
    from labs.services.strategy_report import SOURCES
    assert SOURCES["nna_c1"].qty == 130 and SOURCES["nna_c3"].qty == 65
    from jinja2 import Environment, FileSystemLoader
    env = Environment(loader=FileSystemLoader(str(ROOT / "templates")))
    env.get_template("_nna_twins.html")                 # compiles


def test_the_twins_never_place_orders():
    src = (ROOT / "labs/engine/nna_twins_tracker.py").read_text(encoding="utf-8")
    for banned in ("place_order", "live_executor", "proposer_runner", "kite.place", "smartapi"):
        assert banned not in src


def test_post_close_runs_the_twins_and_the_marker_waits_for_them(tmp_path, monkeypatch):
    """The paper loop's post-close pass replays the twins on the compacted ticks like the proposer tick books, and the
    downloader's done-marker needs every twin's day closed on ticks."""
    monkeypatch.setattr(tick_replay, "TICK_ARCHIVE_DIR", tmp_path / "archive")
    monkeypatch.setattr(tick_replay, "DONE_DIR", tmp_path / "done")
    monkeypatch.setattr(tick_replay, "TICK_BOOKS", {k: "2026-10-27" for k in tw.BOOKS})
    d = tmp_path / "archive" / DAY
    d.mkdir(parents=True)
    pd.DataFrame({"recv_ts": pd.to_datetime([f"{DAY} 09:21:01", f"{DAY} 09:21:30"]).tz_localize("Asia/Kolkata"),
                  "ltp": [100.0, 101.0], "bid1_px": [None, None], "ask1_px": [None, None]}).to_parquet(d / "NIFTY_50.parquet")
    (d / "manifest.json").write_text("{}", encoding="utf-8")
    conn = sqlite3.connect(tmp_path / "t.db")
    ran = []

    def run_book_day(key, day):
        ran.append(key)
        inp, bars = call_day()
        inp.update(range_lo=1, range_hi=2)
        sess = {"frame": _frame(sorted(bars)), "inp": inp, "bars": bars, "prev_close": 1.0, "prev_close_date": "x",
                "prev_close_source": "kite_daily", "baseline_date": "x", "vix_open": None, "vix_source": "missing"}
        tw._ensure_tables(conn, key)
        tw._save(conn, day, tw.BOOKS[key], tw.replay_twin(sess, tw.BOOKS[key], tick_replay.DayTicks(day)))

    out = tick_replay.post_close(DAY, conn, run_book_day)
    assert sorted(ran) == sorted(tw.BOOKS) and out["marker"] and tick_replay.day_done(conn, DAY)
    assert {r for r in tick_replay.replays_for_day(conn, DAY).values()} == {"ticks"}
    conn.execute("UPDATE nna_c2_daily SET status='live' WHERE trade_date=?", (DAY,))
    assert not tick_replay.day_done(conn, DAY)


def test_a_past_day_uses_the_official_close_nna_v1_locked(monkeypatch, tmp_path):
    conn = sqlite3.connect(tmp_path / "t.db")
    from labs.engine import bn_alpha_tracker as core
    core._ensure_tables(conn, "nna_v1")
    conn.execute("INSERT INTO nna_v1_daily (trade_date,status,prev_close,prev_close_date,prev_close_source,n_trades,"
                 "strategy_version,updated_at) VALUES (?,?,?,?,?,?,?,?)",
                 ("2026-11-06", "traded", 24536.6, "2026-11-05", "kite_daily", 1, "v", "t"))
    seen = {}

    def fake_session(day, **kw):
        seen.update(kw)
        raise core.BnAlphaInputError("stop here")
    monkeypatch.setattr(tw, "session", fake_session)
    out = tw.run_days("2026-11-06", list(tw.BOOKS), now=datetime(2026, 11, 7, 10, 0, tzinfo=tw.IST), connection=conn,
                      ticks=None)
    assert seen["known_close"] == (24536.6, "2026-11-05", "kite_daily")
    assert all(v["status"] == "unavailable" for v in out.values())


def test_the_tabs_render(monkeypatch, tmp_path):
    from flask import Flask
    from labs.ui.routes import labs_bp
    from labs.engine.paper_strategy_tracker import _ensure_tables as paper_tables
    conn = sqlite3.connect(tmp_path / "t.db", check_same_thread=False)
    conn.row_factory = sqlite3.Row
    paper_tables(conn)                                  # the page reads the v2.11 ledger first
    for key, twin in tw.BOOKS.items():
        tw._ensure_tables(conn, key)
        inp, bars = call_day({at("09:22"): (100, 111, 100, 105), at("09:23"): (105, 106, 99, 100)})
        inp["range_lo"], inp["range_hi"] = 24000, 24700
        sess = {"frame": _frame(sorted(bars)), "inp": inp, "bars": bars, "prev_close": 24360.0,
                "prev_close_date": "2026-11-06", "prev_close_source": "kite_daily", "baseline_date": "2026-11-06",
                "vix_open": 16.2, "vix_source": "alpha_v214e_daily.context"}
        tw._save(conn, "2026-11-06", twin, tw.replay_twin(sess, twin))
    app = Flask(__name__, template_folder="../templates")
    app.register_blueprint(labs_bp)
    conn.commit()

    def fresh():                                        # the page closes its connection after each request
        c = sqlite3.connect(tmp_path / "t.db", check_same_thread=False)
        c.row_factory = sqlite3.Row
        return c
    monkeypatch.setattr("storage.db.get_conn", fresh)
    for key, twin in tw.BOOKS.items():
        html = app.test_client().get(f"/labs/live?tab={key}").get_data(as_text=True)
        assert f"{twin.label} (paper)" in html and "<th>Replay</th>" in html and "1-minute" in html
        assert ("Lot A" in html) == bool(twin.book) and "never calls a broker order API" in html
        empty = app.test_client().get(f"/labs/live?tab={key}&date_from=2026-06-01&date_to=2026-06-05").get_data(as_text=True)
        assert f"{twin.label} (paper)" in empty and "No sessions recorded" in empty
