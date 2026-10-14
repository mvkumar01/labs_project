"""SENSEX Proposer v3 shadow on Pramanaa's print clock (paper only), and the Pramanaa captures it is
compared with."""

from __future__ import annotations

import sqlite3
from datetime import date, datetime, timedelta
from email.message import EmailMessage
from pathlib import Path

import jinja2

from labs.engine import pramanaa_capture as pc
from labs.engine import proposer_px_tracker as tr
from labs.engine import proposer_v3_book as v3

ROOT = Path(__file__).resolve().parents[1]
DAY = datetime(2026, 10, 8)


def _session(minutes: int = 60):
    base = DAY.replace(hour=9, minute=15)
    today = [(base + timedelta(minutes=i), 72400.0 + 3 * i) for i in range(minutes)]
    prior = [(datetime(2026, 10, 7, 14, 0) + timedelta(minutes=i), 72390.0) for i in range(80)]
    return today, prior


def _times(out, n=6):
    return [(p["t"].strftime("%H:%M"), p["sec"]) for p in out[:n]]


# -- the clock ---------------------------------------------------------------------------
def test_pramanaa_clock_is_five_passes_of_its_loop():
    one_pass = tr.PRAMANAA_LOOP_SLEEP_S + tr.PRAMANAA_PASS_WORK_S
    assert 4 * one_pass < tr.PRAMANAA_MIN_GAP_S <= 5 * one_pass
    assert tr.PRAMANAA_PRINT_GAP_S == 310.5
    assert (tr.V3_CLOCK.strategy_version, tr.V3_CLOCK.print_gap_s) == (tr.V3.strategy_version, 310.5)
    assert tr.V3.print_gap_s == tr.PX.print_gap_s == 300.0          # the live-twin books are unchanged


def test_books_on_the_five_minute_clock_print_as_before():
    today, prior = _session()
    out, _ = tr._prints(DAY.date().isoformat(), prior, today, today[0][1], {}, today[-1][0])
    assert _times(out) == [("09:22", 25.0), ("09:27", 25.0), ("09:32", 25.0), ("09:37", 25.0), ("09:42", 25.0),
                           ("09:47", 25.0)]


def test_shadow_prints_every_310_5_seconds_and_its_minute_slides():
    today, prior = _session()
    out, _ = tr._prints(DAY.date().isoformat(), prior, today, today[0][1], {}, today[-1][0], gap_s=310.5)
    assert _times(out) == [("09:22", 25.0), ("09:27", 35.5), ("09:32", 46.0), ("09:37", 56.5), ("09:43", 7.0),
                           ("09:48", 17.5)]
    late = today[4:]                                                 # the first bar is 09:19: the clock starts later
    out, _ = tr._prints(DAY.date().isoformat(), prior, late, late[0][1], {}, today[-1][0], gap_s=310.5)
    assert _times(out, 2) == [("09:26", 25.0), ("09:31", 35.5)]


def test_the_loop_runs_the_shadow_after_v3_and_a_shadow_failure_never_stops_v3(monkeypatch):
    calls = []

    def fake(trade_date=None, *, book, **kw):
        calls.append(book.key)
        if book.key == "proposer_v3_clock":
            raise RuntimeError("no quotes")
        return {"trade_date": trade_date, "status": "closed"}

    monkeypatch.setattr(v3._tracker, "run_day", fake)
    assert v3.run_day("2026-10-08")["status"] == "closed"
    assert calls == ["proposer_v3", "proposer_dt25_shadow", "proposer_dt25_clock", "proposer_v3_clock"]
    rules = {b.key: (b.strategy_version, b.print_gap_s) for b, _ in v3.SHADOWS}
    assert rules == {"proposer_dt25_shadow": ("proposer_dt25", 300.0), "proposer_dt25_clock": ("proposer_dt25", 310.5),
                     "proposer_v3_clock": ("proposer_dt25_v3", 310.5)}


def test_backfill_starts_the_shadows_where_pramanaas_ledger_starts(monkeypatch):
    seen = []
    monkeypatch.setattr(v3._backfill, "run_backfill", lambda **kw: seen.append((kw["book"].key, kw["start_date"])) or {})
    v3.run_backfill(start_date="2026-06-01", limit=5)
    assert seen == [("proposer_v3", "2026-06-01"), ("proposer_dt25_shadow", "2026-09-21"),
                    ("proposer_dt25_clock", "2026-09-21"), ("proposer_v3_clock", "2026-09-21")]


# -- Pramanaa captures ---------------------------------------------------------------------
TSV = ("2026-09-25\t14:10\td\tdrift_up\t40\t\t\tok\n"
       "2026-09-25\t14:10\t5\tStrong Bull\t79\t1,2,3,4,90\tnn\tok\n"
       "2026-09-25\t14:05\t5\tStrong Bull\t40\t1,2,3,4,90\tnn\tok\n")


