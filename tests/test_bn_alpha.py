"""BANKNIFTY Alpha paper book: the rule, the alpha, the option leg and the wiring. Parity with the research
(reference_book.py) is checked outside the suite on the research's own inputs (alphaIMB
research/experiments/2026-10-22_banknifty_range/labs_book_parity.py: 85 sessions, 95/95 trades, +3,869.7 pts)."""
from __future__ import annotations

import py_compile
import shutil
import subprocess
import sys
from datetime import datetime, timedelta
from pathlib import Path

import numpy as np
import pandas as pd
import pytest

from labs.engine import bn_alpha_tracker as bn

ROOT = Path(__file__).resolve().parents[1]
DAY = "2026-10-26"


def test_range_is_prev_close_rounded_to_100_plus_minus_400():
    assert bn.range_for(55642.25) == (55200, 56000)
    assert bn.range_for(55651.0) == (55300, 56100)
    assert bn.range_for(55650.0) == (55200, 56000)       # Python rounds half to even, as the research does
    assert (bn.HALF_WIDTH, bn.ENTRY_TH, bn.ALPHA_TGT, bn.ALPHA_SL, bn.TRAIL_ARM, bn.TRAIL_DIST) == (400, 30, 100, -30, 100, 50)


def _inp(alpha, spot, wh=None, wl=None, done=True):
    marks = [datetime(2026, 10, 26, 9, 15) + timedelta(minutes=5 * i) for i in range(len(alpha))]
    spot = np.array(spot, float)
    return {"alpha": np.array(alpha, float), "spot": spot, "marks": marks, "session_done": done,
            "win_high": np.array(wh if wh is not None else spot, float),
            "win_low": np.array(wl if wl is not None else spot, float)}


def test_call_on_cross_above_30_and_alpha_stop_at_minus_30():
    trades, open_ = bn.simulate(_inp([0, 31, 50, -31, 0], [100, 110, 120, 105, 100]))
    assert open_ is None and len(trades) == 1
    t = trades[0]
    assert (t["side"], t["exit_reason"], t["spot_pts"]) == ("CALL", "ALPHA_SL", -5.0)


def test_put_on_cross_below_minus_30_and_alpha_target():
    trades, _ = bn.simulate(_inp([0, -31, -60, -101, -101], [100, 90, 80, 70, 70]))
    assert [(t["side"], t["exit_reason"], t["spot_pts"]) for t in trades] == [("PUT", "ALPHA_TGT", 20.0)]


def test_trail_arms_at_100_and_stops_50_below_the_best_known_before_the_window():
    # entry 1000; best reaches 1120 in window 2 (armed from window 3); window 3 low 1060 <= 1070
    spot = [1000, 1000, 1110, 1100, 1100]
    wh = [1000, 1000, 1120, 1100, 1100]
    wl = [1000, 1000, 1100, 1060, 1100]
    trades, _ = bn.simulate(_inp([0, 40, 60, 60, 60], spot, wh, wl))
    t = trades[0]
    assert t["exit_reason"] == "TRAIL" and t["exit_spot"] == min(1120 - 50, 1110)  # min(stop, previous mark spot)
    assert t["exit_ts"].strftime("%H:%M") == "09:30"


def test_no_entry_on_the_exit_mark_but_a_later_fresh_cross_re_enters():
    trades, _ = bn.simulate(_inp([0, 31, -40, 0, 31, 31], [100, 100, 90, 95, 100, 120]))
    assert [(t["side"], t["entry_ts"].strftime("%H:%M")) for t in trades] == [("CALL", "09:20"), ("CALL", "09:35")]
    assert trades[-1]["exit_reason"] == "EOD"


def test_marks_beyond_200_are_skipped_for_entries_and_alpha_exits():
    # a skipped mark still serves as the previous alpha of the next one (as in the reference)
    assert bn.simulate(_inp([0, 250, 31, 40], [100] * 4))[0] == []
    trades, _ = bn.simulate(_inp([0, 250, 20, 31, 250, 40], [100] * 6))
    assert [t["entry_ts"].strftime("%H:%M") for t in trades] == ["09:30"] and trades[0]["exit_reason"] == "EOD"


