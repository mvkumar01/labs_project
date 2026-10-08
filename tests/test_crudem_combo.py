"""CRUDEOILM consistent six-member combination: the port must reproduce Strategy Tester v2.

Fixtures (tests/fixtures): the engine's rolled, back-adjusted CRUDEOILM 1-minute series to 6 Oct 2026
and the combination's 136 reference trades from the same run (c8 of crudem_20261005).
"""
from __future__ import annotations

import py_compile
import shutil
import sqlite3
import subprocess
import sys
from datetime import date, datetime, timedelta
from pathlib import Path

import numpy as np
import pandas as pd
import pytest

from labs.engine import crudem_combo_engine as eng
from labs.engine import crudem_combo_tracker as tr

FIX = Path(__file__).resolve().parent / "fixtures"


@pytest.fixture(scope="module")
def frame() -> pd.DataFrame:
    return pd.read_parquet(FIX / "crudem_continuous_1min_to_20261006.parquet")


@pytest.fixture(scope="module")
def reference() -> pd.DataFrame:
    return pd.read_csv(FIX / "crudem_consistent_combo_reference_trades.csv",
                       parse_dates=["signal_time", "entry_time", "exit_time"]).sort_values("entry_time").reset_index(drop=True)


@pytest.fixture(scope="module")
def full(frame):
    return eng.replay(frame, "2026-06-01", end="2026-10-06")


def _same(ref: pd.DataFrame, trades: list[dict]) -> None:
    mine = pd.DataFrame(trades).sort_values("entry_ts").reset_index(drop=True)
    assert len(mine) == len(ref)
    assert (mine["cid"].to_numpy() == ref["cid"].to_numpy()).all()
    for a, b in (("signal_ts", "signal_time"), ("entry_ts", "entry_time"), ("exit_ts", "exit_time")):
        assert (pd.to_datetime(mine[a]).to_numpy() == ref[b].to_numpy()).all(), a
    assert np.allclose(mine["entry_price"], ref["entry_price"], atol=1e-6)
    assert np.allclose(mine["exit_price"], ref["exit_price"], atol=1e-2)       # reference prices went through float32
    assert np.allclose(mine["stop_dist"], ref["stop_dist"], atol=1e-3)
    assert (mine["reason"].to_numpy() == ref["reason"].to_numpy()).all()
    assert (np.where(mine["side"] > 0, "long", "short") == ref["direction"].to_numpy()).all()


# ══════════════════════════════════════════════════════════════════ parity ══
def test_the_port_reproduces_all_136_reference_trades(full, reference):
    _same(reference, full["trades"])
    gross = sum(t["points"] for t in full["trades"]) * eng.LOT_QTY
    ref_gross = (np.where(reference.direction == "long", 1, -1)
                 * (reference.exit_price - reference.entry_price)).sum() * eng.LOT_QTY
    assert gross == pytest.approx(ref_gross, abs=1.0)


def test_every_signal_is_accounted_for(full):
    outcomes = pd.Series([s["outcome"] for s in full["signals"]]).value_counts()
    assert outcomes["taken"] == 136 == len(full["trades"])
    assert set(outcomes.index) <= {"taken", "position_held", "member_in_trade", "cooldown", "gate", "ineligible",
                                   "no_stop_distance"}
    held = [s for s in full["signals"] if s["outcome"] == "position_held"]
    assert held and all(s["blocked_by"] in eng.MEMBER_BY_CID and s["blocked_by"] != s["cid"] for s in held)
    # one position at a time
    t = sorted(full["trades"], key=lambda x: x["entry_idx"])
    assert all(a["exit_idx"] <= b["signal_idx"] for a, b in zip(t, t[1:]))


def test_a_60_day_window_gives_the_same_sessions(frame, reference):
    """The tracker replays 60 calendar days of warm-up, not the whole history."""
    for day in ("2026-08-05", "2026-09-10", "2026-10-06"):
        d = date.fromisoformat(day)
        window = frame[(frame.ts.dt.date >= d - timedelta(days=tr.LOOKBACK_DAYS)) & (frame.ts.dt.date <= d)]
        out = eng.replay(window, "2026-06-01")
        _same(reference[reference["date"] == day].reset_index(drop=True),
              [t for t in out["trades"] if t["trade_date"] == day])


