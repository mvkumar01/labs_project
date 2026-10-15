"""NIFTY Tuesday put paper book: the research's strike and prices, ask-in/bid-out headline, the
research's own figure beside it, skips with their reason, sample tags and the retirement rule."""
from __future__ import annotations

import sqlite3
from datetime import datetime, timedelta

import pandas as pd
import pytest

from labs.engine import nifty_tuesday_put_tracker as tp
from labs.services import book_overview_more as more

DAY = "2026-09-08"          # a forward Tuesday, weekly code 26908
IST = tp.IST


def chain(day=DAY, code="26908", *, spot=24650.0, drift=-0.1, drop=(), wide=()):
    """One snapshot a minute, 09:15-15:29. NIFTY drifts `drift` points a minute; puts are intrinsic
    value + a decaying time value, spread 0.5; a later expiry is listed beside the nearest one."""
    start = datetime.fromisoformat(f"{day}T09:15:00")
    rows = []
    for i in range(375):
        ts = start + timedelta(minutes=i)
        hhmm = ts.strftime("%H:%M")
        if hhmm in drop:
            continue
        s = spot + drift * i
        tv = 40.0 * (1 - i / 400)
        for strike in range(24400, 25000, 50):
            mid = max(strike - s, 0.0) + tv
            half = 20.0 if hhmm in wide else 0.25
            rows.append({"timestamp": ts, "underlying": "NIFTY", "tradingsymbol": f"NIFTY{code}{strike}PE", "strike": strike,
                         "option_type": "PE", "expiry": code, "ltp": mid + 0.1, "bid": mid - half, "ask": mid + half,
                         "oi": 1000, "volume": 10, "spot": s})
            rows.append({**rows[-1], "tradingsymbol": f"NIFTY26915{strike}PE", "expiry": "26915",
                         "ltp": mid + 80, "bid": mid + 79.5, "ask": mid + 80.5})
    return pd.DataFrame(rows)


@pytest.fixture
def book(tmp_path, monkeypatch):
    data, spots = {}, {}
    monkeypatch.setattr(tp, "load_options_frame", lambda symbol, day, **_kw: data[day])
    monkeypatch.setattr(tp, "spot_0920", lambda day: (spots.get(day), "test") if day in spots else (None, None))
    monkeypatch.setattr(tp, "seed_quote_days", lambda: set())
    conn = sqlite3.connect(tmp_path / "labs.db")
    yield data, spots, conn
    conn.close()


def test_strike_is_about_89_points_in_the_money():
    assert tp.strike_for(24650.0) == 24750          # 24739 -> 24750
    assert tp.strike_for(24600.0) == 24700          # 24689 -> 24700
    assert tp.strike_for(24585.9) == 24650          # 24674.9 -> 24650
    assert tp.sample_tag("2026-09-01") == "research sample" and tp.sample_tag("2026-09-08") == "forward"


def test_a_tuesday_bought_at_the_ask_sold_at_the_bid_beside_the_research_figure(book):
    data, spots, conn = book
    data[DAY], spots[DAY] = chain(), 24650.0
    r = tp.simulate_day(DAY)
    assert r["status"] == "closed" and r["strike"] == 24750 and r["expiry_code"] == "26908"
    assert r["entry_ts"].endswith("09:20") and r["exit_ts"].endswith("15:25")
    s_in, s_out = 24650.0 - 0.5, 24650.0 - 0.1 * 370
    mid_in, mid_out = 24750 - s_in + 40 * (1 - 5 / 400), 24750 - s_out + 40 * (1 - 370 / 400)
    assert r["entry_research"] == pytest.approx(mid_in) and r["exit_research"] == pytest.approx(mid_out)
    assert r["bought"] == pytest.approx(mid_in + 0.25) and r["sold"] == pytest.approx(mid_out - 0.25)
    assert r["research_net_rs"] == pytest.approx((mid_out - mid_in) * 65 - 95, abs=0.01)
    assert r["net_rs"] == pytest.approx(r["gross_rs"] - r["charges_rs"]) and r["fills"] == "ask in, bid out"


def test_the_research_waits_up_to_two_minutes_for_a_price(book):
    data, spots, _ = book
    data[DAY], spots[DAY] = chain(drop=("09:20", "09:21")), 24650.0
    assert tp.simulate_day(DAY)["entry_ts"].endswith("09:22")
    data[DAY] = chain(drop=("09:20", "09:21", "09:22"))
    with pytest.raises(tp.TuesdayPutSkip, match="09:20 to 09:22"):
        tp.simulate_day(DAY)


def test_a_wide_quote_is_priced_at_the_last_trade(book):
    data, spots, _ = book
    data[DAY], spots[DAY] = chain(wide=("09:20",)), 24650.0
    r = tp.simulate_day(DAY)
    assert r["entry_research"] == pytest.approx(24750 - 24649.5 + 40 * (1 - 5 / 400) + 0.1)


