"""Far-OTM expiry strangle paper book: sigma, expiry dates, strikes and fills, the tail rule, settlement at
intrinsic, retirement, the card / tab and the wiring."""
from __future__ import annotations

import py_compile
import sqlite3
import subprocess
import sys
from datetime import date
from pathlib import Path

import numpy as np
import pandas as pd
import pytest

from labs.engine import expiry_strangle_tracker as es

ROOT = Path(__file__).resolve().parents[1]
ENTRY, EXPIRY, CODE = "2026-06-01", "2026-06-02", "26602"


def _closes(n=30, sd=0.008, seed=1):
    rng = np.random.RandomState(seed)
    days = pd.bdate_range(end="2026-05-29", periods=n)
    c = 24000 * np.exp(np.cumsum(rng.normal(0, sd, n)))
    return pd.Series(c, index=[d.date().isoformat() for d in days])


def test_sigma_uses_the_last_20_returns_before_the_entry_day():
    c = _closes()
    want = np.log(c.iloc[-21:]).diff().dropna().std()
    assert es.sigma_before(c, ENTRY) == pytest.approx(want)
    assert np.isnan(es.sigma_before(c.iloc[:15], ENTRY))
    assert es.sigma_before(pd.concat([c, pd.Series({ENTRY: 1.0})]), ENTRY) == pytest.approx(want)   # the entry day's own close is not used


def test_expiry_dates():
    assert es.expiry_date_of("26602", "NIFTY") == date(2026, 6, 2)
    assert es.expiry_date_of("26OCT", "NIFTY") == date(2026, 10, 27)      # last Tuesday
    assert es.expiry_date_of("26OCT", "SENSEX") == date(2026, 10, 29)     # last Thursday


def _chain(day, minutes, spot, quote):
    """quote(minute, strike, cp) -> (bid, ask)."""
    rows = []
    for m in minutes:
        for k in range(23000, 25050, 50):
            for cp in ("CE", "PE"):
                b, a = quote(m, k, cp[0])
                rows.append({"timestamp": f"{day} {m}:00", "spot": spot, "strike": k, "option_type": cp, "expiry": CODE,
                             "ltp": (b + a) / 2, "bid": b, "ask": a})
    return pd.DataFrame(rows)


def _otm_value(spot, k, cp, scale):
    dist = (k - spot) if cp == "C" else (spot - k)
    return max(0.05, scale * np.exp(-max(dist, 0) / 120.0)) + max(-dist, 0)


@pytest.fixture
def market(monkeypatch):
    """Spot 24000 at 15:25 on the entry day; the expiry day calm unless `spike` sets a minute and a factor."""
    state = {"spike": None, "no_ask_at_exit": False}
    sigma = es.sigma_before(_closes(), ENTRY)

    def load(und, day, *, live_root, archive_root):
        if day == ENTRY:
            return _chain(day, ["15:20", "15:25"], 24000.0,
                          lambda m, k, cp: (round(_otm_value(24000, k, cp, 60), 2), round(_otm_value(24000, k, cp, 60) + 0.5, 2)))
        mins = [f"{h:02d}:{mm:02d}" for h in range(9, 16) for mm in range(0, 60, 5) if "09:15" <= f"{h:02d}:{mm:02d}" <= "15:25"]

        def q(m, k, cp):
            v = _otm_value(24000, k, cp, 20)
            if state["spike"] and m >= state["spike"][0]:
                v *= state["spike"][1]
            ask = v + 0.5
            if state["no_ask_at_exit"] and m == "15:25":
                ask = 0.0
            return round(v, 2), round(ask, 2)
        return _chain(day, mins, 24000.0, q)
    monkeypatch.setattr(es, "load_options_frame", load)
    return state, sigma


def _close_series():
    return pd.concat([_closes(), pd.Series({ENTRY: 24000.0, EXPIRY: 24010.0})])


def _row(rows, variant, fill):
    return next(r for r in rows if r["variant"] == variant and r["fill"] == fill)


def test_strikes_fills_and_charges(market):
    _, sigma = market
    rows = es.simulate("NIFTY", EXPIRY, ENTRY, CODE, _close_series())
    r = _row(rows, "k2", "bidask")
    assert r["status"] == "closed" and r["sigma"] == pytest.approx(sigma) and r["entry_minute"].endswith("15:25:00")
    assert r["call_strike"] == round(24000 * (1 + 2 * sigma) / 50) * 50 and r["put_strike"] == round(24000 * (1 - 2 * sigma) / 50) * 50
    assert r["credit"] == pytest.approx(r["call_in"] + r["put_in"]) and r["exit_reason"] == "15:25 on expiry day"
    qty = es.lot_size("NIFTY")
    assert r["gross_rs"] == pytest.approx((r["credit"] - r["buyback"]) * qty)
    assert r["net_rs"] == pytest.approx(r["gross_rs"] - r["charges_rs"]) and r["charges_rs"] > 40
    assert r["net_bps"] == pytest.approx(r["net_rs"] / (24000 * qty) * 1e4)
    assert r["margin_rs"] == pytest.approx(0.10 * 24000 * qty) and r["worst_multiple"] < 1
    m = _row(rows, "k2", "mid")
    assert m["credit"] == pytest.approx(r["credit"] + 0.5) and m["net_rs"] > r["net_rs"]           # half the spread each way
    k3 = _row(rows, "k3", "bidask")
    assert k3["call_strike"] > r["call_strike"] and k3["put_strike"] < r["put_strike"] and k3["credit"] < r["credit"]