def test_a_live_replay_never_uses_a_minute_that_has_not_closed(frame, full):
    day = [t for t in full["trades"] if t["trade_date"] == "2026-10-06"]
    assert [(t["cid"], t["reason"]) for t in day] == [(2803, "target"), (593002, "eod")]
    for hh, mm in ((9, 30), (10, 10), (10, 11), (11, 30), (11, 31), (20, 15), (20, 16), (23, 29)):
        cutoff = datetime(2026, 10, 6, hh, mm)
        out = eng.replay(frame, "2026-06-01", cutoff=cutoff)
        live = [t for t in out["trades"] if t["trade_date"] == "2026-10-06"]
        for t in live:
            match = next(x for x in day if x["cid"] == t["cid"] and x["entry_ts"] == t["entry_ts"])
            assert t["entry_ts"] < pd.Timestamp(cutoff) and t["entry_price"] == match["entry_price"]
            if t["status"] == "closed":
                assert t["exit_ts"] < pd.Timestamp(cutoff) and t["exit_ts"] == match["exit_ts"]
            else:
                assert t["exit_ts"] is None and match["exit_ts"] >= pd.Timestamp(cutoff) - pd.Timedelta(minutes=1)
        # an entry needs its own bar complete: the 10:10 entry appears once 10:10 has closed
        assert len(live) == sum(x["entry_ts"] < pd.Timestamp(cutoff) for x in day)
    # at the session's last minute the eod exit is not booked until that bar has closed
    last = eng.replay(frame, "2026-06-01", cutoff=datetime(2026, 10, 6, 23, 29))
    assert [t["status"] for t in last["trades"] if t["trade_date"] == "2026-10-06"] == ["closed", "open"]


# ════════════════════════════════════════════════════════════ engine units ══
def test_exit_rules_inside_one_bar():
    def grid(bars):
        ts = pd.date_range("2026-10-06 09:00", periods=len(bars), freq="min")
        return eng.build_bars(pd.DataFrame(bars, columns=["open", "high", "low", "close"]).assign(ts=ts))
    # long from open[1] = 100, stop 1 -> 99, target 2R -> 102
    both = grid([(100, 100, 100, 100), (100, 103, 98, 101), (101, 101, 101, 101)])
    assert eng.simulate(both, 0, +1, 1.0, 2.0)["reason"] == "stop"                       # stop wins a both-touched bar
    gap_t = grid([(100, 100, 100, 100), (100, 100.5, 99.5, 100), (104, 105, 98, 104)])
    r = eng.simulate(gap_t, 0, +1, 1.0, 2.0)
    assert (r["reason"], r["exit"]) == ("target", 104.0)                                 # opened beyond the target
    gap_s = grid([(100, 100, 100, 100), (100, 100.5, 99.5, 100), (97, 99, 96, 98)])
    r = eng.simulate(gap_s, 0, +1, 1.0, 2.0)
    assert (r["reason"], r["exit"]) == ("stop", 97.0)                                    # opened beyond the stop
    short = grid([(100, 100, 100, 100), (100, 100.4, 98.9, 99), (99, 99, 99, 99)])
    r = eng.simulate(short, 0, -1, 1.0, 1.0)
    assert (r["reason"], r["exit"]) == ("target", 99.0)
    assert eng.simulate(short, 0, -1, float("nan"), 1.0) is None


def test_first_in_merge_rule():
    def t(cid, sig, exit_):
        return {"cid": cid, "signal_idx": sig, "entry_idx": sig + 1, "exit_idx": exit_}
    a, b = [t(1, 10, 50), t(1, 80, 90)], [t(2, 10, 20), t(2, 50, 60), t(2, 85, None)]
    taken, skipped = eng.first_in(a, b)
    # same minute: side a wins; b's 50 signal is free (a's exit bar is not "after" it); a still open at 85
    assert [(x["cid"], x["signal_idx"]) for x in taken] == [(1, 10), (2, 50), (1, 80)]
    assert [(x["cid"], x["signal_idx"], x["blocked_by"]) for x in skipped] == [(2, 10, 1), (2, 85, 1)]