def _ledger_mhtml() -> bytes:
    head = ["Date", "Entry", "Exit", "Duration", "Dir", "Signal", "Instrument", "Qty", "Fills", "Entry ₹",
            "Exit ₹", "Delta", "Entry Gate", "RSI In", "Spot In", "RSI Out", "Spot Out", "Pts", "P&amp;L",
            "Exit Rule", "Peak P&amp;L", "Post-exit peak"]
    rows = [["25 Sept", "02:10:28 pm", "02:11:37 pm", "1m 7s", "CE", "proposer_5class_strong_bull", "O0173600CE",
             "2000", "2 in", "749.88", "768.93", "0.60", "-", "64.8", "73849", "64.8", "73859", "+10",
             "₹38,100", "daily_target", "+₹48,740", "₹1,12,040"],
            ["29 Sept", "09:48:20 am", "09:48:31 am", "10s", "PE", "proposer_5class_strong_bear", "O0172400PE",
             "2000", "2 in", "468.05", "467.83", "0.59", "-", "34.0", "72167", "34.0", "72152", "+15",
             "-₹440", "daily_target", "+₹2,700", "₹54,900"]]
    html = "<table><tr>" + "".join(f"<th>{h}</th>" for h in head) + "</tr>" + "".join(
        "<tr>" + "".join(f"<td>{c}</td>" for c in r) + "</tr>" for r in rows) + "</table>"
    msg = EmailMessage()
    msg.set_content(html, subtype="html")
    return msg.as_bytes()


def test_prints_and_ledger_are_parsed_from_the_saved_pages():
    assert pc.parse_prints_tsv(TSV) == [("2026-09-25", "14:10", "strong_bull", 0.79),
                                        ("2026-09-25", "14:05", "strong_bull", 0.40)]
    rows = pc.parse_ledger_mhtml(_ledger_mhtml(), date(2026, 10, 14))
    assert rows == [
        ("2026-09-25", "14:10:28", "14:11:37", "CE", "proposer_5class_strong_bull", "O0173600CE", 2000, 749.88,
         768.93, 38100.0, "daily_target"),
        ("2026-09-29", "09:48:20", "09:48:31", "PE", "proposer_5class_strong_bear", "O0172400PE", 2000, 468.05,
         467.83, -440.0, "daily_target")]
    assert pc.parse_ledger_mhtml(_ledger_mhtml(), date(2027, 1, 5))[0][0] == "2026-09-25"     # no year: last year's


def _book(conn, key, days):
    tr._ensure_tables(conn, tr.Book(key, "proposer_dt25_v3"))
    for day, net, minutes, entry in days:
        conn.execute(f"INSERT INTO {key}_daily (trade_date,status,n_trades,gross_rs,charges_rs,net_rs,lots,qty,"
                     "strategy_version,updated_at) VALUES (?,?,?,?,0,?,100,2000,'v3','x')",
                     (day, "closed" if entry else "no_trade", 1 if entry else 0, net, net))
        conn.executemany(f"INSERT INTO {key}_prints (trade_date,seq,minute,sec,x5,x5_conf) VALUES (?,?,?,?,?,?)",
                         [(day, i, m, 25.0, "chop", 0.5) for i, m in enumerate(minutes, 1)])
        if entry:
            conn.execute(f"INSERT INTO {key}_trades (trade_date,seq,signal,side,strike,entry_ts) VALUES (?,1,'s','CE',1,?)",
                         (day, f"{day}T{entry}"))


def _four(conn, rows):
    """rows: {book key: [(day, net, print minutes, first entry)]}"""
    for key, days in rows.items():
        _book(conn, key, days)