def test_an_unfinished_session_keeps_the_trade_open():
    trades, open_ = bn.simulate(_inp([0, 31, 40], [100, 100, 130], done=False))
    assert trades == [] and open_["status"] == "open" and open_["spot_pts"] == 30.0


def _frame(rows):
    return pd.DataFrame(rows, columns=["timestamp", "strike", "option_type", "expiry", "oi", "bid", "ask", "tradingsymbol"])


def test_alpha_counts_missing_baseline_as_zero_and_missing_rows_as_no_change():
    t0, t1 = pd.Timestamp(f"{DAY} 09:15"), pd.Timestamp(f"{DAY} 09:20")
    rows = [(t0, 55600, "CE", "26OCT", 100, 1, 2, "x"), (t0, 55600, "PE", "26OCT", 100, 1, 2, "x"),
            (t1, 55600, "PE", "26OCT", 160, 1, 2, "x"),                      # CE has no row at 09:20
            (t1, 55700, "CE", "26OCT", 20, 1, 2, "x"),                       # not in the baseline: counts from 0
            (t1, 57000, "PE", "26OCT", 999, 1, 2, "x"),                      # outside the range
            (t1, 55600, "PE", "26NOV", 999, 1, 2, "x")]                      # a later expiry
    base = pd.DataFrame({"strike": [55600, 55600], "type": ["ce", "pe"], "oi": [100, 100]})
    bars = {datetime(2026, 10, 26, 9, m): (55600, 55610 + m, 55590 - m, 55600 + m) for m in range(15, 21)}
    inp = bn.session_inputs(DAY, close=55600.0, frame=_frame(rows), bars=bars, base=base)
    assert inp["expiry"] == "26OCT" and (inp["range_lo"], inp["range_hi"]) == (55200, 56000)
    assert inp["alpha"][0] == 0.0                                               # no change from the baseline
    assert inp["alpha"][1] == pytest.approx((60 - 20) * 100 / (60 + 20))        # dPE 60, dCE 20 (55700 from 0)
    assert list(inp["spot"]) == [55615, 55620]
    assert inp["win_high"][1] == 55610 + 20 and inp["win_low"][1] == 55590 - 20  # minutes 09:16..09:20


def test_a_mark_counts_only_once_its_minute_has_closed():
    t0, t1 = pd.Timestamp(f"{DAY} 09:15"), pd.Timestamp(f"{DAY} 09:20")
    rows = [(t, 55600, "PE", "26OCT", 100, 1, 2, "x") for t in (t0, t1)]
    base = pd.DataFrame({"strike": [55600], "type": ["pe"], "oi": [100]})
    bars = {datetime(2026, 10, 26, 9, m): (1, 1, 1, 1) for m in range(15, 21)}
    inp = bn.session_inputs(DAY, close=55600.0, frame=_frame(rows), bars=bars, base=base,
                            now=datetime(2026, 10, 26, 9, 20, 40))
    assert [m.strftime("%H:%M") for m in inp["marks"]] == ["09:15"]


def test_option_leg_buys_the_atm_strike_at_the_ask_and_sells_at_the_bid():
    m0, m1 = datetime(2026, 10, 26, 9, 20), datetime(2026, 10, 26, 10, 0)
    t = dict(side="PUT", entry_ts=m0, exit_ts=m1, entry_spot=55648.0, exit_spot=55500.0, spot_pts=148.0)
    inp = {"quotes": {(m0, 55600.0, "pe"): (300.0, 302.0, "BANKNIFTY26NOV55600PE"),
                      (m1, 55600.0, "pe"): (390.0, 392.0, "BANKNIFTY26NOV55600PE")}}
    row = bn.price_trade(t, inp)
    assert (row["strike"], row["entry_ask"], row["exit_bid"], row["option_pts"]) == (55600, 302.0, 390.0, 88.0)
    assert row["gross_rs"] == 88.0 * bn.LOT_SIZE and 0 < row["charges_rs"] < 200 and row["quote_status"] == "priced"
    missing = bn.price_trade(dict(t, exit_ts=m1 + timedelta(minutes=5)), inp)
    assert missing["quote_status"] == "no_exit_bid" and missing["net_rs"] is None