def test_contracts_roll_one_session_before_expiry_and_back_adjust():
    def contract(days, base):
        ts = [pd.Timestamp(d) + pd.Timedelta(hours=9, minutes=m) for d in days for m in range(3)]
        return pd.DataFrame({"ts": ts, "open": base, "high": base + 1, "low": base - 1, "close": base, "volume": 1.0})
    days = [date(2026, 10, 14), date(2026, 10, 15), date(2026, 10, 16), date(2026, 10, 19), date(2026, 10, 20)]
    octf = contract(days[:4], 7000.0)                       # trades through its expiry, Mon 19 Oct
    novf = contract(days, 7040.0)
    series, rolls = eng.stitch([("NOV", date(2026, 11, 19), novf), ("OCT", date(2026, 10, 19), octf)],
                               today=date(2026, 10, 20))
    assert [(r["contract"], r["first"], r["last"]) for r in rolls] == [
        ("OCT", "2026-10-14", "2026-10-16"), ("NOV", "2026-10-19", "2026-10-20")]
    assert rolls[0]["roll_gap"] == 40.0 and rolls[0]["adjustment"] == 40.0 and rolls[1]["adjustment"] == 0.0
    assert set(series["close"]) == {7040.0}                 # no false jump at the roll
    # before the expiry day the front contract is simply the live one
    series, rolls = eng.stitch([("OCT", date(2026, 10, 19), octf[octf.ts.dt.date <= days[2]]),
                                ("NOV", date(2026, 11, 19), novf[novf.ts.dt.date <= days[2]])],
                               today=date(2026, 10, 16))
    assert [r["contract"] for r in rolls] == ["OCT"] and set(series["close"]) == {7000.0}


# ══════════════════════════════════════════════════════════════ paper book ══
class FakeKite:
    """Serves the fixture series as one listed contract."""

    def __init__(self, frame):
        self.frame, self.calls = frame, 0

    def instruments(self, exchange):
        return [{"name": "CRUDEOILM", "instrument_type": "FUT", "tradingsymbol": "CRUDEOILM26OCTFUT",
                 "instrument_token": 111, "expiry": date(2026, 10, 19)},
                {"name": "CRUDEOIL", "instrument_type": "FUT", "tradingsymbol": "CRUDEOIL26OCTFUT",
                 "instrument_token": 222, "expiry": date(2026, 10, 19)}]

    def historical_data(self, token, frm, to, interval):
        self.calls += 1
        f = self.frame[(self.frame.ts >= frm) & (self.frame.ts <= to)]
        return [{"date": r.ts.to_pydatetime(), "open": r.open, "high": r.high, "low": r.low, "close": r.close,
                 "volume": r.volume} for r in f.itertuples()]


@pytest.fixture
def book(tmp_path, monkeypatch, frame):
    conn = sqlite3.connect(tmp_path / "labs.db")
    monkeypatch.setattr(tr, "PAPER_START", "2026-10-05")       # the fixture ends on 6 Oct
    monkeypatch.setattr(tr.mcx._time, "sleep", lambda _s: None)
    yield conn, FakeKite(frame)
    conn.close()


def test_paper_book_stores_the_engines_trades_for_a_session(book, reference):
    conn, kite = book
    out = tr.run_day("2026-10-06", kite=kite, now=datetime(2026, 10, 7, 8, 0), connection=conn)
    assert out["status"] == "final" and out["n_trades"] == 2 and out["tradingsymbol"] == "CRUDEOILM26OCTFUT"
    rows = conn.execute("SELECT cid,direction,entry_ts,exit_ts,entry_price,exit_price,exit_reason,gross_rs,"
                        "charges_rs,slippage_rs,net_rs FROM crudem_combo_trades ORDER BY seq").fetchall()
    ref = reference[reference["date"] == "2026-10-06"].reset_index(drop=True)
    for row, r in zip(rows, ref.itertuples()):
        assert (row[0], row[1], row[6]) == (r.cid, r.direction, r.reason)
        assert pd.Timestamp(row[2]) == r.entry_time and pd.Timestamp(row[3]) == r.exit_time
        assert row[4] == pytest.approx(r.entry_price) and row[5] == pytest.approx(r.exit_price, abs=1e-2)
        assert row[9] == 20.0 and row[10] == pytest.approx(row[7] - row[8] - row[9], abs=0.01)
        assert row[10] == pytest.approx(r.pnl, abs=1.0)        # the back test's cost model, to the rupee
    # idempotent: a finished session is not replayed again, and a rebuild writes the same rows
    calls = kite.calls
    assert tr.run_day("2026-10-06", kite=kite, now=datetime(2026, 10, 7, 8, 0), connection=conn)["skipped"]
    assert kite.calls == calls
    tr.run_day("2026-10-06", kite=kite, now=datetime(2026, 10, 7, 8, 0), connection=conn, rebuild=True)
    assert conn.execute("SELECT COUNT(*) FROM crudem_combo_trades").fetchone()[0] == 2
    logged = dict(conn.execute("SELECT outcome, COUNT(*) FROM crudem_combo_signals GROUP BY outcome").fetchall())
    assert logged["taken"] == 2 and sum(logged.values()) >= 2


