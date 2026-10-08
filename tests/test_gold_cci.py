"""MCX GOLD CCI short: the engine port against Strategy Tester v2's own trades, the stop-to-entry
exit, and the paper book (seeded October contract, roll to December, ledger)."""
from __future__ import annotations

import py_compile
import shutil
import sqlite3
import subprocess
import sys
from datetime import date, datetime
from pathlib import Path

import numpy as np
import pandas as pd
import pytest

from labs.engine import gold_cci_tracker as gt
from labs.engine.charges import mcx_futures_round_trip_charges
from live.engine import crudem_combo_engine as base
from live.engine import gold_cci_engine as eng

ROOT = Path(__file__).resolve().parents[1]
FIX = ROOT / "tests" / "fixtures"
NOW = datetime(2026, 10, 9, 1, 0)


@pytest.fixture(scope="module")
def frame():
    f = pd.read_parquet(gt.SEED_FILE)
    return f.astype({c: "float64" for c in ("open", "high", "low", "close", "volume")})


@pytest.fixture(scope="module")
def reference():
    return pd.read_csv(FIX / "gold_cci_short_reference_trades.csv")


def test_engine_takes_the_back_tests_135_trades(frame, reference):
    """The Tester's rule over its own range: same signal, entry and exit minute, prices, exit kind
    and - with the book's cost model - the run's net of Rs 24,26,934."""
    out = eng.replay(frame, "2026-04-13", end="2026-09-25")
    t = pd.DataFrame(out["trades"])
    assert len(t) == len(reference) == 135
    for mine, ref in (("signal_ts", "signal_ts"), ("entry_ts", "entry_ts"), ("exit_ts", "exit_ts")):
        assert (t[mine].astype(str).str[:19].to_numpy() == reference[ref].str[:19].to_numpy()).all()
    assert np.allclose(t.entry_price, reference.entry_price) and np.allclose(t.exit_price, reference.exit_price)
    assert np.allclose(t.stop_dist, reference.stop_dist) and (t.reason.to_numpy() == reference.reason.to_numpy()).all()
    assert (t.stop_moved.to_numpy() == reference.stop_moved.to_numpy()).all()
    charges = [mcx_futures_round_trip_charges(b, s, eng.LOT_QTY)["raw_total"] for b, s in zip(t.exit_price, t.entry_price)]
    net = (t.points * eng.LOT_QTY - np.array(charges) - 2 * eng.TICK_SIZE * eng.LOT_QTY).sum()
    assert net == pytest.approx(2426934, abs=1.0)
    outcomes = pd.Series([s["outcome"] for s in out["signals"]]).value_counts().to_dict()
    assert outcomes["taken"] == 135 and outcomes["gate"] > 0 and outcomes["in_trade"] > 0
    assert all(s["cci"] > 100 for s in out["signals"])                      # every event is a CCI turn-on
    taken = [s for s in out["signals"] if s["outcome"] == "taken"]
    assert all(s["rsi"] > 50 and s["minus_di"] > s["plus_di"] for s in taken)


def test_cci_is_the_typical_price_against_its_mean_deviation():
    h = np.array([10.0, 11, 12, 13, 14, 20]); l = h - 2; c = h - 1
    tp = (h + l + c) / 3
    want = (tp[5] - tp[1:6].mean()) / (0.015 * np.abs(tp[1:6] - tp[1:6].mean()).mean())
    got = eng.cci(h, l, c, 5)
    assert np.isnan(got[:4]).all() and got[5] == pytest.approx(want)


def _grid(bars):
    """A one-session grid from (open, high, low, close) rows; row 0 is the signal bar."""
    a = np.array(bars, dtype=float)
    n = len(a)
    return {"open": a[:, 0], "high": a[:, 1], "low": a[:, 2], "close": a[:, 3], "valid": np.ones(n, bool),
            "day_id": np.zeros(n, np.int64), "n": n, "n_known": n, "live_day": None}


