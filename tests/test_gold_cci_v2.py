"""Gold V2 accounting, daily halt, restart determinism and V1 isolation."""
from collections import defaultdict
from datetime import datetime, timedelta
from pathlib import Path
import sqlite3

import jinja2
import pandas as pd
import pytest

from labs.engine import gold_cci_tracker as v1
from labs.engine import gold_cci_v2_tracker as tracker
from labs.services.strategy_report import SOURCES, build_reports
from labs.services.book_overview_more import _gold_v2
from live.engine import gold_cci_v2_engine as engine

RATE = 1314292 / (143058 * 100)


@pytest.fixture(scope="module")
def frame():
    return pd.read_parquet(v1.SEED_FILE)


@pytest.fixture(scope="module")
def research_replay(frame):
    return engine.replay(frame, "2026-03-10", end="2026-09-25", margin_rate=RATE)


def test_matches_selected_combined_filter_3pct_variant(research_replay):
    rows = v1._priced(research_replay["trades"])
    assert len(rows) == 62
    assert sum(t["net_rs"] for t in rows) == pytest.approx(1046234, abs=1)
    daily = defaultdict(float)
    for t in research_replay["trades"]:
        daily[t["trade_date"]] += engine.net_pnl(t["entry_price"], t["exit_price"])
    assert len(daily) == 54
    assert sum(n > 0 for n in daily.values()) == 40
    assert sum(n < 0 for n in daily.values()) == 14
    equity = peak = drawdown = 0.0
    for t in rows:
        equity += t["net_rs"]
        peak = max(peak, equity)
        drawdown = max(drawdown, peak - equity)
    assert drawdown == pytest.approx(129579, abs=1)


def test_target_is_net_after_exit_costs_and_halts_remaining_signals(research_replay):
    hits = {day: state for day, state in research_replay["daily"].items() if state["target_hit"]}
    assert len(hits) == 37
    assert any(s["outcome"] == "daily_target" for s in research_replay["signals"])
    for day, state in hits.items():
        trades = [t for t in research_replay["trades"] if t["trade_date"] == day]
        assert trades[-1]["reason"] == "daily_target"
        assert state["realized_net_rs"] >= state["target_rs"]
        assert state["target_rs"] == pytest.approx(state["capital_rs"] * .03, abs=.01)
    price = engine.daily_exit_price(150000, 42000)
    assert engine.net_pnl(150000, price) >= 42000
    assert engine.net_pnl(150000, price + 1) < 42000


def test_cutoff_keeps_open_trade_and_never_uses_future_exit(frame, research_replay):
    t = next(t for t in research_replay["trades"] if t["trade_date"] >= "2026-06-01" and t["exit_ts"] - t["entry_ts"] > timedelta(minutes=5))
    cutoff = t["entry_ts"].to_pydatetime() + timedelta(minutes=1)
    a = engine.replay(frame, t["trade_date"], end=t["trade_date"], cutoff=cutoff, margin_rate=RATE)
    prefix = frame[pd.to_datetime(frame["ts"]) < cutoff]
    b = engine.replay(prefix, t["trade_date"], cutoff=cutoff, margin_rate=RATE)
    assert a["trades"] == b["trades"]
    assert a["trades"][-1]["status"] == "open" and a["trades"][-1]["exit_ts"] is None


def test_frozen_capital_overrides_a_new_entry_margin_estimate(frame, research_replay):
    day = next(iter(research_replay["daily"]))
    result = engine.replay(frame, day, end=day, margin_rate=.2, capital_by_day={day: 1000000})
    assert result["daily"][day]["capital_rs"] == 1000000
    assert result["daily"][day]["target_rs"] == 30000


@pytest.mark.parametrize("rate", [None, 0, -1, float("nan"), float("inf"), 2])
def test_invalid_margin_cannot_create_paper_trades(frame, rate):
    with pytest.raises(ValueError, match="margin"):
        engine.replay(frame, "2026-06-01", margin_rate=rate)


