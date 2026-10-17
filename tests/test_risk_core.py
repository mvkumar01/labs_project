"""Risk core (trend x vol) paper book: the rule's pieces, parity with the V2 Strategy Tester's research book,
the seed + feed inputs and the ledger / card."""
from __future__ import annotations

import io
import json
import py_compile
import sqlite3
import subprocess
import sys
import zipfile
from datetime import date, datetime
from pathlib import Path

import numpy as np
import pandas as pd
import pytest

from labs.engine import risk_core_data as data
from labs.engine import risk_core_engine as rc
from labs.engine import risk_core_tracker as tr

ROOT = Path(__file__).resolve().parents[1]


# ------------------------------------------------------------------- rule ---
def test_band_keeps_the_fraction_until_the_target_moves_a_tenth_or_hits_zero():
    t = pd.Series([0.0, 0.05, 0.12, 0.15, 0.30, 0.0, 0.08, np.nan, 0.5])
    assert rc.banded(t).tolist() == [0.0, 0.0, 0.12, 0.12, 0.30, 0.0, 0.0, 0.0, 0.5]


def test_donchian_enters_above_the_high_and_leaves_below_the_half_window_low():
    close = np.array([10, 11, 12, 11, 10, 9, 8], dtype=float)
    hi = np.array([np.nan, 10, 11, 12, 12, 12, 12], dtype=float)       # previous highs
    lo = np.array([np.nan, 10, 10, 11, 11, 10, 9], dtype=float)        # previous half-window lows
    assert rc._donchian(close, hi, lo)[1:].tolist() == [1.0, 1.0, 1.0, 0.0, 0.0, 0.0]


def test_roll_days_take_the_open_to_close_return():
    idx = pd.to_datetime(["2026-09-18", "2026-09-19", "2026-09-21", "2026-09-23"])
    d = pd.DataFrame({"open": [100, 101, 120, 121], "high": [101, 102, 122, 123], "low": [99, 100, 119, 120],
                      "close": [100, 101, 121, 122]}, index=idx, dtype=float)
    adj = rc.remove_roll_gaps(d)
    r = adj["close"].pct_change()
    assert r.iloc[1] == pytest.approx(101 / 101 - 1)                    # 19th: open-to-close
    assert r.iloc[2] == pytest.approx(121 / 120 - 1)                    # 21st: the 101 -> 120 jump is dropped
    assert r.iloc[3] == pytest.approx(122 / 121 - 1)                    # 23rd: close to close again


def test_book_rebalances_to_equal_on_the_first_of_the_month_and_pays_the_move():
    idx = pd.date_range("2026-01-30", "2026-02-02", freq="D")
    R = pd.DataFrame({"a": [0.10, 0.0, 0.0, 0.0], "b": [0.0, 0.0, 0.0, 0.0]}, index=idx)
    b = rc.book(R, {"a": 0.01, "b": 0.01}, capital=100.0)
    assert b["book_value"].iloc[0] == pytest.approx(105.0)
    w = 55 / 105
    fee = 2 * abs(0.5 - w) * 0.01
    assert b["book_value"].iloc[2] == pytest.approx(105.0 * (1 - fee))  # 1 Feb: back to 50/50, fee on the move
    assert b["a_value"].iloc[2] == pytest.approx(b["b_value"].iloc[2])


def test_sleeve_costs_as_specified():
    idx = pd.date_range("2026-01-01", periods=3, freq="D")
    d = pd.DataFrame({"close": [100.0, 110.0, 99.0]}, index=idx)
    pos = pd.Series([1.0, 1.0, 0.0], index=idx)
    r = rc.sleeve_returns(d, pos, rc.COSTS["crudeoil"])
    roll = 0.001 / 21
    assert r.iloc[1] == pytest.approx(0.10 - 0.0005 - roll)             # entered at day 0's close
    assert r.iloc[2] == pytest.approx(-0.10 - roll)                     # still held over day 2; exit cost next day
    f = pd.Series([0.0, 0.001, 0.0], index=idx)
    r = rc.sleeve_returns(d, pos, rc.COSTS["btcusdt"], f)
    assert r.iloc[1] == pytest.approx(0.10 - 0.0007 - 0.001)            # funding paid by the long
    assert rc.sleeve_returns(d, pd.Series(0.0, index=idx), rc.COSTS["nifty50"]).abs().sum() == 0.0   # idle earns 0


# ----------------------------------------------------------------- parity ---
@pytest.fixture(scope="module")
def seed_inputs():
    conn = sqlite3.connect(":memory:")
    data.ensure_tables(conn)
    out = data.load_inputs(conn)
    conn.close()
    return out


