"""NIFTY expiry-day straddle sale paper book: only on the expiring contract's day, three books,
research prices for the decisions, bid/ask for the fills, skips recorded with their reason."""
from __future__ import annotations

import sqlite3
from datetime import date, datetime, timedelta

import pandas as pd
import pytest

from labs.engine import nifty_expiry_sale_tracker as tr

DAY = "2026-09-08"          # a Tuesday; weekly code 26908


def chain(day=DAY, code="26908", *, spot0=23660.0, jump_at=None, jump=0.0, drop=(), wide=()):
    """One snapshot a minute, 09:15-15:29. NIFTY sits at spot0, then moves `jump` at `jump_at`.
    Options are intrinsic value + a time value that decays through the day; spread 0.5."""
    start = datetime.fromisoformat(f"{day}T09:15:00")
    rows = []
    for i in range(375):
        ts = start + timedelta(minutes=i)
        hhmm = ts.strftime("%H:%M")
        if hhmm in drop:
            continue
        spot = spot0 + (jump if jump_at and hhmm >= jump_at else 0.0)
        tv = 60.0 * (1 - i / 400)
        for strike in range(23400, 23950, 50):
            for typ in ("CE", "PE"):
                intrinsic = max(spot - strike, 0.0) if typ == "CE" else max(strike - spot, 0.0)
                mid = intrinsic + tv
                half = 20.0 if hhmm in wide else 0.25
                rows.append({"timestamp": ts, "underlying": "NIFTY", "tradingsymbol": f"NIFTY{code}{strike}{typ}",
                             "strike": strike, "option_type": typ, "expiry": code, "ltp": mid + 0.1,
                             "bid": mid - half, "ask": mid + half, "oi": 1000, "volume": 10, "spot": spot})
                rows.append({**rows[-1], "tradingsymbol": f"NIFTY26915{strike}{typ}", "expiry": "26915",
                             "ltp": mid + 80, "bid": mid + 79.5, "ask": mid + 80.5})
    return pd.DataFrame(rows)


@pytest.fixture
def book(tmp_path, monkeypatch):
    data = {}
    monkeypatch.setattr(tr, "load_options_frame", lambda symbol, day, **_kw: data[day])
    conn = sqlite3.connect(tmp_path / "labs.db")
    yield data, conn
    conn.close()


def test_expiry_day_comes_from_the_contract_not_the_weekday():
    assert tr.expiry_date_of("26908") == date(2026, 9, 8)
    assert tr.expiry_date_of("26O19") == date(2026, 10, 19)            # a holiday week: the code carries the Monday
    assert tr.expiry_date_of("26SEP") == date(2026, 9, 29)             # monthly: the month's last Tuesday
    assert tr.expiry_date_of("26JUN") == date(2026, 6, 30)
    assert tr.sample_tag("2026-06-23") == "in the research sample"
    assert tr.sample_tag("2026-06-30") == "not seen in research" and tr.sample_tag("2026-10-13") == "live"


def test_no_trade_on_a_day_that_is_not_the_contracts_expiry(book):
    data, conn = book
    data["2026-09-07"] = chain("2026-09-07")                           # the Monday before: nearest contract is 26908
    r = tr.simulate_day("2026-09-07")
    assert r["is_expiry"] is False and r["books"] == {}
    assert tr.run_day("2026-09-07", connection=conn)["status"] == "not_expiry"
    data.clear()                                                       # the second call must not read quotes again
    assert tr.run_day("2026-09-07", connection=conn)["status"] == "not_expiry"
    assert conn.execute("SELECT COUNT(*) FROM nifty_expiry_sale_trades").fetchone()[0] == 0