def test_no_exit_price_is_a_recorded_skip(book):
    data, spots, conn = book
    data[DAY], spots[DAY] = chain(drop=("15:25", "15:26", "15:27")), 24650.0
    out = tp.run_day(DAY, connection=conn, now=datetime(2026, 9, 9, 10, 0, tzinfo=IST))
    assert out["status"] == "skipped" and "15:25 to 15:27" in out["error"]
    row = conn.execute("SELECT status, sample, error FROM nifty_tuesday_put_trades").fetchone()
    assert row[0] == "skipped" and row[1] == "forward"


def test_no_spot_is_a_recorded_skip_and_other_weekdays_are_ignored(book):
    data, spots, conn = book
    data[DAY] = chain()
    assert tp.run_day(DAY, connection=conn)["error"].startswith("no NIFTY 09:20 minute open")
    assert tp.run_day("2026-09-09", connection=conn)["status"] == "not_tuesday"


def test_today_is_open_and_marked_until_the_exit(book):
    data, spots, conn = book
    data[DAY], spots[DAY] = chain(), 24650.0
    data[DAY] = data[DAY][data[DAY]["timestamp"] <= pd.Timestamp(f"{DAY} 11:00")]
    assert tp.run_day(DAY, connection=conn, now=datetime(2026, 9, 8, 9, 21, tzinfo=IST))["status"] == "waiting"
    out = tp.run_day(DAY, connection=conn, now=datetime(2026, 9, 8, 11, 0, 30, tzinfo=IST))
    assert out["status"] == "open"
    row = conn.execute("SELECT status, exit_ts FROM nifty_tuesday_put_trades").fetchone()
    assert row == ("open", f"{DAY}T11:00")


def test_seed_days_have_last_traded_prices_only(monkeypatch, tmp_path):
    q = tmp_path / "q.csv"
    pd.DataFrame([{"trade_date": "2025-09-02", "timestamp": f"2025-09-02 {hm}:00", "tradingsymbol": "NIFTY2590224750PE",
                   "expiry": "25902", "strike": 24750, "option_type": "PE", "ltp": px, "source_file": "x"}
                  for hm, px in (("09:20", 120.0), ("15:25", 150.0))]).to_csv(q, index=False)
    monkeypatch.setattr(tp, "QUOTE_SEED", q)
    monkeypatch.setattr(tp, "spot_0920", lambda day: (24650.0, "seed"))
    r = tp.simulate_day("2025-09-02")
    assert r["quote_source"] == tp.SEED_SOURCE and r["fills"].startswith("last traded price")
    assert r["bought"] == pytest.approx(120.2) and r["sold"] == pytest.approx(149.8)
    assert r["research_net_rs"] == pytest.approx(30 * 65 - 95)


def _row(day, net, sample=None, status="closed"):
    return {"trade_date": day, "status": status, "sample": sample or tp.sample_tag(day), "net_rs": net}


def test_retirement_is_judged_on_the_first_26_forward_tuesdays():
    fwd = [(datetime(2026, 9, 8) + timedelta(days=7 * i)).date().isoformat() for i in range(30)]
    assert tp.retirement([_row(d, 100) for d in fwd[:10]]) == {"n": 10, "of": 26, "net_rs": 1000.0, "verdict": "running"}
    losing = [_row(d, -10) for d in fwd[:26]] + [_row(fwd[26], 10_000)]
    assert tp.retirement(losing)["verdict"] == "retire"                     # later Tuesdays do not rescue it
    assert tp.retirement([_row(d, 5) for d in fwd[:26]])["verdict"] == "keep"
    assert tp.retirement([_row("2026-08-25", 9_999)] + [_row(d, 0) for d in fwd[:26]])["verdict"] == "retire"


def test_tab_and_overview_show_both_samples_and_the_rule(book):
    data, spots, conn = book
    for day in ("2026-09-01", "2026-09-08", "2026-10-06"):
        code = {"2026-09-01": "26901", "2026-09-08": "26908", "2026-10-06": "26O06"}[day]
        data[day], spots[day] = chain(day, code), 24650.0
        tp.run_day(day, connection=conn)
    rows, summaries, stats = tp.tab_data(conn)
    assert [s["sample"] for s in summaries] == ["research sample", "forward"]
    assert summaries[0]["headline"]["days"] == 1 and summaries[1]["headline"]["days"] == 2
    assert stats["retirement"]["n"] == 2 and stats["reserved_read"] == ["2026-10-06"]
    assert stats["no_data"] == ["2026-09-15", "2026-09-22", "2026-09-29"]
    card = more._tuesday_put(conn, "2026-10-06")
    assert card["label"] == "NIFTY Tuesday put" and card["trades"] == 3
    assert card["note"].startswith("Forward Tuesdays 2 / 26")