def test_stop_moves_to_entry_only_from_the_next_bar_and_then_holds():
    # short at 1000, stop distance 10: stop 1010, target 970
    g = _grid([(1000, 1000, 1000, 1000),      # signal
               (1000, 1001, 989, 992),        # entry bar: trades 11 in favour -> stop to 1000 from the NEXT bar
               (992, 1003, 990, 1001),        # back through the entry: out at 1000
               (1001, 1002, 1000, 1001)])
    r = eng.simulate(g, 0, 10.0)
    assert (r["exit_idx"], r["exit"], r["reason"], r["moved"], r["stop"]) == (2, 1000.0, "stop at entry", True, 1010.0)
    # the same favourable bar also spikes above the entry: the original stop still rules that bar
    g = _grid([(1000,) * 4, (1000, 1009, 989, 992), (992, 993, 969, 975), (975,) * 4])
    r = eng.simulate(g, 0, 10.0)
    assert (r["exit_idx"], r["exit"], r["reason"]) == (2, 970.0, "target")
    # not yet one stop in favour: the stop stays, and wins when stop and target are both touched
    g = _grid([(1000,) * 4, (1000, 1002, 991, 995), (995, 1011, 965, 990)])
    r = eng.simulate(g, 0, 10.0)
    assert (r["exit_idx"], r["exit"], r["reason"], r["moved"]) == (2, 1010.0, "stop", False)
    # a gap above the moved stop fills at the open; nothing hit: flat at the last close
    g = _grid([(1000,) * 4, (1000, 1001, 988, 990), (1004, 1006, 1003, 1005)])
    assert eng.simulate(g, 0, 10.0)["exit"] == 1004.0
    g = _grid([(1000,) * 4, (1000, 1004, 996, 998), (998, 1003, 995, 997)])
    r = eng.simulate(g, 0, 10.0)
    assert (r["reason"], r["exit"], r["held"]) == ("eod", 997.0, 2)
    assert eng.levels(150000.0) == (375.0, 150375.0, 148875.0)


# ═══════════════════════════════════════════════════════════════ paper book ══
@pytest.fixture
def book(tmp_path):
    conn = sqlite3.connect(tmp_path / "labs.db")
    yield conn
    conn.close()


def test_book_replays_sessions_from_the_seed_and_matches_the_reference(book, reference):
    assert gt.run_day("2026-05-29", now=NOW, connection=book, offline=True)["status"] == "before_start"
    days = ["2026-06-01", "2026-06-02", "2026-06-03", "2026-06-04", "2026-06-05"]
    for d in days:
        out = gt.run_day(d, now=NOW, connection=book, offline=True)
        assert out["status"] == "final" and out["tradingsymbol"] == "GOLD26OCTFUT"
    assert gt.seed_cache(book) == 0                                         # loaded once
    got = pd.read_sql_query("SELECT * FROM gold_cci_trades ORDER BY entry_ts", book)
    ref = reference[(reference.entry_ts >= days[0]) & (reference.entry_ts < "2026-06-06")].reset_index(drop=True)
    assert len(got) == len(ref) > 0
    assert (got.entry_ts.str[:19].to_numpy() == ref.entry_ts.str[:19].to_numpy()).all()
    assert np.allclose(got.net_rs, ref.net_rs, atol=0.01) and (got.exit_reason.to_numpy() == ref.reason.to_numpy()).all()
    assert gt.run_day(days[0], now=NOW, connection=book, offline=True).get("skipped") is True      # frozen
    rows, trades, stats = gt.tab_data(book)
    assert stats["trades"] == len(ref) and stats["net_total"] == pytest.approx(ref.net_rs.sum(), abs=0.05)
    assert stats["seen"]["trades"] == len(ref) and stats["unseen"]["trades"] == 0 and stats["unseen"]["days"] == 0
    assert stats["first_unseen"] == "2026-09-28" and stats["qty"] == 100 and stats["outcomes"]["taken"] == len(ref)
    n_sig = book.execute("SELECT COUNT(*) FROM gold_cci_signals").fetchone()[0]
    assert n_sig == sum(r["n_signals"] for r in rows) > len(ref)