def test_the_three_books_on_a_quiet_expiry_day(book):
    data, conn = book
    data[DAY] = chain()
    r = tr.simulate_day(DAY)
    a, b, s = r["books"]["A"], r["books"]["B"], r["books"]["B50"]
    assert r["is_expiry"] and r["expiry_code"] == "26908"
    assert (a["entry_ts"][11:], a["exit_ts"][11:], a["strike"]) == ("09:45", "15:15", 23650)
    assert (b["entry_ts"][11:], b["exit_ts"][11:], b["strike"]) == ("09:21", "15:15", 23650)
    # put-call parity on the three strikes nearest the money gives the index back as the forward
    assert b["forward_at_entry"] == pytest.approx(23660.0, abs=0.01) and b["ref_level"] == b["forward_at_entry"]
    assert a["ref_level"] == 23660.0 and a["forward_at_entry"] is None
    # sold at the bid, bought back at the ask: a quarter point worse than the middle on each side of each leg
    tv_in, tv_out = 60.0 * (1 - 30 / 400), 60.0 * (1 - 360 / 400)
    assert a["legs"]["C"]["sold"] == pytest.approx(10 + tv_in - 0.25) and a["legs"]["P"]["sold"] == pytest.approx(tv_in - 0.25)
    assert a["legs"]["C"]["bought"] == pytest.approx(10 + tv_out + 0.25)
    assert a["gross_rs"] == pytest.approx((2 * (tv_in - tv_out) - 1.0) * tr.QTY, abs=0.01)
    assert a["net_rs"] == pytest.approx(a["gross_rs"] - a["charges_rs"], abs=0.01) and a["charges_rs"] > 80
    # the research number is on the middle prices, in basis points of the level the strike came from
    assert a["research_gross_bps"] == pytest.approx(2 * (tv_in - tv_out) / 23660.0 * 1e4, abs=1e-6)
    assert a["net_bps"] == pytest.approx(a["net_rs"] / (tr.QTY * 23660.0) * 1e4, abs=0.001)
    assert s["stop_triggered"] is False and s["net_rs"] == b["net_rs"] and s["stop_level"] == pytest.approx(b["straddle_sold_research"] * 1.5)
    out = tr.run_day(DAY, connection=conn)
    assert out["status"] == "final" and out["A"] == a["net_rs"]
    assert tr.run_day(DAY, connection=conn) == out                       # idempotent
    rows = conn.execute("SELECT book, status, source, sample, strike, exit_reason FROM nifty_expiry_sale_trades ORDER BY book").fetchall()
    assert rows == [("A", "closed", "backfill", "not seen in research", 23650, "15:15"),
                    ("B", "closed", "backfill", "not seen in research", 23650, "15:15"),
                    ("B50", "closed", "backfill", "not seen in research", 23650, "15:15")]


def test_pair_stop_buys_both_back_at_the_snapshot_that_shows_it(book):
    data, _conn = book
    data[DAY] = chain(jump_at="11:30", jump=150.0)                       # NIFTY jumps 150 points at 11:30
    r = tr.simulate_day(DAY)
    b, s = r["books"]["B"], r["books"]["B50"]
    assert b["exit_ts"][11:] == "15:15" and b["net_rs"] < 0 and b["stop_triggered"] is False
    assert s["stop_triggered"] and s["stop_ts"][11:] == "11:30" and s["exit_ts"][11:] == "11:30" and s["exit_reason"] == "pair stop"
    assert s["straddle_bought_research"] >= s["stop_level"] and s["net_rs"] < 0
    assert r["books"]["A"]["stop_triggered"] is False


def test_days_without_usable_quotes_are_skipped_with_the_reason(book):
    data, conn = book
    late = {f"{h:02d}:{m:02d}" for h in (9, 10) for m in range(60)}      # the collector started at 11:00
    data[DAY] = chain(drop=late)
    r = tr.simulate_day(DAY)
    assert r["books"]["A"] == {"status": "skipped", "error": "no snapshot from 09:45 to 10 minutes later"}
    assert r["books"]["B"]["error"] == "no snapshot from 09:21 to 6 minutes later"
    data[DAY] = chain(drop={f"15:{m:02d}" for m in range(15, 30)})       # nothing from 15:15 on
    r = tr.simulate_day(DAY)
    assert all(b == {"status": "skipped", "error": "no quote for both legs from 15:15 to 15:21"} for b in r["books"].values())
    tr.run_day(DAY, connection=conn)
    assert conn.execute("SELECT status, error, net_rs FROM nifty_expiry_sale_trades WHERE book='A'").fetchone() == (
        "skipped", "no quote for both legs from 15:15 to 15:21", None)
    # Book B does not wait for a later minute when the 09:21 snapshot cannot price the straddle; Book A may, to 09:55
    data[DAY] = chain(wide={"09:21", "09:45", "09:46"})
    for row in data[DAY].index[data[DAY]["timestamp"].dt.strftime("%H:%M").isin(["09:21", "09:45", "09:46"])]:
        data[DAY].loc[row, "ltp"] = 0.0
    r = tr.simulate_day(DAY)
    assert r["books"]["B"]["status"] == "skipped" and "forward" in r["books"]["B"]["error"]
    assert r["books"]["A"]["status"] == "closed" and r["books"]["A"]["entry_ts"][11:] == "09:47"