def test_engine_reproduces_the_research_book_on_its_own_inputs(seed_inputs):
    bars, funding, fx, _ = seed_inputs
    r = rc.run(bars, funding, fx, tr.START, rc.RESEARCH_COSTS)["book"]["book_return"]
    ref = pd.read_csv(ROOT / "data" / "risk_core_research_ref.csv", index_col=0, parse_dates=True)["trend_vol"]
    j = pd.concat([r, ref], axis=1, keys=["labs", "ref"]).dropna()
    assert len(j) == 2179 and j.corr().iloc[0, 1] > 0.999999
    assert (j.labs - j.ref).iloc[1:].abs().max() < 2e-6               # the first day differs by 7e-5
    a, b = rc.stats(j.labs), rc.stats(j.ref)
    assert a["cagr"] == pytest.approx(b["cagr"], abs=1e-4) and a["max_dd"] == pytest.approx(b["max_dd"], abs=1e-6)
    assert b["cagr"] == pytest.approx(0.1163, abs=1e-4) and b["max_dd"] == pytest.approx(-0.0530, abs=1e-4)


# ------------------------------------------------------------------ feeds ---
class FakeKite:
    def __init__(self, candles: dict | None = None):
        self.candles, self.calls = candles or {}, []

    def instruments(self, exchange):
        assert exchange == "MCX"
        return [{"name": "CRUDEOIL", "instrument_type": "FUT", "expiry": date(2099, 1, 19), "instrument_token": 777},
                {"name": "CRUDEOILM", "instrument_type": "FUT", "expiry": date(2099, 1, 19), "instrument_token": 778}]

    def historical_data(self, token, frm, to, interval, continuous=False):
        assert interval == "day"
        self.calls.append((token, continuous))
        return self.candles.get(token, [])


def test_kite_feed_stores_final_days_only_and_reads_crude_continuous(tmp_path):
    conn = sqlite3.connect(tmp_path / "labs.db")
    data.ensure_tables(conn)
    candle = lambda d, c: {"date": datetime.fromisoformat(d), "open": c, "high": c, "low": c, "close": c}
    kite = FakeKite({256265: [candle("2026-10-15", 25000.0), candle("2026-10-16", 25100.0)],
                     777: [candle("2026-10-15", 5900.0), candle("2026-10-16", 6000.0)]})
    out = data.update_kite(conn, kite, datetime(2026, 10, 16, 16, 0))
    assert out == {"nifty50": 2, "banknifty": 0, "crudeoil": 1}         # MCX's 16 Oct is not final at 16:00
    assert (777, True) in kite.calls and (256265, False) in kite.calls
    conn.close()


def _zip(text: str) -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        z.writestr("x.csv", text)
    return buf.getvalue()


def test_binance_feed_stops_at_the_first_unpublished_day_and_estimates_funding(tmp_path):
    conn = sqlite3.connect(tmp_path / "labs.db")
    data.ensure_tables(conn)
    ms = lambda ts: int(pd.Timestamp(ts, tz="UTC").timestamp() * 1000)
    kline = _zip(f"{ms('2026-10-15')},100,110,95,105,1,0,0,0,0,0,0\n")
    hours = pd.date_range("2026-10-15", periods=24, freq="h", tz="UTC")
    premium = _zip("".join(f"{int(h.timestamp() * 1000)},0,0,0,0.0001,0,0,0,0,0,0,0\n" for h in hours))

    def get(url):
        if url.endswith("1d-2026-10-15.zip"):
            return kline
        if "premiumIndexKlines" in url and url.endswith("2026-10-15.zip"):
            return premium
        return None
    out = data.update_binance(conn, datetime(2026, 10, 18, 9, 0), get=get)
    assert out["btcusdt"]["bars"] == 1 and out["btcusdt"]["funding_estimated"] == 1
    row = conn.execute("SELECT funding, estimated FROM risk_core_funding WHERE sleeve='btcusdt' AND trade_date='2026-10-15'").fetchone()
    assert row[1] == 1 and row[0] == pytest.approx(0.0002)               # settlements at 08:00 and 16:00 UTC
    conn.close()


def test_fx_feed_and_feed_days_extend_only_after_the_seed(tmp_path):
    conn = sqlite3.connect(tmp_path / "labs.db")
    data.ensure_tables(conn)
    body = json.dumps({"rates": {"2026-10-15": {"INR": 88.1}, "2026-10-16": {"INR": 88.2}}}).encode()
    assert data.update_fx(conn, datetime(2026, 10, 18, 9, 0), get=lambda url: body) == 2
    conn.executemany("INSERT INTO risk_core_bars (sleeve,trade_date,open,high,low,close,source) VALUES (?,?,?,?,?,?,?)",
                     [("nifty50", "2026-09-17", 1, 1, 1, 1.0, "feed"), ("nifty50", "2026-09-21", 25300, 25350, 25200, 25327.0, "feed")])
    bars, funding, fx, info = data.load_inputs(conn)
    n = bars["nifty50"]
    assert n.loc["2026-09-17", "close"] > 20000                          # a feed day never replaces a seeded one
    assert n.loc["2026-09-21", "close"] == 25327.0 and info["through"]["nifty50"] == "2026-09-21"
    assert fx.loc["2026-10-16"] == 88.2 and info["fx_through"] == "2026-10-16"
    conn.close()