def test_empty_frame_is_safe(frame):
    out = engine.replay(frame.iloc[:0], "2026-06-01", margin_rate=.1)
    assert not out["trades"] and not out["daily"]


def test_both_bullish_warnings_veto_before_cooldown(research_replay):
    taken = [s for s in research_replay["signals"] if s["outcome"] == "taken"]
    blocked = [s for s in research_replay["signals"] if s["outcome"] == "bullish_filter"]
    assert taken and blocked
    assert all(not s["bullish_di15"] and not s["bb_upcross15"] for s in taken)
    assert any(s["bullish_di15"] and not s["bb_upcross15"] for s in blocked)
    assert any(s["bb_upcross15"] and not s["bullish_di15"] for s in blocked)


def test_filter_cannot_see_future_candles(frame, research_replay):
    signals = research_replay["signals"]
    probes = [next(s for s in signals if s["outcome"] == "taken"),
              next(s for s in signals if s["outcome"] == "bullish_filter" and s["bullish_di15"]),
              next(s for s in signals if s["outcome"] == "bullish_filter" and s["bb_upcross15"])]
    flags = engine.entry_filter(research_replay["grid"])
    for s in probes:
        stamp = s["signal_ts"]
        prefix = v1.eng.base.build_bars(frame[pd.to_datetime(frame.ts) <= stamp])
        other = engine.entry_filter(prefix)
        j = list(prefix["ts"]).index(stamp.to_datetime64())
        for key in flags:
            assert flags[key][s["signal_idx"]] == other[key][j]


def test_legacy_signal_schema_migrates_without_losing_rows():
    conn = sqlite3.connect(":memory:")
    conn.execute("CREATE TABLE gold_cci_v2_signals (trade_date TEXT,signal_ts TEXT PRIMARY KEY,outcome TEXT,cci REAL,rsi REAL,plus_di REAL,minus_di REAL)")
    conn.execute("INSERT INTO gold_cci_v2_signals VALUES ('2026-10-29','old','gate',1,2,3,4)")
    tracker._ensure_tables(conn)
    columns = {row[1] for row in conn.execute("PRAGMA table_info(gold_cci_v2_signals)")}
    assert {"bullish_di15", "bb_upcross15"} <= columns
    assert conn.execute("SELECT outcome FROM gold_cci_v2_signals WHERE signal_ts='old'").fetchone()[0] == "gate"
    conn.close()


@pytest.fixture
def book(monkeypatch, frame):
    conn = sqlite3.connect(":memory:")
    tracker._ensure_tables(conn)
    monkeypatch.setattr(tracker, "PAPER_START", "2026-06-01")
    rolls = [{"contract": "GOLD26OCTFUT", "expiry": "2026-10-05", "first": "2026-06-01", "last": "2026-09-25"}]
    monkeypatch.setattr(v1, "session_frame", lambda *a, **k: (frame.assign(ts=pd.to_datetime(frame.ts)), rolls))
    monkeypatch.setattr(tracker.margin_estimates, "cached_quotes", lambda: {})
    yield conn
    conn.close()


def quote(rate=.1, day="2026-06-03"):
    return dict(asof=day + "T09:00:00+05:30", contract="GOLD26OCTFUT", sides={"SELL": {"rate": rate}})