def test_a_running_expiry_day_is_open_and_marked_then_closes(book):
    data, conn = book
    full = chain()
    data[DAY] = full[full["timestamp"] <= f"{DAY} 09:30:00"]
    now = datetime.fromisoformat(f"{DAY}T09:31:00+05:30")
    r = tr.simulate_day(DAY, now=now)
    assert r["final"] is False and r["books"]["A"] == {"status": "waiting"}
    assert r["books"]["B"]["status"] == "open" and r["books"]["B"]["exit_ts"] is None and r["books"]["B"]["mark_ts"][11:] == "09:30"
    out = tr.run_day(DAY, connection=conn, now=now)
    assert out["status"] == "live" and out["A"] == "waiting"
    assert conn.execute("SELECT book, status FROM nifty_expiry_sale_trades").fetchall() == [("B", "open"), ("B50", "open")]
    data[DAY] = full
    tr.run_day(DAY, connection=conn, now=datetime.fromisoformat(f"{DAY}T15:40:00+05:30"))
    assert [r[0] for r in conn.execute("SELECT status FROM nifty_expiry_sale_trades ORDER BY book")] == ["closed"] * 3


def test_backfill_trades_expiry_days_notes_the_rest_and_records_a_missing_one(book, monkeypatch):
    data, conn = book
    data["2026-09-07"], data[DAY] = chain("2026-09-07"), chain()
    data["2026-09-14"] = chain("2026-09-14", code="26915")                # the Monday before an expiry day with no file
    monkeypatch.setattr(tr, "sessions_with_quotes", lambda s, e: [d for d in sorted(data) if s <= d <= e])

    class Shared:
        def __getattr__(self, name):
            return getattr(conn, name)

        def close(self):
            pass

    monkeypatch.setattr(tr, "get_conn", lambda: Shared())
    out = tr.run_backfill(start_date="2026-09-01", end_date="2026-09-30", limit=2)
    assert [d["trade_date"] for d in out["done"]] == ["2026-09-07", DAY] and out["remaining"] == 1
    assert out["expiry_days"] == [DAY] and out["missing_expiry_days"] == []
    out = tr.run_backfill(start_date="2026-09-01", end_date="2026-09-30", limit=5)
    assert out["remaining"] == 0 and out["missing_expiry_days"] == ["2026-09-15"]
    assert conn.execute("SELECT status, error FROM nifty_expiry_sale_trades WHERE trade_date='2026-09-15' AND book='B'").fetchone() == (
        "skipped", "no NIFTY quotes stored for this session")
    assert tr.run_backfill(start_date="2026-09-01", end_date="2026-09-30")["done"] == []
    rows, books, stats = tr.tab_data(conn)
    assert stats["expiry_days"] == 2 and len(stats["skipped"]) == 3 and [b["key"] for b in books] == ["A", "B", "B50"]
    a = books[0]
    assert a["days"] == 1 and a["skipped"] == 1 and a["net_rs"] == rows[-3]["net_rs"]
    assert [s["days"] for s in a["by_sample"]] == [0, 1, 0]