def test_registered_as_tab_loop_runner_and_overview_card():
    from labs.services.book_overview_more import MORE_BOOKS
    from labs.ui.routes import LIVE_TABS
    assert LIVE_TABS["bn_alpha"] == "BANKNIFTY Alpha" and MORE_BOOKS["bn_alpha"]["label"] == "BANKNIFTY Alpha"
    assert '("bn_alpha", run_bn_alpha_day)' in (ROOT / "pa_paper_tracker_loop.py").read_text(encoding="utf-8")
    assert "{% include '_bn_alpha.html' %}" in (ROOT / "templates" / "live_strategy.html").read_text(encoding="utf-8")


def test_ui_patch_is_idempotent_and_compiles(tmp_path):
    files = ("labs/ui/routes.py", "templates/live_strategy.html", "pa_paper_tracker_loop.py",
             "labs/services/book_overview_more.py", "templates/_bn_alpha.html")
    for rel in files:
        target = tmp_path / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy(ROOT / rel, target)
    run = subprocess.run([sys.executable, str(ROOT / "scripts" / "patch_bn_alpha_ui.py"), str(tmp_path)],
                         capture_output=True, text=True)
    assert run.returncode == 0 and run.stdout.count("0 edits applied") == 4, run.stdout + run.stderr
    for rel in ("labs/ui/routes.py", "pa_paper_tracker_loop.py", "labs/services/book_overview_more.py"):
        py_compile.compile(str(tmp_path / rel), doraise=True)
    import jinja2
    for rel in ("templates/live_strategy.html", "templates/_bn_alpha.html"):
        jinja2.Environment().parse((tmp_path / rel).read_text(encoding="utf-8"))


def test_ledger_round_trip_and_tab_stats():
    import sqlite3
    m = [datetime(2026, 10, 26, 9, 15) + timedelta(minutes=5 * i) for i in range(5)]
    inp = _inp([0, 31, 50, -31, 0], [55600, 55610, 55620, 55605, 55600])
    inp.update(expiry="26OCT", range_lo=55200, range_hi=56000,
               quotes={(m[1], 55600.0, "ce"): (300.0, 302.0, "BANKNIFTY26OCT55600CE"),
                       (m[3], 55600.0, "ce"): (296.0, 298.0, "BANKNIFTY26OCT55600CE")})
    closed, _ = bn.simulate(inp)
    r = {"inp": inp, "trades": [dict(bn.price_trade(t, inp), status="closed") for t in closed],
         "prev_close": 55612.3, "prev_close_date": "2026-10-23", "prev_close_source": "kite_daily",
         "baseline_date": "2026-10-23"}
    conn = sqlite3.connect(":memory:")
    bn._ensure_tables(conn)
    bn._save(conn, DAY, r)
    rows, trades, stats = bn.tab_data(conn)
    assert rows[0]["status"] == "traded" and rows[0]["range_lo"] == 55200 and rows[0]["n_trades"] == 1
    assert trades[0]["spot_pnl_pts"] == -5.0 and trades[0]["exit_reason"] == "ALPHA_SL" and trades[0]["option_pts"] == -6.0
    assert stats["trades"] == 1 and stats["spot_pts"] == -5.0 and stats["unseen_trades"] == 1
    assert stats["net_rs"] == round(trades[0]["net_rs"]) and stats["exit_reasons"]["ALPHA_SL"] == 1
    bn._save(conn, "2026-10-27", {}, error="No settled BANKNIFTY baseline")
    assert conn.execute("SELECT status FROM bn_alpha_daily WHERE trade_date='2026-10-27'").fetchone()[0] == "unavailable"
