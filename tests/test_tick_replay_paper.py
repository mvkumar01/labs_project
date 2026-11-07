"""Tick replay for the paper books (Vipin, 2026-11-07): V4 1.5% / 2.5%, Nifty Scalper V3 and Sensex 2.5% V6 (SENSEX
index ticks); recorded ticks after the close, the live files intraday, the one-minute path before 27 Oct, a hard stop (never a silent one-minute day) where a past day's ticks are missing, the
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
    monkeypatch.setattr(tk, "TICK_LIVE_DIR", tmp_path / "live")
    tk.forget_live()
    d = tmp_path / "archive" / DAY
    d.mkdir(parents=True)
    _ticks([("2026-11-04 09:40:01", 24500.0, None, None), ("2026-11-04 09:40:20", 24490.0, None, None),
            ("2026-11-04 09:40:50", 24510.0, None, None), ("2026-11-04 09:41:30", 24505.0, None, None)]
           ).to_parquet(d / "NIFTY_50.parquet")
    _ticks([("2026-11-04 09:40:00", 101.0, 100.0, 102.0), ("2026-11-04 09:40:30", 105.0, 104.0, 106.0),
            ("2026-11-04 09:43:00", 99.0, None, None)]).to_parquet(d / "NIFTY26N1024500CE.parquet")
    _ticks([("2026-11-04 09:40:02", 79000.0, None, None), ("2026-11-04 09:40:40", 79050.0, None, None)]
           ).to_parquet(d / "SENSEX.parquet")
    (d / "manifest.json").write_text("{}", encoding="utf-8")
    return d


BOOKS = (tr.NIFTY_V4, tr.NIFTY_V4_DT25, tr.NIFTY_V3, tr.V6_DT25)
NNA_TWINS = ("nna_c1", "nna_c2", "nna_c3")          # tick books too; their own post-close test: test_nna_twins.py


@pytest.fixture(autouse=True)
def _proposer_tick_books(monkeypatch):
    """These tests are about the proposer tick books: the NNA twins are taken out of TICK_BOOKS here."""
    assert all(k in tk.TICK_BOOKS for k in NNA_TWINS)
    monkeypatch.setattr(tk, "TICK_BOOKS", {k: v for k, v in tk.TICK_BOOKS.items() if k not in NNA_TWINS})


def _book(key):
    return next(b for b in BOOKS if b.key == key)


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
    v4, v3 = tr.NIFTY_V4.key, tr.NIFTY_V3_PCLOCK.key
    assert set(tk.TICK_BOOKS) == {b.key for b in BOOKS}
    assert all(tk.TICK_BOOKS[b.key] == "2026-10-27" for b in BOOKS)
    assert tk.ticks_for(v3, DAY, DAY) is None                                        # not a tick book (V3 Pramanaa clock)
    assert tk.ticks_for(v4, "2026-10-26", "2026-11-07") is None                      # before the option bands
    assert isinstance(tk.ticks_for(v4, DAY, "2026-11-07"), tk.DayTicks)              # compacted: ticks
    assert tk.ticks_for(v4, "2026-11-09", "2026-11-09") is None                      # today before compaction
    with pytest.raises(tk.TicksMissing):
        tk.ticks_for(v4, "2026-11-05", "2026-11-07")                                 # past day, ticks not here
    assert not issubclass(tk.TicksMissing, tr.ProposerPxInputError)                  # never recorded unavailable
    sx = tk.ticks_for(tr.V6_DT25.key, DAY, "2026-11-07")                              # the SENSEX book: SENSEX ticks
    assert sx.index == "SENSEX" and sx.idx[1] == [79000.0, 79050.0]
    assert tk.ticks_for(tr.NIFTY_V3.key, DAY, "2026-11-07").index == "NIFTY_50"
    assert sx.path(T0, None, [], _Trm, []) == [(2.0, 79000.0), (40.0, 79050.0)]
    (archive / "SENSEX.parquet").unlink()
    assert not tk.ready_for(tr.V6_DT25.key, DAY) and tk.ready_for(tr.NIFTY_V3.key, DAY)
    with pytest.raises(tk.TicksMissing):
        tk.ticks_for(tr.V6_DT25.key, DAY, "2026-11-07")


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
    tr._ensure_tables(conn, tr.NIFTY_V3_PCLOCK)
    tr._persist(conn, _result(DAY, 5.0), tr.NIFTY_V3_PCLOCK)                          # not a tick book: no row
    assert tk.replays(conn, tr.NIFTY_V3_PCLOCK.key) == {}
    rows, trades, stats = view.tab_data(conn, book=tr.NIFTY_V4)
    assert [r["replay"] for r in rows] == ["ticks", "1min"]
    split = stats["replay_split"]
    assert (split["tick_days"], split["min_days"], split["tick_from"], split["latency_s"]) == (1, 1, "2026-10-27", 2.0)
    assert split["tick_net"] == 1990.0 and split["min_net"] == 990.0
    assert "replay_split" not in view.tab_data(conn, book=tr.NIFTY_V3_PCLOCK)[2]
    env = jinja2.Environment(loader=jinja2.FileSystemLoader(str(ROOT / "templates")), autoescape=True)
    for key in tk.TICK_BOOKS:
        b = _book(key)
        tr._ensure_tables(conn, b)
        tr._persist(conn, _result("2026-10-23", 1000.0), b)
        tr._persist(conn, _result(DAY, 2000.0, {"replay": "ticks", "fb_min": 1, "fb_opt": 0, "latency_s": 2.0}), b)
        rows, trades, stats = view.tab_data(conn, book=b)
        html = env.get_template(f"_{key}.html").render(**{f"{key}_rows": rows, f"{key}_trades": trades,
                                                          f"{key}_stats": stats})
        assert "<th>Replay</th>" in html and "ticks (1 fallback)" in html and "1-min" in html
        assert f"recorded {'SENSEX' if key == tr.V6_DT25.key else 'NIFTY'} index" in html and "2 s after the" in html


def test_post_close_replays_then_drops_the_marker(tmp_path, archive):
    conn = sqlite3.connect(tmp_path / "labs.db")
    ran = []

    def run_book_day(key, day):
        ran.append(key)
        book = _book(key)
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


def test_post_close_waits_for_the_sensex_ticks(tmp_path, archive):
    conn = sqlite3.connect(tmp_path / "labs.db")
    (archive / "SENSEX.parquet").rename(archive.parent / "SENSEX.parquet")
    ran = []

    def run_book_day(key, day):
        ran.append(key)
        book = _book(key)
        tr._ensure_tables(conn, book)
        tr._persist(conn, _result(day, 100.0, tk.DayTicks(day, index=tk.index_of(key)).summary()), book)

    out = tk.post_close(DAY, conn, run_book_day)
    assert out["ready"] is False and out["marker"] is None and tr.V6_DT25.key not in ran
    assert sorted(ran) == sorted(k for k in tk.TICK_BOOKS if k != tr.V6_DT25.key)
    assert not tk.day_done(conn, DAY)                                                      # the downloader keeps them
    (archive.parent / "SENSEX.parquet").rename(archive / "SENSEX.parquet")
    ran.clear()
    out = tk.post_close(DAY, conn, run_book_day)
    assert ran == [tr.V6_DT25.key] and out["marker"] and tk.day_done(conn, DAY)


def test_no_marker_while_a_book_is_not_on_ticks(tmp_path, archive):
    conn = sqlite3.connect(tmp_path / "labs.db")
    for b in BOOKS:
        tr._ensure_tables(conn, b)
        tr._persist(conn, _result(DAY, 1.0, tk.DayTicks(DAY).summary()), b)
    tr._persist(conn, _result(DAY, 1.0), tr.V6_DT25)
    assert not tk.day_done(conn, DAY)
    tr._persist(conn, _result(DAY, 1.0, tk.DayTicks(DAY).summary()), tr.V6_DT25)
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


def test_a_carried_book_imports_only_onto_the_book_it_was_replayed_with(tmp_path):
    pa = sqlite3.connect(tmp_path / "pa.db")
    for book in (tr.NIFTY_V3, tr.V6_DT25):
        tr._ensure_tables(pa, book)
        tr._persist(pa, _result("2026-10-23", 500.0), book)
        tr._persist(pa, _result("2026-10-27", 900.0), book)
        r27, r28 = _result("2026-10-27", 300.0, {"replay": "ticks", "fb_min": 0, "fb_opt": 0, "latency_s": 2.0}), \
            _result("2026-10-28", 200.0, {"replay": "ticks", "fb_min": 0, "fb_opt": 0, "latency_s": 2.0})
        r27["book_gross_before"], r28["book_gross_before"] = 500.0, 799.0             # 28 Oct: wrong carry
        with pytest.raises(ValueError, match="nothing was imported"):
            xfer.import_days(pa, [json.loads(json.dumps(xfer.export_days(book, [r27, r28])))])
        assert pa.execute(f"SELECT gross_rs FROM {book.key}_daily WHERE trade_date='2026-10-27'").fetchone()[0] == 900.0
        r28["book_gross_before"] = 800.0
        assert xfer.import_days(pa, [xfer.export_days(book, [r27, r28])]) == {book.key: ["2026-10-27", "2026-10-28"]}


def test_the_paper_loop_runs_the_post_close_pass():
    loop = (ROOT / "pa_paper_tracker_loop.py").read_text(encoding="utf-8")
    assert "tick_replay.run_post_close(now.date().isoformat())" in loop
    assert "CLOSE < now.time() <= time(*tick_replay.POST_CLOSE_END)" in loop



HEAD = "recv_ts,exch_ts,last_trade_ts,ltp,last_qty,bid1_px,bid1_qty,ask1_px\n"


def _line(ts, ltp, bid="", ask=""):
    return f"2026-11-09T{ts}+05:30,,,{ltp},1,{bid},5,{ask}\n"


def test_live_csv_is_read_incrementally_like_the_archive(tmp_path, monkeypatch, archive):
    live = tk.TICK_LIVE_DIR / "2026-11-09"
    live.mkdir(parents=True)
    f = live / "NIFTY_50.csv"
    f.write_text(HEAD + _line("09:40:01.120", 24500.5) + _line("09:40:20.000", 24490) + "2026-11-09T09:40:3",
                 encoding="utf-8")                                                      # torn last line
    s = tk._live_series(f)
    assert s[0] == [datetime(2026, 11, 9, 9, 40, 1, 120000), datetime(2026, 11, 9, 9, 40, 20)]
    assert s[1] == [24500.5, 24490.0] and all(x != x for x in s[2])                    # empty book -> NaN
    with f.open("a", encoding="utf-8") as fh:                                           # the line completes, more come
        fh.write("0.500+05:30,,,24510,1,,5,\n" + _line("09:40:10.000", 24495) + _line("09:41:02.000", 24505))
    s = tk._live_series(f)
    assert [x.strftime("%H:%M:%S") for x in s[0]] == ["09:40:01", "09:40:10", "09:40:20", "09:40:30", "09:41:02"]
    assert s[1] == [24500.5, 24495.0, 24490.0, 24510.0, 24505.0]                          # stable time order
    assert tk._LIVE[str(f)]["offset"] == f.stat().st_size
    assert tk._live_series(live / "NIFTY26N1024500CE.csv") is None


def test_today_replays_on_the_live_files(tmp_path, archive):
    v4 = tr.NIFTY_V4.key
    assert tk.ticks_for(v4, "2026-11-09", "2026-11-09") is None                            # no live file yet
    live = tk.TICK_LIVE_DIR / "2026-11-09"
    live.mkdir(parents=True)
    (live / "NIFTY_50.csv").write_text(HEAD + _line("09:40:01.000", 24500) + _line("09:40:30.000", 24520),
                                       encoding="utf-8")
    (live / "NIFTY26N1024500CE.csv").write_text(HEAD + _line("09:40:05.000", 101, 100, 102), encoding="utf-8")
    t = tk.ticks_for(v4, "2026-11-09", "2026-11-09")
    assert isinstance(t, tk.DayTicks) and t.source == "live"
    assert t.path(datetime(2026, 11, 9, 9, 40), None, [], _Trm, []) == [(1.0, 24500.0), (30.0, 24520.0)]
    assert t.quote("NIFTY26N1024500CE", datetime(2026, 11, 9, 9, 40, 10)) == (101.0, 1.0)
    assert t.summary()["replay"] == "ticks_live"


def test_live_days_count_as_ticks_and_are_replayed_again_after_compaction(tmp_path, archive):
    conn = sqlite3.connect(tmp_path / "labs.db")
    for b in BOOKS:
        tr._ensure_tables(conn, b)
        tr._persist(conn, _result(DAY, 50.0, {"replay": "ticks_live", "fb_min": 0, "fb_opt": 0, "latency_s": 2.0}), b)
    rows, trades, stats = view.tab_data(conn, book=tr.NIFTY_V4)
    assert stats["replay_split"]["tick_days"] == 1
    env = jinja2.Environment(loader=jinja2.FileSystemLoader(str(ROOT / "templates")), autoescape=True)
    html = env.get_template("_proposer_nifty_v4.html").render(proposer_nifty_v4_rows=rows,
                                                               proposer_nifty_v4_trades=trades,
                                                               proposer_nifty_v4_stats=stats)
    assert "ticks (live files)" in html and "recorder's live files" in html
    assert not tk.day_done(conn, DAY)                                                      # the marker waits
    ran = []

    def run_book_day(key, day):
        ran.append(key)
        tr._persist(conn, _result(day, 50.0, tk.DayTicks(day).summary()), _book(key))

    out = tk.post_close(DAY, conn, run_book_day)
    assert sorted(ran) == sorted(tk.TICK_BOOKS) and out["marker"] and tk.day_done(conn, DAY)


def test_the_sensex_book_reads_its_live_files(tmp_path, archive):
    v6 = tr.V6_DT25.key
    live = tk.TICK_LIVE_DIR / "2026-11-09"
    live.mkdir(parents=True)
    (live / "NIFTY_50.csv").write_text(HEAD + _line("09:40:01.000", 24500), encoding="utf-8")
    assert tk.ticks_for(v6, "2026-11-09", "2026-11-09") is None                            # no SENSEX file yet
    (live / "SENSEX.csv").write_text(HEAD + _line("09:40:03.000", 79000) + _line("09:40:33.000", 79020),
                                     encoding="utf-8")
    t = tk.ticks_for(v6, "2026-11-09", "2026-11-09")
    assert t.source == "live" and t.index == "SENSEX"
    assert t.path(datetime(2026, 11, 9, 9, 40), None, [], _Trm, []) == [(3.0, 79000.0), (33.0, 79020.0)]


def test_the_sensex_tracker_has_the_tick_hooks():
    import inspect
    src = inspect.getsource(tr.simulate_day)
    for hook in ("ticks.path(t, bar,", "ticks.held(pos, now_, ltp)", "ticks.exit_px(pos, now_,",
                 "ticks.entry(c.symbol(t), now_, est, q)", 'pos["sym"] = c.symbol(t)', "if ticks is None:",
                 '"replay": ticks.summary() if ticks is not None else None', 'px = pos.pop("tick_exit", px)'):
        assert hook in src, hook