# ════════════════════════════════════════════════════════ wiring and overview ══
def test_ui_patch_applies_next_to_the_v3_wiring_and_is_idempotent(tmp_path):
    import py_compile
    import shutil
    import subprocess
    import sys
    from pathlib import Path
    root = Path(__file__).resolve().parents[1]
    for rel in ("labs/ui/routes.py", "templates/live_strategy.html", "pa_paper_tracker_loop.py",
                "labs/services/book_overview.py"):
        target = tmp_path / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy(root / rel, target)
    script = root / "scripts" / "patch_nifty_expiry_sale_ui.py"
    for name in ("patch_proposer_px_ui.py", "patch_proposer_v3_ui.py"):
        subprocess.run([sys.executable, str(root / "scripts" / name), str(tmp_path)], capture_output=True, text=True)
    first = subprocess.run([sys.executable, str(script), str(tmp_path)], capture_output=True, text=True)
    assert first.returncode == 0, first.stderr
    second = subprocess.run([sys.executable, str(script), str(tmp_path)], capture_output=True, text=True)
    assert second.returncode == 0 and second.stdout.count("0 edits applied") == 4
    routes = (tmp_path / "labs/ui/routes.py").read_text(encoding="utf-8")
    assert routes.count('"nifty_expiry_sale": "NIFTY Expiry Straddle Sale"') == 1
    assert routes.count('@labs_bp.route("/api/nifty_expiry_sale/backfill"') == 1
    assert (tmp_path / "labs/services/book_overview.py").read_text(encoding="utf-8").count("more_cards(conn, today)") == 1
    for rel in ("labs/ui/routes.py", "pa_paper_tracker_loop.py", "labs/services/book_overview.py"):
        py_compile.compile(str(tmp_path / rel), doraise=True)


def test_overview_shows_the_new_books_and_keeps_real_money_to_status(book, tmp_path, monkeypatch):
    import jinja2
    from pathlib import Path
    from labs.services import book_overview_more as more
    from storage import live_db
    data, conn = book
    data[DAY] = chain()
    tr.run_day(DAY, connection=conn)
    monkeypatch.setattr(live_db, "LIVE_DB_PATH", tmp_path / "live.db")
    live_db.init_live_db()
    with live_db.get_live_conn() as lc:
        lc.executemany("INSERT INTO live_config (user_id, conn_id, key, value) VALUES (?,?,?,?)", [
            ("u", "u:angel", "mode", "LIVE_ARMED"), ("u", "u:angel", "strategy_version", "proposer_dt25_v3"),
            ("u", "u:zerodha", "mode", "DISARMED"), ("u", "u:zerodha", "strategy_version", "v2.14")])
    cards = {c["key"]: c for c in more.more_cards(conn, DAY)}
    a = cards["nifty_expiry_sale_A"]
    net = conn.execute("SELECT net_rs FROM nifty_expiry_sale_trades WHERE book='A'").fetchone()[0]
    assert (a["total_net"], a["today_net"], a["trades"], a["position"], a["kind"], a["tab"]) == (
        net, net, 1, "Flat", "Simulated", "nifty_expiry_sale")
    assert cards["proposer_v3"]["status"] == "Waiting" and "error" in cards["proposer_v3"]     # no table in this database
    live = cards["live_proposer"]
    assert live["status"] == "Armed" and live["kind"] == "Live" and live["no_figures"] and live["href"] == "/live"
    assert "total_net" not in live and "u:angel" not in str(live)                          # no amounts, no account names
    assert cards["live_nifty"]["status"] == "Stopped"
    # the card template renders every kind of card
    env = jinja2.Environment(loader=jinja2.FileSystemLoader(str(Path(__file__).resolve().parents[1] / "templates")))
    env.globals["url_for"] = lambda *a, **k: "/labs/live?tab=" + str(k.get("tab"))
    usd = {**cards["nifty_expiry_sale_A"], "key": "crypto_xs", "usd": True}
    html = env.get_template("_overview.html").render(overview_cards=list(cards.values()) + [usd],
                                                     live_tabs={"overview": "Overview"}, active_live_tab="overview")
    assert "Expiry straddle sale A" in html and "Live Trading page (login)" in html and 'href="/live"' in html
    assert "/labs/live?tab=nifty_expiry_sale" in html and "$" in html
