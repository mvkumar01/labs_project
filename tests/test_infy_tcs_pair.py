"""Infosys / TCS divergence pair paper book: the rule against the research reference, what it does
on a day that is still in progress, and the ledger."""
from __future__ import annotations

import py_compile
import shutil
import sqlite3
import subprocess
import sys
from datetime import datetime
from pathlib import Path

import numpy as np
import pandas as pd
import pytest

from labs.engine import infy_tcs_pair_tracker as pt

ROOT = Path(__file__).resolve().parents[1]
FIX = ROOT / "tests" / "fixtures" / "infy_tcs_daily_20251001_20261009.csv"


@pytest.fixture(scope="module")
def daily():
    return pd.read_csv(FIX)


def _frame(daily, through: str, open_only: str | None = None) -> pd.DataFrame:
    d = daily[daily.date <= (open_only or through)]
    f = pd.DataFrame({"a_open": d.open_infy.values, "a_close": d.close_infy.values, "b_open": d.open_tcs.values,
                      "b_close": d.close_tcs.values}, index=d.date.values)
    if open_only:
        f.loc[open_only, ["a_close", "b_close"]] = np.nan
    return f


def test_rule_reproduces_the_reference_trades(daily):
    """alphaIMB research (walkforward_infy_tcs.py, 60-day window): the four trades of 2026, and
    exactly one entered on or after 1 June - signal 3 Jun z +2.03, 4 Jun -> 22 Jun, +5.89% gross."""
    out = pt.replay(_frame(daily, "2026-10-09"))
    got = [(t["signal_date"], round(t["z"], 2), t["long"], t["entry_date"], t["exit_date"], t["days"],
            round(100 * t["gross"], 2), t["reason"]) for t in out["trades"]]
    assert got == [
        ("2026-01-16", 4.18, "TCS", "2026-01-19", "2026-02-11", 17, 4.38, "back to average"),
        ("2026-03-25", 2.02, "TCS", "2026-03-27", "2026-04-10", 8, 4.18, "back to average"),
        ("2026-04-24", -2.57, "INFY", "2026-04-27", "2026-05-20", 16, 5.86, "back to average"),
        ("2026-06-03", 2.03, "TCS", "2026-06-04", "2026-06-22", 12, 5.89, "back to average"),
    ]
    june = out["trades"][-1]
    assert june["net"] == pytest.approx(june["gross"] - 0.0020) and round(100 * june["net"], 2) == 5.69
    assert out["pending"] is None and all(t["status"] == "closed" for t in out["trades"])
    assert pt.zscore(_frame(daily, "2026-10-09")).iloc[-1] == pytest.approx(-0.98, abs=0.01)


def test_a_day_in_progress_fills_orders_at_its_open_but_gives_no_signal(daily):
    # the signal day has closed: an order is due at the next open, nothing is open yet
    out = pt.replay(_frame(daily, "2026-06-03"))
    assert out["pending"]["action"] == "enter" and (out["pending"]["long"], out["pending"]["short"]) == ("TCS", "INFY")
    assert [t["entry_date"] for t in out["trades"]][-1] == "2026-04-27"
    # next morning, open known, close not: the trade is on from that open and marked there
    out = pt.replay(_frame(daily, "2026-06-03", open_only="2026-06-04"))
    t = out["trades"][-1]
    assert (t["status"], t["entry_date"], t["days"]) == ("open", "2026-06-04", 0) and out["pending"] is None
    assert t["gross"] == 0.0 and t["net"] == pytest.approx(-0.0020)
    # the exit signal is the close of 19 Jun: out at the open of the 22nd, not before
    out = pt.replay(_frame(daily, "2026-06-18"))
    assert out["trades"][-1]["status"] == "open" and out["pending"] is None
    out = pt.replay(_frame(daily, "2026-06-19"))
    assert out["trades"][-1]["status"] == "open" and out["pending"]["action"] == "exit"
    assert out["pending"]["reason"] == "back to average"
    out = pt.replay(_frame(daily, "2026-06-19", open_only="2026-06-22"))
    t = out["trades"][-1]
    assert (t["status"], t["exit_date"], t["days"], round(100 * t["gross"], 2)) == ("closed", "2026-06-22", 12, 5.89)


def test_twenty_day_limit_and_one_position_at_a_time():
    """A gap that never closes: out after 20 trading days, and the exit day's close can re-enter."""
    n = 130
    a = np.full(n, 1000.0)
    a[80:] = 1100.0                                    # INFY jumps 10% and stays there
    idx = pd.bdate_range("2026-01-01", periods=n).strftime("%Y-%m-%d")
    f = pd.DataFrame({"a_open": a, "a_close": a, "b_open": 2000.0, "b_close": 2000.0}, index=idx)
    f.loc[idx[:60], "a_close"] += np.tile([0.5, -0.5], 30)       # some variance for the first window
    out = pt.replay(f)
    first = out["trades"][0]
    assert first["signal_date"] == idx[80] and first["entry_date"] == idx[81] and first["long"] == "TCS"
    assert (first["reason"], first["days"], first["exit_date"]) == ("20-day limit", 20, idx[101])
    assert all(a_["exit_date"] <= b_["signal_date"] for a_, b_ in zip(out["trades"], out["trades"][1:])
               if a_["exit_date"] and b_["signal_date"])


