"""Tick replay for the V4 paper books (Vipin, 2026-11-07): recorded ticks after the close, the one-minute path
intraday and before 27 Oct, a hard stop (never a silent one-minute day) where a past day's ticks are missing, the
done-marker for the depth downloader, the Replay column, and moving locally rebuilt days to PA."""
from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timedelta

import jinja2
import pandas as pd
import pytest

from labs.engine import paper_day_transfer as xfer
from labs.engine import proposer_px_backfill as bf
from labs.engine import proposer_px_tracker as tr
from labs.engine import proposer_px_view as view
from labs.engine import tick_replay as tk

from pathlib import Path
ROOT = Path(__file__).resolve().parents[1]
DAY = "2026-11-04"
T0 = datetime(2026, 11, 4, 9, 40)


def _ticks(rows):
    return pd.DataFrame({"recv_ts": pd.to_datetime([r[0] for r in rows]).tz_localize("Asia/Kolkata"),
                         "ltp": [r[1] for r in rows], "bid1_px": [r[2] for r in rows],
                         "ask1_px": [r[3] for r in rows]})


@pytest.fixture
def archive(tmp_path, monkeypatch):
    monkeypatch.setattr(tk, "TICK_ARCHIVE_DIR", tmp_path / "archive")
    monkeypatch.setattr(tk, "DONE_DIR", tmp_path / "done")
    d = tmp_path / "archive" / DAY
    d.mkdir(parents=True)
    _ticks([("2026-11-04 09:40:01", 24500.0, None, None), ("2026-11-04 09:40:20", 24490.0, None, None),
            ("2026-11-04 09:40:50", 24510.0, None, None), ("2026-11-04 09:41:30", 24505.0, None, None)]
           ).to_parquet(d / "NIFTY_50.parquet")
    _ticks([("2026-11-04 09:40:00", 101.0, 100.0, 102.0), ("2026-11-04 09:40:30", 105.0, 104.0, 106.0),
            ("2026-11-04 09:43:00", 99.0, None, None)]).to_parquet(d / "NIFTY26N1024500CE.parquet")
    (d / "manifest.json").write_text("{}", encoding="utf-8")
    return d


class _Trm:
    @staticmethod
    def _with_print_seconds(pts, secs, close):
        return list(pts)


def test_minute_path_from_index_ticks_and_fallback(archive):
    t = tk.DayTicks(DAY)
    base = [(0.0, 1.0)]
    assert t.path(T0, None, [], _Trm, base) == [(1.0, 24500.0), (20.0, 24490.0), (50.0, 24510.0)]
    assert t.path(T0 + timedelta(minutes=1), None, [], _Trm, base) is base        # one tick: the 1-min path
    assert t.fallback == {"min": 1, "opt": 0}


def test_option_quotes_fills_and_staleness(archive):
    t = tk.DayTicks(DAY, latency_s=2.0)
    sym = "NIFTY26N1024500CE"
    assert t.quote(sym, T0 + timedelta(seconds=10)) == (101.0, 1.0)
    est, q = t.entry(sym, T0 + timedelta(seconds=29), 90.0, (90.0, 0.5))           # 2 s later: the 09:40:30 tick
    assert (est, q) == (105.0, (105.0, 1.0)) and est + q[1] == 106.0                 # in at the ask
    pos = {"sym": sym}
    assert t.exit_px(pos, T0 + timedelta(seconds=40), 1.0) == 104.0                  # out at the bid
    assert t.quote(sym, T0 + timedelta(minutes=3, seconds=10)) == (99.0, 0.0)        # no book: last price
    assert t.quote(sym, T0 + timedelta(minutes=6)) is None                           # older than 120 s
    assert t.held(pos, T0 + timedelta(minutes=6), 50.0) == 50.0 and t.fallback["opt"] == 1
    assert t.quote("NIFTY26N1024000PE", T0) is None


def test_which_days_replay_on_ticks(archive):
    v4, v3 = tr.NIFTY_V4.key, tr.NIFTY_V3.key
    assert set(tk.TICK_BOOKS) == {tr.NIFTY_V4.key, tr.NIFTY_V4_DT25.key}
    assert tk.ticks_for(v3, DAY, DAY) is None                                        # not a tick book
    assert tk.ticks_for(v4, "2026-10-26", "2026-11-07") is None                      # before the option bands
    assert isinstance(tk.ticks_for(v4, DAY, "2026-11-07"), tk.DayTicks)              # compacted: ticks
    assert tk.ticks_for(v4, "2026-11-09", "2026-11-09") is None                      # today before compaction
    with pytest.raises(tk.TicksMissing):
        tk.ticks_for(v4, "2026-11-05", "2026-11-07")                                 # past day, ticks not here
    assert not issubclass(tk.TicksMissing, tr.ProposerPxInputError)                  # never recorded unavailable