def test_comparison_lines_up_the_books_and_pramanaa(tmp_path):
    conn = sqlite3.connect(tmp_path / "labs.db")
    conn.row_factory = sqlite3.Row
    _four(conn, {
        "proposer_v3": [("2026-09-25", -112807.0, ["14:02", "14:07", "14:12"], "14:07:25"),
                        ("2026-09-26", 0.0, ["10:00"], None), ("2026-10-12", 5000.0, ["09:22"], "09:22:25")],
        "proposer_dt25_shadow": [("2026-09-25", -112807.0, ["14:02", "14:07", "14:12"], "14:07:25"),
                                 ("2026-09-26", 0.0, ["10:00"], None), ("2026-10-12", 7000.0, ["09:22"], "09:22:25")],
        "proposer_dt25_clock": [("2026-09-25", 34944.0, ["14:05", "14:10", "14:15"], "14:10:28"),
                                ("2026-09-26", 0.0, ["10:01"], None), ("2026-10-12", 7000.0, ["09:22"], "09:22:25")],
        "proposer_v3_clock": [("2026-09-25", 34944.0, ["14:05", "14:10", "14:15"], "14:10:28"),
                              ("2026-09-26", 0.0, ["10:01"], None), ("2026-10-12", 5000.0, ["09:22"], "09:22:25")]})
    (tmp_path / "prints.tsv").write_text(TSV + "2026-09-26" + chr(9) + chr(9).join(["10:00", "5", "Chop", "50", "", "", "ok"]) + chr(10), encoding="utf-8")
    (tmp_path / "ledger.mhtml").write_bytes(_ledger_mhtml())
    assert pc.import_file(tmp_path / "prints.tsv", conn)["rows"] == 3
    assert pc.import_file(tmp_path / "ledger.mhtml", conn, date(2026, 10, 14))["rows"] == 2
    s = v3.shadow_comparison(conn)
    by = {d["trade_date"]: d for d in s["days"]}
    assert by["2026-09-25"]["books"]["proposer_dt25_shadow"]["match"] == (0, 2)
    assert by["2026-09-25"]["books"]["proposer_dt25_clock"]["match"] == (2, 2)
    assert by["2026-09-26"]["books"]["proposer_dt25_shadow"]["match"] == (1, 1)
    assert by["2026-09-25"]["pramanaa_net"] == 38100.0 and by["2026-09-25"]["pramanaa_entries"] == ["14:10:28 CE"]
    assert by["2026-09-26"]["pramanaa_net"] == 0.0                   # inside the ledger's span, no trade
    assert by["2026-10-12"]["pramanaa_net"] is None                  # after it: not captured
    books = {b["key"]: b for b in s["books"]}
    assert [b["key"] for b in s["books"]] == ["proposer_v3", "proposer_dt25_shadow", "proposer_dt25_clock",
                                               "proposer_v3_clock"]
    assert (books["proposer_dt25_shadow"]["minute_match_pct"], books["proposer_dt25_clock"]["minute_match_pct"]) == (33.3, 66.7)
    assert (books["proposer_dt25_clock"]["net_on_pramanaa_days"], books["proposer_dt25_shadow"]["net_on_pramanaa_days"]) ==         (34944.0, -112807.0)
    assert books["proposer_dt25_clock"]["net"] == 41944.0 and books["proposer_dt25_clock"]["same_as_pramanaa_side_days"] == 1
    assert (s["sessions"], s["pramanaa_days"], s["pramanaa_net"], s["pramanaa_traded_days"]) == (3, 2, 38100.0, 1)
    assert s["pramanaa_print_count"] == 3


def test_v3_tab_renders_with_and_without_the_comparison(tmp_path):
    env = jinja2.Environment(loader=jinja2.FileSystemLoader(str(ROOT / "templates")))
    page = env.get_template("_proposer_v3.html")
    stats = {"lots": 100, "net_total": 1.0, "net_per_lot": 0.0, "charges_total": 0.0, "first_unseen": "2026-10-09",
             "unseen_net": 0.0, "unseen_days": 0, "fitted_net": 0.0, "green_pct": 0, "green_days": 0, "red_days": 0,
             "traded_days": 0, "trades": 0, "worst_day": 0, "worst_trade": 0, "max_dd": 0, "months": [], "by_rule": [],
             "open_trades": [], "with_day_pts": 75, "latest": {}}
    conn = sqlite3.connect(tmp_path / "labs.db")
    conn.row_factory = sqlite3.Row
    _four(conn, {k: [("2026-09-25", net, [m], e)] for k, net, m, e in (
        ("proposer_v3", -112807.0, "14:07", "14:07:25"), ("proposer_dt25_shadow", -112807.0, "14:07", "14:07:25"),
        ("proposer_dt25_clock", 34944.0, "14:10", "14:10:28"), ("proposer_v3_clock", 34944.0, "14:10", "14:10:28"))})
    for shadow in ({}, v3.shadow_comparison(conn)):
        html = page.render(proposer_v3_stats={**stats, "shadow": shadow},
                           proposer_v3_rows=[{"trade_date": "2026-09-25", "status": "closed", "n_trades": 1,
                                              "regime": "neutral", "gap_pct": 0.0, "expiry_code": "26O01",
                                              "n_losses": 1, "day_banked": 0, "gross_rs": -109584.0,
                                              "charges_rs": 3223.0, "net_rs": -112807.0,
                                              "book_gross_before": 0.0, "error": None}],
                           proposer_v3_trades=[])
        assert "Shadows: Pramanaa" in html
    assert "34,944" in html and "none imported" in html and "Pramanaa&#39;s rules, Pramanaa&#39;s clock" not in html
    assert "Pramanaa's rules, Pramanaa's clock" in html