# ═══════════════════════════════════════════════════════════════ paper book ══
class FakeKite:
    def __init__(self, daily, now: datetime):
        self.daily, self.now, self.calls = daily, now, 0

    def ltp(self, keys):
        return {"NSE:INFY": {"instrument_token": 1, "last_price": 0}, "NSE:TCS": {"instrument_token": 2, "last_price": 0}}

    def historical_data(self, token, frm, to, interval):
        assert interval == "day"
        self.calls += 1
        leg = "infy" if token == 1 else "tcs"
        d = self.daily[(self.daily.date >= frm.date().isoformat()) & (self.daily.date <= self.now.date().isoformat())]
        return [{"date": datetime.fromisoformat(r.date), "open": getattr(r, f"open_{leg}"), "high": 0, "low": 0,
                 "close": getattr(r, f"close_{leg}"), "volume": 0} for r in d.itertuples()]


@pytest.fixture
def book(tmp_path):
    conn = sqlite3.connect(tmp_path / "labs.db")
    yield conn
    conn.close()


def test_book_backfills_from_june_and_fetches_only_when_due(book, daily):
    now = datetime(2026, 10, 9, 16, 0)
    kite = FakeKite(daily, now)
    out = pt.run_day("2026-10-09", kite=kite, now=now, connection=book)
    assert (out["trades"], out["closed"], out["open"], out["through"], out["final"]) == (1, 1, 0, "2026-10-09", True)
    assert out["net_rs"] == pytest.approx(56900, abs=60) and out["pending"] is None and kite.calls == 2
    pt.run_day(kite=kite, now=now, connection=book)                     # same afternoon, flat: no second fetch
    assert kite.calls == 2
    rows, trades, stats = pt.tab_data(book)
    assert len(trades) == 1 and trades[0]["entry_date"] == "2026-06-04" and trades[0]["exit_date"] == "2026-06-22"
    assert (trades[0]["long_leg"], trades[0]["short_leg"], trades[0]["days"]) == ("TCS", "INFY", 12)
    assert trades[0]["gross_pct"] == pytest.approx(5.89, abs=0.005) and trades[0]["costs_rs"] == 2000.0
    assert rows[0]["trade_date"] == "2026-10-09" and rows[-1]["trade_date"] == "2026-06-01"
    assert stats["trades"] == 1 and stats["wins"] == 1 and stats["unseen_trades"] == 0 and stats["latest"]["position"] == "flat"
    assert stats["latest_z"]["z"] == pytest.approx(-0.98, abs=0.01) and stats["days_in_trade"] == 12
    by_day = {r["trade_date"]: r for r in rows}
    assert by_day["2026-06-03"]["event"] == "signal z +2.03" and by_day["2026-06-03"]["position"] == "flat"
    assert by_day["2026-06-04"]["event"] == "enter (long TCS)" and by_day["2026-06-04"]["position"] == "long TCS / short INFY"
    assert by_day["2026-06-22"]["event"] == "exit (back to average)" and by_day["2026-06-22"]["position"] == "flat"


def test_book_on_the_morning_after_a_signal(book, daily):
    now = datetime(2026, 6, 4, 10, 0)
    kite = FakeKite(daily, now)
    out = pt.run_day(kite=kite, now=now, connection=book)
    assert (out["trades"], out["open"], out["final"]) == (1, 1, False)
    rows, trades, stats = pt.tab_data(book)
    t = stats["open_trades"][0]
    assert t["entry_date"] == "2026-06-04" and t["a_entry"] == daily[daily.date == "2026-06-04"].open_infy.iloc[0]
    assert rows[0]["final"] == 0 and rows[0]["a_close"] is None and rows[0]["z"] is None
    # the trade is marked at the latest price, not at its own entry
    last = daily[daily.date == "2026-06-04"].iloc[0]
    want = -((last.close_infy / last.open_infy - 1) - (last.close_tcs / last.open_tcs - 1))
    assert t["gross_pct"] == pytest.approx(100 * want, abs=1e-3)
    calls = kite.calls
    pt.run_day(kite=kite, now=datetime(2026, 6, 4, 10, 5), connection=book)       # a trade is open: again after 15 min
    assert kite.calls == calls
    pt.run_day(kite=kite, now=datetime(2026, 6, 4, 10, 16), connection=book)
    assert kite.calls == calls + 2


def test_ui_patch_applies_next_to_the_v3_wiring_and_is_idempotent(tmp_path):
    for rel in ("labs/ui/routes.py", "templates/live_strategy.html", "pa_paper_tracker_loop.py", "templates/_infy_tcs_pair.html"):
        target = tmp_path / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy(ROOT / rel, target)
    for name in ("patch_proposer_px_ui.py", "patch_proposer_v3_ui.py", "patch_infy_tcs_pair_ui.py"):
        run = subprocess.run([sys.executable, str(ROOT / "scripts" / name), str(tmp_path)], capture_output=True, text=True)
        assert run.returncode == 0, run.stderr
    second = subprocess.run([sys.executable, str(ROOT / "scripts" / "patch_infy_tcs_pair_ui.py"), str(tmp_path)],
                            capture_output=True, text=True)
    assert second.returncode == 0 and second.stdout.count("0 edits applied") == 3
    routes = (tmp_path / "labs/ui/routes.py").read_text(encoding="utf-8")
    assert routes.count('"infy_tcs_pair": "INFY / TCS pair"') == 1
    assert routes.count('@labs_bp.route("/api/infy_tcs_pair/refresh"') == 1
    loop = (tmp_path / "pa_paper_tracker_loop.py").read_text(encoding="utf-8")
    assert loop.count('("infy_tcs_pair", run_infy_tcs_pair_day)') == 1
    py_compile.compile(str(tmp_path / "labs/ui/routes.py"), doraise=True)
    py_compile.compile(str(tmp_path / "pa_paper_tracker_loop.py"), doraise=True)
    import jinja2
    for rel in ("templates/live_strategy.html", "templates/_infy_tcs_pair.html"):
        jinja2.Environment().parse((tmp_path / rel).read_text(encoding="utf-8"))