def test_the_tail_rule_buys_both_back_at_three_times_the_credit(market):
    state, _ = market
    state["spike"] = ("11:00", 20.0)
    rows = es.simulate("NIFTY", EXPIRY, ENTRY, CODE, _close_series())
    tail, none = _row(rows, "k2", "bidask"), _row(rows, "k2_notail", "bidask")
    assert tail["exit_reason"].startswith("tail") and tail["exit_minute"].endswith("11:00:00")
    assert tail["buyback"] >= 3 * tail["credit"]
    assert none["exit_reason"] == "15:25 on expiry day" and none["worst_multiple"] >= 3


def test_a_leg_with_no_ask_settles_at_intrinsic_on_the_close(market):
    state, _ = market
    state["no_ask_at_exit"] = True
    rows = es.simulate("NIFTY", EXPIRY, ENTRY, CODE, _close_series())
    r = _row(rows, "k2", "bidask")
    assert r["buyback"] == 0.0 and "settled at intrinsic" in r["reason"] and r["close_exit"] == 24010.0


def test_retirement():
    def ev(i, net, notional=1.6e6):
        return {"und": "NIFTY", "expiry": f"2026-11-{i:02d}", "entry_date": f"2026-11-{i:02d}", "period": "forward",
                "status": "closed", "variant": "k2", "fill": "bidask", "net_rs": net, "notional_rs": notional}
    assert es.retirement([ev(1, 500)])["verdict"] == "keep"
    assert es.retirement([ev(1, 500), ev(2, -25_000)])["retire"]                   # -1.56% of 1.6m
    assert not es.retirement([ev(1, 500), ev(2, -23_000)])["retire"]
    assert es.retirement([ev(i, -10) for i in range(1, 27)])["retire"]
    assert not es.retirement([ev(i, -10) for i in range(1, 26)])["retire"]       # 25 events: not yet judged


def test_card_and_tab_render(market, tmp_path):
    conn = sqlite3.connect(tmp_path / "labs.db")
    es._ensure_tables(conn)
    rows = es.simulate("NIFTY", EXPIRY, ENTRY, CODE, _close_series())
    es._persist(conn, rows)
    primary, every, st = es.tab_data(conn)
    assert len(primary) == 1 and len(every) == 6 and st["books"]["k2_bidask"]["research_ALL"]["events"] == 1
    assert st["retirement"]["verdict"] == "keep" and st["reserved_read"] == []
    from labs.services import book_overview_more as more
    card = more._expiry_strangle(conn, "2026-06-02")
    assert card["key"] == "expiry_strangle" and "Retirement: keep" in card["note"] and card["trades"] == 1
    import jinja2
    env = jinja2.Environment(loader=jinja2.FileSystemLoader(str(ROOT / "templates")), autoescape=True)   # as Flask
    html = env.get_template("_expiry_strangle.html").render(expiry_strangle_rows=primary, expiry_strangle_stats=st)
    assert "Far-OTM expiry strangle" in html and "keep" in html
    assert "&amp;mdash;" not in html and "&amp;times;" not in html and "&amp;#8377;" not in html
    with pytest.raises(sqlite3.OperationalError):
        more._expiry_strangle(sqlite3.connect(":memory:"), "2026-06-02")
    conn.close()


def test_ui_patch_applies_and_is_idempotent(tmp_path):
    files = ("labs/ui/routes.py", "templates/live_strategy.html", "pa_paper_tracker_loop.py", "labs/services/book_overview_more.py")
    for rel in files:
        target = tmp_path / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(subprocess.run(["git", "-C", str(ROOT), "show", f"HEAD:{rel}"], capture_output=True, check=True).stdout)
    script = str(ROOT / "scripts" / "patch_expiry_strangle_ui.py")
    assert subprocess.run([sys.executable, script, str(tmp_path)], capture_output=True, text=True).returncode == 0
    second = subprocess.run([sys.executable, script, str(tmp_path)], capture_output=True, text=True)
    assert second.returncode == 0 and second.stdout.count("0 edits applied") == 4
    for rel in ("labs/ui/routes.py", "pa_paper_tracker_loop.py", "labs/services/book_overview_more.py"):
        py_compile.compile(str(tmp_path / rel), doraise=True)
    import jinja2
    jinja2.Environment().parse((tmp_path / "templates/live_strategy.html").read_text(encoding="utf-8"))


def test_backfill_waits_for_the_daily_closes_instead_of_skipping(market, tmp_path, monkeypatch):
    conn = sqlite3.connect(tmp_path / "labs.db")
    es._ensure_tables(conn)
    monkeypatch.setattr(es, "events", lambda und, start, end: [(EXPIRY, ENTRY, CODE)] if und == "NIFTY" else [])
    out = es.run_backfill(end_date="2026-06-05", kite=False, connection=conn)
    assert out["errors"] == {"NIFTY 2026-06-02": "daily closes not available yet"} and out["done"] == []
    assert conn.execute("SELECT COUNT(*) FROM expiry_strangle_events").fetchone()[0] == 0
    c = _close_series()
    conn.executemany("INSERT INTO expiry_strangle_closes VALUES (?,?,?,?,?)", [("NIFTY", d, v, "test", None) for d, v in c.items()])
    out = es.run_backfill(end_date="2026-06-05", kite=False, connection=conn)
    assert out["done"] == ["NIFTY 2026-06-02"] and out["remaining"] == 0
    assert conn.execute("SELECT COUNT(*) FROM expiry_strangle_events WHERE status='closed'").fetchone()[0] == 6
    conn.close()