def test_paper_book_mid_session_is_live_and_marks_the_open_trade(book):
    conn, kite = book
    out = tr.run_day("2026-10-06", kite=kite, now=datetime(2026, 10, 6, 11, 0, 20), connection=conn)
    assert out["status"] == "live" and out["n_trades"] == 1 and out["open"] == 1 and out["net_rs"] == 0
    row = conn.execute("SELECT status, exit_ts, entry_price FROM crudem_combo_trades").fetchone()
    assert row[0] == "open" and row[1] is None
    daily = conn.execute("SELECT status, through_ts, open_trades FROM crudem_combo_daily").fetchone()
    assert daily == ("live", "2026-10-06 10:59:00", 1)
    # later the same session: the trade has closed; and nothing before the book's start is recorded
    out = tr.run_day("2026-10-06", kite=kite, now=datetime(2026, 10, 6, 12, 0, 5), connection=conn)
    assert out["open"] == 0 and conn.execute("SELECT status FROM crudem_combo_trades").fetchone()[0] == "closed"
    assert tr.run_day("2026-10-02", kite=kite, now=datetime(2026, 10, 6, 12, 0), connection=conn)["status"] == "before_start"
    assert tr.run_day("2026-10-10", kite=kite, now=datetime(2026, 10, 12, 9, 0), connection=conn)["status"] == "weekend"


def test_dashboard_stats(book):
    conn, kite = book
    for day in ("2026-10-05", "2026-10-06"):
        tr.run_day(day, kite=kite, now=datetime(2026, 10, 7, 8, 0), connection=conn)
    rows, trades, stats = tr.tab_data(conn)
    assert [r["trade_date"] for r in rows] == ["2026-10-06", "2026-10-05"]
    assert stats["trades"] == len(trades) == sum(r["n_trades"] for r in rows)
    assert stats["net_total"] == pytest.approx(sum(r["net_rs"] for r in rows), abs=0.05)
    assert stats["outcomes"].get("taken") == stats["trades"]
    assert {b["cid"] for b in stats["by_member"]} <= set(eng.MEMBER_BY_CID)
    assert tr.tab_data(conn, " AND trade_date >= ?", ("2026-10-06",))[2]["days"] == 1


# ═══════════════════════════════════════════════════════════════ UI wiring ══
def test_ui_patch_applies_and_is_idempotent(tmp_path):
    root = Path(__file__).resolve().parents[1]
    for rel in ("labs/ui/routes.py", "templates/live_strategy.html", "pa_paper_tracker_loop.py"):
        target = tmp_path / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy(root / rel, target)
    for name in ("patch_proposer_px_ui.py", "patch_crudem_combo_ui.py"):       # the second anchors on the first
        run = subprocess.run([sys.executable, str(root / "scripts" / name), str(tmp_path)], capture_output=True, text=True)
        assert run.returncode == 0, run.stderr
    again = subprocess.run([sys.executable, str(root / "scripts" / "patch_crudem_combo_ui.py"), str(tmp_path)],
                           capture_output=True, text=True)
    assert again.returncode == 0 and again.stdout.count("0 edits applied") == 3
    routes = (tmp_path / "labs/ui/routes.py").read_text(encoding="utf-8")
    assert routes.count('"crudem_combo": "CRUDEOILM Combo (6 rules)"') == 1
    assert routes.count('@labs_bp.route("/api/crudem_combo/backfill"') == 1
    loop = (tmp_path / "pa_paper_tracker_loop.py").read_text(encoding="utf-8")
    assert loop.count("run_crudem_combo_live(now)") == 1 and loop.count('"crudem_combo": None') == 1
    py_compile.compile(str(tmp_path / "labs/ui/routes.py"), doraise=True)
    py_compile.compile(str(tmp_path / "pa_paper_tracker_loop.py"), doraise=True)
    import jinja2
    jinja2.Environment().parse((tmp_path / "templates/live_strategy.html").read_text(encoding="utf-8"))