def test_margin_freezes_across_rebuild_and_v1_ledger_is_untouched(book):
    book.execute("INSERT INTO gold_cci_daily (trade_date,status,qty,strategy_version,updated_at) VALUES ('2000-01-01','final',100,'sentinel','x')")
    before = book.execute("SELECT * FROM gold_cci_daily").fetchall()
    now = datetime(2026, 6, 3, 23, 59)
    first = tracker.run_day("2026-06-03", now=now, connection=book, offline=True, margin_quote=quote())
    assert first["n_trades"] > 0
    saved = book.execute("SELECT margin_rate,margin_asof,capital_rs,daily_target_rs,net_rs FROM gold_cci_v2_daily").fetchone()
    trades = book.execute("SELECT * FROM gold_cci_v2_trades ORDER BY seq").fetchall()
    tracker.run_day("2026-06-03", now=now, connection=book, offline=True, margin_quote=quote(.2), rebuild=True)
    assert book.execute("SELECT margin_rate,margin_asof,capital_rs,daily_target_rs,net_rs FROM gold_cci_v2_daily").fetchone() == saved
    assert book.execute("SELECT * FROM gold_cci_v2_trades ORDER BY seq").fetchall() == trades
    assert book.execute("SELECT * FROM gold_cci_daily").fetchall() == before
    assert tracker.run_day("2026-06-03", now=now, connection=book, offline=True)["skipped"]
    # A stale live session can be recovered with its own stored margin the next day.
    book.execute("UPDATE gold_cci_v2_daily SET status='live'")
    tracker.run_day("2026-06-03", now=datetime(2026, 6, 4), connection=book, offline=True, rebuild=True)
    assert book.execute("SELECT capital_rs FROM gold_cci_v2_daily").fetchone()[0] == saved[2]


def test_missing_or_stale_margin_blocks_new_entries_and_displays_reason(book):
    now = datetime(2026, 6, 3, 12)
    for candidate in (None, {}, quote(day="2026-06-01"), quote(day="2026-06-04")):
        result = tracker.run_day("2026-06-03", now=now, connection=book, offline=True, margin_quote=candidate)
        assert result["status"] == "blocked_margin"
        assert book.execute("SELECT COUNT(*) FROM gold_cci_v2_trades").fetchone()[0] == 0
    card = _gold_v2(book, "2026-06-03")
    assert card["status"] == "Blocked" and "margin" in card["note"]


def test_new_past_session_cannot_use_todays_margin(book):
    result = tracker.run_day("2026-06-01", now=datetime(2026, 6, 2, 12), connection=book, offline=True, margin_quote=quote())
    assert result["status"] == "blocked_margin"


def test_forward_start_does_not_silently_backfill_old_history(book, monkeypatch):
    monkeypatch.setattr(tracker, "PAPER_START", "2026-10-29")
    assert tracker.run_day("2026-09-25", now=datetime(2026, 10, 29), connection=book, offline=True)["status"] == "before_start"


def test_uniform_report_uses_frozen_estimate_and_filters_dates(book):
    tracker.run_day("2026-06-03", now=datetime(2026, 6, 3, 23, 59), connection=book, offline=True, margin_quote=quote())
    report = build_reports(book, "gold_cci_v2", "2026-06-03", "2026-06-03")[0]
    assert report["available"] and report["stats"]["closed"] > 0
    t = report["trades"][0]
    assert t["capital_estimated"] and "frozen" in t["capital_basis"]
    assert t["capital"] == pytest.approx(t["entry_price"] * t["qty"] * .1, abs=.01)
    assert not build_reports(book, "gold_cci_v2", "2026-06-04", "2026-06-04")[0]["trades"]
    assert _gold_v2(book, "2026-06-03")["capital_estimated"]
    env = jinja2.Environment(loader=jinja2.FileSystemLoader("templates"), autoescape=True)
    html = env.get_template("_gold_cci_v2.html").render(gold_cci_v2_rows=tracker.tab_data(book))
    assert "3% net target" in html and "Daily target history" in html and "&amp;#8377;" not in html


def test_v2_is_registered_only_as_a_separate_paper_book():
    from labs.ui.routes import LIVE_TABS
    assert LIVE_TABS["gold_cci"] == "GOLD CCI short"
    assert "gold_cci_v2" in LIVE_TABS
    assert SOURCES["gold_cci_v2"].table == "gold_cci_v2_trades"
    source = Path("pa_paper_tracker_loop.py").read_text()
    assert source.count("run_gold_cci_v2_live(now)") == 1
    for file in ("live/engine/gold_cci_v2_engine.py", "labs/engine/gold_cci_v2_tracker.py"):
        assert "place_order" not in Path(file).read_text()