def test_rebuild_refuses_before_clearing_a_tick_day(tmp_path, monkeypatch, archive):
    db = tmp_path / "labs.db"
    monkeypatch.setattr(bf, "get_conn", lambda: sqlite3.connect(db))
    monkeypatch.setattr(bf, "sessions_with_quotes", lambda s, e: ["2026-10-23", DAY, "2026-11-05"])
    conn = sqlite3.connect(db)
    tr._ensure_tables(conn, tr.NIFTY_V4)
    conn.execute("INSERT INTO proposer_nifty_v4_daily (trade_date,status,n_trades,lots,qty,strategy_version,"
                 "updated_at) VALUES ('2026-11-05','closed',1,1,1,'x','x')")
    conn.commit()
    out = bf.run_backfill(start_date="2026-10-01", end_date="2026-11-05", rebuild=True, book=tr.NIFTY_V4)
    assert "2026-11-05" in out["errors"] and "TicksMissing" in out["errors"]["2026-11-05"] and not out["done"]
    assert conn.execute("SELECT COUNT(*) FROM proposer_nifty_v4_daily").fetchone()[0] == 1   # nothing cleared


def _result(day, gross, replay=None, trades=1):
    tl = [{"signal": "s", "side": "CE", "strike": 24500, "tradingsymbol": "NIFTY26N1024500CE", "expiry_code": "26N10",
           "entry_ts": f"{day}T09:40:00", "exit_ts": f"{day}T09:50:00", "entry_spot": 1.0, "exit_spot": 2.0,
           "entry_price": 100.0, "exit_price": 110.0, "gross_rs": gross, "charges_rs": 10.0, "net_rs": gross - 10,
           "exit_rule": "daily_target"}] * trades
    return {"trade_date": day, "status": "closed", "expiry_code": "26N10", "regime": "neutral", "gap_pct": 0.1,
            "n_trades": trades, "n_losses": 0, "day_banked": True, "gross_rs": gross * trades,
            "charges_rs": 10.0 * trades, "net_rs": (gross - 10) * trades, "book_gross_before": 0.0,
            "through_ts": f"{day}T15:25", "trades": tl, "prints": [{"minute": "09:40", "sec": 25, "x5": "bull",
                                                                     "x5_conf": 0.7}], "replay": replay}


def test_persist_records_the_replay_and_the_tab_shows_it(tmp_path):
    conn = sqlite3.connect(tmp_path / "labs.db")
    tr._ensure_tables(conn, tr.NIFTY_V4)
    tr._persist(conn, _result("2026-10-23", 1000.0), tr.NIFTY_V4)
    tr._persist(conn, _result(DAY, 2000.0, {"replay": "ticks", "fb_min": 1, "fb_opt": 0, "latency_s": 2.0}),
                tr.NIFTY_V4)
    assert tk.replays(conn, tr.NIFTY_V4.key) == {
        "2026-10-23": {"replay": "1min", "fb_min": None, "fb_opt": None, "latency_s": None},
        DAY: {"replay": "ticks", "fb_min": 1, "fb_opt": 0, "latency_s": 2.0}}
    tr._ensure_tables(conn, tr.NIFTY_V3)
    tr._persist(conn, _result(DAY, 5.0), tr.NIFTY_V3)                                # not a tick book: no row
    assert tk.replays(conn, tr.NIFTY_V3.key) == {}
    rows, trades, stats = view.tab_data(conn, book=tr.NIFTY_V4)
    assert [r["replay"] for r in rows] == ["ticks", "1min"]
    split = stats["replay_split"]
    assert (split["tick_days"], split["min_days"], split["tick_from"], split["latency_s"]) == (1, 1, "2026-10-27", 2.0)
    assert split["tick_net"] == 1990.0 and split["min_net"] == 990.0
    assert "replay_split" not in view.tab_data(conn, book=tr.NIFTY_V3)[2]
    env = jinja2.Environment(loader=jinja2.FileSystemLoader(str(ROOT / "templates")), autoescape=True)
    for key in tk.TICK_BOOKS:
        html = env.get_template(f"_{key}.html").render(**{f"{key}_rows": rows, f"{key}_trades": trades,
                                                          f"{key}_stats": stats})
        assert "<th>Replay</th>" in html and "ticks (1 fallback)" in html and "1-min" in html
        assert "recorded NIFTY" in html and "2 s after the" in html