# ----------------------------------------------------------------- ledger ---
def test_book_builds_from_the_seed_and_the_card_shows_the_protection_status(tmp_path, monkeypatch):
    monkeypatch.setattr(data, "update_binance", lambda conn, now: {})
    monkeypatch.setattr(data, "update_fx", lambda conn, now: 0)
    conn = sqlite3.connect(tmp_path / "labs.db")
    out = tr.run_day(kite=FakeKite(), now=datetime(2026, 10, 18, 9, 0), connection=conn)
    assert out["through"] == "2026-09-18" and out["days"] == 2271       # NIFTY's seed ends 18 Sep
    assert tr.run_day(kite=FakeKite(), now=datetime(2026, 10, 18, 9, 30), connection=conn) == {"status": "up to date"}
    s = tr.summary(conn)
    assert s["research"]["cagr"] == pytest.approx(0.0907, abs=5e-4) and s["research"]["worst_fall"] == pytest.approx(-0.0628, abs=5e-4)
    assert s["research"]["years_up"] == 7 and s["forward"]["days"] == 0 and s["status"] == "holding" and not s["broken"]
    assert [x["key"] for x in s["sleeves"]] == list(rc.SLEEVES) and all(0 <= x["held"] <= 1 for x in s["sleeves"])
    months, years, stats = tr.tab_data(conn)
    assert months[0]["month"] == "2026-09" and years[-1]["year"] == "2020" and stats["through"] == "2026-09-18"
    from labs.services import book_overview_more as more
    card = more._risk_core(conn, "2026-10-18")
    assert card["capital"] == 1_000_000 and "Protection holding" in card["note"] and card["position"].startswith("Held NIFTY 50")
    conn.close()


def test_forward_worst_fall_beyond_15_percent_marks_the_protection_broken(tmp_path, monkeypatch):
    conn = sqlite3.connect(tmp_path / "labs.db")
    tr._ensure_tables(conn)
    cols = len(rc.SLEEVES) * 2
    rows = [("2026-10-15", "research", 0.0, 1_500_000, 0.0, *[0.0] * cols),
            ("2026-10-16", "forward", -0.10, 1_350_000, -150_000, *[0.0] * cols),
            ("2026-10-17", "forward", -0.07, 1_255_500, -94_500, *[0.0] * cols)]
    conn.executemany(f"INSERT INTO risk_core_days VALUES ({','.join('?' * (5 + cols))})", rows)
    s = tr.summary(conn)
    assert s["forward"]["worst_fall"] == pytest.approx(-0.163, abs=1e-3) and s["broken"] and s["status"] == "BROKEN"
    assert s["forward"]["net_rs"] == pytest.approx(-244_500)
    conn.close()


def test_ui_patch_applies_next_to_the_pair_wiring_and_is_idempotent(tmp_path):
    files = ("labs/ui/routes.py", "templates/live_strategy.html", "pa_paper_tracker_loop.py", "labs/services/book_overview_more.py")
    for rel in files:
        target = tmp_path / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(subprocess.run(["git", "-C", str(ROOT), "show", f"HEAD:{rel}"], capture_output=True, check=True).stdout)
    script = str(ROOT / "scripts" / "patch_risk_core_ui.py")
    first = subprocess.run([sys.executable, script, str(tmp_path)], capture_output=True, text=True)
    assert first.returncode == 0, first.stderr
    second = subprocess.run([sys.executable, script, str(tmp_path)], capture_output=True, text=True)
    assert second.returncode == 0 and second.stdout.count("0 edits applied") == 4
    routes = (tmp_path / "labs/ui/routes.py").read_text(encoding="utf-8")
    assert routes.count('"risk_core": "Risk core · trend × vol"') == 1 and routes.count('/api/risk_core/refresh') == 1
    loop = (tmp_path / "pa_paper_tracker_loop.py").read_text(encoding="utf-8")
    assert loop.count('("risk_core", run_risk_core_day)') == 1
    for rel in ("labs/ui/routes.py", "pa_paper_tracker_loop.py", "labs/services/book_overview_more.py"):
        py_compile.compile(str(tmp_path / rel), doraise=True)
    import jinja2
    jinja2.Environment().parse((tmp_path / "templates/live_strategy.html").read_text(encoding="utf-8"))
    jinja2.Environment().parse((ROOT / "templates/_risk_core.html").read_text(encoding="utf-8"))


def test_card_waits_before_the_first_build(tmp_path):
    conn = sqlite3.connect(tmp_path / "labs.db")
    from labs.services import book_overview_more as more
    with pytest.raises(sqlite3.OperationalError):
        more._risk_core(conn, "2026-10-18")
    assert tr.summary(conn) == {}
    conn.close()