def test_book_rolls_to_december_on_28_september_without_touching_the_session_price(book):
    gt._ensure_tables(book)
    gt.seed_cache(book)
    book.execute("INSERT INTO gold_cci_contracts VALUES ('GOLD26DECFUT', '2026-12-04', 126774535)")
    oct_bars = gt.mcx.load_minute_bars("GOLD26OCTFUT", date(2026, 9, 25), date(2026, 9, 25), book)
    dec = []
    for day, shift in (("2026-09-25", 1400.0), ("2026-09-28", 1500.0)):      # December trades 1,400 above October
        for _, b in oct_bars.iterrows():
            ts = b.ts.replace(day=int(day[-2:]))
            dec.append(("GOLD26DECFUT", ts.strftime("%Y-%m-%d %H:%M:%S"), b.open + shift, b.high + shift, b.low + shift,
                        b.close + shift, 1.0))
    book.executemany("INSERT INTO crude_minute_bars VALUES (?,?,?,?,?,?,?)", dec)
    book.commit()
    frame, rolls = gt.session_frame(book, date(2026, 9, 28), NOW)
    assert [(r["contract"], r["last"]) for r in rolls] == [("GOLD26OCTFUT", "2026-09-25"), ("GOLD26DECFUT", "2026-09-28")]
    assert rolls[0]["roll_gap"] == 1400.0 and rolls[0]["adjustment"] == 1400.0 and rolls[1]["adjustment"] == 0.0
    last_oct = frame[frame.ts.dt.date == date(2026, 9, 25)].iloc[-1]
    assert last_oct.close == oct_bars.close.iloc[-1] + 1400.0                # earlier prices carry the gap
    first_dec = frame[frame.ts.dt.date == date(2026, 9, 28)].iloc[0]
    assert first_dec.open == oct_bars.open.iloc[0] + 1500.0                  # the session's own contract is untouched
    # a session before the roll sees the October contract alone, unadjusted
    frame, rolls = gt.session_frame(book, date(2026, 9, 24), NOW)
    assert [r["contract"] for r in rolls] == ["GOLD26OCTFUT"] and rolls[0]["adjustment"] == 0.0


def test_ui_patch_applies_next_to_the_crude_wiring_and_is_idempotent(tmp_path):
    for rel in ("labs/ui/routes.py", "templates/live_strategy.html", "pa_paper_tracker_loop.py", "templates/_gold_cci.html"):
        target = tmp_path / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy(ROOT / rel, target)
    for name in ("patch_proposer_px_ui.py", "patch_crudem_combo_ui.py", "patch_gold_cci_ui.py"):
        run = subprocess.run([sys.executable, str(ROOT / "scripts" / name), str(tmp_path)], capture_output=True, text=True)
        assert run.returncode == 0, run.stderr
    second = subprocess.run([sys.executable, str(ROOT / "scripts" / "patch_gold_cci_ui.py"), str(tmp_path)],
                            capture_output=True, text=True)
    assert second.returncode == 0 and second.stdout.count("0 edits applied") == 3
    routes = (tmp_path / "labs/ui/routes.py").read_text(encoding="utf-8")
    assert routes.count('"gold_cci": "GOLD CCI short"') == 1 and routes.count('@labs_bp.route("/api/gold_cci/backfill"') == 1
    loop = (tmp_path / "pa_paper_tracker_loop.py").read_text(encoding="utf-8")
    assert loop.count("run_gold_cci_live(now)") == 1 and loop.count("run_crudem_combo_live(now)") == 1
    py_compile.compile(str(tmp_path / "labs/ui/routes.py"), doraise=True)
    py_compile.compile(str(tmp_path / "pa_paper_tracker_loop.py"), doraise=True)
    import jinja2
    for rel in ("templates/live_strategy.html", "templates/_gold_cci.html"):
        jinja2.Environment().parse((tmp_path / rel).read_text(encoding="utf-8"))


def test_the_crude_engine_this_one_builds_on_is_unchanged():
    assert (base.COOLDOWN_BARS, base.MAX_HOLD_BARS, base.BARS_PER_DAY) == (30, 870, 870)
    assert (eng.STOP_PCT, eng.TARGET_R, eng.ACTIVATE_R, eng.SIDE, eng.LOT_QTY) == (0.25, 3.0, 1.0, -1, 100)