def test_post_close_replays_then_drops_the_marker(tmp_path, archive):
    conn = sqlite3.connect(tmp_path / "labs.db")
    ran = []

    def run_book_day(key, day):
        ran.append(key)
        book = next(b for b in (tr.NIFTY_V4, tr.NIFTY_V4_DT25) if b.key == key)
        tr._ensure_tables(conn, book)
        tr._persist(conn, _result(day, 100.0, tk.DayTicks(day).summary()), book)

    tr._ensure_tables(conn, tr.NIFTY_V4)
    tr._persist(conn, _result(DAY, 100.0), tr.NIFTY_V4)                                # intraday, 1-min
    out = tk.post_close(DAY, conn, run_book_day)
    assert sorted(ran) == sorted(tk.TICK_BOOKS) and out["marker"]
    marker = json.loads((tk.DONE_DIR / f"{DAY}.json").read_text(encoding="utf-8"))
    assert marker["trade_date"] == DAY and sorted(marker["books"]) == sorted(tk.TICK_BOOKS)
    ran.clear()
    assert tk.post_close(DAY, conn, run_book_day) == {"ready": True, "ran": [], "marker": None}   # idempotent
    assert tk.post_close("2026-11-09", conn, run_book_day)["ready"] is False and not ran         # no ticks yet


def test_no_marker_while_a_book_is_not_on_ticks(tmp_path, archive):
    conn = sqlite3.connect(tmp_path / "labs.db")
    for b in (tr.NIFTY_V4, tr.NIFTY_V4_DT25):
        tr._ensure_tables(conn, b)
    tr._persist(conn, _result(DAY, 1.0, tk.DayTicks(DAY).summary()), tr.NIFTY_V4)
    tr._persist(conn, _result(DAY, 1.0), tr.NIFTY_V4_DT25)
    assert not tk.day_done(conn, DAY)
    tr._persist(conn, _result(DAY, 1.0, tk.DayTicks(DAY).summary()), tr.NIFTY_V4_DT25)
    assert tk.day_done(conn, DAY)


def test_export_import_replaces_sessions_and_rebases_the_book(tmp_path):
    pa = sqlite3.connect(tmp_path / "pa.db")
    pa.row_factory = sqlite3.Row
    tr._ensure_tables(pa, tr.NIFTY_V4)
    for day, g in (("2026-10-23", 500.0), ("2026-10-27", 900.0), ("2026-10-28", 50.0)):
        tr._persist(pa, _result(day, g, trades=2), tr.NIFTY_V4)
    payload = xfer.export_days(tr.NIFTY_V4, [
        _result("2026-10-27", 300.0, {"replay": "ticks", "fb_min": 0, "fb_opt": 0, "latency_s": 2.0}),
        _result("2026-10-28", 200.0, {"replay": "ticks", "fb_min": 1, "fb_opt": 0, "latency_s": 2.0})])
    payload = json.loads(json.dumps(payload))                                              # through a file
    assert xfer.import_days(pa, [payload]) == {tr.NIFTY_V4.key: ["2026-10-27", "2026-10-28"]}
    daily = {r["trade_date"]: dict(r) for r in pa.execute("SELECT * FROM proposer_nifty_v4_daily")}
    assert daily["2026-10-23"]["gross_rs"] == 1000.0                                       # untouched
    assert daily["2026-10-27"]["gross_rs"] == 300.0 and daily["2026-10-27"]["book_gross_before"] == 1000.0
    assert daily["2026-10-28"]["book_gross_before"] == 1300.0
    assert pa.execute("SELECT COUNT(*) FROM proposer_nifty_v4_trades WHERE trade_date='2026-10-27'").fetchone()[0] == 1
    assert tk.replays(pa, tr.NIFTY_V4.key)["2026-10-28"]["fb_min"] == 1
    bad = json.loads(json.dumps(payload))
    bad["sessions"]["2026-10-27"]["trades"][0]["no_such_column"] = 1
    with pytest.raises(sqlite3.OperationalError):
        xfer.import_days(pa, [bad])
    assert pa.execute("SELECT gross_rs FROM proposer_nifty_v4_daily WHERE trade_date='2026-10-27'").fetchone()[0] == 300.0


def test_the_paper_loop_runs_the_post_close_pass():
    loop = (ROOT / "pa_paper_tracker_loop.py").read_text(encoding="utf-8")
    assert "tick_replay.run_post_close(now.date().isoformat())" in loop
    assert "CLOSE < now.time() <= time(*tick_replay.POST_CLOSE_END)" in loop
